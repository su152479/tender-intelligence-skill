from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import Counter
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date, datetime

from .business_time import business_now_naive
from .db import Database
from .identity import NoticeIdentity, NoticeIdentityService


EXTRACTOR_VERSION = "project-event-v1.1"
EVENT_TYPES = (
    "TENDER_ANNOUNCED",
    "CONTRACTOR_CANDIDATE_SELECTED",
    "CONTRACTOR_SELECTED",
    "PROCUREMENT_ANNOUNCED",
    "PRODUCT_PROCUREMENT_ANNOUNCED",
    "PRODUCT_PROCUREMENT_RESULT",
    "CONSTRUCTION_PROGRESS",
    "PROJECT_TERMINATED",
    "UNKNOWN",
)

_EXPLICIT_DATE = re.compile(
    r"(?:中标日期|成交日期|开标日期|定标日期|确定日期|中标时间|成交时间)\s*[：:]\s*"
    r"(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})日?"
)
_FINAL_RESULT = re.compile(r"中标结果|成交结果|中选结果|中标通知")
_CANDIDATE_RESULT = re.compile(r"中标候选人|成交候选人|中选候选人")
_PROCUREMENT_NOTICE = re.compile(r"采购公告|询价公告|招标公告|竞争性谈判|竞争性磋商")
_GENERIC_PROCUREMENT = re.compile(r"采购|询价|货物|物资")
_PROJECT_TERMINATED = re.compile(r"项目(?:建设)?终止|工程(?:建设)?终止|项目取消")


