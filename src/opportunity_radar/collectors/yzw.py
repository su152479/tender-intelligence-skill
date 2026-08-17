import logging
import os
import re
from urllib.parse import quote

from playwright.sync_api import sync_playwright

from .base import BaseCollector
from .regions import infer_region, is_target_region
from ..auth import LoginManager
from ..config import DATA_DIR
from ..models import Project

log = logging.getLogger(__name__)


class CSCECYunZhuCollector(BaseCollector):
    """Reuse the manually saved YunZhu session and scrape visible bid cards."""

    SEARCH_URL = "https://xy.yzw.cn/search/sj/bid?from=radar&keyword={}"

    def __init__(self, source: dict):
        super().__init__(source)
        self.login = LoginManager(DATA_DIR / "auth")
        self.timeout_ms = int(os.getenv("RADAR_COLLECTION_TIMEOUT_SECONDS", "60")) * 1000
        self.wait_ms = int(os.getenv("RADAR_YZW_RENDER_WAIT_MS", "5000"))

    def collect(self) -> list[Project]:
        context_options = self.login.context_kwargs(self.source["id"])
        projects: dict[str, Project] = {}
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(**context_options)
            page = context.new_page()
            page.set_default_timeout(self.timeout_ms)
            try:
                for keyword in self.source.get("keywords", []):
                    with page.expect_response(
                        lambda response: "/api/mtg-core/portal/tender/search" in response.url,
                        timeout=self.timeout_ms,
                    ) as search_response:
                        page.goto(self.SEARCH_URL.format(quote(keyword)), wait_until="domcontentloaded")
                    self._ensure_logged_in(page)
                    response = search_response.value
                    if response.status != 200:
                        raise RuntimeError(f"云筑搜索接口 HTTP {response.status}")
                    body = response.json()
                    page.wait_for_timeout(min(self.wait_ms, 1500))
                    for project in self._projects_from_api(body, keyword):
                        projects[project.url] = project
            finally:
                context.storage_state(path=str(self.login.state_path(self.source["id"])))
                browser.close()
        log.info("中建云筑登录态采集完成 count=%s", len(projects))
        return list(projects.values())

    def _projects_from_api(self, body: dict, keyword: str) -> list[Project]:
        if body.get("code") != 200:
            log.warning("云筑搜索接口返回失败 keyword=%s message=%s", keyword, body.get("message"))
            return []
        records = (body.get("data") or {}).get("records") or []
        projects = []
        for row in records:
            title = row.get("name", "")
            area = row.get("area", "")
            categories = "、".join(row.get("purchaserCategoryList") or [])
            combined = "\n".join(filter(None, [title, area, categories, row.get("secondLevelCoopCategory", "")]))
            allowed_regions = self.source.get("regions")
            if not title or not is_target_region(area, allowed_regions):
                continue
            tender_code = row.get("tenderCode", "")
            source = row.get("source", "")
            tenant = row.get("tenantId", "")
            detail_url = (
                "https://xy.yzw.cn/sj/bid-detail"
                f"?tenderCode={quote(str(tender_code))}&source={quote(str(source))}&tenantId={quote(str(tenant))}"
            )
            projects.append(Project(
                name=title,
                project_no=tender_code,
                publish_date=(row.get("publishDate") or "")[:10],
                region=infer_region(area, allowed_regions),
                tenderer=row.get("tenderCompanyName", ""),
                stage="招标采购",
                construction_content=categories or row.get("secondLevelCoopCategory", ""),
                source_site=self.source["name"],
                url=detail_url,
                raw_text=combined,
            ))
        log.info("云筑关键词接口结果 keyword=%s total=%s target_regions=%s", keyword, len(records), len(projects))
        return projects

    def _ensure_logged_in(self, page) -> None:
        current = page.url.lower()
        if "ucenter.yzw.cn/login" in current or ("login" in current and "yzw.cn" in current):
            self.login.remind_expired(self.source["id"])
            raise RuntimeError("中建云筑登录状态已失效，请运行 radar login cscec_yzw 后重试")

    def _projects_from_page(self, page, keyword: str) -> list[Project]:
        selector = 'a[href*="bid-detail"], a[href*="supplier-bidding/detail"], a[href*="bidding/detail"]'
        rows = page.locator(selector).evaluate_all("""
            links => links.map(a => {
              const box = a.closest('li, article, tr, [class*="card"], [class*="item"]') || a.parentElement;
              return {href: a.href, title: (a.title || a.textContent || '').trim(), text: (box?.innerText || a.textContent || '').trim()};
            })
        """)
        projects = []
        for row in rows:
            title, text, url = row.get("title", ""), row.get("text", ""), row.get("href", "")
            combined = f"{title}\n{text}"
            allowed_regions = self.source.get("regions")
            if not title or not url or keyword not in combined or not is_target_region(combined, allowed_regions):
                continue
            match = re.search(r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}", combined)
            published = match.group(0).replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-").replace(".", "-") if match else ""
            projects.append(Project(
                name=title, publish_date=published, region=infer_region(combined, allowed_regions),
                stage="招标公告", construction_content=text,
                source_site=self.source["name"], url=url, raw_text=combined,
            ))
        return projects
