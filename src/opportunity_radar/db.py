import hashlib, html, json, re, sqlite3, unicodedata
from datetime import date, datetime
from pathlib import Path
from bs4 import BeautifulSoup
from .models import Project
from .source_health import assess_source_health
from .business_time import business_day_bounds, business_now_naive, coerce_business_date

PROJECT_SCHEMA = """CREATE TABLE IF NOT EXISTS project (
 id INTEGER PRIMARY KEY AUTOINCREMENT, project_name TEXT NOT NULL, project_no TEXT,
 publish_date TEXT, region TEXT, owner TEXT, tenderer TEXT, project_stage TEXT,
 construction_content TEXT, source_site TEXT NOT NULL, url TEXT NOT NULL,
 raw_text TEXT, ai_score INTEGER DEFAULT 0, matched_products TEXT DEFAULT '[]',
 analysis_json TEXT DEFAULT '{}', analyzer_version TEXT DEFAULT '', rules_version TEXT DEFAULT '',
 notice_actionability TEXT NOT NULL DEFAULT 'UNKNOWN',
 project_progress_signal TEXT NOT NULL DEFAULT 'UNKNOWN',
 project_tracking_recommendation TEXT NOT NULL DEFAULT 'UNKNOWN',
 created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(source_site, url));"""
SOURCE_RUN_SCHEMA = """CREATE TABLE IF NOT EXISTS source_run (
 id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, source_name TEXT NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT NOT NULL, status TEXT NOT NULL,
 item_count INTEGER DEFAULT 0, error TEXT DEFAULT '', run_id TEXT DEFAULT '',
 collected_count INTEGER DEFAULT 0, rejected_count INTEGER DEFAULT 0,
 analyzer_version TEXT DEFAULT '', rules_version TEXT DEFAULT '', duration_ms INTEGER DEFAULT 0);"""
NOTICE_OBSERVATION_SCHEMA = """CREATE TABLE IF NOT EXISTS notice_observation (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 run_id INTEGER NOT NULL,
 notice_id INTEGER NOT NULL,
 source_id TEXT NOT NULL,
 observed_at TEXT NOT NULL,
 is_first_seen INTEGER NOT NULL DEFAULT 0 CHECK(is_first_seen IN (0,1)),
 content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
 content_changed INTEGER NOT NULL DEFAULT 0 CHECK(content_changed IN (0,1)),
 FOREIGN KEY(run_id) REFERENCES source_run(id),
 FOREIGN KEY(notice_id) REFERENCES project(id),
 UNIQUE(run_id, notice_id));"""
NOTICE_PRODUCT_SCHEMA = """CREATE TABLE IF NOT EXISTS notice_product_assessment (
 id INTEGER PRIMARY KEY AUTOINCREMENT, notice_id INTEGER NOT NULL, product TEXT NOT NULL,
 evidence_score INTEGER NOT NULL DEFAULT 0, demand_intent TEXT NOT NULL DEFAULT '可能使用',
 lead_score INTEGER NOT NULL DEFAULT 0, opportunity_score INTEGER NOT NULL DEFAULT 0,
 demand_status TEXT NOT NULL DEFAULT 'UNKNOWN', engineering_need TEXT NOT NULL DEFAULT 'UNKNOWN',
 scope_status TEXT NOT NULL DEFAULT 'UNCERTAIN', opportunity_status TEXT NOT NULL DEFAULT 'UNKNOWN',
 product_opportunity_status TEXT NOT NULL DEFAULT 'NO_EVIDENCE',
 product_demand_evidence_level TEXT NOT NULL DEFAULT '',
 product_demand_evidence_type TEXT NOT NULL DEFAULT '',
 procurement_window_status TEXT NOT NULL DEFAULT 'UNKNOWN',
 procurement_window_evidence TEXT NOT NULL DEFAULT '[]',
 evidence_level TEXT NOT NULL DEFAULT '无有效证据', positive_evidence TEXT NOT NULL DEFAULT '[]',
 evidence_relation TEXT NOT NULL DEFAULT '工程方向', score_explanation TEXT NOT NULL DEFAULT '[]',
 negative_evidence TEXT NOT NULL DEFAULT '[]', analyzer_version TEXT NOT NULL DEFAULT '',
 rules_version TEXT NOT NULL DEFAULT '', assessed_at TEXT DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(notice_id, product, analyzer_version, rules_version));"""
FEEDBACK_SCHEMA = """CREATE TABLE IF NOT EXISTS feedback_label (
 id INTEGER PRIMARY KEY AUTOINCREMENT, notice_id INTEGER NOT NULL, product TEXT DEFAULT '',
 label TEXT NOT NULL, note TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);"""
ATTACHMENT_SCHEMA = """CREATE TABLE IF NOT EXISTS notice_attachment (
 id INTEGER PRIMARY KEY AUTOINCREMENT, notice_id INTEGER NOT NULL,
 attachment_url TEXT NOT NULL, file_name TEXT NOT NULL DEFAULT '',
 content_type TEXT NOT NULL DEFAULT '', size_bytes INTEGER NOT NULL DEFAULT 0,
 sha256 TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'DISCOVERED',
 local_path TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
 discovered_at TEXT DEFAULT CURRENT_TIMESTAMP, downloaded_at TEXT, parsed_at TEXT,
 UNIQUE(notice_id, attachment_url));"""
