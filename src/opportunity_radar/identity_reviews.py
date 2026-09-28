"""Auditable human review and correction for notice identity facts."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
import gc
import json
import sqlite3
import tempfile

from .business_time import business_now_naive
from .db import Database
from .identity import (
    EXTRACTOR_VERSION, NoticeIdentityService, clean_owner, identity_text, normalize_lot,
    normalize_region,
)
from .project_entities import normalize_identifier


REVIEWER_DEFAULT = "LOCAL_USER"
REVIEWABLE_FACT_TYPES = {
    "REGION", "OWNER", "CORE_NAME", "PARENT_CORE_NAME", "LOT", "PHASE", "YEAR",
    "STATION", "RANGE", "SUBPROJECT_SCOPE", "RESULT_PARTY", "PROJECT_IDENTIFIER",
    "PROCUREMENT_OBJECT",
}
MULTI_VALUE_TYPES = {"LOT", "PROJECT_IDENTIFIER", "RESULT_PARTY"}
IDENTIFIER_TYPES = {
    "TRANSACTION_PROJECT_CODE", "ENGINEERING_CODE", "PROJECT_CODE",
    "BUSINESS_NOTICE_CODE", "CCCC_SCHEME_CODE", "YUNZHU_TENDER_CODE",
    "UNCERTAIN_IDENTIFIER",
}
IDENTITY_STRENGTHS = {"STRONG", "BUSINESS_ONLY", "UNCERTAIN"}


class IdentityReviewService:
    def __init__(self, db: Database):
        self.db = db
        self.db.init()

    def review_fact(
        self, fact_id: int, decision: str, *, note: str = "",
        reviewer: str = REVIEWER_DEFAULT, dry_run: bool = False,
    ) -> dict:
        decision = decision.upper()
        if decision not in {"CONFIRM", "REJECT"}:
            raise ValueError("fact审核只支持CONFIRM或REJECT")
        if dry_run:
            return self._preview("FACT", fact_id=fact_id, decision=decision, note=note, reviewer=reviewer)
        now = business_now_naive().isoformat(timespec="seconds")
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            fact = con.execute("SELECT * FROM notice_identity_fact WHERE id=?", (fact_id,)).fetchone()
            if not fact:
                raise ValueError(f"身份事实不存在：{fact_id}")
            if fact["source_type"] == "HUMAN":
                raise ValueError("HUMAN事实应通过新的OVERRIDE/CLEAR修订，不能作为机器fact确认")
            existing = con.execute(
                """SELECT * FROM notice_identity_review WHERE target_fact_id=? AND decision=?
                AND review_note=? AND reviewer=? AND is_active=1 ORDER BY id DESC LIMIT 1""",
                (fact_id, decision, note.strip(), reviewer or REVIEWER_DEFAULT),
            ).fetchone()
            if existing:
                return self._review_result(con, int(existing["id"]), idempotent=True)
            superseded_human_ids = [
                int(row[0]) for row in con.execute(
                    """SELECT human_fact_id FROM notice_identity_review
                    WHERE notice_id=? AND fact_type=? AND is_active=1
                    AND decision IN ('OVERRIDE','CLEAR') AND human_fact_id IS NOT NULL""",
                    (fact["notice_id"], fact["fact_type"]),
                )
            ]
            con.execute(
                """UPDATE notice_identity_review SET is_active=0,updated_at=?
                WHERE notice_id=? AND fact_type=? AND is_active=1
                  AND (target_fact_id=? OR decision IN ('OVERRIDE','CLEAR'))""",
                (now, fact["notice_id"], fact["fact_type"], fact_id),
            )
            for human_fact_id in superseded_human_ids:
                con.execute(
                    "UPDATE notice_identity_fact SET is_active=0,updated_at=? WHERE id=?",
                    (now, human_fact_id),
                )
            cursor = con.execute(
                """INSERT INTO notice_identity_review(
                notice_id,fact_type,target_fact_id,decision,review_note,reviewer,updated_at)
                VALUES(?,?,?,?,?,?,?)""",
                (
                    fact["notice_id"], fact["fact_type"], fact_id, decision,
                    note.strip(), reviewer or REVIEWER_DEFAULT, now,
                ),
            )
            return self._review_result(con, int(cursor.lastrowid))

    def override(
        self, notice_id: int, fact_type: str, value: str, *, note: str = "",
        reviewer: str = REVIEWER_DEFAULT, identifier_type: str = "",
        identifier_namespace: str = "", identity_strength: str = "",
        dry_run: bool = False,
    ) -> dict:
        fact_type = self._validate_fact_type(fact_type)
        value = value.strip()
        if not value:
            raise ValueError("OVERRIDE必须提供非空value；不确定时请使用CLEAR")
        identifier_type, identifier_namespace, identity_strength = self._validate_identifier_metadata(
            fact_type, identifier_type, identifier_namespace, identity_strength,
        )
        if dry_run:
            return self._preview(
                "OVERRIDE", notice_id=notice_id, fact_type=fact_type, value=value,
                note=note, reviewer=reviewer, identifier_type=identifier_type,
                identifier_namespace=identifier_namespace, identity_strength=identity_strength,
            )
        normalized = self._normalize(fact_type, value)
        now = business_now_naive().isoformat(timespec="seconds")
        with self.db.connect() as con:
            if not con.execute("SELECT 1 FROM project WHERE id=?", (notice_id,)).fetchone():
                raise ValueError(f"公告不存在：{notice_id}")
            current = con.execute(
                """SELECT * FROM notice_identity_review WHERE notice_id=? AND fact_type=?
                AND decision='OVERRIDE' AND human_normalized_value=? AND is_active=1
                AND review_note=? AND reviewer=? ORDER BY id DESC LIMIT 1""",
                (notice_id, fact_type, normalized, note.strip(), reviewer or REVIEWER_DEFAULT),
            ).fetchone()
            if current:
                return self._review_result(con, int(current[0]), idempotent=True)
            self._deactivate_field_reviews(con, notice_id, fact_type, now)
            con.execute(
                "UPDATE notice_identity_fact SET is_active=0,updated_at=? WHERE notice_id=? AND fact_type=? AND source_type='HUMAN'",
                (now, notice_id, fact_type),
            )
            location = "human:override"
            cursor = con.execute(
                """INSERT INTO notice_identity_fact(
                notice_id,fact_type,normalized_value,raw_value,source_type,source_reference,
                source_location,confidence,quality,identifier_type,identifier_namespace,
                identity_strength,extractor_version,is_active,updated_at)
                VALUES(?,?,?,?,?,'review:pending',?,'HIGH','HUMAN_OVERRIDE',?,?,?,?,1,?)""",
                (
                    notice_id, fact_type, normalized, value, "HUMAN", location,
                    identifier_type, identifier_namespace, identity_strength,
                    "human-review-v1", now,
                ),
            )
            human_fact_id = int(cursor.lastrowid)
            review_cursor = con.execute(
                """INSERT INTO notice_identity_review(
                notice_id,fact_type,decision,human_value,human_normalized_value,human_fact_id,
                review_note,reviewer,updated_at) VALUES(?,?,'OVERRIDE',?,?,?,?,?,?)""",
                (
                    notice_id, fact_type, value, normalized, human_fact_id,
                    note.strip(), reviewer or REVIEWER_DEFAULT, now,
                ),
            )
            review_id = int(review_cursor.lastrowid)
            con.execute(
                "UPDATE notice_identity_fact SET source_reference=? WHERE id=?",
                (f"review:{review_id}", human_fact_id),
            )
            return self._review_result(con, review_id)

    def clear(
        self, notice_id: int, fact_type: str, *, note: str = "",
        reviewer: str = REVIEWER_DEFAULT, dry_run: bool = False,
    ) -> dict:
        fact_type = self._validate_fact_type(fact_type)
        if dry_run:
            return self._preview(
                "CLEAR", notice_id=notice_id, fact_type=fact_type, note=note, reviewer=reviewer,
            )
        now = business_now_naive().isoformat(timespec="seconds")
        with self.db.connect() as con:
            if not con.execute("SELECT 1 FROM project WHERE id=?", (notice_id,)).fetchone():
                raise ValueError(f"公告不存在：{notice_id}")
            existing = con.execute(
                """SELECT id FROM notice_identity_review WHERE notice_id=? AND fact_type=?
                AND decision='CLEAR' AND review_note=? AND reviewer=? AND is_active=1""",
                (notice_id, fact_type, note.strip(), reviewer or REVIEWER_DEFAULT),
            ).fetchone()
            if existing:
                return self._review_result(con, int(existing[0]), idempotent=True)
            self._deactivate_field_reviews(con, notice_id, fact_type, now)
            con.execute(
                "UPDATE notice_identity_fact SET is_active=0,updated_at=? WHERE notice_id=? AND fact_type=? AND source_type='HUMAN'",
                (now, notice_id, fact_type),
            )
            cursor = con.execute(
                """INSERT INTO notice_identity_review(
                notice_id,fact_type,decision,review_note,reviewer,updated_at)
                VALUES(?,?,'CLEAR',?,?,?)""",
                (notice_id, fact_type, note.strip(), reviewer or REVIEWER_DEFAULT, now),
            )
            return self._review_result(con, int(cursor.lastrowid))

    def review_detail(self, fact_id: int) -> dict:
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            fact = con.execute(
                """SELECT f.*,p.project_name,p.source_site,p.url,p.owner legacy_owner FROM notice_identity_fact f
                JOIN project p ON p.id=f.notice_id WHERE f.id=?""", (fact_id,),
            ).fetchone()
            if not fact:
                raise ValueError(f"身份事实不存在：{fact_id}")
            history = [dict(row) for row in con.execute(
                "SELECT * FROM notice_identity_review WHERE target_fact_id=? ORDER BY id", (fact_id,),
            )]
        item = dict(fact)
        item["review_history"] = history
        item["downstream_impact"] = self._candidate_impact(int(fact["notice_id"]))
        return item

    def queue(
        self, *, fact_type: str = "", priority: str = "", limit: int = 50,
    ) -> list[dict]:
        NoticeIdentityService(self.db).extract_all()
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            facts = con.execute(
                """SELECT f.*,p.project_name,p.source_site,p.url,p.owner legacy_owner FROM notice_identity_fact f
                JOIN project p ON p.id=f.notice_id
                LEFT JOIN notice_identity_review r ON r.target_fact_id=f.id AND r.is_active=1
                WHERE f.is_active=1 AND f.source_type<>'HUMAN' AND r.id IS NULL
                ORDER BY CASE WHEN f.source_location LIKE 'body:%' THEN 0 ELSE 1 END,f.id"""
            ).fetchall()
            candidate_rows = con.execute(
                """SELECT id,notice_id_a,notice_id_b,evidence_score,evidence_level
                FROM project_link_candidate WHERE is_active=1 AND candidate_status='PENDING'"""
            ).fetchall()
            linked_notices = {int(row[0]) for row in con.execute("SELECT notice_id FROM project_notice_link")}
        impacts: dict[int, list[dict]] = defaultdict(list)
        for row in candidate_rows:
            item = dict(row)
            impacts[int(row["notice_id_a"])].append(item)
            impacts[int(row["notice_id_b"])].append(item)
        identities = NoticeIdentityService(self.db).current_identities()
        result = []
        seen_p0_notices: set[int] = set()
        for row in facts:
            item_priority = self._priority(row, impacts, linked_notices, identities)
            if not item_priority:
                continue
            if item_priority == "P0" and int(row["notice_id"]) in seen_p0_notices:
                continue
            if item_priority == "P0":
                seen_p0_notices.add(int(row["notice_id"]))
            if fact_type and row["fact_type"] != fact_type.upper():
                continue
            if priority and item_priority != priority.upper():
                continue
            impact = impacts[int(row["notice_id"])]
            result.append({
                "fact_id": int(row["id"]), "priority": item_priority,
                "risk": self._risk(row, impact, facts), "notice_id": int(row["notice_id"]),
                "title": row["project_name"], "fact_type": row["fact_type"],
                "value": row["raw_value"], "normalized_value": row["normalized_value"],
                "source_type": row["source_type"], "source_reference": row["source_reference"],
                "source_location": row["source_location"], "confidence": row["confidence"],
                "quality": row["quality"], "identifier_type": row["identifier_type"],
                "identifier_namespace": row["identifier_namespace"],
                "identity_strength": row["identity_strength"], "candidate_impact": impact,
            })
        order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        risk_order = {"HIGH_RISK": 0, "MEDIUM_RISK": 1, "LOW_RISK": 2}
        result.sort(key=lambda row: (
            order[row["priority"]], risk_order[row["risk"]],
            -max((item["evidence_score"] for item in row["candidate_impact"]), default=0), row["fact_id"],
        ))
        return result[:max(1, int(limit))]

    def queue_statistics(self) -> dict:
        queue = self.queue(limit=100000)
        owner = [row for row in queue if row["priority"] == "P0"]
        candidate = [
            row for row in queue
            if row["candidate_impact"] and row["fact_type"] in {
                "OWNER", "REGION", "LOT", "PHASE", "PROJECT_IDENTIFIER",
            }
        ]
        with self.db.connect() as con:
            strong = con.execute(
                """SELECT identifier_namespace,COUNT(*) FROM notice_identity_fact
                WHERE is_active=1 AND fact_type='PROJECT_IDENTIFIER' AND identity_strength='STRONG'
                GROUP BY identifier_namespace ORDER BY identifier_namespace"""
            ).fetchall()
            strong_unique = int(con.execute(
                """SELECT COUNT(DISTINCT identifier_namespace||char(31)||identifier_type||char(31)||normalized_value)
                FROM notice_identity_fact WHERE is_active=1 AND fact_type='PROJECT_IDENTIFIER'
                AND identity_strength='STRONG'"""
            ).fetchone()[0])
        high_fact_ids = {
            row["fact_id"] for row in candidate
            if any(item["evidence_level"] == "HIGH" for item in row["candidate_impact"])
        }
        return {
            "owner_body_extracted": len(owner),
            "owner_risk": dict(Counter(row["risk"] for row in owner)),
            "candidate_related_identity_facts": len(candidate),
            "candidate_high_related_facts": len(high_fact_ids),
            "candidate_medium_only_facts": len(candidate) - len(high_fact_ids),
            "strong_identifiers": sum(int(row[1]) for row in strong),
            "strong_identifier_unique_keys": strong_unique,
            "strong_identifier_namespaces": {row[0]: int(row[1]) for row in strong},
        }

    def formal_relation_conflicts(self) -> list[dict]:
        identities = NoticeIdentityService(self.db).current_identities()
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """SELECT l.engineering_project_id,l.notice_id,e.region entity_region,e.owner entity_owner,
                p.project_name FROM project_notice_link l
                JOIN engineering_project e ON e.id=l.engineering_project_id
                JOIN project p ON p.id=l.notice_id ORDER BY l.engineering_project_id,l.notice_id"""
            ).fetchall()
        result = []
        for row in rows:
            identity = identities.get(int(row["notice_id"]))
            if not identity:
                continue
            reasons = []
            entity_region = normalize_region(row["entity_region"] or "")
            notice_region = normalize_region(identity.region_value)
            if entity_region and notice_region and entity_region != notice_region:
                reasons.append(f"地区冲突：工程={row['entity_region']}，公告={identity.region_value}")
            if row["entity_owner"] and identity.owner and identity_text(row["entity_owner"]) != identity_text(identity.owner):
                reasons.append(f"建设单位冲突：工程={row['entity_owner']}，公告={identity.owner}")
            if reasons:
                result.append({
                    "status": "FORMAL_RELATION_CONFLICT",
                    "engineering_project_id": int(row["engineering_project_id"]),
                    "notice_id": int(row["notice_id"]), "title": row["project_name"],
                    "reasons": reasons,
                })
        return result

    def review_history(self, notice_id: int | None = None) -> list[dict]:
        sql, params = "SELECT * FROM notice_identity_review", ()
        if notice_id is not None:
            sql += " WHERE notice_id=?"
            params = (notice_id,)
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            return [dict(row) for row in con.execute(sql + " ORDER BY id", params)]

    def _preview(self, operation: str, **kwargs) -> dict:
        if operation == "FACT":
            with self.db.connect() as con:
                row = con.execute(
                    "SELECT notice_id FROM notice_identity_fact WHERE id=?", (kwargs["fact_id"],),
                ).fetchone()
            if not row:
                raise ValueError(f"身份事实不存在：{kwargs['fact_id']}")
            preview_notice_id = int(row[0])
        else:
            preview_notice_id = int(kwargs["notice_id"])
        before_identity = NoticeIdentityService(self.db).current_identities().get(preview_notice_id)
        before_candidates = self._candidate_snapshot()
        handle = tempfile.NamedTemporaryFile(prefix="radar-identity-review-", suffix=".sqlite3", delete=False)
        temp_path = Path(handle.name)
        handle.close()
        try:
            source = self.db.connect()
            target = sqlite3.connect(temp_path)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            temp_db = Database(temp_path)
            service = IdentityReviewService(temp_db)
            if operation == "FACT":
                applied = service.review_fact(kwargs.pop("fact_id"), kwargs.pop("decision"), dry_run=False, **kwargs)
            elif operation == "OVERRIDE":
                applied = service.override(dry_run=False, **kwargs)
            else:
                applied = service.clear(dry_run=False, **kwargs)
            from .project_candidates import ProjectCandidateService
            ProjectCandidateService(temp_db).build()
            after_candidates = service._candidate_snapshot()
            notice_id = int(applied["notice_id"])
            after_identity = NoticeIdentityService(temp_db).current_identities().get(notice_id)
            keys = {key for key in set(before_candidates) | set(after_candidates) if notice_id in key}
            changes = []
            for key in sorted(keys):
                before, after = before_candidates.get(key), after_candidates.get(key)
                if before != after:
                    changes.append({"notices": key, "before": before, "after": after})
            return {
                "dry_run": True, "operation": operation, "proposed_review": applied,
                "identity_before": asdict(before_identity) if before_identity else None,
                "identity_after": asdict(after_identity) if after_identity else None,
                "candidate_changes": changes,
                "formal_link_count_before": self._count("project_notice_link"),
                "formal_link_count_after": service._count("project_notice_link"),
                "formal_relation_conflicts_after": service.formal_relation_conflicts(),
            }
        finally:
            gc.collect()
            temp_path.unlink(missing_ok=True)

    def _candidate_snapshot(self) -> dict[tuple[int, int], dict]:
        with self.db.connect() as con:
            rows = con.execute(
                """SELECT notice_id_a,notice_id_b,evidence_score,evidence_level,candidate_relation
                FROM project_link_candidate WHERE is_active=1 AND candidate_status='PENDING'"""
            ).fetchall()
        return {
            (int(row[0]), int(row[1])): {
                "score": int(row[2]), "level": row[3], "relation": row[4],
            } for row in rows
        }

    def _candidate_impact(self, notice_id: int) -> list[dict]:
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            return [dict(row) for row in con.execute(
                """SELECT id,notice_id_a,notice_id_b,evidence_score,evidence_level,candidate_relation
                FROM project_link_candidate WHERE is_active=1 AND candidate_status='PENDING'
                AND (notice_id_a=? OR notice_id_b=?) ORDER BY evidence_score DESC,id""",
                (notice_id, notice_id),
            )]

    @staticmethod
    def _priority(row, impacts, linked_notices, identities) -> str:
        if (
            row["fact_type"] == "OWNER" and row["source_type"] == "BODY"
            and row["quality"] == "EXTRACTED_MEDIUM" and not clean_owner(row["legacy_owner"] or "")
        ):
            return "P0"
        if impacts[int(row["notice_id"])] and row["fact_type"] in {
            "OWNER", "REGION", "LOT", "PHASE", "PROJECT_IDENTIFIER",
        }:
            return "P1"
        if row["fact_type"] == "PROJECT_IDENTIFIER" and row["identity_strength"] == "STRONG":
            return "P2"
        identity = identities.get(int(row["notice_id"]))
        if row["fact_type"] == "REGION" and row["confidence"] == "LOW" and identity and not identity.region_value:
            return "P3"
        return ""

    @staticmethod
    def _risk(row, impact: list[dict], all_facts) -> str:
        if any(item["evidence_level"] == "HIGH" for item in impact):
            return "HIGH_RISK"
        raw = row["raw_value"] or ""
        same_notice_owners = {
            item["normalized_value"] for item in all_facts
            if item["notice_id"] == row["notice_id"] and item["fact_type"] == "OWNER"
        }
        if len(raw) > 60 or raw.count("公司") > 1 or any(
            marker in raw for marker in ("联系人", "电话", "地址", "代理机构")
        ) or len(same_notice_owners) > 1:
            return "HIGH_RISK"
        if impact or len(raw) > 35:
            return "MEDIUM_RISK"
        return "LOW_RISK"

    @staticmethod
    def _deactivate_field_reviews(con, notice_id: int, fact_type: str, now: str) -> None:
        con.execute(
            "UPDATE notice_identity_review SET is_active=0,updated_at=? WHERE notice_id=? AND fact_type=? AND is_active=1",
            (now, notice_id, fact_type),
        )

    @staticmethod
    def _review_result(con, review_id: int, *, idempotent: bool = False) -> dict:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM notice_identity_review WHERE id=?", (review_id,)).fetchone()
        result = dict(row)
        result["idempotent"] = idempotent
        return result

    @staticmethod
    def _validate_fact_type(fact_type: str) -> str:
        value = fact_type.strip().upper()
        if value not in REVIEWABLE_FACT_TYPES:
            raise ValueError(f"不支持的fact_type：{fact_type}")
        return value

    @staticmethod
    def _validate_identifier_metadata(fact_type, identifier_type, namespace, strength):
        if fact_type != "PROJECT_IDENTIFIER":
            return "", "", ""
        identifier_type = identifier_type.strip().upper()
        namespace = namespace.strip().upper()
        strength = strength.strip().upper()
        if identifier_type not in IDENTIFIER_TYPES or not namespace or strength not in IDENTITY_STRENGTHS:
            raise ValueError("PROJECT_IDENTIFIER覆盖必须提供有效identifier-type、namespace和strength")
        return identifier_type, namespace, strength

    @staticmethod
    def _normalize(fact_type: str, value: str) -> str:
        if fact_type == "REGION":
            return normalize_region(value) or identity_text(value)
        if fact_type in {"LOT", "PHASE"}:
            return normalize_lot(value)
        if fact_type == "PROJECT_IDENTIFIER":
            return normalize_identifier(value)
        return identity_text(value)

    def _count(self, table: str) -> int:
        with self.db.connect() as con:
            return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
