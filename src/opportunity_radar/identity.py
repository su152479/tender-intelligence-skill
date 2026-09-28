"""Persistent, versioned notice identity facts and current identity summaries."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import re
import sqlite3
import unicodedata

from .business_time import business_now_naive
from .db import Database
from .project_entities import canonicalize_project_name, normalize_identifier


EXTRACTOR_VERSION = "identity-v1.2"

CONFIDENCE_RANK = {"UNKNOWN": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
SOURCE_RANK = {
    "HUMAN": 100,
    "ATTACHMENT": 90,
    "API_STRUCTURED": 85,
    "PAGE_STRUCTURED": 80,
    "BODY": 65,
    "TITLE": 60,
    "SOURCE_SECTION": 50,
    "COLLECTOR_DEFAULT": 10,
    "LEGACY_INFERENCE": 5,
}

_SAFE_PROCUREMENT_SPLIT = re.compile(r"采购|租赁|劳务分包|专业分包")
_PHASE = re.compile(r"(?:第)?([一二三四五六七八九十百零〇\d]+)期")
_YEAR = re.compile(r"(20\d{2})年(?:度)?")
_STATION = re.compile(r"([\u4e00-\u9fffA-Za-z0-9]{1,8}站)(?!房|台)")
_RAIL_CONTEXT = re.compile(r"地铁|轨道交通|铁路|[A-Za-z]\d+\s*线|号线|线路|车站|站厅|站台|区间|换乘")
_NON_RAIL_STATION = re.compile(
    r"(?:变电站|泵站|供水站|收费站|监测站|污水处理站|水文站|工作站|中继站|基站|消防站)$"
)
_CHAINAGE_RANGE = re.compile(r"K?\d+\+\d+\s*(?:-|~|至|到)\s*K?\d+\+\d+", re.IGNORECASE)
_PLACE_RANGE = re.compile(r"[（(]([^（）()]{2,30}(?:-|~|至|到)[^（）()]{2,30})[）)]")
_RESULT_PARTY = re.compile(
    r"(?:中标人|中标单位)(?:名称)?\s*[：:]\s*([^\r\n,，。；;]{2,100})"
)
_INVALID_RESULT_PARTY = re.compile(r"^(?:如下|信息如下|情况如下)\s*[：:]?$")
_BODY_IDENTIFIER = re.compile(
    r"(交易项目编号|工程编号|项目代码|项目编号|招标编号|采购编号)\s*[：:]\s*"
    r"([A-Za-z0-9][A-Za-z0-9_./\-]{4,60})"
)
_TRANSACTION_S = re.compile(r"^S\d{6,}[A-Z0-9_-]*$")
_HEBEI_TRANSACTION = re.compile(r"^[GI]\d{12,}[A-Z0-9_-]*$")
_ENGINEERING_CODE = re.compile(r"^20\d{2}-[A-Z0-9]{3,24}$")
_BAD_OWNER_MARKERS = (
    "或招标代理机构提出", "招标代理机构", "联系人", "联系电话", "电话", "电子邮箱", "监督部门",
)
_OWNER_LABELS = ("建设单位", "项目业主", "招标人", "采购人")
_ORG_SUFFIX = re.compile(r"(?:有限责任公司|股份有限公司|有限公司|集团|中心|委员会|交通局|水务局|农业农村局|医院|学校|政府)$")

_STRUCTURED_REGION_SOURCES = {
    "中交集团供应链管理信息系统",
    "中建云筑网",
    "中国政府采购网",
}
_TITLE_REGION_MARKERS = {
    "北京市": (
        "北京市", "东城区", "西城区", "朝阳区", "海淀区", "丰台区", "石景山区", "通州区", "顺义区",
        "昌平区", "大兴区", "房山区", "门头沟区", "怀柔区", "平谷区", "密云区", "延庆区", "亦庄", "孙河",
    ),
    "天津市": ("天津市", "滨海新区", "武清区", "宝坻区", "蓟州区", "静海区", "宁河区"),
    "河北省": (
        "河北省", "石家庄", "唐山", "秦皇岛", "邯郸", "邢台", "保定", "张家口", "承德", "沧州", "廊坊", "衡水",
        "定州", "辛集", "雄安", "涿州", "武安", "顺平", "涞源", "莲池区", "藁城区",
    ),
}

_SUBPROJECT_SCOPE_HINT = re.compile(
    r"酒店|商业|山姆|住宅|写字楼|办公楼|学校|医院|地库|地下室|"
    r"裙房|塔楼|厂房|宿舍|站房|车站|楼栋|地块|功能区"
)
_SUBPROJECT_SCOPE_BOUNDARY = re.compile(
    r"装配式|预制|混凝土|构件|管片|箱梁|材料|设备|工程|劳务|专业|"
    r"租赁|采购|供应|制作|安装"
)

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_LOT_TOKEN = r"(?:[A-Za-z]{1,5}-?\d{1,3}|[一二三四五六七八九十百零〇]{1,5}|\d{1,3})"
_MULTI_LOT_LIST = re.compile(rf"((?:{_LOT_TOKEN}、)+{_LOT_TOKEN})标段", re.IGNORECASE)
_MULTI_LOT_RANGE = re.compile(rf"第?({_LOT_TOKEN})\s*[-~至]\s*({_LOT_TOKEN})标段", re.IGNORECASE)
_LOT_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9-])(\d{1,3})#\s*(?:标段|合同段)", re.IGNORECASE),
    re.compile(rf"第({_LOT_TOKEN})标段", re.IGNORECASE),
    re.compile(rf"标段({_LOT_TOKEN})", re.IGNORECASE),
    re.compile(rf"({_LOT_TOKEN})标段", re.IGNORECASE),
    re.compile(rf"({_LOT_TOKEN})合同段", re.IGNORECASE),
    re.compile(rf"施工({_LOT_TOKEN})标(?!段)", re.IGNORECASE),
    # Only prefixes verified in the notice corpus are accepted without an
    # adjacent 标/标段 marker; a broad A-01 rule misclassified building numbers.
    re.compile(r"(?<![A-Za-z0-9])((?:SG|TJ)-?\d{1,3})(?![A-Za-z0-9])", re.IGNORECASE),
    re.compile(r"(?<=[\u4e00-\u9fff])([一二三四五六七八九十百零〇\d]{1,3})标(?=项目|工程|招标|采购|桥梁)", re.IGNORECASE),
)


@dataclass(frozen=True)
class IdentityFact:
    fact_type: str
    normalized_value: str
    raw_value: str
    source_type: str
    source_reference: str
    source_location: str
    confidence: str
    quality: str = ""
    identifier_type: str = ""
    identifier_namespace: str = ""
    identity_strength: str = ""


@dataclass(frozen=True)
class IdentityIdentifier:
    identifier_type: str
    namespace: str
    value: str
    strength: str
    source_reference: str


@dataclass(frozen=True)
class NoticeIdentity:
    notice_id: int
    region_value: str = ""
    region_source: str = "UNKNOWN"
    region_confidence: str = "UNKNOWN"
    owner: str = ""
    owner_quality: str = "MISSING"
    core_name: str = ""
    parent_core_name: str = ""
    procurement_object: str = ""
    lots: tuple[str, ...] = ()
    lot_kind: str = "NONE"
    phases: tuple[str, ...] = ()
    years: tuple[str, ...] = ()
    stations: tuple[str, ...] = ()
    ranges: tuple[str, ...] = ()
    subproject_scope: str = ""
    result_party: str = ""
    result_party_source_reference: str = ""
    identifiers: tuple[IdentityIdentifier, ...] = ()


def identity_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"\s+", "", value)
    return re.sub(r"[，,。.;；:：'\"“”‘’()（）\[\]【】_-]", "", value).casefold()


def clean_owner(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"\s+", " ", value).strip(" ，,。.;；")
    if not 2 <= len(value) <= 100 or any(marker in value for marker in _BAD_OWNER_MARKERS):
        return ""
    return value


def normalize_region(value: str) -> str:
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or ""))
    matches = [province for province, markers in _TITLE_REGION_MARKERS.items() if any(marker in compact for marker in markers)]
    return matches[0] if len(matches) == 1 else ""


def _title_region(title: str) -> tuple[str, str]:
    value = unicodedata.normalize("NFKC", title or "")
    value = re.sub(r"^.{0,45}?(?:集团股份有限公司|集团有限公司|有限责任公司|有限公司)", "", value)
    hits = [(province, marker) for province, markers in _TITLE_REGION_MARKERS.items() for marker in markers if marker in value]
    provinces = {province for province, _ in hits}
    if len(provinces) != 1:
        return "", ""
    province = next(iter(provinces))
    marker = max((marker for candidate, marker in hits if candidate == province), key=len)
    return province, marker


def _split_project_identity(value: str) -> tuple[str, str]:
    positions = [match.end() for match in re.finditer("项目", value)]
    for end in reversed(positions):
        prefix, tail = value[:end], value[end:].strip(" ，,。.;；-—")
        if len(identity_text(prefix)) >= 6 and tail and _SAFE_PROCUREMENT_SPLIT.search(tail):
            return prefix, tail
    return value, ""


def _subproject_scope(procurement_tail: str) -> str:
    value = unicodedata.normalize("NFKC", procurement_tail or "").strip(" ，,。.;；-—")
    boundary = _SUBPROJECT_SCOPE_BOUNDARY.search(value)
    if not boundary:
        return ""
    prefix = value[:boundary.start()].strip(" ，,。.;；-—")
    return prefix if prefix and len(prefix) <= 20 and _SUBPROJECT_SCOPE_HINT.search(prefix) else ""


def _cn_number(value: str) -> int | None:
    if not value:
        return None
    if value.isdigit():
        return int(value)
    if all(char in _CN_DIGITS for char in value):
        result = 0
        for char in value:
            result = result * 10 + _CN_DIGITS[char]
        return result
    if "百" in value:
        left, _, right = value.partition("百")
        return (_CN_DIGITS.get(left, 1) * 100) + (_cn_number(right) or 0)
    if "十" in value:
        left, _, right = value.partition("十")
        return (_CN_DIGITS.get(left, 1) * 10) + (_cn_number(right) or 0)
    return None


def normalize_lot(value: str) -> str:
    compact = normalize_identifier(value)
    number = _cn_number(compact)
    if number is not None:
        return str(number)
    match = re.fullmatch(r"([A-Z]{1,5})-?(\d{1,3})", compact)
    if match:
        prefix, digits = match.groups()
        return f"{prefix}-{int(digits)}" if "-" in compact else f"{prefix}{int(digits)}"
    return compact


def extract_lots(value: str) -> list[tuple[str, str, str]]:
    """Return normalized value, raw expression and SINGLE/MULTI quality."""
    text = unicodedata.normalize("NFKC", value or "")
    result: list[tuple[str, str, str]] = []
    occupied: list[tuple[int, int]] = []
    for match in _MULTI_LOT_LIST.finditer(text):
        raw = match.group(0)
        for token in match.group(1).split("、"):
            result.append((normalize_lot(token), raw, "MULTI_LOT_MEMBER"))
        occupied.append(match.span())
    for match in _MULTI_LOT_RANGE.finditer(text):
        start, end = _cn_number(match.group(1)), _cn_number(match.group(2))
        if start is not None and end is not None and 0 <= end - start <= 20:
            for number in range(start, end + 1):
                result.append((str(number), match.group(0), "MULTI_LOT_RANGE_MEMBER"))
            occupied.append(match.span())
    for pattern in _LOT_PATTERNS:
        for match in pattern.finditer(text):
            if any(start <= match.start() and match.end() <= end for start, end in occupied):
                continue
            value = normalize_lot(match.group(1))
            if value:
                result.append((value, match.group(0), "SINGLE_LOT"))
    unique = {}
    for normalized, raw, quality in result:
        unique[(normalized, raw, quality)] = (normalized, raw, quality)
    return list(unique.values())


def _namespace(source_site: str, region: str = "") -> str:
    if source_site in {"北京市公共资源交易服务平台"}:
        return "BEIJING_GGZY"
    if source_site == "京津冀公共资源交易协同专区":
        if region == "北京市":
            return "BEIJING_GGZY"
        if region == "天津市":
            return "TIANJIN_GGZY"
        if region == "河北省":
            return "HEBEI_GGZY"
        return "JJJ_GGZY"
    return {
        "京冀公共资源交易跨区域信息专区（河北）": "HEBEI_GGZY",
        "天津市交通运输委员会招标公告": "TIANJIN_GGZY",
        "中交集团供应链管理信息系统": "CCCC",
        "中建云筑网": "YUNZHU",
        "中铁鲁班网": "CRECG",
        "中国政府采购网": "CCGP",
    }.get(source_site, "UNKNOWN")


class NoticeIdentityService:
    def __init__(self, db: Database, extractor_version: str = EXTRACTOR_VERSION):
        self.db = db
        self.extractor_version = extractor_version

    def extract_all(self) -> dict:
        self.db.init()
        now = business_now_naive().isoformat(timespec="seconds")
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT * FROM project ORDER BY id").fetchall()
            for row in rows:
                self._persist(con, int(row["id"]), self._extract_row(con, row), now)
        return self.statistics()

    def extract_notice(self, notice_id: int) -> int:
        self.db.init()
        now = business_now_naive().isoformat(timespec="seconds")
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT * FROM project WHERE id=?", (notice_id,)).fetchone()
            if not row:
                raise ValueError(f"公告不存在：{notice_id}")
            facts = self._extract_row(con, row)
            self._persist(con, notice_id, facts, now)
            return len(facts)

    def _persist(self, con: sqlite3.Connection, notice_id: int, facts: list[IdentityFact], now: str) -> None:
        con.execute(
            """UPDATE notice_identity_fact SET is_active=0,updated_at=?
            WHERE notice_id=? AND source_type<>'HUMAN'
              AND id NOT IN (
                SELECT target_fact_id FROM notice_identity_review
                WHERE notice_id=? AND decision='CONFIRM' AND is_active=1 AND target_fact_id IS NOT NULL
              )""",
            (now, notice_id, notice_id),
        )
        sql = """INSERT INTO notice_identity_fact(
        notice_id,fact_type,normalized_value,raw_value,source_type,source_reference,source_location,
        confidence,quality,identifier_type,identifier_namespace,identity_strength,extractor_version,is_active,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)
        ON CONFLICT(
          notice_id,fact_type,normalized_value,source_type,source_reference,source_location,
          identifier_type,identifier_namespace,extractor_version
        ) DO UPDATE SET raw_value=excluded.raw_value,confidence=excluded.confidence,
        quality=excluded.quality,identity_strength=excluded.identity_strength,is_active=1,updated_at=excluded.updated_at"""
        for fact in facts:
            con.execute(sql, (
                notice_id, fact.fact_type, fact.normalized_value, fact.raw_value,
                fact.source_type, fact.source_reference, fact.source_location,
                fact.confidence, fact.quality, fact.identifier_type,
                fact.identifier_namespace, fact.identity_strength,
                self.extractor_version, now,
            ))

    def _extract_row(self, con: sqlite3.Connection, row: sqlite3.Row) -> list[IdentityFact]:
        notice_id = int(row["id"])
        ref = f"project:{notice_id}"
        title = row["project_name"] or ""
        body = row["raw_text"] or ""
        source = row["source_site"] or ""
        canonical = canonicalize_project_name(title)
        canonical = re.sub(r"^[A-Z]\d{10,}", "", canonical).strip(" ，,。.;；-—")
        core, tail = _split_project_identity(canonical)
        facts = [IdentityFact("CORE_NAME", identity_text(core), core, "TITLE", ref, "title", "MEDIUM", "TITLE_NORMALIZED")]
        if tail:
            facts.append(IdentityFact(
                "PROCUREMENT_OBJECT", identity_text(tail), tail, "TITLE", ref,
                "title:procurement-tail", "MEDIUM", "TITLE_TRAILING_OBJECT",
            ))

        self._region_facts(facts, row, ref)
        self._owner_facts(facts, row, ref)
        lot_matches = extract_lots(core)
        for normalized, raw, quality in lot_matches:
            facts.append(IdentityFact("LOT", normalized, raw, "TITLE", ref, "title", "HIGH", quality))
        parent_core = core
        for raw in sorted({item[1] for item in lot_matches}, key=len, reverse=True):
            parent_core = parent_core.replace(raw, "")
        parent_core = re.sub(r"\s+", " ", parent_core).strip(" ，,。.;；-—()（）")
        facts.append(IdentityFact(
            "PARENT_CORE_NAME", identity_text(parent_core), parent_core, "TITLE", ref,
            "title", "MEDIUM", "LOT_REMOVED_BY_IDENTITY_EXTRACTOR",
        ))
        for match in _PHASE.finditer(core):
            facts.append(IdentityFact("PHASE", normalize_lot(match.group(1)), match.group(0), "TITLE", ref, "title", "HIGH", "PROJECT_PHASE"))
        for match in _YEAR.finditer(core):
            facts.append(IdentityFact("YEAR", match.group(1), match.group(0), "TITLE", ref, "title", "HIGH", "PROJECT_NAME_YEAR"))
        station_context = f"{core}\n{body}"
        if _RAIL_CONTEXT.search(station_context):
            for match in _STATION.finditer(core):
                raw = re.split(r"项目|工程|区间|号线|线路|至|~|～", match.group(1))[-1]
                raw = re.sub(r"^(?:地铁|轨道交通|(?:高速|城际|市郊)?铁路)", "", raw)
                raw = re.sub(r"^(?:[A-Za-z]\d+|\d+号)?线", "", raw)
                if "线" in raw:
                    suffix = raw.rpartition("线")[2]
                    if 1 < len(suffix) <= 9 and suffix.endswith("站"):
                        raw = suffix
                if raw and raw != "站" and not _NON_RAIL_STATION.search(raw):
                    facts.append(IdentityFact(
                        "STATION", identity_text(raw), raw, "TITLE", ref, "title",
                        "MEDIUM", "RAIL_STATION_NAME",
                    ))
        for pattern in (_CHAINAGE_RANGE, _PLACE_RANGE):
            for match in pattern.finditer(core):
                raw = match.group(0) if pattern is _CHAINAGE_RANGE else match.group(1)
                facts.append(IdentityFact("RANGE", identity_text(raw), raw, "TITLE", ref, "title", "HIGH", "PROJECT_RANGE"))
        scope = _subproject_scope(tail)
        if scope:
            facts.append(IdentityFact("SUBPROJECT_SCOPE", identity_text(scope), scope, "TITLE", ref, "title", "HIGH", "FUNCTIONAL_SCOPE"))
        result = next(
            (match for match in _RESULT_PARTY.finditer(body)
             if not _INVALID_RESULT_PARTY.fullmatch(match.group(1).strip())),
            None,
        )
        if result:
            raw = result.group(1).strip()
            facts.append(IdentityFact("RESULT_PARTY", identity_text(raw), raw, "BODY", ref, "body:中标人", "HIGH", "LABELED_FIELD"))

        project_no = normalize_identifier(row["project_no"] or "")
        if project_no:
            facts.append(self._identifier_fact(project_no, row, ref, "project.project_no", "API_STRUCTURED"))
        for match in _BODY_IDENTIFIER.finditer(body):
            value = normalize_identifier(match.group(2))
            facts.append(self._identifier_fact(value, row, ref, f"body:{match.group(1)}", "BODY", label=match.group(1)))
        for document in con.execute(
            """SELECT d.id,di.identifier_type,di.identifier FROM document_text d
            JOIN document_identifier di ON di.document_id=d.id WHERE d.notice_id=?""", (notice_id,),
        ):
            value = normalize_identifier(document["identifier"])
            facts.append(self._identifier_fact(
                value, row, f"document:{document['id']}", f"identifier:{document['identifier_type']}",
                "ATTACHMENT", label=document["identifier_type"],
            ))
        unique = {}
        for fact in facts:
            if fact.normalized_value:
                key = (
                    fact.fact_type, fact.normalized_value, fact.source_type, fact.source_reference,
                    fact.source_location, fact.identifier_type, fact.identifier_namespace,
                )
                unique[key] = fact
        return list(unique.values())

    def _region_facts(self, facts: list[IdentityFact], row: sqlite3.Row, ref: str) -> None:
        source = row["source_site"] or ""
        raw_region = row["region"] or ""
        normalized = normalize_region(raw_region)
        body = row["raw_text"] or ""
        body_match = re.search(r"(?:建设地点|建设地址|建设地|所属地区)\s*[：:]\s*([^\n。；;]{2,100})", body)
        if body_match:
            raw = body_match.group(1).strip()
            value = normalize_region(raw)
            if value:
                facts.append(IdentityFact("REGION", value, raw, "PAGE_STRUCTURED", ref, "body:建设地点/所属地区", "HIGH", "EXPLICIT_PROJECT_REGION"))
        if normalized:
            if source in _STRUCTURED_REGION_SOURCES:
                source_type, confidence, quality = "API_STRUCTURED", "HIGH", "PLATFORM_REGION_FIELD"
            elif source == "北京市公共资源交易服务平台" and body_match:
                source_type, confidence, quality = "PAGE_STRUCTURED", "HIGH", "EXPLICIT_PROJECT_REGION"
            elif source == "京津冀公共资源交易协同专区":
                source_type, confidence, quality = "SOURCE_SECTION", "MEDIUM", "SOURCE_SECTION_REGION"
            elif source == "中铁鲁班网":
                source_type, confidence, quality = "LEGACY_INFERENCE", "LOW", "UNSCOPED_TEXT_INFERENCE"
            elif source in {
                "京冀公共资源交易跨区域信息专区（河北）",
                "天津市交通运输委员会招标公告",
            }:
                source_type, confidence, quality = "COLLECTOR_DEFAULT", "LOW", "COLLECTOR_DEFAULT"
            else:
                source_type, confidence, quality = "PAGE_STRUCTURED", "MEDIUM", "LEGACY_REGION_FIELD"
            facts.append(IdentityFact("REGION", normalized, raw_region, source_type, ref, "project.region", confidence, quality))
        title_region, marker = _title_region(row["project_name"] or "")
        if title_region:
            facts.append(IdentityFact("REGION", title_region, marker, "TITLE", ref, "title", "MEDIUM", "TITLE_PLACE_MARKER"))

    def _owner_facts(self, facts: list[IdentityFact], row: sqlite3.Row, ref: str) -> None:
        raw_owner = unicodedata.normalize("NFKC", row["owner"] or "").strip()
        source = row["source_site"] or ""
        if raw_owner:
            clean = clean_owner(raw_owner)
            if not clean:
                facts.append(IdentityFact("OWNER", identity_text(raw_owner), raw_owner, "BODY", ref, "project.owner", "LOW", "CONTAMINATED"))
            elif source == "中交集团供应链管理信息系统":
                facts.append(IdentityFact("OWNER", identity_text(clean), clean, "API_STRUCTURED", ref, "project.owner:opUnitName", "MEDIUM", "PROCUREMENT_ORG_MEDIUM"))
            elif source == "中国政府采购网":
                facts.append(IdentityFact("OWNER", identity_text(clean), clean, "API_STRUCTURED", ref, "project.owner:purchaser", "HIGH", "STRUCTURED_HIGH"))
            else:
                facts.append(IdentityFact("OWNER", identity_text(clean), clean, "BODY", ref, "project.owner", "MEDIUM", "EXTRACTED_MEDIUM"))
        lines = [line.strip(" ，,。.;；") for line in (row["raw_text"] or "").splitlines() if line.strip()]
        for index, line in enumerate(lines):
            for label in _OWNER_LABELS:
                match = re.fullmatch(rf"{label}(?:名称)?(?:为)?\s*[：:]?\s*(.*)", line)
                if not match:
                    continue
                raw = match.group(1).strip() or (lines[index + 1] if index + 1 < len(lines) else "")
                clean = clean_owner(raw)
                if clean and _ORG_SUFFIX.search(clean) and clean.count("公司") <= 1:
                    facts.append(IdentityFact("OWNER", identity_text(clean), clean, "BODY", ref, f"body:{label}", "MEDIUM", "EXTRACTED_MEDIUM"))

    def _identifier_fact(
        self, value: str, row: sqlite3.Row, reference: str, location: str,
        source_type: str, *, label: str = "",
    ) -> IdentityFact:
        source, region = row["source_site"] or "", normalize_region(row["region"] or "")
        namespace = _namespace(source, region)
        label = label or ""
        if _TRANSACTION_S.fullmatch(value) and source in {
            "北京市公共资源交易服务平台", "京津冀公共资源交易协同专区",
        }:
            kind, namespace, strength, confidence = "TRANSACTION_PROJECT_CODE", "BEIJING_GGZY", "STRONG", "HIGH"
        elif _HEBEI_TRANSACTION.fullmatch(value) and source == "京津冀公共资源交易协同专区":
            kind, namespace, strength, confidence = "TRANSACTION_PROJECT_CODE", "HEBEI_GGZY", "STRONG", "HIGH"
        elif label == "项目代码":
            kind, strength, confidence = "PROJECT_CODE", "STRONG", "HIGH"
        elif label in {"工程编号"} and _ENGINEERING_CODE.fullmatch(value):
            kind, strength, confidence = "ENGINEERING_CODE", "STRONG", "HIGH"
            owner = clean_owner(row["owner"] or "")
            namespace = f"OWNER:{identity_text(owner)}" if owner else namespace
        elif source == "中交集团供应链管理信息系统" and location == "project.project_no":
            kind, strength, confidence = "CCCC_SCHEME_CODE", "BUSINESS_ONLY", "HIGH"
        elif source == "中建云筑网" and location == "project.project_no":
            kind, strength, confidence = "YUNZHU_TENDER_CODE", "BUSINESS_ONLY", "HIGH"
        elif label in {"招标编号", "采购编号"}:
            kind, strength, confidence = "BUSINESS_NOTICE_CODE", "BUSINESS_ONLY", "MEDIUM"
        else:
            kind, strength, confidence = "UNCERTAIN_IDENTIFIER", "UNCERTAIN", "MEDIUM"
        return IdentityFact(
            "PROJECT_IDENTIFIER", value, value, source_type, reference, location,
            confidence, "SEMANTICALLY_CLASSIFIED", kind, namespace, strength,
        )

    def current_identities(self, notice_ids: list[int] | None = None) -> dict[int, NoticeIdentity]:
        self.db.init()
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            sql = """SELECT DISTINCT f.* FROM notice_identity_fact f
            LEFT JOIN notice_identity_review r
              ON r.target_fact_id=f.id AND r.is_active=1
            WHERE (f.is_active=1 OR r.id IS NOT NULL)"""
            params: tuple = ()
            if notice_ids is not None:
                if not notice_ids:
                    return {}
                placeholders = ",".join("?" for _ in notice_ids)
                sql += f" AND f.notice_id IN ({placeholders})"
                params = tuple(notice_ids)
            rows = con.execute(sql + " ORDER BY f.id", params).fetchall()
            review_sql = "SELECT * FROM notice_identity_review WHERE is_active=1"
            if notice_ids is not None:
                review_sql += f" AND notice_id IN ({placeholders})"
            reviews = con.execute(review_sql + " ORDER BY id", params).fetchall()
        grouped: dict[int, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            grouped[int(row["notice_id"])].append(row)
        reviews_by_notice: dict[int, list[sqlite3.Row]] = defaultdict(list)
        for row in reviews:
            reviews_by_notice[int(row["notice_id"])].append(row)
        notice_keys = set(grouped) | set(reviews_by_notice)
        return {
            notice_id: self._summary(
                notice_id, grouped.get(notice_id, []), reviews_by_notice.get(notice_id, []),
            )
            for notice_id in notice_keys
        }

    @staticmethod
    def _best(
        facts: list[sqlite3.Row], fact_type: str, *, usable_only: bool = False,
        confirmed_ids: set[int] | None = None,
    ) -> sqlite3.Row | None:
        values = [row for row in facts if row["fact_type"] == fact_type]
        if usable_only:
            values = [row for row in values if row["quality"] not in {"CONTAMINATED", "MISSING"}]
        confirmed_ids = confirmed_ids or set()
        return max(
            values,
            key=lambda row: (
                int(row["id"]) in confirmed_ids,
                CONFIDENCE_RANK[row["confidence"]], SOURCE_RANK.get(row["source_type"], 0), -int(row["id"]),
            ),
            default=None,
        )

    def _summary(
        self, notice_id: int, facts: list[sqlite3.Row], reviews: list[sqlite3.Row] | None = None,
    ) -> NoticeIdentity:
        reviews = reviews or []
        rejected_ids = {
            int(row["target_fact_id"]) for row in reviews
            if row["decision"] == "REJECT" and row["target_fact_id"] is not None
        }
        rejected_signatures = {
            (
                row["fact_type"], row["normalized_value"], row["source_type"],
                row["source_reference"], row["source_location"], row["identifier_type"],
                row["identifier_namespace"],
            )
            for row in facts if int(row["id"]) in rejected_ids
        }
        confirmed_ids = {
            int(row["target_fact_id"]) for row in reviews
            if row["decision"] == "CONFIRM" and row["target_fact_id"] is not None
        }
        cleared = {row["fact_type"] for row in reviews if row["decision"] == "CLEAR"}
        overrides = {
            row["fact_type"]: int(row["human_fact_id"])
            for row in reviews if row["decision"] == "OVERRIDE" and row["human_fact_id"] is not None
        }
        facts = [
            row for row in facts
            if (
                row["fact_type"], row["normalized_value"], row["source_type"],
                row["source_reference"], row["source_location"], row["identifier_type"],
                row["identifier_namespace"],
            ) not in rejected_signatures
        ]
        facts = [
            row for row in facts
            if row["fact_type"] not in cleared
            and (row["fact_type"] not in overrides or int(row["id"]) == overrides[row["fact_type"]])
        ]
        region = self._best(facts, "REGION", confirmed_ids=confirmed_ids)
        region_confirmed = bool(region and int(region["id"]) in confirmed_ids)
        usable_region = region if region and (
            region["confidence"] in {"HIGH", "MEDIUM"} or region_confirmed
        ) else None
        owner_any = self._best(facts, "OWNER", confirmed_ids=confirmed_ids)
        owner_confirmed = bool(owner_any and int(owner_any["id"]) in confirmed_ids)
        owner = owner_any if owner_confirmed else self._best(
            facts, "OWNER", usable_only=True, confirmed_ids=confirmed_ids,
        )
        core = self._best(facts, "CORE_NAME", confirmed_ids=confirmed_ids)
        parent_core = self._best(facts, "PARENT_CORE_NAME", confirmed_ids=confirmed_ids)
        procurement = self._best(facts, "PROCUREMENT_OBJECT", confirmed_ids=confirmed_ids)
        scope = self._best(facts, "SUBPROJECT_SCOPE", confirmed_ids=confirmed_ids)
        result = self._best(facts, "RESULT_PARTY", confirmed_ids=confirmed_ids)
        lots = [row for row in facts if row["fact_type"] == "LOT"]
        identifiers = tuple(sorted((
            IdentityIdentifier(
                row["identifier_type"], row["identifier_namespace"], row["normalized_value"],
                row["identity_strength"], row["source_reference"],
            )
            for row in facts if row["fact_type"] == "PROJECT_IDENTIFIER"
        ), key=lambda item: (item.namespace, item.identifier_type, item.value, item.source_reference)))
        return NoticeIdentity(
            notice_id=notice_id,
            region_value=usable_region["normalized_value"] if usable_region else "",
            region_source=(usable_region or region)["source_type"] if (usable_region or region) else "UNKNOWN",
            region_confidence=(usable_region or region)["confidence"] if (usable_region or region) else "UNKNOWN",
            owner=owner["raw_value"] if owner else "",
            owner_quality=("HUMAN_CONFIRMED" if owner_confirmed else (
                owner["quality"] if owner else (owner_any["quality"] if owner_any else "MISSING")
            )),
            core_name=core["raw_value"] if core else "",
            parent_core_name=parent_core["raw_value"] if parent_core else (core["raw_value"] if core else ""),
            procurement_object=procurement["raw_value"] if procurement else "",
            lots=tuple(sorted({row["normalized_value"] for row in lots})),
            lot_kind="MULTI_LOT" if any(row["quality"].startswith("MULTI_LOT") for row in lots) else ("SINGLE_LOT" if lots else "NONE"),
            phases=tuple(sorted({row["normalized_value"] for row in facts if row["fact_type"] == "PHASE"})),
            years=tuple(sorted({row["normalized_value"] for row in facts if row["fact_type"] == "YEAR"})),
            stations=tuple(sorted({row["normalized_value"] for row in facts if row["fact_type"] == "STATION"})),
            ranges=tuple(sorted({row["normalized_value"] for row in facts if row["fact_type"] == "RANGE"})),
            subproject_scope=scope["raw_value"] if scope else "",
            result_party=result["raw_value"] if result else "",
            result_party_source_reference=f"notice_identity_fact:{result['id']}" if result else "",
            identifiers=identifiers,
        )

    def facts(self, notice_id: int, *, active_only: bool = False) -> list[dict]:
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM notice_identity_fact WHERE notice_id=?" + (" AND is_active=1" if active_only else "") + " ORDER BY id",
                (notice_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def statistics(self) -> dict:
        with self.db.connect() as con:
            fact_types = dict(con.execute(
                "SELECT fact_type,COUNT(*) FROM notice_identity_fact WHERE is_active=1 GROUP BY fact_type ORDER BY fact_type"
            ).fetchall())
            confidence = dict(con.execute(
                "SELECT confidence,COUNT(*) FROM notice_identity_fact WHERE is_active=1 GROUP BY confidence ORDER BY confidence"
            ).fetchall())
            sources = dict(con.execute(
                "SELECT source_type,COUNT(*) FROM notice_identity_fact WHERE is_active=1 GROUP BY source_type ORDER BY source_type"
            ).fetchall())
            notices = int(con.execute("SELECT COUNT(DISTINCT notice_id) FROM notice_identity_fact WHERE is_active=1").fetchone()[0])
            total = int(con.execute("SELECT COUNT(*) FROM notice_identity_fact WHERE is_active=1").fetchone()[0])
            historical = int(con.execute("SELECT COUNT(*) FROM notice_identity_fact WHERE is_active=0").fetchone()[0])
        return {
            "extractor_version": self.extractor_version, "notice_count": notices,
            "active_fact_count": total, "historical_fact_count": historical,
            "fact_type_counts": fact_types, "confidence_counts": confidence,
            "source_type_counts": sources,
        }

    def difference_report(self) -> dict:
        identities = self.current_identities()
        fields = ("region", "owner", "lot", "phase", "station", "identifier")
        counters = {field: Counter() for field in fields}
        samples = {field: [] for field in fields}
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT * FROM project ORDER BY id").fetchall()
            for row in rows:
                identity = identities.get(int(row["id"]), NoticeIdentity(int(row["id"])))
                canonical = canonicalize_project_name(row["project_name"] or "")
                core, _ = _split_project_identity(re.sub(r"^[A-Z]\d{10,}", "", canonical))
                legacy = {
                    "region": {normalize_region(row["region"] or "")} - {""},
                    "owner": {identity_text(clean_owner(row["owner"] or ""))} - {""},
                    "lot": {identity_text(match) for match in re.findall(r"第?[一二三四五六七八九十百零〇\d]+(?:施工)?标段|[一二三四五六七八九十百零〇\d]+标段|标段[一二三四五六七八九十百零〇\d]+", core)},
                    "phase": {identity_text(value) for value in _PHASE.findall(core)},
                    "station": {identity_text(value) for value in _STATION.findall(core)},
                    "identifier": {normalize_identifier(row["project_no"] or "")} - {""},
                }
                current = {
                    "region": {identity.region_value} - {""}, "owner": {identity_text(identity.owner)} - {""},
                    "lot": set(identity.lots), "phase": set(identity.phases), "station": set(identity.stations),
                    "identifier": {item.value for item in identity.identifiers},
                }
                for field in fields:
                    old, new = legacy[field], current[field]
                    state = "unchanged" if old == new else ("added" if not old and new else ("removed" if old and not new else "changed"))
                    counters[field][state] += 1
                    if state != "unchanged" and len(samples[field]) < 10:
                        samples[field].append({"notice_id": int(row["id"]), "title": row["project_name"], "old": sorted(old), "new": sorted(new), "change": state})
        return {field: {"counts": dict(counters[field]), "samples": samples[field]} for field in fields}