DOCUMENT_SCHEMA = """CREATE TABLE IF NOT EXISTS document_text (
 id INTEGER PRIMARY KEY AUTOINCREMENT, attachment_id INTEGER NOT NULL UNIQUE,
 notice_id INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '',
 extracted_text TEXT NOT NULL DEFAULT '', page_count INTEGER NOT NULL DEFAULT 0,
 parser TEXT NOT NULL DEFAULT '', text_sha256 TEXT NOT NULL DEFAULT '',
 metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT DEFAULT CURRENT_TIMESTAMP);"""
DOCUMENT_IDENTIFIER_SCHEMA = """CREATE TABLE IF NOT EXISTS document_identifier (
 id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL,
 identifier TEXT NOT NULL, identifier_type TEXT NOT NULL DEFAULT '工程编号',
 UNIQUE(document_id, identifier, identifier_type));"""
DOCUMENT_FTS_SCHEMA = """CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
 document_id UNINDEXED, notice_id UNINDEXED, title, body, identifiers,
 tokenize='unicode61');"""
ENGINEERING_PROJECT_SCHEMA = """CREATE TABLE IF NOT EXISTS engineering_project (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 identity_key TEXT NOT NULL UNIQUE,
 canonical_name TEXT NOT NULL,
 region TEXT DEFAULT '',
 owner TEXT DEFAULT '',
 project_type TEXT DEFAULT '',
 -- Deprecated compatibility field: read-only lifecycle aggregation does not
 -- write this column. Keep it until an explicit schema migration removes it.
 lifecycle_stage TEXT DEFAULT '',
 first_seen_at TEXT,
 last_seen_at TEXT,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT DEFAULT CURRENT_TIMESTAMP);"""
PROJECT_NOTICE_LINK_SCHEMA = """CREATE TABLE IF NOT EXISTS project_notice_link (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 engineering_project_id INTEGER NOT NULL,
 notice_id INTEGER NOT NULL UNIQUE,
 relation_type TEXT NOT NULL DEFAULT 'BELONGS_TO' CHECK(relation_type='BELONGS_TO'),
 match_method TEXT NOT NULL,
 match_score INTEGER NOT NULL CHECK(match_score BETWEEN 0 AND 100),
 confirmed_by_human INTEGER NOT NULL DEFAULT 0 CHECK(confirmed_by_human IN (0,1)),
 created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY(engineering_project_id) REFERENCES engineering_project(id),
 FOREIGN KEY(notice_id) REFERENCES project(id));"""
ENGINEERING_PROJECT_IDENTIFIER_SCHEMA = """CREATE TABLE IF NOT EXISTS engineering_project_identifier (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 engineering_project_id INTEGER NOT NULL,
 identifier_type TEXT NOT NULL,
 identifier_value TEXT NOT NULL,
 source_notice_id INTEGER NOT NULL,
 source_document_id INTEGER,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY(engineering_project_id) REFERENCES engineering_project(id),
 FOREIGN KEY(source_notice_id) REFERENCES project(id),
 FOREIGN KEY(source_document_id) REFERENCES document_text(id),
 UNIQUE(identifier_type, identifier_value));"""
PROJECT_LINK_CANDIDATE_SCHEMA = """CREATE TABLE IF NOT EXISTS project_link_candidate (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 notice_id_a INTEGER NOT NULL,
 notice_id_b INTEGER NOT NULL,
 candidate_key TEXT NOT NULL UNIQUE,
 candidate_relation TEXT NOT NULL CHECK(candidate_relation IN ('SAME_PROJECT','SAME_PARENT_PROJECT','UNCERTAIN')),
 evidence_score INTEGER NOT NULL CHECK(evidence_score BETWEEN 0 AND 100),
 evidence_level TEXT NOT NULL CHECK(evidence_level IN ('HIGH','MEDIUM')),
 candidate_status TEXT NOT NULL DEFAULT 'PENDING'
   CHECK(candidate_status IN ('PENDING','CONFIRMED','REJECTED','DEFERRED')),
 evidence_json TEXT NOT NULL DEFAULT '{}',
 evidence_hash TEXT NOT NULL DEFAULT '',
 reviewed_evidence_hash TEXT,
 is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
 algorithm_version TEXT NOT NULL,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
 reviewed_at TEXT,
 review_note TEXT NOT NULL DEFAULT '',
 FOREIGN KEY(notice_id_a) REFERENCES project(id),
 FOREIGN KEY(notice_id_b) REFERENCES project(id),
 CHECK(notice_id_a < notice_id_b),
 UNIQUE(notice_id_a, notice_id_b));"""
NOTICE_IDENTITY_FACT_SCHEMA = """CREATE TABLE IF NOT EXISTS notice_identity_fact (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 notice_id INTEGER NOT NULL,
 fact_type TEXT NOT NULL,
 normalized_value TEXT NOT NULL,
 raw_value TEXT NOT NULL DEFAULT '',
 source_type TEXT NOT NULL,
 source_reference TEXT NOT NULL,
 source_location TEXT NOT NULL DEFAULT '',
 confidence TEXT NOT NULL CHECK(confidence IN ('HIGH','MEDIUM','LOW','UNKNOWN')),
 quality TEXT NOT NULL DEFAULT '',
 identifier_type TEXT NOT NULL DEFAULT '',
 identifier_namespace TEXT NOT NULL DEFAULT '',
 identity_strength TEXT NOT NULL DEFAULT '',
 extractor_version TEXT NOT NULL,
 is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
 created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY(notice_id) REFERENCES project(id),
 UNIQUE(
   notice_id,fact_type,normalized_value,source_type,source_reference,source_location,
   identifier_type,identifier_namespace,extractor_version
 ));"""
