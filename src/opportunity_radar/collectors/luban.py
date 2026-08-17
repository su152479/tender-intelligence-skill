from datetime import date, datetime, timedelta
import logging
import os

from bs4 import BeautifulSoup
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .base import BaseCollector
from .regions import infer_region, is_target_region
from ..models import Project

log = logging.getLogger(__name__)


class CRECGLubanCollector(BaseCollector):
    """Collect current public procurement notices from the official homepage."""

    def __init__(self, source: dict):
        super().__init__(source)
        retry = Retry(total=3, connect=3, read=3, backoff_factor=0.8, allowed_methods={"GET"})
        self.session = requests.Session()
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) EngineeringOpportunityRadar/0.1",
            "Referer": source.get("url", "https://www.crecgec.com/"),
        })
        self.timeout = int(os.getenv("RADAR_COLLECTION_TIMEOUT_SECONDS", "60"))
        self.lookback_days = int(os.getenv("RADAR_LOOKBACK_DAYS", "30"))

    def collect(self) -> list[Project]:
        response = self.session.get(self.source["url"], timeout=self.timeout)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "utf-8"
        projects = self.parse_homepage(response.text)
        log.info("中铁鲁班公开首页采集完成 count=%s", len(projects))
        return projects

    def parse_homepage(self, html: str) -> list[Project]:
        soup = BeautifulSoup(html, "html.parser")
        keywords = tuple(self.source.get("keywords", []))
        projects: list[Project] = []
        seen: set[str] = set()
        selector = 'a[href*="eproport.crecgec.com/assets/tmp/redirect.html"]'
        for anchor in soup.select(selector):
            url = anchor.get("href", "")
            title = anchor.get("title") or anchor.get_text(" ", strip=True)
            row = anchor.find_parent(class_="luban-notice-row") or anchor.parent
            row_text = row.get_text(" ", strip=True) if row else title
            published_node = row.select_one(".date") if row else None
            published = published_node.get_text(" ", strip=True)[:10] if published_node else ""
            combined = f"{title}\n{row_text}"
            if not title or url in seen or not any(keyword in combined for keyword in keywords):
                continue
            allowed_regions = self.source.get("regions")
            if not is_target_region(combined, allowed_regions) or not self._is_recent(published):
                continue
            seen.add(url)
            projects.append(Project(
                name=title, publish_date=published, region=infer_region(combined, allowed_regions),
                tenderer=self._tenderer(title), stage="采购公告",
                construction_content=row_text, source_site=self.source["name"],
                url=url, raw_text=combined,
            ))
        return projects

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
