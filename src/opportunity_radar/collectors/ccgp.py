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


class CCGPCollector(BaseCollector):
    """中国政府采购网地方公开招标公告（公开静态页面）。"""

    def __init__(self, source: dict):
        super().__init__(source)
        self.list_url = source.get("list_url", "https://www.ccgp.gov.cn/cggg/dfgg/gkzb/")
        self.lookback_days = int(os.getenv("RADAR_LOOKBACK_DAYS", "30"))
        self.max_pages = int(os.getenv("RADAR_CCGP_MAX_PAGES", "10"))
        self.client = PublicPageClient(source["name"])

    def collect(self) -> list[Project]:
        entries: dict[str, tuple[str, str, str, str]] = {}
        stop = False
        for page in range(1, self.max_pages + 1):
            url = urljoin(self.list_url, "index.htm" if page == 1 else f"index_{page}.htm")
            html = self.client.get_text(url)
            for title, published, region, owner, detail_url in self.parse_list(html):
                if not self._is_recent(published):
                    stop = True
                    continue
                if region in self.source.get("region_labels", []) and is_construction_tender(title):
                    entries[detail_url] = (title, published, region, owner)
            if stop:
                break

        projects = []
        for url, (title, published, region, owner) in entries.items():
            html = self.client.get_text(url)
            project = self.parse_detail(html, url, title, published, region, owner)
            if project and has_opportunity_direction(f"{project.name}\n{project.raw_text}"):
                projects.append(project)
        log.info("中国政府采购网采集完成 candidates=%s opportunities=%s", len(entries), len(projects))
        return projects

    def parse_list(self, html: str) -> list[tuple[str, str, str, str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        rows = []
        for item in soup.select("ul.c_list_bid li"):
            anchor = item.find("a", href=True)
            if not anchor:
                continue
            em = [node.get_text(" ", strip=True) for node in item.select("em")]
            if len(em) < 3:
                continue
            title = (anchor.get("title") or anchor.get_text(" ", strip=True)).strip()
            rows.append((title, em[0][:10], em[1].strip(), em[2].strip(), urljoin(self.list_url, anchor["href"])))
        return rows

    def parse_detail(self, html: str, url: str, title: str, published: str, region: str, owner: str) -> Project | None:
        soup = BeautifulSoup(html, "html.parser")
        article = soup.select_one(".vF_detail_content") or soup.select_one(".vT_detail_main")
        raw_text = (article or soup).get_text("\n", strip=True)
        if not raw_text:
            return None
        project_no = self._match(r"项目编号[：:]\s*([^\s，,。]+)", raw_text)
        return Project(
            name=title, project_no=project_no, publish_date=published,
            region={"北京": "北京市", "天津": "天津市", "河北": "河北省"}.get(region, region),
            owner=owner, tenderer=owner, stage="政府采购公开招标公告",
            construction_content=evidence_excerpt(raw_text), source_site=self.source["name"],
            url=url, raw_text=raw_text,
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
