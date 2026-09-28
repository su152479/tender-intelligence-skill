"""Explainable, review-only engineering-project link candidates."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from itertools import combinations
import json
import hashlib
import re
import sqlite3
import unicodedata

from .business_time import business_now_naive
from .db import Database
from .identity import NoticeIdentity, NoticeIdentityService, identity_text
from .project_entities import ProjectEntityService, canonicalize_project_name, normalize_identifier


ALGORITHM_VERSION = "project-candidate-v1.2"
MIN_CANDIDATE_SCORE = 75
HIGH_SCORE = 90

_LOT = re.compile(
    r"第?[一二三四五六七八九十百零〇\d]+(?:施工)?标段|"
    r"[一二三四五六七八九十百零〇\d]+标段|"
    r"标段[一二三四五六七八九十百零〇\d]+"
)
_PHASE = re.compile(r"(?:第)?([一二三四五六七八九十百零〇\d]+)期")
_YEAR = re.compile(r"(20\d{2})年(?:度)?")
_STATION = re.compile(r"([\u4e00-\u9fffA-Za-z0-9]{1,8}站)(?!房|台)")
_CHAINAGE_RANGE = re.compile(r"K?\d+\+\d+\s*(?:-|~|至|到)\s*K?\d+\+\d+", re.IGNORECASE)
_PLACE_RANGE = re.compile(r"[（(]([^（）()]{2,20}(?:-|~|至|到)[^（）()]{2,20})[）)]")
_LEADING_TRANSACTION = re.compile(r"^[A-Z]\d{10,}")
_ANCHOR_PATTERNS = (
    re.compile(r"(?:国道|省道|县道|高速公路|轨道交通|地铁)\s*[A-Za-z]?\d+(?:号线)?"),
    re.compile(r"[\u4e00-\u9fff]{2,10}(?:河|渠|水库|泵站|水闸|道路|公路|桥|隧道)"),
)
_GENERIC_GRAMS = {
    "建设工程", "施工工程", "工程施工", "施工项目", "采购项目", "招标项目",
    "资格预审", "中标结果", "中标候选", "公共资源", "基础设施",
}
_BAD_OWNER_MARKERS = (
    "或招标代理机构提出", "招标代理机构", "联系人", "联系电话", "电子邮箱", "监督部门",
)
_SUBPROJECT_SCOPE_HINT = re.compile(
    r"酒店|商业|山姆|住宅|写字楼|办公楼|学校|医院|地库|地下室|"
    r"裙房|塔楼|厂房|宿舍|站房|车站|楼栋|地块|功能区"
)
_SUBPROJECT_SCOPE_BOUNDARY = re.compile(
    r"装配式|预制|混凝土|构件|管片|箱梁|材料|设备|工程|劳务|专业|"
    r"租赁|采购|供应|制作|安装"
)
_STRUCTURED_REGION_SOURCES = {
    "中交集团供应链管理信息系统",
    "中建云筑网",
    "中国政府采购网",
}
_SOURCE_SECTION_REGION_SOURCES = {"京津冀公共资源交易协同专区"}
_COLLECTOR_DEFAULT_REGION_SOURCES = {
    "京冀公共资源交易跨区域信息专区（河北）",
    "天津市交通运输委员会招标公告",
}
_TITLE_REGION_MARKERS = {
    "北京市": (
        "北京市", "东城区", "西城区", "朝阳区", "海淀区", "丰台区", "石景山区",
        "通州区", "顺义区", "昌平区", "大兴区", "房山区", "门头沟区", "怀柔区",
        "平谷区", "密云区", "延庆区", "亦庄", "孙河",
    ),
    "天津市": ("天津市", "滨海新区", "武清区", "宝坻区", "蓟州区", "静海区", "宁河区"),
    "河北省": (
        "河北省", "石家庄", "唐山", "秦皇岛", "邯郸", "邢台", "保定", "张家口",
        "承德", "沧州", "廊坊", "衡水", "定州", "辛集", "雄安", "涿州", "武安",
        "顺平", "涞源", "莲池区", "藁城区",
    ),
}


def _identity(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"\s+", "", value)
    return re.sub(r"[，,。.;；:：'\"“”‘’()（）\[\]【】_-]", "", value).casefold()


def _owner(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"\s+", " ", value).strip(" ，,。.;；")
    if not 2 <= len(value) <= 100 or any(marker in value for marker in _BAD_OWNER_MARKERS):
        return ""
    return value


def _region_bucket(value: str) -> str:
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or ""))
    for token in ("北京市", "天津市", "河北省"):
        if token[:2] in compact:
            return token
    return compact


def _title_region(title: str) -> str:
    """Extract only recognizable project-place markers after a leading organization name."""
    value = unicodedata.normalize("NFKC", title or "")
    value = re.sub(r"^.{0,45}?(?:集团股份有限公司|集团有限公司|有限责任公司|有限公司)", "", value)
    matches = [region for region, markers in _TITLE_REGION_MARKERS.items() if any(marker in value for marker in markers)]
    return matches[0] if len(matches) == 1 else ""


def _region_evidence(row: sqlite3.Row) -> tuple[str, str]:
    """Return provenance and confidence without rewriting the legacy project.region field."""
    source = row["source_site"] or ""
    raw_text = row["raw_text"] or ""
    if source in _STRUCTURED_REGION_SOURCES:
        return "PAGE_STRUCTURED_FIELD", "HIGH"
    if source == "北京市公共资源交易服务平台":
        if re.search(r"建设地点\s*[：:]?\s*\S+", raw_text):
            return "PAGE_STRUCTURED_FIELD", "HIGH"
        title_region = _title_region(row["project_name"])
        return ("TITLE_EXTRACTED", "MEDIUM") if title_region else ("COLLECTOR_DEFAULT", "LOW")
    if source in _SOURCE_SECTION_REGION_SOURCES:
        return "SOURCE_SECTION", "MEDIUM"
    if source in _COLLECTOR_DEFAULT_REGION_SOURCES:
        title_region = _title_region(row["project_name"])
        if title_region and title_region == _region_bucket(row["region"] or ""):
            return "TITLE_EXTRACTED", "MEDIUM"
        return "COLLECTOR_DEFAULT", "LOW"
    if source == "中铁鲁班网":
        title_region = _title_region(row["project_name"])
        if title_region and title_region == _region_bucket(row["region"] or ""):
            return "TITLE_EXTRACTED", "MEDIUM"
        return "TEXT_INFERRED_LOW", "LOW"
    if row["region"]:
        return "UNKNOWN_LEGACY", "LOW"
    return "UNKNOWN", "UNKNOWN"


def _owner_evidence(row: sqlite3.Row) -> tuple[str, str]:
    raw_owner = unicodedata.normalize("NFKC", row["owner"] or "").strip()
    if not raw_owner:
        return "", "MISSING"
    clean = _owner(raw_owner)
    if not clean:
        return "", "CONTAMINATED"
    if row["source_site"] == "中交集团供应链管理信息系统":
        # CCCC opUnitName is a structured procurement/operating unit, not proven project owner.
        return clean, "PROCUREMENT_ORG_MEDIUM"
    if row["source_site"] == "中国政府采购网":
        return clean, "STRUCTURED_HIGH"
    return clean, "EXTRACTED_MEDIUM"


def _parent_core(value: str) -> str:
    value = _LOT.sub("", value)
    return re.sub(r"\s+", " ", value).strip(" ，,。.;；-—")


def _split_project_identity(value: str) -> tuple[str, str]:
    """Separate a clear project-prefix from a trailing procurement package conservatively."""
    positions = [match.end() for match in re.finditer("项目", value)]
    for end in reversed(positions):
        prefix, tail = value[:end], value[end:].strip(" ，,。.;；-—")
        if len(_identity(prefix)) >= 6 and tail and re.search(r"采购|租赁|劳务分包|专业分包", tail):
            return prefix, tail
    return value, ""


def _subproject_scope(procurement_tail: str) -> str:
    """Extract an explicit functional/building scope before the procurement subject."""
    value = unicodedata.normalize("NFKC", procurement_tail or "").strip(" ，,。.;；-—")
    boundary = _SUBPROJECT_SCOPE_BOUNDARY.search(value)
    if not boundary:
        return ""
    prefix = value[:boundary.start()].strip(" ，,。.;；-—")
    if not prefix or len(prefix) > 20 or not _SUBPROJECT_SCOPE_HINT.search(prefix):
        return ""
    return prefix


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def _ngrams(value: str, size: int = 6) -> set[str]:
    compact = _identity(value)
    if len(compact) < size:
        return {compact} if compact else set()
    return {
        compact[index:index + size]
        for index in range(len(compact) - size + 1)
        if compact[index:index + size] not in _GENERIC_GRAMS
    }


def _extract(pattern: re.Pattern, value: str) -> tuple[str, ...]:
    return tuple(sorted({_identity(match) for match in pattern.findall(value) if _identity(match)}))


@dataclass(frozen=True)
class NoticeFeature:
    id: int
    title: str
    region: str
    region_bucket: str
    region_source: str
    region_confidence: str
    owner: str
    owner_quality: str
    publish_date: str
    source_site: str
    core: str
    core_key: str
    parent_core: str
    parent_key: str
    lots: tuple[str, ...]
    phases: tuple[str, ...]
    years: tuple[str, ...]
    stations: tuple[str, ...]
    ranges: tuple[str, ...]
    identifiers: tuple[str, ...]
    procurement_tail: str
    subproject_scope: str
    url: str
    result_party: str


class ProjectCandidateService:
    """Generate review candidates without ever changing formal project links."""

    def __init__(self, db: Database):
        self.db = db

    def build(self) -> dict:
        self.db.init()
        identity_service = NoticeIdentityService(self.db)
        identity_service.extract_all()
        identities = identity_service.current_identities()
        now = business_now_naive().isoformat(timespec="seconds")
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            features = self._eligible_features(con, identities)
            by_id = {feature.id: feature for feature in features}
            pairs = self._blocked_pairs(features)
            con.execute("UPDATE project_link_candidate SET is_active=0")
            generated = 0
            for notice_id_a, notice_id_b in sorted(pairs):
                result = self._evaluate_pair(by_id[notice_id_a], by_id[notice_id_b])
                if result is None or result["evidence_score"] < MIN_CANDIDATE_SCORE:
                    continue
                generated += 1
                candidate_key = f"{notice_id_a}:{notice_id_b}"
                evidence_json = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                evidence_hash = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
                con.execute(
                    """INSERT INTO project_link_candidate(
                    notice_id_a,notice_id_b,candidate_key,candidate_relation,evidence_score,
                    evidence_level,candidate_status,evidence_json,evidence_hash,is_active,algorithm_version,updated_at)
                    VALUES(?,?,?,?,?,?,'PENDING',?,?,1,?,?)
                    ON CONFLICT(notice_id_a,notice_id_b) DO UPDATE SET
                    candidate_relation=excluded.candidate_relation,
                    evidence_score=excluded.evidence_score,
                    evidence_level=excluded.evidence_level,
                    evidence_json=excluded.evidence_json,
                    evidence_hash=excluded.evidence_hash,
                    is_active=1,
                    algorithm_version=excluded.algorithm_version,
                    candidate_status=CASE
                      WHEN project_link_candidate.candidate_status='DEFERRED'
                       AND COALESCE(project_link_candidate.reviewed_evidence_hash,'')<>excluded.evidence_hash
                      THEN 'PENDING'
                      ELSE project_link_candidate.candidate_status END,
                    reviewed_at=CASE
                      WHEN project_link_candidate.candidate_status='DEFERRED'
                       AND COALESCE(project_link_candidate.reviewed_evidence_hash,'')<>excluded.evidence_hash
                      THEN NULL ELSE project_link_candidate.reviewed_at END,
                    review_note=CASE
                      WHEN project_link_candidate.candidate_status='DEFERRED'
                       AND COALESCE(project_link_candidate.reviewed_evidence_hash,'')<>excluded.evidence_hash
                      THEN '' ELSE project_link_candidate.review_note END,
                    updated_at=excluded.updated_at""",
                    (
                        notice_id_a, notice_id_b, candidate_key, result["candidate_relation"],
                        result["evidence_score"], result["evidence_level"],
                        evidence_json, evidence_hash, ALGORITHM_VERSION, now,
                    ),
                )
        stats = self.statistics()
        stats.update({
            "eligible_notices": len(features),
            "blocking_pairs": len(pairs),
            "generated_candidates": generated,
            "algorithm_version": ALGORITHM_VERSION,
        })
        return stats

    def list_candidates(self, status: str = "PENDING", limit: int = 20) -> list[dict]:
        status = status.upper()
        if status not in {"PENDING", "CONFIRMED", "REJECTED", "DEFERRED"}:
            raise ValueError(f"不支持的候选状态：{status}")
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """SELECT c.*,a.project_name title_a,a.region region_a,a.owner owner_a,
                a.publish_date publish_date_a,a.source_site source_site_a,
                b.project_name title_b,b.region region_b,b.owner owner_b,
                b.publish_date publish_date_b,b.source_site source_site_b
                FROM project_link_candidate c
                JOIN project a ON a.id=c.notice_id_a
                JOIN project b ON b.id=c.notice_id_b
                WHERE c.candidate_status=? AND (?<>'PENDING' OR c.is_active=1)
                ORDER BY c.evidence_score DESC,c.id LIMIT ?""",
                (status, status, max(1, int(limit))),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["owner_a"] = _owner(item["owner_a"])
                item["owner_b"] = _owner(item["owner_b"])
                item["evidence"] = json.loads(item.pop("evidence_json"))
                result.append(item)
            return result

    def identity_audit(self) -> dict:
        """Report the persisted identity layer used by candidates."""
        service = NoticeIdentityService(self.db)
        service.extract_all()
        identities = service.current_identities()
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT * FROM project ORDER BY id").fetchall()
        features = [self._feature(row, identities[int(row["id"])]) for row in rows]
        region_sources = Counter(feature.region_source for feature in features)
        # Compatibility label retained for existing audit consumers; the fact
        # layer records the canonical source type as LEGACY_INFERENCE.
        if region_sources.get("LEGACY_INFERENCE"):
            region_sources["TEXT_INFERRED_LOW"] = region_sources["LEGACY_INFERENCE"]
        region_confidence = Counter(feature.region_confidence for feature in features)
        owner_quality = Counter(feature.owner_quality for feature in features)
        region_by_source_site: dict[str, Counter] = defaultdict(Counter)
        owner_by_source_site: dict[str, Counter] = defaultdict(Counter)
        for feature in features:
            region_by_source_site[feature.source_site][feature.region_source] += 1
            owner_by_source_site[feature.source_site][feature.owner_quality] += 1
        low_region_samples = [
            {
                "notice_id": feature.id,
                "title": feature.title,
                "region_value": feature.region,
                "region_source": feature.region_source,
                "region_confidence": feature.region_confidence,
                "source_site": feature.source_site,
            }
            for feature in features if feature.region_confidence in {"LOW", "UNKNOWN"}
        ]
        return {
            "total_notices": len(features),
            "region_source_counts": dict(sorted(region_sources.items())),
            "region_confidence_counts": dict(sorted(region_confidence.items())),
            "region_source_counts_by_site": {
                source: dict(sorted(counts.items()))
                for source, counts in sorted(region_by_source_site.items())
            },
            "low_or_unknown_region_count": len(low_region_samples),
            "low_region_samples": low_region_samples,
            "owner_non_empty": sum(bool((row["owner"] or "").strip()) for row in rows),
            "owner_quality_counts": dict(sorted(owner_quality.items())),
            "owner_quality_counts_by_site": {
                source: dict(sorted(counts.items()))
                for source, counts in sorted(owner_by_source_site.items())
            },
            "owner_structured": owner_quality["STRUCTURED_HIGH"],
            "owner_contaminated": owner_quality["CONTAMINATED"],
            "owner_inferred": 0,
            "owner_usable": (
                owner_quality["STRUCTURED_HIGH"] + owner_quality["EXTRACTED_MEDIUM"]
                + owner_quality["PROCUREMENT_ORG_MEDIUM"]
            ),
            "owner_unusable": owner_quality["CONTAMINATED"] + owner_quality["MISSING"],
            "field_origins": {
                "core_project_name": "TITLE_NORMALIZED；来自公告标题，保守删除公告阶段后缀并拆分明确采购尾部",
                "lot": "TITLE_REGEX；从core project name提取标段",
                "station": "TITLE_REGEX；从core project name提取站点",
                "phase": "TITLE_REGEX；从core project name提取期次",
                "year": "TITLE_REGEX；从core project name提取年度",
                "range": "TITLE_REGEX；从core project name提取桩号或括号内起止范围",
                "region": "按Collector真实赋值路径回溯分类；不改写project.region",
                "owner": (
                    "按Collector字段来源及污染检测分级；中交opUnitName单列为"
                    "PROCUREMENT_ORG_MEDIUM，不冒充项目建设单位；不做NER推断"
                ),
            },
        }

    def clusters(self) -> list[dict]:
        """Dynamically group active review edges; connectivity is never treated as identity."""
        identity_service = NoticeIdentityService(self.db)
        identity_service.extract_all()
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            edge_rows = con.execute(
                """SELECT * FROM project_link_candidate
                WHERE is_active=1 AND candidate_status IN ('PENDING','DEFERRED')
                ORDER BY evidence_score DESC,id"""
            ).fetchall()
            components = self._components([(int(row["notice_id_a"]), int(row["notice_id_b"])) for row in edge_rows])
            if not components:
                return []
            notice_ids = sorted({notice_id for members in components for notice_id in members})
            placeholders = ",".join("?" for _ in notice_ids)
            notice_rows = con.execute(
                f"SELECT * FROM project WHERE id IN ({placeholders})", notice_ids
            ).fetchall()
            identities = identity_service.current_identities(notice_ids)
            features = {
                int(row["id"]): self._feature(
                    row, identities.get(int(row["id"]), NoticeIdentity(int(row["id"])))
                ) for row in notice_rows
            }
            all_edges = con.execute(
                "SELECT * FROM project_link_candidate WHERE is_active=1 ORDER BY id"
            ).fetchall()

        result = []
        for member_ids in components:
            member_set = set(member_ids)
            cluster_edges = [
                row for row in all_edges
                if int(row["notice_id_a"]) in member_set and int(row["notice_id_b"]) in member_set
            ]
            edge_pairs = {(int(row["notice_id_a"]), int(row["notice_id_b"])) for row in cluster_edges}
            pair_conflicts = []
            for a_id, b_id in combinations(member_ids, 2):
                conflicts = self._hard_conflicts(features[a_id], features[b_id], same_project=True)
                if conflicts:
                    pair_conflicts.append({"notice_id_a": a_id, "notice_id_b": b_id, "conflicts": conflicts})
                elif (a_id, b_id) not in edge_pairs:
                    pair_conflicts.append({
                        "notice_id_a": a_id, "notice_id_b": b_id,
                        "conflicts": ["两公告之间没有直接达到候选门槛的边"],
                    })
            nodes = [self._cluster_node(features[notice_id]) for notice_id in member_ids]
            edges = [self._cluster_edge(row) for row in cluster_edges]
            relation_counts = Counter(edge["candidate_relation"] for edge in edges)
            common_names = Counter(node["core_name"] for node in nodes)
            suggested_name = min(
                common_names, key=lambda value: (-common_names[value], len(value), value)
            )
            risky = bool(pair_conflicts) or relation_counts["SAME_PARENT_PROJECT"] or relation_counts["UNCERTAIN"]
            result.append({
                "cluster_id": min(member_ids),
                "notice_count": len(member_ids),
                "notice_ids": member_ids,
                "suggested_core_name": suggested_name,
                "regions": sorted({node["region_value"] for node in nodes if node["region_value"]}),
                "owners": sorted({node["owner"] for node in nodes if node["owner"]}),
                "relation_counts": dict(sorted(relation_counts.items())),
                "nodes": nodes,
                "edges": edges,
                "identity_conflicts": pair_conflicts,
                "recommendation": (
                    "不要整簇确认SAME_PROJECT；请按边或明确子集审核"
                    if risky else "关系图未见身份冲突；仍需人工明确选择确认子集"
                ),
            })
        return sorted(
            result,
            key=lambda cluster: (-max(edge["evidence_score"] for edge in cluster["edges"]), cluster["cluster_id"]),
        )

    def get_cluster(self, cluster_id: int) -> dict:
        for cluster in self.clusters():
            if cluster["cluster_id"] == int(cluster_id):
                return cluster
        raise ValueError(f"活动候选簇不存在：{cluster_id}")

    def review_cluster(
        self, cluster_id: int, *, confirm_notices: list[int] | None = None,
        confirm_parent_candidates: list[int] | None = None,
        reject_candidates: list[int] | None = None, note: str = "",
    ) -> dict:
        cluster = self.get_cluster(cluster_id)
        actions = [bool(confirm_notices), bool(confirm_parent_candidates), bool(reject_candidates)]
        if sum(actions) != 1:
            raise ValueError("每次簇审核必须且只能选择一种操作")
        member_ids = set(cluster["notice_ids"])
        edge_by_id = {edge["candidate_id"]: edge for edge in cluster["edges"]}

        if confirm_notices:
            selected = sorted(set(int(value) for value in confirm_notices))
            if len(selected) < 2 or not set(selected) <= member_ids:
                raise ValueError("CONFIRM_SUBSET至少选择簇内两条公告")
            features = self._features_for_ids(selected)
            for a_id, b_id in combinations(selected, 2):
                conflicts = self._hard_conflicts(features[a_id], features[b_id], same_project=True)
                if conflicts:
                    raise ValueError(f"公告 {a_id} 与 {b_id} 存在身份冲突：{'；'.join(conflicts)}")
                direct = next((edge for edge in cluster["edges"] if {
                    edge["notice_id_a"], edge["notice_id_b"]
                } == {a_id, b_id}), None)
                if direct and direct["candidate_relation"] == "SAME_PARENT_PROJECT":
                    raise ValueError(f"公告 {a_id} 与 {b_id} 仅支持SAME_PARENT_PROJECT，不能确认同一工程")
            entity_id = ProjectEntityService(self.db).confirm_notices(
                selected, canonical_name=cluster["suggested_core_name"]
            )
            self._mark_cluster_edges(
                [edge["candidate_id"] for edge in cluster["edges"]
                 if edge["candidate_relation"] == "SAME_PROJECT"
                 and edge["notice_id_a"] in selected and edge["notice_id_b"] in selected],
                "CONFIRMED", note,
            )
            return {
                "cluster_id": cluster_id, "action": "CONFIRM_SUBSET",
                "confirmed_notice_ids": selected, "engineering_project_id": entity_id,
            }

        candidate_ids = sorted(set(int(value) for value in (confirm_parent_candidates or reject_candidates or [])))
        if not candidate_ids or not set(candidate_ids) <= set(edge_by_id):
            raise ValueError("必须明确选择当前簇内的候选边")
        if confirm_parent_candidates:
            invalid = [value for value in candidate_ids if edge_by_id[value]["candidate_relation"] != "SAME_PARENT_PROJECT"]
            if invalid:
                raise ValueError(f"以下候选不是SAME_PARENT_PROJECT：{invalid}")
            self._mark_cluster_edges(candidate_ids, "CONFIRMED", note)
            return {
                "cluster_id": cluster_id, "action": "CONFIRM_PARENT_GROUP",
                "confirmed_candidate_ids": candidate_ids, "engineering_project_id": None,
            }
        for candidate_id in candidate_ids:
            self.review(candidate_id, "REJECT", note)
        return {"cluster_id": cluster_id, "action": "REJECT_EDGES", "rejected_candidate_ids": candidate_ids}

    def review(self, candidate_id: int, action: str, note: str = "") -> dict:
        action = action.upper()
        status_by_action = {"CONFIRM": "CONFIRMED", "REJECT": "REJECTED", "DEFER": "DEFERRED"}
        if action not in status_by_action:
            raise ValueError(f"不支持的审核动作：{action}")
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            candidate = con.execute(
                "SELECT * FROM project_link_candidate WHERE id=?", (candidate_id,)
            ).fetchone()
        if not candidate:
            raise ValueError(f"候选不存在：{candidate_id}")

        entity_id = None
        if action == "CONFIRM" and candidate["candidate_relation"] != "SAME_PARENT_PROJECT":
            evidence = json.loads(candidate["evidence_json"])
            canonical_name = evidence.get("common_project_name") or min(
                (evidence["core_name_a"], evidence["core_name_b"]), key=lambda value: (len(value), value)
            )
            entity_id = ProjectEntityService(self.db).confirm_notices(
                [int(candidate["notice_id_a"]), int(candidate["notice_id_b"])],
                canonical_name=canonical_name,
            )

        reviewed_at = business_now_naive().isoformat(timespec="seconds")
        with self.db.connect() as con:
            con.execute(
                """UPDATE project_link_candidate SET candidate_status=?,reviewed_at=?,
                review_note=?,reviewed_evidence_hash=evidence_hash,updated_at=? WHERE id=?""",
                (status_by_action[action], reviewed_at, note.strip(), reviewed_at, candidate_id),
            )
        return {
            "candidate_id": candidate_id,
            "candidate_status": status_by_action[action],
            "engineering_project_id": entity_id,
        }

    def _features_for_ids(self, notice_ids: list[int]) -> dict[int, NoticeFeature]:
        service = NoticeIdentityService(self.db)
        service.extract_all()
        identities = service.current_identities(notice_ids)
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            placeholders = ",".join("?" for _ in notice_ids)
            rows = con.execute(f"SELECT * FROM project WHERE id IN ({placeholders})", notice_ids).fetchall()
        return {
            int(row["id"]): self._feature(row, identities.get(int(row["id"]), NoticeIdentity(int(row["id"]))))
            for row in rows
        }

    def _mark_cluster_edges(self, candidate_ids: list[int], status: str, note: str) -> None:
        if not candidate_ids:
            return
        reviewed_at = business_now_naive().isoformat(timespec="seconds")
        placeholders = ",".join("?" for _ in candidate_ids)
        with self.db.connect() as con:
            con.execute(
                f"""UPDATE project_link_candidate SET candidate_status=?,reviewed_at=?,review_note=?,
                reviewed_evidence_hash=evidence_hash,updated_at=? WHERE id IN ({placeholders})""",
                (status, reviewed_at, note.strip(), reviewed_at, *candidate_ids),
            )

    @staticmethod
    def _cluster_node(feature: NoticeFeature) -> dict:
        return {
            "notice_id": feature.id, "title": feature.title, "source_site": feature.source_site,
            "publish_date": feature.publish_date, "region_value": feature.region,
            "region_source": feature.region_source, "region_confidence": feature.region_confidence,
            "owner": feature.owner, "owner_quality": feature.owner_quality,
            "core_name": feature.core, "lot": feature.lots, "phase": feature.phases,
            "year": feature.years, "station": feature.stations, "range": feature.ranges,
            "procurement_object": feature.procurement_tail,
            "subproject_scope": feature.subproject_scope,
            "result_party": feature.result_party,
        }

    @staticmethod
    def _cluster_edge(row: sqlite3.Row) -> dict:
        evidence = json.loads(row["evidence_json"])
        return {
            "candidate_id": int(row["id"]), "notice_id_a": int(row["notice_id_a"]),
            "notice_id_b": int(row["notice_id_b"]), "candidate_relation": row["candidate_relation"],
            "evidence_score": int(row["evidence_score"]), "evidence_level": row["evidence_level"],
            "candidate_status": row["candidate_status"],
            "positive_evidence": [item for item in evidence.get("components", []) if item.get("score", 0) > 0],
            "negative_evidence": evidence.get("negative_evidence", []),
        }

    def statistics(self) -> dict:
        with self.db.connect() as con:
            rows = con.execute(
                """SELECT candidate_status,evidence_level,COUNT(*) FROM project_link_candidate
                WHERE is_active=1 GROUP BY candidate_status,evidence_level"""
            ).fetchall()
            statuses: dict[str, int] = defaultdict(int)
            levels: dict[str, int] = defaultdict(int)
            for status, level, count in rows:
                statuses[status] += int(count)
                if status == "PENDING":
                    levels[level] += int(count)
            pending_pairs = con.execute(
                "SELECT notice_id_a,notice_id_b FROM project_link_candidate WHERE candidate_status='PENDING' AND is_active=1"
            ).fetchall()
        return {
            "candidate_statuses": dict(sorted(statuses.items())),
            "pending_candidates": statuses.get("PENDING", 0),
            "pending_high": levels.get("HIGH", 0),
            "pending_medium": levels.get("MEDIUM", 0),
            "potential_project_clusters": self._cluster_count(pending_pairs),
        }

    def historical_duplicate_analysis(self) -> dict:
        """Audit the exact-name, currently-unlinked groups noted in step 3."""
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """SELECT p.* FROM project p LEFT JOIN project_notice_link l ON l.notice_id=p.id
                WHERE l.id IS NULL ORDER BY p.id"""
            ).fetchall()
            groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
            for row in rows:
                key = _identity(canonicalize_project_name(row["project_name"]))
                if key:
                    groups[key].append(row)
            duplicate_groups = [members for members in groups.values() if len(members) > 1]
            details = []
            counts = {"HIGH": 0, "MEDIUM": 0, "NOT_QUEUED": 0}
            for members in duplicate_groups:
                ids = sorted(int(row["id"]) for row in members)
                placeholders = ",".join("?" for _ in ids)
                candidate_rows = con.execute(
                    f"""SELECT evidence_level,evidence_score,candidate_status FROM project_link_candidate
                    WHERE notice_id_a IN ({placeholders}) AND notice_id_b IN ({placeholders}) AND is_active=1""",
                    (*ids, *ids),
                ).fetchall()
                if candidate_rows:
                    level = "HIGH" if any(row["evidence_level"] == "HIGH" for row in candidate_rows) else "MEDIUM"
                    max_score = max(int(row["evidence_score"]) for row in candidate_rows)
                else:
                    level, max_score = "NOT_QUEUED", 0
                counts[level] += 1
                details.append({
                    "notice_ids": ids,
                    "title": members[0]["project_name"],
                    "classification": level,
                    "max_score": max_score,
                    "reason": (
                        "存在达到人工审核门槛的可解释候选"
                        if candidate_rows else "地区、业主、时间或名称冲突使其未达到门槛"
                    ),
                })
        return {"total_groups": len(duplicate_groups), "counts": counts, "groups": details}

    def _eligible_features(
        self, con: sqlite3.Connection, identities: dict[int, NoticeIdentity],
    ) -> list[NoticeFeature]:
        rows = con.execute(
            """WITH entity_sizes AS (
              SELECT engineering_project_id,COUNT(*) notice_count
              FROM project_notice_link GROUP BY engineering_project_id
            )
            SELECT p.*,l.engineering_project_id,COALESCE(s.notice_count,0) entity_notice_count
            FROM project p
            LEFT JOIN project_notice_link l ON l.notice_id=p.id
            LEFT JOIN entity_sizes s ON s.engineering_project_id=l.engineering_project_id
            WHERE l.id IS NULL OR s.notice_count=1
            ORDER BY p.id"""
        ).fetchall()
        return [
            self._feature(row, identities.get(int(row["id"]), NoticeIdentity(int(row["id"]))))
            for row in rows
        ]

    @staticmethod
    def _feature(row: sqlite3.Row, identity: NoticeIdentity) -> NoticeFeature:
        core = identity.core_name
        parent = identity.parent_core_name or core
        identifiers = {
            f"{item.namespace}:{item.identifier_type}:{item.value}"
            for item in identity.identifiers if item.strength == "STRONG"
        }
        return NoticeFeature(
            id=int(row["id"]), title=row["project_name"], region=identity.region_value,
            region_bucket=identity.region_value,
            region_source=identity.region_source, region_confidence=identity.region_confidence,
            owner=identity.owner, owner_quality=identity.owner_quality,
            publish_date=row["publish_date"] or "", source_site=row["source_site"] or "",
            core=core, core_key=identity_text(core), parent_core=parent, parent_key=identity_text(parent),
            lots=identity.lots, phases=identity.phases, years=identity.years,
            stations=identity.stations, ranges=identity.ranges,
            identifiers=tuple(sorted(identifiers)),
            procurement_tail=identity.procurement_object,
            subproject_scope=identity.subproject_scope,
            url=row["url"] or "",
            result_party=identity.result_party,
        )

    def _blocked_pairs(self, features: list[NoticeFeature]) -> set[tuple[int, int]]:
        blocks: dict[str, set[int]] = defaultdict(set)
        for feature in features:
            region = feature.region_bucket
            if not region:
                continue
            blocks[f"exact|{region}|{feature.core_key}"].add(feature.id)
            blocks[f"parent|{region}|{feature.parent_key}"].add(feature.id)
            for pattern in _ANCHOR_PATTERNS:
                for anchor in pattern.findall(feature.core):
                    blocks[f"anchor|{region}|{_identity(anchor)}"].add(feature.id)
            for gram in _ngrams(feature.parent_core):
                blocks[f"gram|{region}|{gram}"].add(feature.id)
        pairs: set[tuple[int, int]] = set()
        for members in blocks.values():
            if not 2 <= len(members) <= 25:
                continue
            for a, b in combinations(sorted(members), 2):
                pairs.add((a, b))
        return pairs

    def _evaluate_pair(self, a: NoticeFeature, b: NoticeFeature) -> dict | None:
        components: list[dict] = []
        conflicts: list[str] = []
        relation = "SAME_PROJECT"

        region_components = self._region_components(a, b)
        if region_components is None:
            return None
        components.extend(region_components)

        for label, values_a, values_b in (
            ("期次", a.phases, b.phases), ("年度", a.years, b.years),
            ("站点", a.stations, b.stations), ("起止范围", a.ranges, b.ranges),
        ):
            if values_a and values_b and set(values_a) != set(values_b):
                return None

        owner_a, owner_b = _identity(a.owner), _identity(b.owner)
        if owner_a and owner_b and owner_a != owner_b:
            return None
        if owner_a and owner_b:
            if {a.owner_quality, b.owner_quality} <= {
                "STRUCTURED_HIGH", "HUMAN_CONFIRMED", "HUMAN_OVERRIDE",
            }:
                components.append(self._component("owner_match", 25, "结构化建设单位完全一致"))
            else:
                components.append(self._component(
                    "owner_match_gated", 15,
                    "提取型建设单位或结构化采购组织一致，按中等可信证据计分",
                ))
        else:
            components.append(self._component("owner_unusable", 0, "至少一条公告建设单位缺失或污染，不加分"))

        name_evidence = self._name_evidence(a, b)
        if name_evidence is None:
            return None
        relation, common_name, ratio, jaccard, name_component = name_evidence
        components.append(name_component)

        if a.subproject_scope and b.subproject_scope and _identity(a.subproject_scope) != _identity(b.subproject_scope):
            relation = "SAME_PARENT_PROJECT"
            components.append(self._component(
                "subproject_scope_difference", 0,
                f"明确功能分区不同（{a.subproject_scope} / {b.subproject_scope}），仅支持同一上级工程",
            ))
            conflicts.append("子工程/功能分区不同")

        if (
            a.source_site == b.source_site and a.url and b.url and a.url != b.url
            and a.result_party and b.result_party
            and _identity(a.result_party) != _identity(b.result_party)
            and a.core_key == b.core_key
        ):
            relation = "SAME_PARENT_PROJECT"
            components.append(self._component(
                "separate_result_pages", 0,
                "同名结果公告来自不同详情页且中标人不同，疑似分标段结果",
            ))
            conflicts.append("同名结果页的中标人不同")

        if a.lots and b.lots:
            if set(a.lots) == set(b.lots):
                components.append(self._component("lot_match", 10, "标段一致"))
            elif relation == "SAME_PARENT_PROJECT":
                components.append(self._component("lot_difference", -10, "标段不同，仅推荐为同一上级工程"))
                conflicts.append("标段不同")
            else:
                return None
        elif not a.lots and not b.lots:
            components.append(self._component("lot_not_conflicting", 5, "均未识别出标段冲突"))
        else:
            relation = "SAME_PARENT_PROJECT"
            components.append(self._component("lot_partial", 0, "仅一条公告包含标段，按上级工程候选处理"))

        ids_a, ids_b = set(a.identifiers), set(b.identifiers)
        if ids_a and ids_b:
            if ids_a & ids_b:
                components.append(self._component("identifier_hint", 20, "存在共同正式项目标识"))
            elif relation == "SAME_PARENT_PROJECT":
                components.append(self._component("identifier_conflict", -10, "正式编号不同，仅支持上级工程关系"))
                conflicts.append("正式项目编号不同")
            else:
                return None
        else:
            components.append(self._component("identifier_hint", 0, "无共同正式项目编号"))

        temporal_component, temporal_conflict = self._temporal_evidence(a, b)
        if temporal_component:
            components.append(temporal_component)
        if temporal_conflict:
            conflicts.append(temporal_conflict)

        if a.source_site != b.source_site:
            components.append(self._component("cross_source", 5, "来自不同来源，可用于交叉核验"))
        components.append(self._component("negative_conflict", 10, "未发现期次、年度、站点、范围或业主硬冲突"))

        score = max(0, min(100, sum(int(item["score"]) for item in components)))
        return {
            "algorithm_version": ALGORITHM_VERSION,
            "candidate_relation": relation,
            "evidence_score": score,
            "evidence_level": "HIGH" if score >= HIGH_SCORE else "MEDIUM",
            "core_name_a": a.core,
            "core_name_b": b.core,
            "common_project_name": common_name,
            "components": components,
            "negative_evidence": conflicts,
            "signals": {
                "lots_a": a.lots, "lots_b": b.lots,
                "phases_a": a.phases, "phases_b": b.phases,
                "years_a": a.years, "years_b": b.years,
                "stations_a": a.stations, "stations_b": b.stations,
                "ranges_a": a.ranges, "ranges_b": b.ranges,
                "procurement_object_a": a.procurement_tail,
                "procurement_object_b": b.procurement_tail,
                "subproject_scope_a": a.subproject_scope,
                "subproject_scope_b": b.subproject_scope,
                "result_party_a": a.result_party,
                "result_party_b": b.result_party,
                "region_source_a": a.region_source,
                "region_source_b": b.region_source,
                "region_confidence_a": a.region_confidence,
                "region_confidence_b": b.region_confidence,
                "owner_quality_a": a.owner_quality,
                "owner_quality_b": b.owner_quality,
                "name_ratio": round(ratio, 4), "name_ngram_overlap": round(jaccard, 4),
            },
        }

    def _region_components(self, a: NoticeFeature, b: NoticeFeature) -> list[dict] | None:
        if not a.region_bucket or a.region_bucket != b.region_bucket:
            return None
        qualities = {a.region_confidence, b.region_confidence}
        if qualities <= {"HIGH"}:
            result = [self._component("region_match", 10, f"高可信地区同属{a.region_bucket}")]
            if _identity(a.region) == _identity(b.region):
                result.append(self._component("region_exact", 5, "高可信地区文本完全一致"))
            return result
        if "LOW" not in qualities and "UNKNOWN" not in qualities:
            return [self._component("region_match_gated", 5, f"中等可信地区同属{a.region_bucket}")]
        return [self._component("region_low_confidence", 0, "至少一条地区来自默认值或未限定文本推断，不加分")]

    def _name_evidence(
        self, a: NoticeFeature, b: NoticeFeature,
    ) -> tuple[str, str, float, float, dict] | None:
        ratio = SequenceMatcher(None, a.core_key, b.core_key).ratio()
        grams_a, grams_b = _ngrams(a.core, 3), _ngrams(b.core, 3)
        union = grams_a | grams_b
        jaccard = len(grams_a & grams_b) / len(union) if union else 0.0
        relation = "SAME_PROJECT"
        common_name = min((a.core, b.core), key=lambda value: (len(value), value))
        if a.core_key == b.core_key:
            common_name = a.core
            component = self._component("name_core_match", 45, "核心名称完全一致")
        elif a.parent_key == b.parent_key and a.parent_key:
            relation, common_name = "SAME_PARENT_PROJECT", a.parent_core
            component = self._component("name_parent_match", 42, "去除标段后工程主体一致")
        elif (
            min(len(a.core_key), len(b.core_key)) >= 8
            and (a.core_key.startswith(b.core_key) or b.core_key.startswith(a.core_key))
        ):
            relation = "SAME_PARENT_PROJECT"
            component = self._component("name_parent_containment", 38, "一个名称是另一子工程名称的主体前缀")
        elif ratio >= 0.88 and jaccard >= 0.65:
            component = self._component(
                "name_core_similarity", 35,
                f"名称结构高度相似 ratio={ratio:.2f}, overlap={jaccard:.2f}",
            )
        elif ratio >= 0.80 and jaccard >= 0.55:
            relation = "UNCERTAIN"
            component = self._component(
                "name_core_similarity", 30,
                f"名称结构相似 ratio={ratio:.2f}, overlap={jaccard:.2f}",
            )
        else:
            return None
        return relation, common_name, ratio, jaccard, component

    def _temporal_evidence(self, a: NoticeFeature, b: NoticeFeature) -> tuple[dict | None, str]:
        date_a, date_b = _parse_date(a.publish_date), _parse_date(b.publish_date)
        if not date_a or not date_b:
            return None, ""
        days = abs((date_a - date_b).days)
        if days <= 730:
            return self._component("temporal_consistency", 5, f"发布日期相差{days}天"), ""
        if days > 1460:
            return self._component("temporal_conflict", -10, f"发布日期相差{days}天"), "发布时间跨度超过四年"
        return None, ""

    @staticmethod
    def _hard_conflicts(a: NoticeFeature, b: NoticeFeature, *, same_project: bool) -> list[str]:
        conflicts = []
        if a.region_bucket and b.region_bucket and a.region_bucket != b.region_bucket:
            conflicts.append("地区冲突")
        for label, values_a, values_b in (
            ("期次", a.phases, b.phases), ("年度", a.years, b.years),
            ("站点", a.stations, b.stations), ("起止范围", a.ranges, b.ranges),
        ):
            if values_a and values_b and set(values_a) != set(values_b):
                conflicts.append(f"{label}冲突")
        if a.owner and b.owner and _identity(a.owner) != _identity(b.owner):
            conflicts.append("建设单位冲突")
        if same_project and a.lots and b.lots and set(a.lots) != set(b.lots):
            conflicts.append("标段冲突")
        if same_project and a.identifiers and b.identifiers and not (set(a.identifiers) & set(b.identifiers)):
            conflicts.append("正式项目编号冲突")
        if (
            same_project and a.subproject_scope and b.subproject_scope
            and _identity(a.subproject_scope) != _identity(b.subproject_scope)
        ):
            conflicts.append("子工程/功能分区冲突")
        if (
            same_project and a.source_site == b.source_site and a.url and b.url and a.url != b.url
            and a.result_party and b.result_party
            and _identity(a.result_party) != _identity(b.result_party)
            and a.core_key == b.core_key
        ):
            conflicts.append("同名结果页的中标人/详情页冲突")
        return conflicts

    @staticmethod
    def _component(key: str, score: int, detail: str) -> dict:
        return {"key": key, "score": score, "detail": detail}

    @staticmethod
    def _components(pairs) -> list[list[int]]:
        parent: dict[int, int] = {}

        def find(value: int) -> int:
            parent.setdefault(value, value)
            if parent[value] != value:
                parent[value] = find(parent[value])
            return parent[value]

        for a, b in pairs:
            root_a, root_b = find(int(a)), find(int(b))
            if root_a != root_b:
                parent[root_b] = root_a
        grouped: dict[int, list[int]] = defaultdict(list)
        for value in parent:
            grouped[find(value)].append(value)
        return sorted((sorted(values) for values in grouped.values()), key=lambda values: values[0])

    @staticmethod
    def _cluster_count(pairs) -> int:
        return len(ProjectCandidateService._components(pairs))
