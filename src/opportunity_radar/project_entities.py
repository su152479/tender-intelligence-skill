"""Conservative, auditable notice-to-engineering-project association."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import re
import sqlite3
import unicodedata
import uuid

from .business_time import business_now_naive
from .db import Database


MATCH_PROJECT_IDENTIFIER = "PROJECT_IDENTIFIER"
MATCH_NAME_OWNER_REGION = "NORMALIZED_NAME_OWNER_REGION"
MATCH_HUMAN = "HUMAN_CONFIRMED"
RELATION_BELONGS_TO = "BELONGS_TO"

_TRUSTED_TRANSACTION_SOURCES = {
    "北京市公共资源交易服务平台",
    "京津冀公共资源交易协同专区",
}
_TRANSACTION_CODE = re.compile(r"^S\d{6,}[A-Z0-9_-]*$")
_ENGINEERING_CODE = re.compile(r"^20\d{2}-[A-Z0-9]{3,24}$")
_SAFE_NOTICE_SUFFIX = re.compile(
    r"(?:"
    r"施工招标公告|招标公告|采购公告|资格预审公告|"
    r"中标候选人公示|中标结果公告|中标结果公示|招标计划|"
    r"竞争性谈判采购公告|竞争性谈判公告"
    r")\s*$"
)
_BAD_OWNER_MARKERS = (
    "或招标代理机构提出", "招标代理机构", "联系人", "联系电话", "电话", "电子邮箱", "地址",
)


@dataclass(frozen=True)
class IdentifierEvidence:
    identifier_type: str
    value: str
    notice_id: int
    document_id: int | None = None
    namespace: str = "UNKNOWN"


def normalize_identifier(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").upper()
    value = value.replace("－", "-").replace("—", "-")
    return re.sub(r"\s+", "", value).strip("，,。.;；")


def canonicalize_project_name(value: str) -> str:
    """Remove only formatting noise and an explicit notice-type suffix."""
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("～", "~")
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n，,。.;；")
    previous = None
    while value and value != previous:
        previous = value
        value = _SAFE_NOTICE_SUFFIX.sub("", value).strip(" \t\r\n，,。.;；-—")
    return value


def _identity_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"\s+", "", value)
    return re.sub(r"[，,。.;；:：'\"“”‘’()（）\[\]【】]", "", value).casefold()


def _clean_owner(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"\s+", " ", value).strip(" ，,。.;；")
    if not (2 <= len(value) <= 100):
        return ""
    if any(marker in value for marker in _BAD_OWNER_MARKERS):
        return ""
    return value


def _valid_engineering_code(value: str) -> bool:
    if not _ENGINEERING_CODE.fullmatch(value):
        return False
    suffix = value.split("-", 1)[1]
    return any(char.isalpha() for char in suffix) or len(suffix) >= 4


class ProjectEntityService:
    def __init__(self, db: Database):
        self.db = db

    def build(self) -> dict:
        """Build only high-confidence automatic links; existing links always win."""
        self.db.init()
        # Local import avoids a module cycle: identity normalization reuses the
        # conservative string helpers defined in this module.
        from .identity import NoticeIdentity, NoticeIdentityService, identity_text
        identity_service = NoticeIdentityService(self.db)
        identity_service.extract_all()
        identities = identity_service.current_identities()
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            notices = self._notice_rows(con)
            links = {
                int(row["notice_id"]): int(row["engineering_project_id"])
                for row in con.execute("SELECT notice_id,engineering_project_id FROM project_notice_link")
            }

            identifier_linked = 0
            for notice in notices:
                notice_id = int(notice["id"])
                identity = identities.get(notice_id, NoticeIdentity(notice_id))
                identifiers = self._trusted_identity_identifiers(identity)
                current_entity = links.get(notice_id)
                matching_entities = self._entities_for_identifiers(con, identifiers)
                if current_entity is not None:
                    self._store_identifiers(con, current_entity, identifiers)
                    continue
                if len(matching_entities) > 1:
                    continue
                if matching_entities:
                    entity_id = next(iter(matching_entities))
                elif identifiers:
                    primary = sorted(
                        identifiers,
                        key=lambda item: (item.identifier_type != "ENGINEERING_CODE", item.identifier_type, item.value),
                    )[0]
                    entity_id = self._create_entity(
                        con,
                        f"IDENTIFIER|{primary.namespace}|{primary.identifier_type}|{primary.value}",
                        identity.core_name or canonicalize_project_name(notice["project_name"]),
                    )
                else:
                    continue
                self._insert_link(con, entity_id, notice_id, MATCH_PROJECT_IDENTIFIER, 100, False)
                self._store_identifiers(con, entity_id, identifiers)
                links[notice_id] = entity_id
                identifier_linked += 1

            groups: dict[tuple[str, str, str], list[sqlite3.Row]] = defaultdict(list)
            for notice in notices:
                identity = identities.get(int(notice["id"]), NoticeIdentity(int(notice["id"])))
                owner_is_usable = identity.owner_quality in {"STRUCTURED_HIGH", "EXTRACTED_MEDIUM"}
                # Keep the established auto-link boundary: newly recovered BODY
                # owners improve audit/candidates but do not silently widen formal
                # entity creation until they have been reviewed.
                legacy_owner = _clean_owner(notice["owner"] or "")
                owner_is_usable = owner_is_usable and identity_text(legacy_owner) == identity_text(identity.owner)
                fingerprint = (
                    identity_text(identity.core_name), identity.region_value,
                    identity_text(identity.owner) if owner_is_usable else "",
                )
                if len(fingerprint[0]) >= 6 and fingerprint[1] and fingerprint[2]:
                    groups[fingerprint].append(notice)

            name_linked = 0
            for fingerprint, candidates in groups.items():
                if len(candidates) < 2:
                    continue
                existing_entities = {
                    links[int(notice["id"])] for notice in candidates if int(notice["id"]) in links
                }
                if len(existing_entities) > 1:
                    continue
                if existing_entities:
                    entity_id = next(iter(existing_entities))
                else:
                    digest = hashlib.sha256("\x1f".join(fingerprint).encode("utf-8")).hexdigest()
                    canonical = min((
                        identities[int(row["id"])].core_name for row in candidates
                    ), key=lambda value: (len(value), value))
                    entity_id = self._create_entity(con, f"NAME_OWNER_REGION|{digest}", canonical)
                for notice in candidates:
                    notice_id = int(notice["id"])
                    if notice_id in links:
                        continue
                    self._insert_link(con, entity_id, notice_id, MATCH_NAME_OWNER_REGION, 95, False)
                    links[notice_id] = entity_id
                    name_linked += 1

            for entity_id in sorted(set(links.values())):
                self._refresh_entity(con, entity_id)
        result = self.statistics()
        result["new_identifier_links"] = identifier_linked
        result["new_name_owner_region_links"] = name_linked
        return result

    def confirm_notices(
        self, notice_ids: list[int], *, canonical_name: str = "",
        engineering_project_id: int | None = None,
    ) -> int:
        """Human-confirm notices into one entity without letting automation override it later."""
        unique_ids = sorted(set(int(value) for value in notice_ids))
        if not unique_ids:
            raise ValueError("至少需要一个公告ID")
        self.db.init()
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            placeholders = ",".join("?" for _ in unique_ids)
            notices = con.execute(
                f"SELECT * FROM project WHERE id IN ({placeholders}) ORDER BY id", unique_ids
            ).fetchall()
            if len(notices) != len(unique_ids):
                found = {int(row["id"]) for row in notices}
                raise ValueError(f"公告不存在：{sorted(set(unique_ids) - found)}")

            if engineering_project_id is not None:
                if not con.execute(
                    "SELECT 1 FROM engineering_project WHERE id=?", (engineering_project_id,)
                ).fetchone():
                    raise ValueError(f"工程实体不存在：{engineering_project_id}")
                target_id = engineering_project_id
            else:
                linked = con.execute(
                    f"""SELECT DISTINCT engineering_project_id,confirmed_by_human
                    FROM project_notice_link WHERE notice_id IN ({placeholders})""", unique_ids
                ).fetchall()
                human_entities = {int(row[0]) for row in linked if row[1]}
                if len(human_entities) > 1:
                    raise ValueError("所选公告已分别由人工确认到不同工程，需明确指定工程实体")
                all_entities = {int(row[0]) for row in linked}
                if human_entities:
                    target_id = next(iter(human_entities))
                elif len(all_entities) == 1:
                    target_id = next(iter(all_entities))
                else:
                    name = canonical_name.strip() or min(
                        (canonicalize_project_name(row["project_name"]) for row in notices),
                        key=lambda value: (len(value), value),
                    )
                    target_id = self._create_entity(con, f"HUMAN|{uuid.uuid4().hex}", name)

            for notice in notices:
                notice_id = int(notice["id"])
                existing = con.execute(
                    "SELECT engineering_project_id,confirmed_by_human FROM project_notice_link WHERE notice_id=?",
                    (notice_id,),
                ).fetchone()
                if existing and existing["confirmed_by_human"] and int(existing["engineering_project_id"]) != target_id:
                    raise ValueError(f"公告 {notice_id} 已由人工确认到其他工程")
                con.execute(
                    """INSERT INTO project_notice_link(
                    engineering_project_id,notice_id,relation_type,match_method,match_score,confirmed_by_human)
                    VALUES(?,?,?,?,?,1)
                    ON CONFLICT(notice_id) DO UPDATE SET
                    engineering_project_id=excluded.engineering_project_id,
                    relation_type=excluded.relation_type,match_method=excluded.match_method,
                    match_score=excluded.match_score,confirmed_by_human=1""",
                    (target_id, notice_id, RELATION_BELONGS_TO, MATCH_HUMAN, 100),
                )
            if canonical_name.strip():
                con.execute(
                    "UPDATE engineering_project SET canonical_name=?,updated_at=? WHERE id=?",
                    (canonicalize_project_name(canonical_name), business_now_naive().isoformat(timespec="seconds"), target_id),
                )
            self._refresh_entity(con, target_id, preserve_name=bool(canonical_name.strip()))
            return target_id

    def statistics(self) -> dict:
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            total = int(con.execute("SELECT COUNT(*) FROM project").fetchone()[0])
            linked = int(con.execute("SELECT COUNT(*) FROM project_notice_link").fetchone()[0])
            entities = int(con.execute("SELECT COUNT(*) FROM engineering_project").fetchone()[0])
            sizes = [int(row[0]) for row in con.execute(
                "SELECT COUNT(*) FROM project_notice_link GROUP BY engineering_project_id"
            )]
            methods = {
                row[0]: int(row[1]) for row in con.execute(
                    "SELECT match_method,COUNT(*) FROM project_notice_link GROUP BY match_method ORDER BY match_method"
                )
            }
            return {
                "total_notices": total,
                "linked_notices": linked,
                "unlinked_notices": total - linked,
                "engineering_projects": entities,
                "single_notice_projects": sum(size == 1 for size in sizes),
                "multi_notice_projects": sum(size > 1 for size in sizes),
                "max_notices_per_project": max(sizes, default=0),
                "match_methods": methods,
            }

    def multi_notice_samples(self, limit: int = 10) -> list[dict]:
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            projects = con.execute(
                """SELECT e.*,COUNT(l.id) notice_count
                FROM engineering_project e JOIN project_notice_link l ON l.engineering_project_id=e.id
                GROUP BY e.id HAVING COUNT(l.id)>1
                ORDER BY notice_count DESC,e.id LIMIT ?""", (max(1, int(limit)),)
            ).fetchall()
            result = []
            for project in projects:
                notices = [dict(row) for row in con.execute(
                    """SELECT p.id,p.project_name,p.project_no,p.region,p.owner,l.match_method,l.match_score,
                    l.confirmed_by_human FROM project_notice_link l
                    JOIN project p ON p.id=l.notice_id
                    WHERE l.engineering_project_id=? ORDER BY p.id""", (project["id"],)
                )]
                result.append({"project": dict(project), "notices": notices})
            return result

    def identifier_analysis(self) -> list[dict]:
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT id,source_site,project_no FROM project").fetchall()
            categories: dict[str, list[tuple[str, int]]] = defaultdict(list)
            for row in rows:
                value = normalize_identifier(row["project_no"] or "")
                if not value:
                    continue
                if row["source_site"] in _TRUSTED_TRANSACTION_SOURCES and _TRANSACTION_CODE.fullmatch(value):
                    category = "TRANSACTION_PROJECT_CODE"
                elif row["source_site"] == "中交集团供应链管理信息系统":
                    category = "CCCC_SCHEME_CODE"
                elif row["source_site"] == "中建云筑网":
                    category = "YUNZHU_TENDER_CODE"
                else:
                    category = "OTHER_NOTICE_CODE"
                categories[category].append((value, int(row["id"])))

            result = []
            suitability = {
                "TRANSACTION_PROJECT_CODE": "可用于完全相同编号的高可信关联",
                "CCCC_SCHEME_CODE": "不适合；属于单次采购方案/公告",
                "YUNZHU_TENDER_CODE": "不适合；属于单次招标",
                "OTHER_NOTICE_CODE": "不适合自动关联；语义混合",
            }
            for category, values in categories.items():
                grouped: dict[str, set[int]] = defaultdict(set)
                for value, notice_id in values:
                    grouped[value].add(notice_id)
                result.append({
                    "identifier_type": category,
                    "count": len(values),
                    "duplicate_values": sum(len(ids) > 1 for ids in grouped.values()),
                    "cross_notice_duplicate_values": sum(len(ids) > 1 for ids in grouped.values()),
                    "suitability": suitability[category],
                })

            documents = con.execute(
                """SELECT di.identifier_type,di.identifier,d.notice_id
                FROM document_identifier di JOIN document_text d ON d.id=di.document_id"""
            ).fetchall()
            by_type: dict[str, list[tuple[str, int]]] = defaultdict(list)
            for row in documents:
                by_type[row["identifier_type"]].append(
                    (normalize_identifier(row["identifier"]), int(row["notice_id"]))
                )
            for identifier_type, values in by_type.items():
                grouped = defaultdict(set)
                for value, notice_id in values:
                    grouped[value].add(notice_id)
                if identifier_type == "交易项目编号":
                    suitable = "仅S开头正式交易项目编号可用"
                elif identifier_type == "工程编号":
                    suitable = "通过格式质量校验后可用；年月样式误报排除"
                else:
                    suitable = "仅可识别为S开头正式交易项目编号时使用"
                result.append({
                    "identifier_type": f"DOCUMENT:{identifier_type}",
                    "count": len(values),
                    "duplicate_values": sum(len(ids) > 1 for ids in grouped.values()),
                    "cross_notice_duplicate_values": sum(len(ids) > 1 for ids in grouped.values()),
                    "suitability": suitable,
                })
            return sorted(result, key=lambda row: row["identifier_type"])

    @staticmethod
    def _notice_rows(con: sqlite3.Connection):
        return con.execute(
            """SELECT p.*,
            MIN(CASE WHEN r.status IN ('success','partial','warning') THEN o.observed_at END) reliable_first_seen,
            MAX(CASE WHEN r.status IN ('success','partial','warning') THEN o.observed_at END) reliable_last_seen
            FROM project p
            LEFT JOIN notice_observation o ON o.notice_id=p.id
            LEFT JOIN source_run r ON r.id=o.run_id
            GROUP BY p.id ORDER BY p.id"""
        ).fetchall()

    def _trusted_identifiers(self, con: sqlite3.Connection, notice) -> list[IdentifierEvidence]:
        notice_id = int(notice["id"])
        result: list[IdentifierEvidence] = []
        project_no = normalize_identifier(notice["project_no"] or "")
        if (
            notice["source_site"] in _TRUSTED_TRANSACTION_SOURCES
            and _TRANSACTION_CODE.fullmatch(project_no)
        ):
            result.append(IdentifierEvidence("TRANSACTION_PROJECT_CODE", project_no, notice_id))

        documents = con.execute(
            """SELECT d.id document_id,di.identifier_type,di.identifier
            FROM document_text d JOIN document_identifier di ON di.document_id=d.id
            WHERE d.notice_id=?""", (notice_id,)
        ).fetchall()
        for row in documents:
            value = normalize_identifier(row["identifier"])
            if row["identifier_type"] in ("交易项目编号", "项目编号") and _TRANSACTION_CODE.fullmatch(value):
                result.append(IdentifierEvidence(
                    "TRANSACTION_PROJECT_CODE", value, notice_id, int(row["document_id"])
                ))
            elif row["identifier_type"] == "工程编号" and _valid_engineering_code(value):
                result.append(IdentifierEvidence(
                    "ENGINEERING_CODE", value, notice_id, int(row["document_id"])
                ))
        unique = {}
        for item in result:
            unique[(item.identifier_type, item.value)] = item
        return list(unique.values())

    @staticmethod
    def _trusted_identity_identifiers(identity) -> list[IdentifierEvidence]:
        """Keep step-3 automatic-link policy while sourcing values from facts."""
        result = []
        for item in identity.identifiers:
            if item.strength != "STRONG":
                continue
            if item.identifier_type == "TRANSACTION_PROJECT_CODE" and item.namespace != "BEIJING_GGZY":
                continue
            if item.identifier_type not in {"TRANSACTION_PROJECT_CODE", "ENGINEERING_CODE"}:
                continue
            document_id = None
            if item.source_reference.startswith("document:"):
                try:
                    document_id = int(item.source_reference.partition(":")[2])
                except ValueError:
                    document_id = None
            result.append(IdentifierEvidence(
                item.identifier_type, item.value, identity.notice_id, document_id, item.namespace,
            ))
        unique = {(item.identifier_type, item.value): item for item in result}
        return list(unique.values())

    @staticmethod
    def _entities_for_identifiers(
        con: sqlite3.Connection, identifiers: list[IdentifierEvidence],
    ) -> set[int]:
        entities = set()
        for item in identifiers:
            row = con.execute(
                """SELECT engineering_project_id FROM engineering_project_identifier
                WHERE identifier_type=? AND identifier_value=?""",
                (item.identifier_type, item.value),
            ).fetchone()
            if row:
                entities.add(int(row[0]))
        return entities

    @staticmethod
    def _create_entity(con: sqlite3.Connection, identity_key: str, canonical_name: str) -> int:
        existing = con.execute(
            "SELECT id FROM engineering_project WHERE identity_key=?", (identity_key,)
        ).fetchone()
        if existing:
            return int(existing[0])
        cursor = con.execute(
            "INSERT INTO engineering_project(identity_key,canonical_name) VALUES(?,?)",
            (identity_key, canonical_name or "未命名工程"),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _insert_link(
        con: sqlite3.Connection, entity_id: int, notice_id: int,
        method: str, score: int, human: bool,
    ) -> None:
        con.execute(
            """INSERT OR IGNORE INTO project_notice_link(
            engineering_project_id,notice_id,relation_type,match_method,match_score,confirmed_by_human)
            VALUES(?,?,?,?,?,?)""",
            (entity_id, notice_id, RELATION_BELONGS_TO, method, score, int(human)),
        )

    @staticmethod
    def _store_identifiers(
        con: sqlite3.Connection, entity_id: int, identifiers: list[IdentifierEvidence],
    ) -> None:
        for item in identifiers:
            existing = con.execute(
                """SELECT engineering_project_id FROM engineering_project_identifier
                WHERE identifier_type=? AND identifier_value=?""",
                (item.identifier_type, item.value),
            ).fetchone()
            if existing and int(existing[0]) != entity_id:
                continue
            con.execute(
                """INSERT OR IGNORE INTO engineering_project_identifier(
                engineering_project_id,identifier_type,identifier_value,source_notice_id,source_document_id)
                VALUES(?,?,?,?,?)""",
                (entity_id, item.identifier_type, item.value, item.notice_id, item.document_id),
            )

    @staticmethod
    def _refresh_entity(
        con: sqlite3.Connection, entity_id: int, *, preserve_name: bool = False,
    ) -> None:
        rows = con.execute(
            """SELECT p.*,
            MIN(CASE WHEN r.status IN ('success','partial','warning') THEN o.observed_at END) reliable_first_seen,
            MAX(CASE WHEN r.status IN ('success','partial','warning') THEN o.observed_at END) reliable_last_seen
            FROM project_notice_link l JOIN project p ON p.id=l.notice_id
            LEFT JOIN notice_observation o ON o.notice_id=p.id
            LEFT JOIN source_run r ON r.id=o.run_id
            WHERE l.engineering_project_id=? GROUP BY p.id ORDER BY p.id""", (entity_id,)
        ).fetchall()
        if not rows:
            return
        names = [canonicalize_project_name(row["project_name"]) for row in rows]
        canonical = min(names, key=lambda value: (len(value), value))
        regions = {re.sub(r"\s+", " ", row["region"] or "").strip() for row in rows if (row["region"] or "").strip()}
        owners = {_clean_owner(row["owner"] or "") for row in rows}
        owners.discard("")
        first_values = [row["reliable_first_seen"] for row in rows if row["reliable_first_seen"]]
        last_values = [row["reliable_last_seen"] for row in rows if row["reliable_last_seen"]]
        current = con.execute(
            "SELECT canonical_name,region,owner,first_seen_at,last_seen_at FROM engineering_project WHERE id=?",
            (entity_id,),
        ).fetchone()
        new_name = current["canonical_name"] if preserve_name else canonical
        new_values = (
            new_name,
            next(iter(regions)) if len(regions) == 1 else "",
            next(iter(owners)) if len(owners) == 1 else "",
            min(first_values) if first_values else None,
            max(last_values) if last_values else None,
        )
        old_values = (
            current["canonical_name"], current["region"], current["owner"],
            current["first_seen_at"], current["last_seen_at"],
        )
        if new_values != old_values:
            con.execute(
                """UPDATE engineering_project SET canonical_name=?,region=?,owner=?,
                first_seen_at=?,last_seen_at=?,updated_at=? WHERE id=?""",
                (*new_values, business_now_naive().isoformat(timespec="seconds"), entity_id),
            )