NOTICE_IDENTITY_REVIEW_SCHEMA = """CREATE TABLE IF NOT EXISTS notice_identity_review (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 notice_id INTEGER NOT NULL,
 fact_type TEXT NOT NULL,
 target_fact_id INTEGER,
 decision TEXT NOT NULL CHECK(decision IN ('CONFIRM','REJECT','OVERRIDE','CLEAR')),
 human_value TEXT NOT NULL DEFAULT '',
 human_normalized_value TEXT NOT NULL DEFAULT '',
 human_fact_id INTEGER,
 review_note TEXT NOT NULL DEFAULT '',
 reviewer TEXT NOT NULL DEFAULT 'LOCAL_USER',
 is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
 created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY(notice_id) REFERENCES project(id),
 FOREIGN KEY(target_fact_id) REFERENCES notice_identity_fact(id),
 FOREIGN KEY(human_fact_id) REFERENCES notice_identity_fact(id));"""
PROJECT_EVENT_SCHEMA = """CREATE TABLE IF NOT EXISTS project_event (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 engineering_project_id INTEGER NOT NULL,
 notice_id INTEGER NOT NULL,
 event_type TEXT NOT NULL CHECK(event_type IN (
   'TENDER_ANNOUNCED','CONTRACTOR_CANDIDATE_SELECTED','CONTRACTOR_SELECTED',
   'PROCUREMENT_ANNOUNCED','PRODUCT_PROCUREMENT_ANNOUNCED',
   'PRODUCT_PROCUREMENT_RESULT','CONSTRUCTION_PROGRESS','PROJECT_TERMINATED','UNKNOWN'
 )),
 event_date TEXT,
 event_date_source TEXT NOT NULL DEFAULT 'UNKNOWN',
 event_scope TEXT NOT NULL DEFAULT '',
 event_subject TEXT NOT NULL DEFAULT '',
 party_name TEXT NOT NULL DEFAULT '',
 party_role TEXT NOT NULL DEFAULT '',
 party_source_reference TEXT NOT NULL DEFAULT '',
 product TEXT NOT NULL DEFAULT '',
 lot TEXT NOT NULL DEFAULT '',
 phase TEXT NOT NULL DEFAULT '',
 source_type TEXT NOT NULL DEFAULT '',
 source_reference TEXT NOT NULL DEFAULT '',
 evidence_text TEXT NOT NULL DEFAULT '',
 confidence TEXT NOT NULL CHECK(confidence IN ('HIGH','MEDIUM','LOW','UNKNOWN')),
 extractor_version TEXT NOT NULL,
 event_key TEXT NOT NULL,
 is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
 created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY(engineering_project_id) REFERENCES engineering_project(id),
 FOREIGN KEY(notice_id) REFERENCES project(id),
 UNIQUE(event_key, extractor_version));"""

PROJECT_LINK_CANDIDATE_MIGRATIONS = {
    "evidence_hash": "TEXT NOT NULL DEFAULT ''",
    "reviewed_evidence_hash": "TEXT",
    "is_active": "INTEGER NOT NULL DEFAULT 1",
}
PROJECT_EVENT_MIGRATIONS = {
    "party_source_reference": "TEXT NOT NULL DEFAULT ''",
}

PROJECT_MIGRATIONS = {
    "analyzer_version": "TEXT DEFAULT ''",
    "rules_version": "TEXT DEFAULT ''",
    "notice_actionability": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
    "project_progress_signal": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
    "project_tracking_recommendation": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
}
SOURCE_RUN_MIGRATIONS = {
    "run_id": "TEXT DEFAULT ''",
    "collected_count": "INTEGER DEFAULT 0",
    "rejected_count": "INTEGER DEFAULT 0",
    "analyzer_version": "TEXT DEFAULT ''",
    "rules_version": "TEXT DEFAULT ''",
    "duration_ms": "INTEGER DEFAULT 0",
    "baseline_average": "REAL DEFAULT 0",
    "baseline_samples": "INTEGER DEFAULT 0",
    "health_reason": "TEXT DEFAULT ''",
    "health_status": "TEXT DEFAULT 'HEALTHY'",
    "observed_count": "INTEGER DEFAULT 0",
    "funnel_json": "TEXT DEFAULT '{}'",
}
NOTICE_PRODUCT_MIGRATIONS = {
    "lead_score": "INTEGER NOT NULL DEFAULT 0",
    "opportunity_score": "INTEGER NOT NULL DEFAULT 0",
    "evidence_relation": "TEXT NOT NULL DEFAULT '工程方向'",
    "score_explanation": "TEXT NOT NULL DEFAULT '[]'",
    "product_opportunity_status": "TEXT NOT NULL DEFAULT 'NO_EVIDENCE'",
    "product_demand_evidence_level": "TEXT NOT NULL DEFAULT ''",
    "product_demand_evidence_type": "TEXT NOT NULL DEFAULT ''",
    "procurement_window_status": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
    "procurement_window_evidence": "TEXT NOT NULL DEFAULT '[]'",
}

