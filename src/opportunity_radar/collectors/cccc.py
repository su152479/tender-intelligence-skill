from datetime import date, datetime, timedelta
import logging
import os

from bs4 import BeautifulSoup
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .base import BaseCollector
from ..models import Project

log = logging.getLogger(__name__)


class CCCCCollector(BaseCollector):
    """Search and retrieve public notices from 中交招采网 without signing in."""

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

    def collect(self) -> list[Project]:
        rows_by_id: dict[str, dict] = {}
        for keyword in self.source.get("keywords", []):
            for row in self._search(keyword):
                rows_by_id[str(row["noticeId"])] = row

        projects = []
        for row in rows_by_id.values():
            if not self._is_recent(row.get("noticeReleaseTime", "")):
                continue
            if not self._region_allowed(row.get("provinceName", "")):
                continue
            detail = self._detail(row)
            projects.append(self._to_project(row, detail))

        log.info("中交关键词采集完成 keywords=%s unique=%s", len(self.source.get("keywords", [])), len(projects))
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
