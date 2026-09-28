"""天津市交通运输委员会公开招标公告采集器。"""

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


class TianjinTransportCollector(BaseCollector):
    def __init__(self, source: dict):
        super().__init__(source)
        self.list_url = source.get(
            "list_url",
            "https://jtys.tj.gov.cn/ZWGK6002/JTYSGCJS1685/ZBGG1389/",
        )
        self.lookback_days = int(os.getenv("RADAR_LOOKBACK_DAYS", "30"))
        self.max_pages = int(os.getenv("RADAR_TIANJIN_TRANSPORT_MAX_PAGES", "3"))
        self.client = PublicPageClient(source["name"], source.get("id"))

    def collect(self) -> list[Project]:
        entries: dict[str, tuple[str, str]] = {}
        stop = False
        for page in range(self.max_pages):
            url = self.list_url if page == 0 else urljoin(self.list_url, f"index_{page}.html")
            html = self.client.get_list_text(url, allow_windows_curl_fallback=True)
            rows = self.parse_list(html)
            self.add_funnel(request_success_count=1, raw_list_count=len(rows))
            for title, published, detail_url in rows:
                if not self._is_recent(published):
                    stop = True
                    continue
                self.add_funnel(recent_count=1)
                excluded = ("机电", "智慧收费站", "智慧服务区", "路面养护")
                if is_construction_tender(title) and not any(word in title for word in excluded):
                    entries[detail_url] = (title, published)
            if stop:
                break

        projects = []
        for url, (title, published) in entries.items():
            project = self.parse_detail(
                self.client.get_detail_text(url, allow_windows_curl_fallback=True), url, title, published
            )
            if project:
                self.add_funnel(detail_success_count=1)
            if project and has_opportunity_direction(f"{project.name}\n{project.raw_text}"):
                projects.append(project)
        self.set_funnel(
            construction_count=len(entries), region_recent_count=len(entries),
            final_opportunity_count=len(projects),
        )
        log.info("天津交通工程公告采集完成 candidates=%s opportunities=%s", len(entries), len(projects))
        return projects

    def parse_list(self, html: str) -> list[tuple[str, str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        rows = []
        for anchor in soup.select(".commonList-text-title a[href]"):
            title = (anchor.get("title") or anchor.get_text(" ", strip=True)).strip()
            published = self._date_from_url(anchor["href"])
            rows.append((title, published, urljoin(self.list_url, anchor["href"])))
        return rows

    def parse_detail(self, html: str, url: str, title: str, published: str) -> Project | None:
        soup = BeautifulSoup(html, "html.parser")
        article = soup.select_one(".trs_editor_view") or soup.select_one(".detail-body")
        if not article:
            return None
        raw_text = article.get_text("\n", strip=True)
        owner = self._match(r"(天津[^\n。]{1,40}(?:有限公司|交通局|委员会))\s*20\d{2}\s*年", raw_text)
        return Project(
            name=title,
            publish_date=published,
            region="天津市",
            owner=owner,
            tenderer=owner,
            stage="招标计划" if "招标计划" in title else "施工招标公告",
            construction_content=evidence_excerpt(raw_text),
            source_site=self.source["name"],
            url=url,
            raw_text=raw_text,
        )

    def _is_recent(self, value: str) -> bool:
        try:
            return datetime.fromisoformat(value[:10]).date() >= date.today() - timedelta(days=self.lookback_days)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _date_from_url(value: str) -> str:
        match = re.search(r"t(20\d{6})_", value)
        if not match:
            return ""
        raw = match.group(1)
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"

    @staticmethod
    def _match(pattern: str, text: str) -> str:
        match = re.search(pattern, text or "")
        return match.group(1).strip() if match else ""
