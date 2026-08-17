import json, sqlite3
from datetime import datetime
from pathlib import Path
from .models import Project

PROJECT_SCHEMA = """CREATE TABLE IF NOT EXISTS project (
 id INTEGER PRIMARY KEY AUTOINCREMENT, project_name TEXT NOT NULL, project_no TEXT,
 publish_date TEXT, region TEXT, owner TEXT, tenderer TEXT, project_stage TEXT,
 construction_content TEXT, source_site TEXT NOT NULL, url TEXT NOT NULL,
 raw_text TEXT, ai_score INTEGER DEFAULT 0, matched_products TEXT DEFAULT '[]',
 analysis_json TEXT DEFAULT '{}', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(source_site, url));"""
SOURCE_RUN_SCHEMA = """CREATE TABLE IF NOT EXISTS source_run (
 id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, source_name TEXT NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT NOT NULL, status TEXT NOT NULL,
 item_count INTEGER DEFAULT 0, error TEXT DEFAULT '');"""

class Database:
    def __init__(self, path: Path): self.path = path
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.path)
    def init(self):
        with self.connect() as con:
            con.execute(PROJECT_SCHEMA)
            con.execute(SOURCE_RUN_SCHEMA)
    def upsert(self, p: Project):
        values = (p.name,p.project_no,p.publish_date,p.region,p.owner,p.tenderer,p.stage,p.construction_content,p.source_site,p.url,p.raw_text,p.ai_score,json.dumps(p.matched_products,ensure_ascii=False),p.analysis_json)
        sql = """INSERT INTO project(project_name,project_no,publish_date,region,owner,tenderer,project_stage,construction_content,source_site,url,raw_text,ai_score,matched_products,analysis_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_site,url) DO UPDATE SET
        project_name=excluded.project_name, project_no=excluded.project_no, publish_date=excluded.publish_date,
        region=excluded.region, owner=excluded.owner, tenderer=excluded.tenderer,
        project_stage=excluded.project_stage, construction_content=excluded.construction_content,
        raw_text=excluded.raw_text, ai_score=excluded.ai_score,
        matched_products=excluded.matched_products, analysis_json=excluded.analysis_json"""
        with self.connect() as con: con.execute(sql, values)
    def record_source_run(self, source: dict, started_at: datetime, status: str, item_count: int = 0, error: str = ""):
        values = (source["id"], source["name"], started_at.isoformat(timespec="seconds"), datetime.now().isoformat(timespec="seconds"), status, item_count, error[:1000])
        with self.connect() as con:
            con.execute("INSERT INTO source_run(source_id,source_name,started_at,finished_at,status,item_count,error) VALUES(?,?,?,?,?,?,?)", values)
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