@dataclass(frozen=True)
class ProjectEventFact:
    engineering_project_id: int
    notice_id: int
    event_type: str
    event_date: str
    event_date_source: str
    event_scope: str
    event_subject: str
    party_name: str
    party_role: str
    party_source_reference: str
    product: str
    lot: str
    phase: str
    source_type: str
    source_reference: str
    evidence_text: str
    confidence: str
    extractor_version: str = EXTRACTOR_VERSION

    @property
    def event_key(self) -> str:
        payload = (
            self.engineering_project_id,
            self.notice_id,
            self.event_type,
            self.event_scope,
            self.party_name,
            self.product,
        )
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ProjectEventService:
    """Build auditable project facts only from formally linked notices."""

    def __init__(self, db: Database, extractor_version: str = EXTRACTOR_VERSION):
        self.db = db
        self.extractor_version = extractor_version

    def build(self) -> dict:
        self.db.init()
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """SELECT p.*,l.engineering_project_id
                FROM project_notice_link l
                JOIN project p ON p.id=l.notice_id
                ORDER BY l.engineering_project_id,p.id"""
            ).fetchall()
            notice_ids = [int(row["id"]) for row in rows]
        identities = NoticeIdentityService(self.db).current_identities(notice_ids)
        now = business_now_naive().isoformat(timespec="seconds")
        reasons = Counter()
        events: list[ProjectEventFact] = []
        projects_with_events: set[int] = set()

        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            if notice_ids:
                placeholders = ",".join("?" for _ in notice_ids)
                con.execute(
                    f"UPDATE project_event SET is_active=0,updated_at=? WHERE notice_id IN ({placeholders})",
                    (now, *notice_ids),
                )
            for row in rows:
                identity = identities.get(int(row["id"]), NoticeIdentity(int(row["id"])))
                assessments = self._latest_assessments(con, int(row["id"]))
                extracted, reason = self._extract(row, identity, assessments)
                if not extracted:
                    reasons[reason] += 1
                    continue
                for event in extracted:
                    event = ProjectEventFact(**{**asdict(event), "extractor_version": self.extractor_version})
                    self._persist(con, event, now)
                    events.append(event)
                    projects_with_events.add(event.engineering_project_id)

        return {
            "engineering_projects": self._count("engineering_project"),
            "linked_notices": len(rows),
            "projects_with_events": len(projects_with_events),
            "events": len(events),
            "event_types": dict(sorted(Counter(event.event_type for event in events).items())),
            "notices_without_events": len(rows) - len({event.notice_id for event in events}),
            "no_event_reasons": dict(sorted(reasons.items())),
            "extractor_version": self.extractor_version,
        }

    def timeline(self, engineering_project_id: int, *, show_sources: bool = False) -> dict:
        self.db.init()
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            project = con.execute(
                "SELECT * FROM engineering_project WHERE id=?", (engineering_project_id,)
            ).fetchone()
            if not project:
                raise ValueError(f"工程实体不存在：{engineering_project_id}")
        events = self.canonical_events(engineering_project_id)
        if not show_sources:
            events = [{key: value for key, value in event.items() if key != "sources"} for event in events]
        return {"project": dict(project), "events": events, "show_sources": show_sources}

    def canonical_events(
        self, engineering_project_id: int | None = None, *, read_only: bool = False,
    ) -> list[dict]:
        source_events = self._source_events(engineering_project_id, read_only=read_only)
        groups: list[list[dict]] = []
        for event in source_events:
            for group in groups:
                if self._equivalent(group[0], event):
                    group.append(event)
                    break
            else:
                groups.append([event])
        result = [self._canonical(group) for group in groups]
        return sorted(result, key=lambda item: (
            item["engineering_project_id"], item["event_date"] or "9999-12-31",
            item["event_type"], self._lot_sort(item["lot"]), item["canonical_event_key"],
        ))

    def equivalence_statistics(self) -> dict:
        source_events = self._source_events()
        canonical = self.canonical_events()
        duplicates = [item for item in canonical if item["source_type_count"] > 1]
        by_type = Counter(item["event_type"] for item in duplicates)
        return {
            "active_source_events": len(source_events),
            "canonical_events": len(canonical),
            "cross_source_duplicate_groups": len(duplicates),
            "average_sources_per_duplicate_group": (
                round(sum(item["source_count"] for item in duplicates) / len(duplicates), 2)
                if duplicates else 0
            ),
            "max_sources": max((item["source_count"] for item in canonical), default=0),
            "duplicate_groups_by_event_type": dict(sorted(by_type.items())),
        }

    def _source_events(
        self, engineering_project_id: int | None = None, *, read_only: bool = False,
    ) -> list[dict]:
        if read_only:
            if not self.db.path.exists():
                raise ValueError(f"数据库不存在：{self.db.path}")
            con = sqlite3.connect(self.db.path.resolve().as_uri() + "?mode=ro", uri=True)
        else:
            self.db.init()
            con = self.db.connect()
        with closing(con):
            con.row_factory = sqlite3.Row
            where = "WHERE e.is_active=1"
            params: tuple = ()
            if engineering_project_id is not None:
                where += " AND e.engineering_project_id=?"
                params = (engineering_project_id,)
            return [dict(row) for row in con.execute(
                f"""SELECT e.*,p.project_name notice_name,p.url notice_url
                FROM project_event e JOIN project p ON p.id=e.notice_id
                {where} ORDER BY e.engineering_project_id,COALESCE(e.event_date,'9999-12-31'),e.id""",
                params,
            )]

    @staticmethod
    def _equivalent(left: dict, right: dict) -> bool:
        if any(left[key] != right[key] for key in (
            "engineering_project_id", "event_type", "product", "lot", "phase"
        )):
            return False
        if ProjectEventService._canonical_text(left["party_name"]) != ProjectEventService._canonical_text(right["party_name"]):
            return False
        left_scope = ProjectEventService._canonical_text(left["event_scope"])
        right_scope = ProjectEventService._canonical_text(right["event_scope"])
        if left_scope and right_scope and left_scope != right_scope:
            return False
        if bool(left_scope) != bool(right_scope) and not (left["party_name"] or left["product"]):
            return False
        if left["event_date"] == right["event_date"]:
            return True
        if left["source_type"] == right["source_type"]:
            return False
        sources = {left["event_date_source"], right["event_date_source"]}
        if sources != {"NOTICE_EXPLICIT_DATE", "PUBLICATION_DATE"}:
            return False
        try:
            first = datetime.fromisoformat(left["event_date"]).date()
            second = datetime.fromisoformat(right["event_date"]).date()
        except (TypeError, ValueError):
            return False
        return abs((first - second).days) <= 7

    @staticmethod
    def _canonical(group: list[dict]) -> dict:
        explicit = sorted(
            (item for item in group if item["event_date_source"] == "NOTICE_EXPLICIT_DATE"),
            key=lambda item: (item["event_date"] or "9999-12-31", item["id"]),
        )
        representative = explicit[0] if explicit else sorted(
            group, key=lambda item: (item["event_date"] or "9999-12-31", item["id"])
        )[0]
        scopes = sorted((item["event_scope"] for item in group if item["event_scope"]), key=len, reverse=True)
        scope = scopes[0] if scopes else ""
        source_types = sorted({item["source_type"] for item in group if item["source_type"]})
        evidence_sources = {
            (item["source_type"], item["source_reference"]) for item in group
        }
        independent_references = {item["source_reference"] for item in group if item["source_reference"]}
        key_payload = (
            representative["engineering_project_id"], representative["event_type"],
            representative["product"], ProjectEventService._canonical_text(representative["party_name"]),
            representative["lot"], representative["phase"],
            ProjectEventService._canonical_text(scope), representative["event_date"],
        )
        canonical_key = hashlib.sha256(
            json.dumps(key_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {
            **representative,
            "event_scope": scope,
            "canonical_event_key": canonical_key,
            "source_count": len(evidence_sources),
            "source_type_count": len(source_types),
            "independent_reference_count": len(independent_references),
            "evidence_count": len(group),
            "evidence_quality": (
                "MULTI_SOURCE_CROSS_CHECKED" if len(independent_references) > 1
                else "MULTI_PIPELINE_SAME_REFERENCE" if len(evidence_sources) > 1
                else "SINGLE_SOURCE"
            ),
            "sources": [{
                "project_event_id": item["id"], "notice_id": item["notice_id"],
                "notice_name": item["notice_name"],
                "source_name": item["source_type"], "source_url": item["source_reference"],
                "source_type": item["source_type"], "source_reference": item["source_reference"],
                "event_date": item["event_date"], "event_date_source": item["event_date_source"],
                "evidence_text": item["evidence_text"],
            } for item in sorted(group, key=lambda item: item["id"])],
        }

    @staticmethod
    def _canonical_text(value: str) -> str:
        return re.sub(r"[^\w\u4e00-\u9fff]", "", unicodedata.normalize("NFKC", value or "")).casefold()

    @staticmethod
    def _lot_sort(value: str) -> tuple:
        parts = (value or "").split(",")
        return tuple((0, int(part)) if part.isdigit() else (1, part) for part in parts)

    def event_rich_projects(self, limit: int = 20) -> list[int]:
        self.db.init()
        with self.db.connect() as con:
            return [int(row[0]) for row in con.execute(
                """SELECT engineering_project_id FROM project_event WHERE is_active=1
                GROUP BY engineering_project_id
                ORDER BY COUNT(*) DESC,engineering_project_id LIMIT ?""",
                (max(1, int(limit)),),
            )]

    def _count(self, table: str) -> int:
        with self.db.connect() as con:
            return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    @staticmethod
    def _latest_assessments(con: sqlite3.Connection, notice_id: int) -> list[sqlite3.Row]:
        return con.execute(
            """SELECT a.* FROM notice_product_assessment a
            WHERE a.notice_id=? AND a.id=(
              SELECT MAX(b.id) FROM notice_product_assessment b
              WHERE b.notice_id=a.notice_id AND b.product=a.product
            ) ORDER BY a.product""",
            (notice_id,),
        ).fetchall()

    def _extract(
        self, row: sqlite3.Row, identity: NoticeIdentity, assessments: list[sqlite3.Row]
    ) -> tuple[list[ProjectEventFact], str]:
        title = unicodedata.normalize("NFKC", row["project_name"] or "")
        stage = unicodedata.normalize("NFKC", row["project_stage"] or "")
        body = unicodedata.normalize("NFKC", row["raw_text"] or "")
        context = f"{title}\n{stage}\n{body}"
        progress = row["project_progress_signal"] or "UNKNOWN"
        event_date, date_source = self._event_date(row, body)
        scope, lot, phase = self._scope(identity)
        entity_id, notice_id = int(row["engineering_project_id"]), int(row["id"])
        base = dict(
            engineering_project_id=entity_id,
            notice_id=notice_id,
            event_date=event_date,
            event_date_source=date_source,
            event_scope=scope,
            lot=lot,
            phase=phase,
            source_type=row["source_site"] or "",
            source_reference=row["url"] or f"project:{notice_id}",
            extractor_version=self.extractor_version,
        )

        if _PROJECT_TERMINATED.search(context):
            return [ProjectEventFact(
                **base, event_type="PROJECT_TERMINATED", event_subject="公告明确终止",
                party_name="", party_role="", product="", evidence_text=self._evidence(title, stage),
                party_source_reference="",
                confidence="HIGH",
            )], ""

        direct_products = [
            item for item in assessments
            if item["product_demand_evidence_type"] == "DIRECT_TARGET_PRODUCT"
            and item["product_demand_evidence_level"] in {"MEDIUM", "STRONG"}
        ]
        is_final = bool(_FINAL_RESULT.search(f"{title}\n{stage}"))
        is_candidate = bool(_CANDIDATE_RESULT.search(f"{title}\n{stage}"))
        if direct_products and is_final:
            return [self._product_event(
                base, identity, item["product"], "PRODUCT_PROCUREMENT_RESULT", title, stage
            ) for item in direct_products], ""
        if direct_products and not is_candidate and _PROCUREMENT_NOTICE.search(f"{title}\n{stage}"):
            return [self._product_event(
                base, identity, item["product"], "PRODUCT_PROCUREMENT_ANNOUNCED", title, stage
            ) for item in direct_products], ""

        mapping = {
            "CONSTRUCTION_TENDER_OPEN": ("TENDER_ANNOUNCED", "施工招标已公告", "", "", "HIGH"),
            "CONTRACTOR_CANDIDATE_SELECTED": (
                "CONTRACTOR_CANDIDATE_SELECTED", "施工中标候选人已公示",
                identity.result_party, "CONTRACTOR", "HIGH",
            ),
            "CONTRACTOR_SELECTED": (
                "CONTRACTOR_SELECTED", "施工单位已确定",
                identity.result_party, "CONTRACTOR", "HIGH",
            ),
            "CONSTRUCTION_PROGRESS": ("CONSTRUCTION_PROGRESS", "施工进展已公告", "", "", "MEDIUM"),
        }
        if progress in mapping:
            event_type, subject, party, role, confidence = mapping[progress]
            return [ProjectEventFact(
                **base, event_type=event_type, event_subject=subject,
                party_name=party, party_role=role if party else "", product="",
                party_source_reference=identity.result_party_source_reference if party else "",
                evidence_text=self._evidence(title, stage), confidence=confidence,
            )], ""

        if _GENERIC_PROCUREMENT.search(f"{title}\n{stage}") and _PROCUREMENT_NOTICE.search(f"{title}\n{stage}"):
            return [ProjectEventFact(
                **base, event_type="PROCUREMENT_ANNOUNCED", event_subject="非目标产品采购已公告",
                party_name="", party_role="", product="", evidence_text=self._evidence(title, stage),
                party_source_reference="",
                confidence="MEDIUM",
            )], ""
        if direct_products and is_candidate:
            return [], "产品采购候选公示不等同最终采购结果"
        return [], "公告未表达受支持的可靠事实事件"

    @staticmethod
    def _product_event(base: dict, identity: NoticeIdentity, product: str, event_type: str,
                       title: str, stage: str) -> ProjectEventFact:
        result = event_type == "PRODUCT_PROCUREMENT_RESULT"
        party = identity.result_party if result else ""
        return ProjectEventFact(
            **base,
            event_type=event_type,
            event_subject=f"{product}{'采购结果已确定' if result else '采购已公告'}",
            party_name=party,
            party_role="SUPPLIER" if party else "",
            party_source_reference=identity.result_party_source_reference if party else "",
            product=product,
            evidence_text=ProjectEventService._evidence(title, stage),
            confidence="HIGH",
        )

    @staticmethod
    def _event_date(row: sqlite3.Row, body: str) -> tuple[str, str]:
        explicit = _EXPLICIT_DATE.search(body)
        if explicit:
            try:
                value = date(*(int(part) for part in explicit.groups())).isoformat()
                return value, "NOTICE_EXPLICIT_DATE"
            except ValueError:
                pass
        published = (row["publish_date"] or "").strip()
        match = re.search(r"20\d{2}-\d{1,2}-\d{1,2}", published)
        if match:
            year, month, day = (int(part) for part in match.group(0).split("-"))
            try:
                return date(year, month, day).isoformat(), "PUBLICATION_DATE"
            except ValueError:
                pass
        return "", "UNKNOWN"

    @staticmethod
    def _scope(identity: NoticeIdentity) -> tuple[str, str, str]:
        lot = ",".join(identity.lots)
        phase = ",".join(identity.phases)
        parts = []
        if lot:
            parts.append("标段:" + lot)
        if phase:
            parts.append("期次:" + phase)
        if identity.stations:
            parts.append("站点:" + ",".join(identity.stations))
        if identity.ranges:
            parts.append("范围:" + ",".join(identity.ranges))
        if identity.subproject_scope:
            parts.append("子工程:" + identity.subproject_scope)
        return "；".join(parts), lot, phase

    @staticmethod
    def _evidence(title: str, stage: str) -> str:
        return "；".join(part for part in (title.strip(), stage.strip()) if part)[:1000]

    @staticmethod
    def _persist(con: sqlite3.Connection, event: ProjectEventFact, now: str) -> None:
        con.execute(
            """INSERT INTO project_event(
            engineering_project_id,notice_id,event_type,event_date,event_date_source,event_scope,
            event_subject,party_name,party_role,party_source_reference,product,lot,phase,source_type,source_reference,
            evidence_text,confidence,extractor_version,event_key,is_active,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)
            ON CONFLICT(event_key,extractor_version) DO UPDATE SET
              event_date=excluded.event_date,event_date_source=excluded.event_date_source,
              event_subject=excluded.event_subject,party_role=excluded.party_role,
              party_source_reference=excluded.party_source_reference,evidence_text=excluded.evidence_text,
              confidence=excluded.confidence,is_active=1,updated_at=excluded.updated_at""",
            (
                event.engineering_project_id, event.notice_id, event.event_type, event.event_date or None,
                event.event_date_source, event.event_scope, event.event_subject, event.party_name,
                event.party_role, event.party_source_reference, event.product, event.lot, event.phase, event.source_type,
                event.source_reference, event.evidence_text, event.confidence,
                event.extractor_version, event.event_key, now,
            ),
        )
