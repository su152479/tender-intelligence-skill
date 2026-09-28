from datetime import date, datetime, timedelta
import json
import logging
import os
from pathlib import Path
from urllib.parse import quote

from bs4 import BeautifulSoup
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .base import BaseCollector
from .browser_resilience import (
    DiagnosticRecorder, LocatorCandidate, ResponseOutcome, SelectorRegistry,
    classify_document,
)
from .search_terms import configured_search_terms
from ..config import DATA_DIR
from ..models import Project
from ..network import NetworkPolicy

log = logging.getLogger(__name__)


class CCCCCollector(BaseCollector):
    """Search the public 中交招采网 UI without signing in.

    The platform switched its list API to encrypted fields in August 2026.  The
    official public page still decrypts and renders those fields, so collection
    follows that page instead of treating the retired plaintext API's empty
    response as a real zero-result day.
    """

    SEARCH_API = "https://zjzcw.iccec.cn/apis/jrw/common/users/signup/qryEsNoticePageList"
    DETAIL_API = "https://sp.iccec.cn/apis/sp/bidc/users/signup/qryNoticeDetail"

    def __init__(self, source: dict):
        super().__init__(source)
        retry = Retry(total=3, connect=3, read=3, backoff_factor=0.8, allowed_methods={"POST"})
        self.session = requests.Session()
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) EngineeringOpportunityRadar/0.1",
            "Referer": "https://zjzcw.iccec.cn/procurementNotice",
            "Content-Type": "application/json",
        })
        self.timeout = int(os.getenv("RADAR_COLLECTION_TIMEOUT_SECONDS", "60"))
        self.limit = int(os.getenv("RADAR_CCCC_RESULTS_PER_KEYWORD", "5"))
        self.lookback_days = int(os.getenv("RADAR_LOOKBACK_DAYS", "30"))
        self.agent_id = int(source.get("agent_id", 107197))
        self.render_wait_ms = int(os.getenv("RADAR_CCCC_RENDER_WAIT_MS", "4000"))
        self.browser_channel = os.getenv("RADAR_CCCC_BROWSER_CHANNEL", "msedge").strip() or None
        self.headless = os.getenv("RADAR_CCCC_HEADLESS", "false").strip().lower() in {"1", "true", "yes"}
        self.profile_dir = Path(
            os.getenv("RADAR_CCCC_PROFILE_DIR", DATA_DIR / "browser" / "cccc-profile")
        ).resolve()
        self.selectors = SelectorRegistry()
        self.diagnostics = DiagnosticRecorder()
        self.network = NetworkPolicy.from_env()

    def collect(self) -> list[Project]:
        from playwright.sync_api import sync_playwright

        if self.waf_blocked_today():
            raise RuntimeError("中交今日已触发 WAF，冷却熔断生效；今天不再自动请求，明日再试")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            if self.network.has_direct_fallback:
                try:
                    return self._collect_browser(playwright, direct=False, record_errors=False)
                except Exception as exc:
                    if not self.network.may_retry_direct(exc):
                        raise
                    log.warning("中交代理通道失败，切换直连仅重试一次：%s", exc)
            return self._collect_browser(playwright, direct=self.network.has_direct_fallback, record_errors=True)

    def _collect_browser(self, playwright, *, direct: bool, record_errors: bool) -> list[Project]:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        projects_by_url: dict[str, Project] = {}
        terms = configured_search_terms(self.source)
        failed_terms = []
        self.set_funnel(search_requested_count=len(terms))
        launch_options = {
            "user_data_dir": str(self.profile_dir),
            "headless": self.headless,
            "channel": self.browser_channel,
            **self.network.browser_launch_options(direct=direct),
        }
        # Use a dedicated, persistent Edge profile so the public site's own
        # WAF/session cookies survive daily runs. We do not hide webdriver,
        # spoof fingerprints, rotate proxies, or bypass access controls.
        context = playwright.chromium.launch_persistent_context(**launch_options)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(self.timeout * 1000)
            response = page.goto(
                self.source.get("public_url", "https://zjzcw.iccec.cn/") + "procurementNotice",
                wait_until="commit", timeout=min(self.timeout, 30) * 1000,
            )
            status = response.status if response else 0
            outcome = classify_document(status, page.title(), page.locator("body").inner_text(timeout=3000))
            if outcome != ResponseOutcome.OK and record_errors:
                self.diagnostics.record_page(
                    self.source["id"], outcome, page, status=status,
                    message="中交公告入口预检失败", screenshot=True,
                )
            if outcome in {ResponseOutcome.WAF, ResponseOutcome.RATE_LIMIT, ResponseOutcome.ERROR, ResponseOutcome.UPSTREAM_BROKEN}:
                raise RuntimeError(self.block_reason(status))
            try:
                search = self.selectors.locate_visible(
                    page, self.source["id"], "采购公告搜索框", [
                        LocatorCandidate("placeholder-exact", lambda p: p.locator('input[placeholder="搜索 采购公告"]')),
                        LocatorCandidate("placeholder-notice", lambda p: p.locator('input[placeholder*="采购公告"]')),
                        LocatorCandidate("role-textbox", lambda p: p.get_by_role("textbox")),
                    ], timeout_ms=min(self.timeout, 15) * 1000,
                )
                search_button = self.selectors.locate_visible(
                    page, self.source["id"], "采购公告搜索按钮", [
                        LocatorCandidate("role-search", lambda p: p.get_by_role("button", name="搜索", exact=True)),
                        LocatorCandidate("button-text", lambda p: p.locator("button").filter(has_text="搜索")),
                    ], timeout_ms=8000,
                )
            except RuntimeError as exc:
                if record_errors:
                    self.diagnostics.record_page(
                        self.source["id"], ResponseOutcome.STRUCTURE_CHANGED, page,
                        status=status, message=str(exc), screenshot=True,
                    )
                raise
            for keyword in terms:
                try:
                    search.fill(keyword)
                    with page.expect_response(
                        lambda response: "qryEsNoticeEncryptPageList" in response.url,
                        timeout=self.timeout * 1000,
                    ) as result:
                        search_button.click()
                    if result.value.status != 200:
                        raise RuntimeError(f"中交公开搜索 HTTP {result.value.status}")
                    page.wait_for_timeout(self.render_wait_ms)
                    rows = page.locator("table tbody tr").evaluate_all(
                        "rows => rows.map(row => [...row.querySelectorAll('td')].map(cell => cell.innerText.trim()))"
                    )
                    found = self.parse_browser_rows(rows, keyword)
                    self.add_funnel(
                        search_success_count=1, raw_list_count=len(rows), region_recent_count=len(found)
                    )
                    for project in found:
                        projects_by_url[project.url] = project
                    log.info("中交公开页面搜索 route=%s keyword=%s visible=%s jingjinji=%s", "直连" if direct else "代理", keyword, len(rows), len(found))
                except PlaywrightTimeoutError as exc:
                    failed_terms.append(keyword)
                    self.add_funnel(search_failed_count=1)
                    log.warning("中交关键词搜索跳过 keyword=%s error=%s", keyword, exc)
        finally:
            context.close()
        log.info(
            "中交关键词采集完成 route=%s keywords=%s failed=%s unique=%s",
            "直连" if direct else "代理", len(terms), len(failed_terms), len(projects_by_url),
        )
        if terms and len(failed_terms) == len(terms):
            raise RuntimeError("中交全部搜索词均采集失败")
        self.set_funnel(final_opportunity_count=len(projects_by_url))
        return list(projects_by_url.values())

    def waf_blocked_today(self, today: date | None = None) -> bool:
        """Do not repeatedly hit the source after a confirmed WAF block."""
        expected = today or date.today()
        folder = self.diagnostics.root / self.source.get("id", "cccc")
        if not folder.exists():
            return False
        for path in sorted(folder.glob("*-waf.json"), reverse=True):
            try:
                captured = json.loads(path.read_text(encoding="utf-8")).get("captured_at", "")
                return datetime.fromisoformat(captured).date() == expected
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return False

    @staticmethod
    def block_reason(status: int) -> str:
        if status == 418:
            return "中交官方 WAF 拦截自动采集（HTTP 418），已停止该来源且未继续重试"
        if status == 429:
            return "中交官方平台限流（HTTP 429），已停止该来源且未继续重试"
        if status >= 500:
            return f"中交官方平台服务异常（HTTP {status}），已停止该来源且未继续重试"
        return f"中交官方平台拒绝访问（HTTP {status}），已停止该来源且未继续重试"

    def parse_browser_rows(self, rows: list[list[str]], keyword: str = "") -> list[Project]:
        projects = []
        public_url = self.source.get("public_url", "https://zjzcw.iccec.cn/").rstrip("/")
        for cells in rows:
            if len(cells) < 9:
                continue
            _, code, title, province, category, purchase_type, published, _, status = cells[:9]
            if not title or not self._region_allowed(province) or not self._is_recent(published):
                continue
            url = f"{public_url}/procurementNotice?radarCode={quote(code)}"
            raw_text = "\n".join(filter(None, [title, province, category, purchase_type, status, keyword]))
            projects.append(Project(
                name=title, project_no=code, publish_date=published[:10], region=province,
                tenderer="", stage=purchase_type or "采购公告", construction_content=category,
                source_site=self.source["name"], url=url, raw_text=raw_text,
            ))
        return projects

    def _search(self, keyword: str) -> list[dict]:
        payload = {
            "provinceList": self.source.get("region_codes", []), "pageNo": 1, "pageSize": self.limit,
            "noticeType": 1, "mergePurchaseTypeList": [], "noticeTitle": "",
            "singUpStatus": "", "dataSourceFlag": None, "keyWord": keyword,
            "schemeClass": None, "agentId": self.agent_id,
        }
        response = self.session.post(self.SEARCH_API, json=payload, timeout=self.timeout)
        response.raise_for_status()
        body = response.json()
        if str(body.get("code")) != "0":
            raise RuntimeError(f"中交公告搜索失败 keyword={keyword}: {body.get('message')}")
        rows = body.get("data", {}).get("rows", [])
        log.info("中交关键词搜索 keyword=%s count=%s", keyword, len(rows))
        return rows

    def _detail(self, row: dict) -> dict:
        payload = {
            "schemeId": str(row["schemeId"]), "noticeId": str(row["noticeId"]),
            "qryFileFlag": True, "agentId": self.agent_id,
        }
        try:
            response = self.session.post(self.DETAIL_API, json=payload, timeout=self.timeout)
            response.raise_for_status()
            body = response.json()
            if str(body.get("code")) == "0":
                return body.get("data") or {}
            log.warning("中交详情接口返回失败 notice=%s message=%s", row["noticeId"], body.get("message"))
        except requests.RequestException as exc:
            log.warning("中交详情采集失败 notice=%s error=%s", row["noticeId"], exc)
        return {}

    def _is_recent(self, value: str) -> bool:
        if not value:
            return False
        published = datetime.fromisoformat(value[:19]).date()
        return published >= date.today() - timedelta(days=self.lookback_days)

    def _region_allowed(self, province_name: str) -> bool:
        allowed = set(self.source.get("regions", []))
        if not allowed:
            return True
        actual = {part.strip() for part in (province_name or "").split(",") if part.strip()}
        return bool(actual & allowed)

    @staticmethod
    def html_to_text(value: str) -> str:
        return BeautifulSoup(value or "", "html.parser").get_text("\n", strip=True)

    def _to_project(self, row: dict, detail: dict) -> Project:
        raw_text = self.html_to_text(detail.get("textInfo", ""))
        if not raw_text:
            raw_text = "\n".join(filter(None, [
                row.get("noticeTitle"), row.get("schemeName"), row.get("matBigClassesNameStr"),
                row.get("schemeClassName"), row.get("opUnitName"), row.get("signUpStatusName"),
            ]))
        detail_url = (row.get("noticeDetailsUrl") or self.source.get("public_url", "https://zjzcw.iccec.cn/"))
        detail_url = detail_url.replace("https://sp.iccec.cn/", "https://zjzcw.iccec.cn/")
        content = "；".join(filter(None, [
            row.get("schemeClassName"), row.get("matBigClassesNameStr"), detail.get("noticeStatusStr"),
        ]))
        return Project(
            name=row.get("noticeTitle") or detail.get("noticeTitle") or row.get("schemeName", "未命名公告"),
            project_no=row.get("schemeCode", ""),
            publish_date=(row.get("noticeReleaseTime") or "")[:10],
            region=row.get("provinceName", ""),
            owner=row.get("opUnitName", ""),
            tenderer=row.get("opUnitName", ""),
            stage=row.get("mergePurchaseTypeName") or row.get("purchaseTypeName", "采购公告"),
            construction_content=content,
            source_site=self.source["name"],
            url=detail_url,
            raw_text=raw_text,
        )
