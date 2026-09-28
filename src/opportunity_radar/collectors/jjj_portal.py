"""京津冀公共资源交易协同专区工程项目全生命周期补漏。"""

from datetime import date, datetime, timedelta
import logging
import os
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import BaseCollector
from .construction import evidence_excerpt, has_opportunity_direction, is_construction_tender
from .http import PublicPageClient
from ..models import Project


log = logging.getLogger(__name__)


class JingJinJiPortalCollector(BaseCollector):
    """Use the aggregation page as an independent lifecycle completeness source."""

    STAGE_SECTIONS = {
        "招标计划": {"jyxxgcjszbjh", "xtgghbzbjh", "xtggtjzbjh", "xtggxazbjh"},
        "施工招标公告": {"jyxxggjtbyqs", "xtgghbzbgg", "xtggtjzbgg", "xtggxazbgg"},
        "中标候选人公示": {"jyxxzbhxrgs", "xtgghbzbhxr", "xtggtjzbhxr", "xtggxazbhxr"},
        "中标结果公告": {"jyxxzbgg", "xtgghbzbjg", "xtggtjzbjg", "xtggxazbjg"},
    }
    REGION_BY_PREFIX = {
        "jyxx": "北京市", "xtgghb": "河北省", "xtggtj": "天津市", "xtggxa": "河北省雄安新区",
    }
    SERVICE_MARKERS = ("监理", "检测", "监测", "勘察", "设计", "咨询", "审计", "造价")

    def __init__(self, source: dict):
        super().__init__(source)
        self.base_url = source.get("url", "https://ggzyfw.beijing.gov.cn/")
        self.list_url = source.get("list_url", urljoin(self.base_url, "/jjjxtlm/index.html"))
        self.lookback_days = int(os.getenv("RADAR_LOOKBACK_DAYS", "30"))
        self.max_details = int(os.getenv("RADAR_JJJ_PORTAL_MAX_DETAILS", "60"))
        self.client = PublicPageClient(source["name"], source.get("id"))

    def collect(self) -> list[Project]:
        html = self.client.get_list_text(self.list_url, allow_windows_curl_fallback=True)
        rows = self.parse_list(html)
        self.set_funnel(request_success_count=1, raw_list_count=len(rows))
        recent = [row for row in rows if self._is_recent(row[1])]
        candidates = [row for row in recent if self._is_engineering_candidate(row[0], row[3])]
        self.set_funnel(recent_count=len(recent), region_recent_count=len(recent), construction_count=len(candidates))

        projects: list[Project] = []
        for title, published, detail_url, stage, region in candidates[: self.max_details]:
            html = self.client.get_detail_text(detail_url, allow_windows_curl_fallback=True)
            project = self.parse_detail(html, detail_url, title, published, stage, region)
            if project:
                self.add_funnel(detail_success_count=1)
            if project and has_opportunity_direction(f"{project.name}\n{project.raw_text}"):
                projects.append(project)
        self.set_funnel(final_opportunity_count=len(projects))
        log.info(
            "京津冀协同专区采集完成 raw=%s recent=%s candidates=%s opportunities=%s",
            len(rows), len(recent), len(candidates), len(projects),
        )
        return projects

    def parse_list(self, html: str) -> list[tuple[str, str, str, str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        stage_by_section = {
            section: stage for stage, sections in self.STAGE_SECTIONS.items() for section in sections
        }
        rows: dict[str, tuple[str, str, str, str, str]] = {}
        for anchor in soup.select("a[href]"):
            url = urljoin(self.base_url, anchor.get("href") or "")
            parts = [part for part in urlsplit(url).path.split("/") if part]
            if not parts:
                continue
            section = parts[0]
            stage = stage_by_section.get(section)
            if not stage:
                continue
            title = (anchor.get("title") or anchor.get_text(" ", strip=True)).strip().rstrip("，,")
            date_match = re.search(r"/(20\d{6})/", urlsplit(url).path)
            if not title or not date_match:
                continue
            published = datetime.strptime(date_match.group(1), "%Y%m%d").date().isoformat()
            region = next(
                (value for prefix, value in self.REGION_BY_PREFIX.items() if section.startswith(prefix)), "京津冀"
            )
            rows[url] = (title, published, url, stage, region)
        return list(rows.values())

    def parse_detail(
        self, html: str, url: str, fallback_title: str, fallback_date: str, stage: str, region: str,
    ) -> Project | None:
        soup = BeautifulSoup(html, "html.parser")
        article = soup.select_one(".newsCon") or soup.select_one(".div-article2")
        if not article:
            return None
        raw_text = article.get_text("\n", strip=True)
        page_text = soup.get_text(" ", strip=True)
        title_node = soup.select_one(".div-title")
        title = fallback_title or (title_node.get_text(" ", strip=True) if title_node else "")
        project_no = self._match(r"(?:交易项目编号|招标项目编号)[：:]\s*([A-Za-z0-9_-]+)", page_text)
        owner = self._match(r"建设单位(?:名称)?[：:]\s*([^，,。]+)", raw_text)
        tenderer = self._match(r"招标人(?:为|名称)?[：:]?\s*([^，,。]+)", raw_text)
        return Project(
            name=title, project_no=project_no, publish_date=fallback_date, region=region,
            owner=owner or tenderer, tenderer=tenderer, stage=stage,
            construction_content=evidence_excerpt(raw_text), source_site=self.source["name"],
            url=url, raw_text=raw_text,
        )

    def _is_recent(self, value: str) -> bool:
        try:
            return datetime.fromisoformat(value[:10]).date() >= date.today() - timedelta(days=self.lookback_days)
        except (TypeError, ValueError):
            return False

    def _is_engineering_candidate(self, title: str, stage: str) -> bool:
        if any(marker in title for marker in self.SERVICE_MARKERS):
            return False
        if stage in {"施工招标公告", "招标计划"}:
            return is_construction_tender(title)
        return True

    @staticmethod
    def _match(pattern: str, text: str) -> str:
        match = re.search(pattern, text or "")
        return match.group(1).strip() if match else ""