_VOLATILE_LINE = re.compile(
    r"^\s*(?:更新时间|更新日期|最后更新|发布时间戳|浏览(?:次数|量)?|点击(?:次数|量)?|访问量)\s*[：:]",
    re.IGNORECASE,
)
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff]")


def _normalize_business_text(value: str) -> str:
    """Remove presentation-only variation while preserving notice business wording."""
    if not value:
        return ""
    value = html.unescape(value)
    if "<" in value and ">" in value:
        soup = BeautifulSoup(value, "html.parser")
        for element in soup(["script", "style", "noscript", "template"]):
            element.decompose()
        value = soup.get_text("\n")
    value = unicodedata.normalize("NFKC", _ZERO_WIDTH.sub("", value))
    lines = []
    for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        if line and not _VOLATILE_LINE.match(line):
            lines.append(line)
    return " ".join(lines)


def notice_content_hash(project: Project) -> str:
    """Hash only normalized notice business fields; analysis and scores are excluded."""
    payload = {
        "name": _normalize_business_text(project.name),
        "project_no": _normalize_business_text(project.project_no),
        "publish_date": _normalize_business_text(project.publish_date),
        "region": _normalize_business_text(project.region),
        "owner": _normalize_business_text(project.owner),
        "tenderer": _normalize_business_text(project.tenderer),
        "stage": _normalize_business_text(project.stage),
        "construction_content": _normalize_business_text(project.construction_content),
        "raw_text": _normalize_business_text(project.raw_text),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

class Database:
    def __init__(self, path: Path): self.path = path
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path)
        con.execute("PRAGMA foreign_keys=ON")
        return con
    def init(self):
        with self.connect() as con:
            con.execute(PROJECT_SCHEMA)
            con.execute(SOURCE_RUN_SCHEMA)
            con.execute(NOTICE_OBSERVATION_SCHEMA)
            con.execute(NOTICE_PRODUCT_SCHEMA)
            con.execute(FEEDBACK_SCHEMA)
            con.execute(ATTACHMENT_SCHEMA)
            con.execute(DOCUMENT_SCHEMA)
            con.execute(DOCUMENT_IDENTIFIER_SCHEMA)
            con.execute(DOCUMENT_FTS_SCHEMA)
            con.execute(ENGINEERING_PROJECT_SCHEMA)
            con.execute(PROJECT_NOTICE_LINK_SCHEMA)
            con.execute(ENGINEERING_PROJECT_IDENTIFIER_SCHEMA)
            con.execute(PROJECT_LINK_CANDIDATE_SCHEMA)
            con.execute(NOTICE_IDENTITY_FACT_SCHEMA)
            con.execute(NOTICE_IDENTITY_REVIEW_SCHEMA)
            con.execute(PROJECT_EVENT_SCHEMA)
            self._ensure_columns(con, "project", PROJECT_MIGRATIONS)
            self._ensure_columns(con, "source_run", SOURCE_RUN_MIGRATIONS)
            self._ensure_columns(con, "notice_product_assessment", NOTICE_PRODUCT_MIGRATIONS)
            self._ensure_columns(con, "project_link_candidate", PROJECT_LINK_CANDIDATE_MIGRATIONS)
            self._ensure_columns(con, "project_event", PROJECT_EVENT_MIGRATIONS)
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_notice_observation_notice ON notice_observation(notice_id, id)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_notice_observation_source_time ON notice_observation(source_id, observed_at)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_notice_observation_time ON notice_observation(observed_at)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_project_notice_link_project ON project_notice_link(engineering_project_id)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_engineering_project_identifier_project ON engineering_project_identifier(engineering_project_id)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_project_link_candidate_queue ON project_link_candidate(candidate_status,evidence_score DESC)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_notice_identity_fact_current ON notice_identity_fact(notice_id,is_active,fact_type)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_notice_identity_fact_identifier ON notice_identity_fact(identifier_namespace,identifier_type,normalized_value,is_active)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_notice_identity_review_notice ON notice_identity_review(notice_id,fact_type,is_active)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_notice_identity_review_target ON notice_identity_review(target_fact_id,is_active)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_project_event_timeline ON project_event(engineering_project_id,is_active,event_date,id)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_project_event_notice ON project_event(notice_id,is_active)"
            )

    def backup_to(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source, sqlite3.connect(destination) as target:
            source.backup(target)
        return destination
    @staticmethod
    def _ensure_columns(con, table: str, columns: dict[str, str]):
        existing = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    @staticmethod
    def _upsert_project(con: sqlite3.Connection, p: Project) -> tuple[int, bool]:
        existing = con.execute(
            "SELECT id FROM project WHERE source_site=? AND url=?", (p.source_site, p.url)
        ).fetchone()
        values = (p.name,p.project_no,p.publish_date,p.region,p.owner,p.tenderer,p.stage,p.construction_content,p.source_site,p.url,p.raw_text,p.ai_score,json.dumps(p.matched_products,ensure_ascii=False),p.analysis_json,p.analyzer_version,p.rules_version,p.notice_actionability,p.project_progress_signal,p.project_tracking_recommendation)
        sql = """INSERT INTO project(project_name,project_no,publish_date,region,owner,tenderer,project_stage,construction_content,source_site,url,raw_text,ai_score,matched_products,analysis_json,analyzer_version,rules_version,notice_actionability,project_progress_signal,project_tracking_recommendation)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_site,url) DO UPDATE SET
        project_name=excluded.project_name, project_no=excluded.project_no, publish_date=excluded.publish_date,
        region=excluded.region, owner=excluded.owner, tenderer=excluded.tenderer,
        project_stage=excluded.project_stage, construction_content=excluded.construction_content,
        raw_text=excluded.raw_text, ai_score=excluded.ai_score,
        matched_products=excluded.matched_products, analysis_json=excluded.analysis_json,
        analyzer_version=excluded.analyzer_version, rules_version=excluded.rules_version,
        notice_actionability=excluded.notice_actionability,
        project_progress_signal=excluded.project_progress_signal,
        project_tracking_recommendation=excluded.project_tracking_recommendation"""
        con.execute(sql, values)
        notice_id = int(con.execute(
            "SELECT id FROM project WHERE source_site=? AND url=?", (p.source_site, p.url)
        ).fetchone()[0])
        return notice_id, existing is None

    def upsert(self, p: Project) -> int:
        with self.connect() as con:
            notice_id, _ = self._upsert_project(con, p)
            self._upsert_product_assessments(con, notice_id, p)
        from .identity import NoticeIdentityService
        NoticeIdentityService(self).extract_notice(notice_id)
        return notice_id

    def upsert_observed(
        self, p: Project, source_run_id: int, observed_at: datetime | None = None,
    ) -> int:
        """Persist a notice and its exact source-run observation atomically."""
        observed_at = observed_at or business_now_naive()
        current_hash = notice_content_hash(p)
        with self.connect() as con:
            run = con.execute(
                "SELECT id,source_id,source_name FROM source_run WHERE id=?", (source_run_id,)
            ).fetchone()
            if not run:
                raise ValueError(f"来源运行不存在：{source_run_id}")
            if p.source_site != run[2]:
                raise ValueError(
                    f"公告来源与采集运行不一致：{p.source_site!r} != {run[2]!r}"
                )
            notice_id, was_created = self._upsert_project(con, p)
            self._upsert_product_assessments(con, notice_id, p)
            previous = con.execute(
                """SELECT content_hash FROM notice_observation
                WHERE notice_id=? ORDER BY id DESC LIMIT 1""", (notice_id,)
            ).fetchone()
            is_first_seen = was_created and previous is None
            content_changed = previous is not None and previous[0] != current_hash
            con.execute(
                """INSERT INTO notice_observation(
                run_id,notice_id,source_id,observed_at,is_first_seen,content_hash,content_changed)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(run_id,notice_id) DO NOTHING""",
                (
                    source_run_id, notice_id, run[1], observed_at.isoformat(timespec="seconds"),
                    int(is_first_seen), current_hash, int(content_changed),
                ),
            )
        from .identity import NoticeIdentityService
        NoticeIdentityService(self).extract_notice(notice_id)
        return notice_id

    def notice_observations(self, notice_id: int | None = None):
        sql = "SELECT * FROM notice_observation"
        params = ()
        if notice_id is not None:
            sql += " WHERE notice_id=?"
            params = (notice_id,)
        sql += " ORDER BY id"
        with self.connect() as con:
            con.row_factory = sqlite3.Row
            return con.execute(sql, params).fetchall()

    @staticmethod
    def _upsert_product_assessments(con: sqlite3.Connection, notice_id: int, project: Project) -> None:
        try:
            analysis = json.loads(project.analysis_json or "{}")
        except json.JSONDecodeError:
            return
        sql = """INSERT INTO notice_product_assessment(
            notice_id,product,evidence_score,lead_score,opportunity_score,demand_intent,demand_status,engineering_need,
            scope_status,opportunity_status,product_opportunity_status,evidence_level,evidence_relation,positive_evidence,negative_evidence,score_explanation,
            product_demand_evidence_level,product_demand_evidence_type,procurement_window_status,procurement_window_evidence,
            analyzer_version,rules_version
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(notice_id,product,analyzer_version,rules_version) DO UPDATE SET
            evidence_score=excluded.evidence_score,lead_score=excluded.lead_score,
            opportunity_score=excluded.opportunity_score,demand_intent=excluded.demand_intent,
            demand_status=excluded.demand_status,engineering_need=excluded.engineering_need,
            scope_status=excluded.scope_status,opportunity_status=excluded.opportunity_status,
            product_opportunity_status=excluded.product_opportunity_status,
            product_demand_evidence_level=excluded.product_demand_evidence_level,
            product_demand_evidence_type=excluded.product_demand_evidence_type,
            procurement_window_status=excluded.procurement_window_status,
            procurement_window_evidence=excluded.procurement_window_evidence,
            evidence_level=excluded.evidence_level,evidence_relation=excluded.evidence_relation,
            positive_evidence=excluded.positive_evidence,negative_evidence=excluded.negative_evidence,
            score_explanation=excluded.score_explanation,assessed_at=CURRENT_TIMESTAMP"""
        for item in analysis.get("命中证据", []):
            product = str(item.get("产品", "")).strip()
            if not product:
                continue
            con.execute(sql, (
                notice_id, product, int(item.get("评分", 0)), int(item.get("项目线索分", item.get("评分", 0))),
                int(item.get("当前商机分", 0)), item.get("需求意图", "可能使用"),
                item.get("需求状态", "UNKNOWN"), item.get("工程需求", "UNKNOWN"),
                item.get("范围状态", "UNCERTAIN"), item.get("当前机会状态", "UNKNOWN"),
                item.get("产品机会状态", "NO_EVIDENCE"),
                item.get("等级", "无有效证据"), item.get("证据关系", "工程方向"),
                json.dumps(item.get("证据句", []), ensure_ascii=False),
                json.dumps(item.get("负向证据", []), ensure_ascii=False),
                json.dumps(item.get("评分说明", []), ensure_ascii=False),
                item.get("产品需求证据等级", ""), item.get("产品需求证据类型", ""),
                item.get("采购窗口状态", "UNKNOWN"),
                json.dumps(item.get("采购窗口证据", []), ensure_ascii=False),
                project.analyzer_version, project.rules_version,
            ))

    def notice_product_assessments(self, notice_id: int | None = None):
        sql = "SELECT * FROM notice_product_assessment"
        params = ()
        if notice_id is not None:
            sql += " WHERE notice_id=?"
            params = (notice_id,)
        sql += " ORDER BY id"
        with self.connect() as con:
            con.row_factory = sqlite3.Row
            return con.execute(sql, params).fetchall()

    def follow_up_assessments(self, limit: int = 50):
        """Return the latest per-product signals that need a later procurement watch."""
        sql = """SELECT p.id AS notice_id,p.project_name,p.publish_date,p.region,
            p.source_site,p.url,a.product,a.evidence_score,a.demand_intent,a.demand_status,
            a.engineering_need,a.scope_status,a.opportunity_status,a.product_opportunity_status,a.evidence_level,
            a.positive_evidence,a.negative_evidence,a.product_demand_evidence_level,
            a.product_demand_evidence_type,a.procurement_window_status,a.procurement_window_evidence,a.assessed_at
        FROM notice_product_assessment a
        JOIN project p ON p.id=a.notice_id
        JOIN (
            SELECT notice_id,product,MAX(id) AS id
            FROM notice_product_assessment GROUP BY notice_id,product
        ) latest ON latest.id=a.id
        WHERE a.engineering_need IN ('YES','LIKELY','POSSIBLE')
          AND a.product_opportunity_status IN ('PRE_PROCUREMENT','FOLLOW_UP')
        ORDER BY p.publish_date DESC,a.evidence_score DESC,a.id DESC
        LIMIT ?"""
        with self.connect() as con:
            con.row_factory = sqlite3.Row
            return con.execute(sql, (limit,)).fetchall()

    def add_feedback(self, notice_id: int, label: str, product: str = "", note: str = "") -> int:
        allowed = {"WORTH_TRACKING", "NOT_RELEVANT", "MISSED_PRODUCT", "WRONG_PRODUCT", "DUPLICATE"}
        if label not in allowed:
            raise ValueError(f"未知反馈标签：{label}")
        with self.connect() as con:
            if not con.execute("SELECT 1 FROM project WHERE id=?", (notice_id,)).fetchone():
                raise ValueError(f"公告不存在：{notice_id}")
            cursor = con.execute(
                "INSERT INTO feedback_label(notice_id,product,label,note) VALUES(?,?,?,?)",
                (notice_id, product.strip(), label, note.strip()),
            )
            return int(cursor.lastrowid)

    def feedback(self, notice_id: int | None = None):
        sql = "SELECT * FROM feedback_label"
        params = ()
        if notice_id is not None:
            sql += " WHERE notice_id=?"
            params = (notice_id,)
        sql += " ORDER BY id DESC"
        with self.connect() as con:
            con.row_factory = sqlite3.Row
            return con.execute(sql, params).fetchall()
    def begin_source_run(
        self, source: dict, started_at: datetime, *, run_id: str = "",
        analyzer_version: str = "", rules_version: str = "",
    ) -> int:
        """Create the source-run row before collection so observations can reference its real PK."""
        started = started_at.isoformat(timespec="seconds")
        with self.connect() as con:
            cursor = con.execute(
                """INSERT INTO source_run(
                source_id,source_name,started_at,finished_at,status,run_id,analyzer_version,rules_version)
                VALUES(?,?,?,?,?,?,?,?)""",
                (source["id"], source["name"], started, started, "running", run_id,
                 analyzer_version, rules_version),
            )
            return int(cursor.lastrowid)

    def finish_source_run(
        self, source_run_id: int, status: str, item_count: int = 0, error: str = "", *,
        collected_count: int = 0, rejected_count: int = 0,
        analyzer_version: str = "", rules_version: str = "",
        funnel: dict[str, int] | None = None,
    ) -> int:
        with self.connect() as con:
            run = con.execute(
                "SELECT source_id,started_at FROM source_run WHERE id=?", (source_run_id,)
            ).fetchone()
            if not run:
                raise ValueError(f"来源运行不存在：{source_run_id}")
            source_id, started_text = run
        started_at = datetime.fromisoformat(started_text)
        finished_at = business_now_naive()
        duration_ms = max(0, int((finished_at - started_at).total_seconds() * 1000))
        with self.connect() as con:
            previous = con.execute(
                """SELECT collected_count,observed_count FROM source_run
                WHERE source_id=? AND id<>? AND status IN ('success','warning')
                ORDER BY id DESC LIMIT 7""", (source_id, source_run_id)
            ).fetchall()
        previous_counts = [row[0] for row in previous]
        previous_observed = [row[1] for row in previous]
        funnel = {name: max(0, int(value)) for name, value in (funnel or {}).items()}
        observed_count = funnel.get("raw_list_count")
        health = assess_source_health(
            status, collected_count, previous_counts, observed_count, previous_observed
        )
        status = health.status
        if health.reason:
            error = "；".join(filter(None, (error, health.reason)))
        values = (
            finished_at.isoformat(timespec="seconds"), status, item_count, error[:1000],
            collected_count, rejected_count, analyzer_version, rules_version, duration_ms,
            health.baseline_average, health.baseline_samples, health.reason,
            health.health_status, observed_count or 0, json.dumps(funnel, ensure_ascii=False),
            source_run_id,
        )
        with self.connect() as con:
            con.execute("""UPDATE source_run SET
                finished_at=?,status=?,item_count=?,error=?,collected_count=?,rejected_count=?,
                analyzer_version=?,rules_version=?,duration_ms=?,baseline_average=?,baseline_samples=?,
                health_reason=?,health_status=?,observed_count=?,funnel_json=? WHERE id=?""", values)
        return source_run_id

    def record_source_run(self, source: dict, started_at: datetime, status: str, item_count: int = 0,
                          error: str = "", *, run_id: str = "", collected_count: int = 0,
                          rejected_count: int = 0, analyzer_version: str = "", rules_version: str = "",
                          funnel: dict[str, int] | None = None) -> int:
        """Backward-compatible one-shot source-run recording."""
        source_run_id = self.begin_source_run(
            source, started_at, run_id=run_id,
            analyzer_version=analyzer_version, rules_version=rules_version,
        )
        return self.finish_source_run(
            source_run_id, status, item_count, error,
            collected_count=collected_count, rejected_count=rejected_count,
            analyzer_version=analyzer_version, rules_version=rules_version, funnel=funnel,
        )
    def source_status(self):
        sql = """SELECT r.* FROM source_run r JOIN (
        SELECT source_id, MAX(id) id FROM source_run GROUP BY source_id
        ) latest ON latest.id=r.id ORDER BY r.source_id"""
        with self.connect() as con:
            con.row_factory = sqlite3.Row
            return con.execute(sql).fetchall()
    def recent(self, limit=50):
        with self.connect() as con:
            con.row_factory = sqlite3.Row
            return con.execute("SELECT * FROM project ORDER BY ai_score DESC, publish_date DESC LIMIT ?", (limit,)).fetchall()

    def daily_notice_summary(self, target_date: date | str | None = None) -> dict:
        """Aggregate completed source-run observations into one row per notice and day."""
        day = coerce_business_date(target_date)
        start, end = business_day_bounds(day)
        sql = """WITH daily AS (
            SELECT o.notice_id,
                MAX(o.is_first_seen) AS first_seen_today,
                MAX(o.content_changed) AS content_changed_today,
                MIN(o.observed_at) AS first_observed_at,
                MAX(o.observed_at) AS last_observed_at,
                COUNT(*) AS observation_count
            FROM notice_observation o
            JOIN source_run r ON r.id=o.run_id
            WHERE o.observed_at>=? AND o.observed_at<?
              AND r.status IN ('success','partial','warning')
            GROUP BY o.notice_id
        )
        SELECT p.*,d.first_seen_today,d.content_changed_today,
            d.first_observed_at,d.last_observed_at,d.observation_count
        FROM daily d JOIN project p ON p.id=d.notice_id"""
        with self.connect() as con:
            con.row_factory = sqlite3.Row
            rows = [dict(row) for row in con.execute(sql, (start, end)).fetchall()]
        for row in rows:
            if row["first_seen_today"]:
                row["observation_status"] = "NEW"
            elif row["content_changed_today"]:
                row["observation_status"] = "UPDATED"
            else:
                row["observation_status"] = "SEEN_AGAIN"
        rows.sort(key=self._daily_sort_key, reverse=True)
        return {
            "business_date": day.isoformat(),
            "new": [row for row in rows if row["observation_status"] == "NEW"],
            "updated": [row for row in rows if row["observation_status"] == "UPDATED"],
            "seen_again": [row for row in rows if row["observation_status"] == "SEEN_AGAIN"],
            "all": rows,
        }

    @staticmethod
    def _daily_sort_key(row: dict) -> tuple[int, int, str, int]:
        try:
            analysis = json.loads(row.get("analysis_json") or "{}")
        except json.JSONDecodeError:
            analysis = {}
        return (
            int(analysis.get("当前商机分0-100", row.get("ai_score") or 0)),
            int(analysis.get("项目线索分0-100", row.get("ai_score") or 0)),
            row.get("publish_date") or "",
            int(row.get("id") or 0),
        )

    def all_projects(self):
        with self.connect() as con:
            con.row_factory = sqlite3.Row
            return con.execute("SELECT * FROM project ORDER BY id").fetchall()

    def projects_for_enrichment(self, project_no: str = "", notice_id: int | None = None, limit: int = 20):
        clauses, params = [], []
        if project_no:
            clauses.append("project_no=?")
            params.append(project_no)
        if notice_id is not None:
            clauses.append("id=?")
            params.append(notice_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as con:
            con.row_factory = sqlite3.Row
            return con.execute(
                f"SELECT * FROM project{where} ORDER BY publish_date DESC,id DESC LIMIT ?",
                (*params, max(1, int(limit))),
            ).fetchall()

    def upsert_attachment(self, notice_id: int, url: str, file_name: str) -> int:
        with self.connect() as con:
            con.execute(
                """INSERT INTO notice_attachment(notice_id,attachment_url,file_name)
                VALUES(?,?,?) ON CONFLICT(notice_id,attachment_url) DO UPDATE SET
                file_name=excluded.file_name""",
                (notice_id, url, file_name),
            )
            return int(con.execute(
                "SELECT id FROM notice_attachment WHERE notice_id=? AND attachment_url=?",
                (notice_id, url),
            ).fetchone()[0])

    def attachment(self, attachment_id: int):
        with self.connect() as con:
            con.row_factory = sqlite3.Row
            return con.execute(
                "SELECT * FROM notice_attachment WHERE id=?", (attachment_id,)
            ).fetchone()

    def attachments_for_cleanup(self, parsed_before: str):
        with self.connect() as con:
            con.row_factory = sqlite3.Row
            return con.execute(
                """SELECT a.* FROM notice_attachment a
                JOIN document_text d ON d.attachment_id=a.id
                WHERE a.status='PARSED' AND a.local_path<>''
                AND a.parsed_at IS NOT NULL AND datetime(a.parsed_at)<=datetime(?)
                ORDER BY a.id""",
                (parsed_before,),
            ).fetchall()

    def update_attachment(self, attachment_id: int, **values) -> None:
        allowed = {
            "content_type", "size_bytes", "sha256", "status", "local_path", "error",
            "downloaded_at", "parsed_at",
        }
        fields = [(name, value) for name, value in values.items() if name in allowed]
        if not fields:
            return
        sql = "UPDATE notice_attachment SET " + ",".join(f"{name}=?" for name, _ in fields) + " WHERE id=?"
        with self.connect() as con:
            con.execute(sql, (*[value for _, value in fields], attachment_id))

    def upsert_document(
        self, attachment_id: int, notice_id: int, title: str, text: str, page_count: int,
        parser: str, identifiers: list[tuple[str, str]], metadata: dict | None = None,
    ) -> int:
        text_hash = __import__("hashlib").sha256(text.encode("utf-8")).hexdigest()
        with self.connect() as con:
            con.execute(
                """INSERT INTO document_text(
                attachment_id,notice_id,title,extracted_text,page_count,parser,text_sha256,metadata_json)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(attachment_id) DO UPDATE SET
                notice_id=excluded.notice_id,title=excluded.title,extracted_text=excluded.extracted_text,
                page_count=excluded.page_count,parser=excluded.parser,text_sha256=excluded.text_sha256,
                metadata_json=excluded.metadata_json,updated_at=CURRENT_TIMESTAMP""",
                (attachment_id, notice_id, title, text, page_count, parser, text_hash,
                 json.dumps(metadata or {}, ensure_ascii=False)),
            )
            document_id = int(con.execute(
                "SELECT id FROM document_text WHERE attachment_id=?", (attachment_id,)
            ).fetchone()[0])
            con.execute("DELETE FROM document_identifier WHERE document_id=?", (document_id,))
            con.executemany(
                "INSERT OR IGNORE INTO document_identifier(document_id,identifier,identifier_type) VALUES(?,?,?)",
                [(document_id, value, kind) for value, kind in identifiers],
            )
            identifier_text = " ".join(value for value, _ in identifiers)
            con.execute("DELETE FROM document_fts WHERE document_id=?", (document_id,))
            con.execute(
                "INSERT INTO document_fts(document_id,notice_id,title,body,identifiers) VALUES(?,?,?,?,?)",
                (document_id, notice_id, title, text, identifier_text),
            )
            return document_id

    def document_search(self, query: str, limit: int = 20):
        with self.connect() as con:
            con.row_factory = sqlite3.Row
            return con.execute(
                """SELECT d.id,d.notice_id,d.title,d.page_count,d.parser,
                a.attachment_url,a.file_name,p.project_name,p.project_no,p.project_stage,
                snippet(document_fts,3,'[',']','…',24) AS snippet,
                bm25(document_fts) AS rank
                FROM document_fts JOIN document_text d ON d.id=document_fts.document_id
                JOIN notice_attachment a ON a.id=d.attachment_id
                JOIN project p ON p.id=d.notice_id
                WHERE document_fts MATCH ? ORDER BY rank LIMIT ?""",
                (query, max(1, int(limit))),
            ).fetchall()

    def attachment_status(self, notice_id: int | None = None):
        sql = """SELECT a.*,d.id AS document_id,d.page_count,d.parser,
        (SELECT group_concat(identifier,' | ') FROM document_identifier i WHERE i.document_id=d.id) identifiers
        FROM notice_attachment a LEFT JOIN document_text d ON d.attachment_id=a.id"""
        params = ()
        if notice_id is not None:
            sql += " WHERE a.notice_id=?"
            params = (notice_id,)
        sql += " ORDER BY a.id DESC"
        with self.connect() as con:
            con.row_factory = sqlite3.Row
            return con.execute(sql, params).fetchall()
