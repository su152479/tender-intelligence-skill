from datetime import date, datetime, timedelta
import hashlib
import logging
import os
import re
from urllib.parse import quote

from bs4 import BeautifulSoup
from .base import BaseCollector
from .browser_resilience import (
    DiagnosticRecorder, LocatorCandidate, ResponseOutcome, SelectorRegistry,
    classify_document,
)
from .regions import infer_region, is_jing_jin_ji
from .http import PublicPageClient
from ..models import Project
from ..network import NetworkPolicy

log = logging.getLogger(__name__)


class CRECGLubanCollector(BaseCollector):
    """Collect current public procurement notices from the official homepage."""

    def __init__(self, source: dict):
        super().__init__(source)
        self.client = PublicPageClient(source["name"], source.get("id"))
        self.timeout = int(os.getenv("RADAR_COLLECTION_TIMEOUT_SECONDS", "60"))
        self.lookback_days = int(os.getenv("RADAR_LOOKBACK_DAYS", "30"))
        self.selectors = SelectorRegistry()
        self.diagnostics = DiagnosticRecorder()
        self.network = NetworkPolicy.from_env()

    def collect(self) -> list[Project]:
        projects: list[Project] = []
        homepage = self.client.get_list_text(self.source["url"])
        projects.extend(self.parse_homepage(homepage))
        try:
            projects.extend(self.collect_public_history())
        except Exception as exc:
            # The public SPA is occasionally unavailable. Homepage results remain useful,
            # and failure here must not stop the other daily sources.
            message = f"中铁鲁班历史公告搜索不可用，已保留公开首页结果：{exc}"
            self.mark_partial(message)
            log.warning("%s", message)
        unique: dict[tuple[str, str], Project] = {}
        for project in projects:
            unique[(project.name, project.publish_date)] = project
        result = list(unique.values())
        self.set_funnel(final_opportunity_count=len(result))
        log.info("中铁鲁班公开信息采集完成 homepage_and_history=%s", len(result))
        return result

    def parse_homepage(self, html: str) -> list[Project]:
        soup = BeautifulSoup(html, "html.parser")
        projects: list[Project] = []
        seen: set[str] = set()
        selector = 'a[href*="eproport.crecgec.com/assets/tmp/redirect.html"]'
        anchors = soup.select(selector)
        self.add_funnel(request_success_count=1, raw_list_count=len(anchors))
        for anchor in anchors:
            url = anchor.get("href", "")
            title = anchor.get("title") or anchor.get_text(" ", strip=True)
            row = anchor.find_parent(class_="luban-notice-row") or anchor.parent
            row_text = row.get_text(" ", strip=True) if row else title
            published_node = row.select_one(".date") if row else None
            published = published_node.get_text(" ", strip=True)[:10] if published_node else ""
            combined = f"{title}\n{row_text}"
            if not title or url in seen or not self._is_relevant(combined):
                continue
            if not is_jing_jin_ji(combined) or not self._is_recent(published):
                continue
            seen.add(url)
            projects.append(Project(
                name=title, publish_date=published, region=infer_region(combined),
                tenderer=self._tenderer(title), stage=self._stage(title),
                construction_content=row_text, source_site=self.source["name"],
                url=url, raw_text=combined,
            ))
        self.add_funnel(region_recent_count=len(projects))
        return projects

    def collect_public_history(self) -> list[Project]:
        """Search the official public history page without authentication."""
        if os.getenv("RADAR_LUBAN_HISTORY_SEARCH", "true").lower() in {"0", "false", "no"}:
            return []
        from playwright.sync_api import sync_playwright

        portal_url = self.source.get("portal_url", "https://www.crecgec.com/portal")
        terms = self.source.get("history_search_terms", ["北京", "天津", "河北", "雄安", "京密"])
        projects: list[Project] = []
        with sync_playwright() as playwright:
            if self.network.has_direct_fallback:
                try:
                    return self._collect_history_browser(playwright, portal_url, terms, direct=False)
                except Exception as exc:
                    if not self.network.may_retry_direct(exc):
                        raise
                    log.warning("鲁班历史页代理通道失败，切换直连仅重试一次：%s", exc)
            return self._collect_history_browser(
                playwright, portal_url, terms, direct=self.network.has_direct_fallback
            )

    def _collect_history_browser(self, playwright, portal_url: str, terms: list[str], *, direct: bool) -> list[Project]:
        projects: list[Project] = []
        raw_count = 0
        search_success_count = 0
        browser = playwright.chromium.launch(
            headless=True, **self.network.browser_launch_options(direct=direct)
        )
        try:
            page = browser.new_page()
            failed_scripts: list[str] = []
            page.on(
                "response",
                lambda response: failed_scripts.append(f"HTTP {response.status} {response.url}")
                if response.request.resource_type == "script" and response.status >= 400 else None,
            )
            try:
                # The portal keeps a long-lived resource open, so waiting for
                # DOMContentLoaded can time out even though the public history UI
                # is already usable. Commit first, then wait for the real control.
                try:
                    response = page.goto(portal_url, wait_until="commit", timeout=min(self.timeout, 30) * 1000)
                except Exception as exc:
                    message = f"官方历史搜索连接失败：{exc}"
                    self.diagnostics.record_page(
                        self.source["id"], ResponseOutcome.UPSTREAM_BROKEN, page,
                        message=message, screenshot=True,
                    )
                    raise RuntimeError(message) from exc
                status = response.status if response else 0
                outcome = classify_document(status)
                if outcome != ResponseOutcome.OK:
                    self.diagnostics.record_page(
                        self.source["id"], outcome, page, status=status,
                        message="中铁鲁班历史搜索入口预检失败", screenshot=True,
                    )
                    raise RuntimeError(f"官方历史搜索入口不可用（HTTP {status}，{outcome.value}）")
                try:
                    search = self.selectors.locate_visible(
                        page, self.source["id"], "历史公告搜索框", [
                            LocatorCandidate("placeholder-keyword", lambda p: p.locator('input[placeholder*="关键词"]')),
                            LocatorCandidate("placeholder-notice", lambda p: p.locator('input[placeholder*="公告"]')),
                            LocatorCandidate("visible-input", lambda p: p.locator("input:visible")),
                        ], timeout_ms=min(
                            self.timeout, int(os.getenv("RADAR_LUBAN_HISTORY_UI_TIMEOUT_SECONDS", "15"))
                        ) * 1000,
                    )
                    search_button = self.selectors.locate_visible(
                        page, self.source["id"], "历史公告搜索按钮", [
                            LocatorCandidate("role-search", lambda p: p.get_by_role("button", name="搜索", exact=True)),
                            LocatorCandidate("text-search", lambda p: p.get_by_text("搜索", exact=True)),
                        ], timeout_ms=8000,
                    )
                except RuntimeError as exc:
                    if failed_scripts:
                        message = (
                            "官方历史搜索前端脚本加载失败（" + "；".join(failed_scripts[:3]) + "）"
                        )
                        outcome = ResponseOutcome.UPSTREAM_BROKEN
                    else:
                        message = str(exc)
                        outcome = ResponseOutcome.STRUCTURE_CHANGED
                    self.diagnostics.record_page(
                        self.source["id"], outcome, page, status=status,
                        message=message, screenshot=True,
                    )
                    raise RuntimeError(message) from exc
                for term in terms:
                    search.fill(term)
                    search_button.click(timeout=10000)
                    page.wait_for_timeout(int(os.getenv("RADAR_LUBAN_SEARCH_INTERVAL_MS", "3000")))
                    body_text = page.locator("body").inner_text()
                    raw_count += len(re.findall(r"20\d{2}-\d{2}-\d{2}", body_text))
                    projects.extend(self.parse_portal_text(body_text, term))
                    search_success_count += 1
            except Exception:
                raise
        finally:
            browser.close()
        log.info("鲁班历史页采集完成 route=%s count=%s", "直连" if direct else "代理", len(projects))
        self.add_funnel(
            search_requested_count=len(terms), search_success_count=search_success_count,
            raw_list_count=raw_count, region_recent_count=len(projects),
        )
        return projects

    def parse_portal_text(self, text: str, search_term: str = "") -> list[Project]:
        """Parse the public history result list rendered by the official SPA."""
        lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
        projects: list[Project] = []
        for index, value in enumerate(lines):
            if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", value) or index < 3:
                continue
            title, region_text, tenderer = lines[index - 3:index]
            combined = "\n".join((title, region_text, tenderer, search_term))
            if not self._is_recent(value) or not is_jing_jin_ji(combined) or not self._is_relevant(combined):
                continue
            digest = hashlib.sha1(f"{title}|{value}".encode("utf-8")).hexdigest()[:12]
            url = f"https://www.crecgec.com/portal/#/service/search?keyword={quote(title)}&radar={digest}"
            projects.append(Project(
                name=title, publish_date=value, region=infer_region(combined),
                tenderer=tenderer, stage=self._stage(title),
                construction_content=title, source_site=self.source["name"],
                url=url, raw_text=combined,
            ))
        return projects

    def _is_relevant(self, text: str) -> bool:
        keywords = tuple(self.source.get("keywords", []))
        directions = tuple(self.source.get("opportunity_keywords", (
            "预制梁", "梁板", "桥梁", "路基", "附属工程", "隧道", "盾构",
            "顶管", "管网", "泵站", "水利", "风电", "塔筒", "装配式",
            "预制构件", "土建工程", "主体工程",
        )))
        return any(word in (text or "") for word in (*keywords, *directions))

    @staticmethod
    def _stage(title: str) -> str:
        if any(word in title for word in ("中标公示", "中标结果", "成交结果", "候选人公示")):
            return "结果公示"
        if "补遗" in title or "澄清" in title:
            return "澄清补遗"
        if "询价" in title:
            return "询价公告"
        return "招标采购公告"

    def _is_recent(self, value: str) -> bool:
        if not value:
            return False
        try:
            published = datetime.fromisoformat(value[:10]).date()
        except ValueError:
            return False
        return published >= date.today() - timedelta(days=self.lookback_days)

    @staticmethod
    def _tenderer(title: str) -> str:
        for suffix in ("有限责任公司", "有限公司"):
            pos = title.find(suffix)
            if pos >= 0:
                return title[:pos + len(suffix)]
        return ""
