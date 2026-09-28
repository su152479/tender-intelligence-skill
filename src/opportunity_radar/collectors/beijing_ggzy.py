from datetime import date, datetime, timedelta
import logging
import os
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .base import BaseCollector
from .construction import evidence_excerpt, has_opportunity_direction, is_construction_tender
from .http import PublicPageClient
from ..models import Project

log = logging.getLogger(__name__)


class BeijingGGZYCollector(BaseCollector):
    """北京市公共资源交易服务平台－工程建设招标公告。"""

    def __init__(self, source: dict):
        super().__init__(source)
        self.base_url = source.get("url", "https://ggzyfw.beijing.gov.cn/")
        self.list_url = source.get("list_url", urljoin(self.base_url, "/jyxxggjtbyqs/"))
        self.lookback_days = int(os.getenv("RADAR_LOOKBACK_DAYS", "30"))
        self.max_pages = int(os.getenv("RADAR_BEIJING_GGZY_MAX_PAGES", "5"))
        self.client = PublicPageClient(source["name"], source.get("id"))

    def collect(self) -> list[Project]:
        entries: dict[str, tuple[str, str]] = {}
        stop = False
        for page in range(1, self.max_pages + 1):
            url = self.list_url if page == 1 else urljoin(self.list_url, f"index_{page}.html")
            html = self.client.get_list_text(url, allow_windows_curl_fallback=True)
            rows = self.parse_list(html)
            self.add_funnel(request_success_count=1, raw_list_count=len(rows))
            for title, published, detail_url in rows:
                if not self._is_recent(published):
                    stop = True
                    continue
                self.add_funnel(recent_count=1)
                if is_construction_tender(title):
                    entries[detail_url] = (title, published)
            if stop:
                break

        projects: list[Project] = []
        for detail_url, (title, published) in entries.items():
            html = self.client.get_detail_text(detail_url, allow_windows_curl_fallback=True)
            project = self.parse_detail(html, detail_url, title, published)
            if project:
                self.add_funnel(detail_success_count=1)
            if project and has_opportunity_direction(f"{project.name}\n{project.raw_text}"):
                projects.append(project)
        self.set_funnel(
            construction_count=len(entries), region_recent_count=len(entries),
            final_opportunity_count=len(projects),
        )
        log.info("北京公共资源工程招标采集完成 candidates=%s opportunities=%s", len(entries), len(projects))
        return projects

    def parse_list(self, html: str) -> list[tuple[str, str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        rows = []
        for anchor in soup.select("a.divtitlejy[href]"):
            title = (anchor.get("title") or anchor.get_text(" ", strip=True)).strip()
            li = anchor.find_parent("li")
            text = li.get_text(" ", strip=True) if li else title
            match = re.search(r"20\d{2}-\d{2}-\d{2}", text)
            published = match.group(0) if match else ""
            rows.append((title, published, urljoin(self.base_url, anchor["href"])))
        return rows

    def parse_detail(self, html: str, url: str, fallback_title: str = "", fallback_date: str = "") -> Project | None:
        soup = BeautifulSoup(html, "html.parser")
        article = soup.select_one(".newsCon") or soup.select_one(".div-article2")
        if not article:
            return None
        raw_text = article.get_text("\n", strip=True)
        title_node = soup.select_one(".div-title")
        title = fallback_title or (title_node.get_text(" ", strip=True) if title_node else "")
        project_no = self._match(r"交易项目编号[：:]\s*([A-Za-z0-9_-]+)", soup.get_text(" ", strip=True))
        tenderer = self._match(r"招标人为\s*([^，,。]+)", raw_text)
        owner = self._match(r"建设单位(?:为|：|:)\s*([^，,。]+)", raw_text) or tenderer
        place = self._match(r"建设地点\s*([^\n]+)", raw_text)
        stage = "施工资格预审公告" if "资格预审" in title else "施工招标公告"
        return Project(
            name=title, project_no=project_no, publish_date=fallback_date,
            region=(place if place.startswith("北京市") else f"北京市{place}") if place else "北京市",
            stage=stage, construction_content=evidence_excerpt(raw_text),
            source_site=self.source["name"], url=url, raw_text=raw_text,
        )

    def _is_recent(self, value: str) -> bool:
        try:
            return datetime.fromisoformat(value[:10]).date() >= date.today() - timedelta(days=self.lookback_days)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _match(pattern: str, text: str) -> str:
        match = re.search(pattern, text or "")
        return match.group(1).strip() if match else ""
