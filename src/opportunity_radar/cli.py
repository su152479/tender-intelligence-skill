import argparse, logging, os
from datetime import datetime
from .ai import OpportunityAnalyzer
from .auth import LoginManager
from .collectors import MockCollector, COLLECTORS
from .config import DATA_DIR, ensure_dirs, load_yaml
from .db import Database
from .logging_config import setup_logging
from .report import generate_daily

log = logging.getLogger(__name__)

def sources(): return load_yaml("sources.yaml")["sources"]
def database(): return Database(DATA_DIR / "radar.sqlite3")

def cmd_init(_):
    ensure_dirs(); database().init(); print(f"初始化完成：{DATA_DIR}")
def cmd_login(args):
    source = next((s for s in sources() if s["id"] == args.source_id), None)
    if not source: raise SystemExit(f"未知数据源：{args.source_id}")
    print(f"登录状态已保存：{LoginManager(DATA_DIR / 'auth').manual_login(source)}")
def cmd_run(args):
    db = database(); db.init(); analyzer = OpportunityAnalyzer(load_yaml("products.yaml")); count = 0
    for source in sources():
        if not source.get("enabled", True): continue
        if args.source and source["id"] not in args.source: continue
        collector = MockCollector(source) if args.mock else COLLECTORS[source["id"]](source)
        started_at = datetime.now(); source_count = 0
        try:
            for project in collector.collect():
                db.upsert(analyzer.apply(project)); count += 1; source_count += 1
            db.record_source_run(source, started_at, "success", source_count)
        except Exception as exc:
            db.record_source_run(source, started_at, "failed", source_count, str(exc))
            log.exception("采集失败 source=%s error=%s", source["id"], exc)
    path = generate_daily(db.recent(), DATA_DIR / "reports")
    print(f"处理 {count} 条公告；日报：{path}")
def cmd_report(_):
    db = database(); db.init(); print(generate_daily(db.recent(), DATA_DIR / "reports"))

def cmd_status(_):
    db = database(); db.init()
    rows = db.source_status()
    if not rows: print("暂无采集运行记录"); return
    for row in rows:
        detail = f"；错误：{row['error']}" if row["error"] else ""
        print(f"{row['source_id']}: {row['status']}，{row['item_count']} 条，{row['finished_at']}{detail}")

def main():
    setup_logging(os.getenv("RADAR_LOG_LEVEL", "INFO"))
    parser = argparse.ArgumentParser(prog="radar", description="工程项目机会雷达 MVP")
    sub = parser.add_subparsers(required=True)
    p = sub.add_parser("init"); p.set_defaults(func=cmd_init)
    p = sub.add_parser("login"); p.add_argument("source_id"); p.set_defaults(func=cmd_login)
    p = sub.add_parser("run"); p.add_argument("--mock", action="store_true", help="使用五个平台的模拟公告"); p.add_argument("--source", action="append", choices=[s["id"] for s in sources()], help="仅运行指定数据源，可重复使用"); p.set_defaults(func=cmd_run)
    p = sub.add_parser("report"); p.set_defaults(func=cmd_report)
    p = sub.add_parser("status"); p.set_defaults(func=cmd_status)
    args = parser.parse_args(); args.func(args)

if __name__ == "__main__": main()
