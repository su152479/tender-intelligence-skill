import argparse, json, logging, os, sys, uuid
from datetime import datetime
from pathlib import Path
from .ai import ANALYZER_VERSION, OpportunityAnalyzer
from .auth import LoginManager
from .collectors import MockCollector, COLLECTORS, collector_is_implemented
from .config import DATA_DIR, PROJECT_ROOT, ensure_dirs, load_yaml
from .db import Database
from .logging_config import setup_logging
from .models import Project
from .obsidian import ObsidianExporter
from .report import generate_daily, write_daily_observation_snapshot
from .business_time import business_now_naive
from .validation import ProjectValidator
from .evaluation import evaluate_fixture
from .documents import AttachmentEnricher, DocumentRetentionManager, fts_query
from .project_entities import ProjectEntityService
from .project_candidates import ProjectCandidateService
from .identity import NoticeIdentityService
from .identity_reviews import IdentityReviewService
from .project_events import ProjectEventService
from .project_integrity import EngineeringProjectRelationCandidateService, direct_unlinked_notices
from .project_lifecycle import ProjectLifecycleAggregator
from . import __version__

log = logging.getLogger(__name__)

def sources():
    configured = load_yaml("sources.yaml")["sources"]
    ids = [source["id"] for source in configured]
    if len(ids) != len(set(ids)):
        raise RuntimeError("sources.yaml 包含重复 source id")
    invalid = [
        source["id"] for source in configured
        if source.get("enabled", True) and not collector_is_implemented(source["id"])
    ]
    if invalid:
        raise RuntimeError(f"启用的数据源尚无真实 Collector：{', '.join(invalid)}")
    return configured


def enabled_sources():
    return [source for source in sources() if source.get("enabled", True)]
def database(): return Database(DATA_DIR / "radar.sqlite3")

def generate_current_report(db: Database):
    summary = db.daily_notice_summary()
    report = generate_daily(summary, DATA_DIR / "reports", db.follow_up_assessments())
    write_daily_observation_snapshot(summary, PROJECT_ROOT / "site" / "data" / "daily-observations.json")
    return report

def cmd_init(_):
    ensure_dirs(); database().init(); print(f"初始化完成：{DATA_DIR}")
def cmd_login(args):
    source = next((s for s in sources() if s["id"] == args.source_id), None)
    if not source: raise SystemExit(f"未知数据源：{args.source_id}")
    if not source.get("enabled", True):
        raise SystemExit(f"数据源尚未启用：{args.source_id}")
    if not str(source.get("login", "none")).startswith("manual"):
        raise SystemExit(f"数据源无需人工登录：{args.source_id}")
    print(f"登录状态已保存：{LoginManager(DATA_DIR / 'auth').manual_login(source)}")
def cmd_run(args):
    db = database(); db.init(); analyzer = OpportunityAnalyzer(load_yaml("products.yaml")); count = 0
    run_id = uuid.uuid4().hex
    log.info("采集批次开始 run_id=%s analyzer=%s rules=%s", run_id, ANALYZER_VERSION, analyzer.rules_version)
    for source in sources():
        if not source.get("enabled", True): continue
        if args.source and source["id"] not in args.source: continue
        collector = MockCollector(source) if args.mock else COLLECTORS[source["id"]](source)
        started_at = business_now_naive(); source_count = 0; collected_count = 0; rejected_count = 0
        source_run_id = db.begin_source_run(
            source, started_at, run_id=run_id,
            analyzer_version=ANALYZER_VERSION, rules_version=analyzer.rules_version,
        )
        try:
            collected = collector.collect()
            collected_count = len(collected)
            valid, rejected = ProjectValidator(source).validate(collected)
            collector.set_funnel(validated_count=len(valid), rejected_count=len(rejected))
            rejected_count = len(rejected)
            if rejected:
                sample = "、".join(
                    f"{item.project.name or '未命名'}（{'/'.join(item.reasons)}）"
                    for item in rejected[:3]
                )
                collector.mark_partial(f"结果校验拒绝 {len(rejected)} 条：{sample}")
                log.warning("结果校验拒绝 source=%s count=%s sample=%s", source["id"], len(rejected), sample)
            for project in valid:
                try:
                    db.upsert_observed(
                        analyzer.apply(project), source_run_id, observed_at=business_now_naive()
                    ); count += 1; source_count += 1
                except Exception as exc:
                    rejected_count += 1
                    collector.mark_partial(f"AI输出校验失败：{project.name}")
                    log.exception("AI输出校验失败 run_id=%s source=%s project=%s error=%s", run_id, source["id"], project.name, exc)
            collector.set_funnel(persisted_count=source_count)
            db.finish_source_run(
                source_run_id, getattr(collector, "run_status", "success"), source_count,
                getattr(collector, "run_error", ""),
                collected_count=collected_count, rejected_count=rejected_count,
                analyzer_version=ANALYZER_VERSION, rules_version=analyzer.rules_version,
                funnel=getattr(collector, "funnel", {}),
            )
        except Exception as exc:
            db.finish_source_run(
                source_run_id, "failed", source_count, str(exc),
                collected_count=collected_count, rejected_count=rejected_count,
                analyzer_version=ANALYZER_VERSION, rules_version=analyzer.rules_version,
                funnel=getattr(collector, "funnel", {}),
            )
            log.exception("采集失败 run_id=%s source=%s error=%s", run_id, source["id"], exc)
    path = generate_current_report(db)
    print(f"批次 {run_id}；处理 {count} 条公告；日报：{path}")
def cmd_report(_):
    db = database(); db.init()
    print(generate_current_report(db))

def cmd_followups(args):
    db = database(); db.init(); rows = db.follow_up_assessments(args.limit)
    if not rows:
        print("暂无后续专项采购跟踪线索"); return
    for row in rows:
        print(
            f"{row['notice_id']} | {row['publish_date'] or '日期未知'} | {row['region'] or '地区未知'} | "
            f"{row['product']} {row['evidence_score']}分 | {row['project_name']} | {row['source_site']}"
        )

def cmd_status(_):
    db = database(); db.init()
    rows = db.source_status()
    if not rows: print("暂无采集运行记录"); return
    for row in rows:
        detail = f"；错误：{row['error']}" if row["error"] else ""
        baseline = (
            f"，近{row['baseline_samples']}次均值 {row['baseline_average']:.2f}"
            if "baseline_samples" in row.keys() and row["baseline_samples"] else ""
        )
        try:
            funnel = json.loads(row["funnel_json"] or "{}")
        except (json.JSONDecodeError, KeyError):
            funnel = {}
        funnel_text = ""
        if funnel:
            funnel_text = (
                f"，漏斗 原始{funnel.get('raw_list_count', 0)}→"
                f"地区/时效{funnel.get('region_recent_count', funnel.get('recent_count', 0))}→"
                f"候选{funnel.get('construction_count', funnel.get('validated_count', 0))}→"
                f"入库{funnel.get('persisted_count', row['item_count'])}"
            )
        print(
            f"{row['source_id']}: {row['status']}/{row['health_status']}，采集 {row['collected_count']} 条，"
            f"入库 {row['item_count']} 条，拒绝 {row['rejected_count']} 条，"
            f"耗时 {row['duration_ms']}ms{baseline}{funnel_text}，批次 {row['run_id'] or '旧记录'}，{row['finished_at']}{detail}"
        )

def cmd_evaluate(args):
    analyzer = OpportunityAnalyzer(load_yaml("products.yaml"))
    result = evaluate_fixture(analyzer, Path(args.fixture))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["schema_pass_rate"] < 1 or result["product_precision"] < args.min_precision or result["product_recall"] < args.min_recall:
        raise SystemExit(1)

def cmd_feedback(args):
    db = database(); db.init()
    feedback_id = db.add_feedback(args.notice_id, args.label, args.product or "", args.note or "")
    print(f"反馈已记录：{feedback_id}；公告 {args.notice_id}；标签 {args.label}")

def cmd_reanalyze(args):
    db = database(); db.init(); analyzer = OpportunityAnalyzer(load_yaml("products.yaml"))
    results = []
    for row in db.all_projects():
        project = Project(
            name=row["project_name"], project_no=row["project_no"] or "",
            publish_date=row["publish_date"] or "", region=row["region"] or "",
            owner=row["owner"] or "", tenderer=row["tenderer"] or "",
            stage=row["project_stage"] or "", construction_content=row["construction_content"] or "",
            source_site=row["source_site"], url=row["url"], raw_text=row["raw_text"] or "",
        )
        analyzed = analyzer.apply(project)
        old_products = json.loads(row["matched_products"] or "[]")
        changed = (
            int(row["ai_score"] or 0) != analyzed.ai_score
            or old_products != analyzed.matched_products
            or row["analyzer_version"] != analyzed.analyzer_version
            or row["rules_version"] != analyzed.rules_version
        )
        results.append((analyzed, changed))
    changed_count = sum(changed for _, changed in results)
    if not args.apply:
        print(
            f"重评预览：共 {len(results)} 条，预计更新 {changed_count} 条；"
            f"分析器 {ANALYZER_VERSION}，规则 {analyzer.rules_version}。使用 --apply 才会写入。"
        )
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = db.backup_to(DATA_DIR / "backups" / f"radar-before-reanalyze-{stamp}.sqlite3")
    for project, _ in results:
        db.upsert(project)
    report = generate_current_report(db)
    print(f"重评完成：{len(results)} 条，更新 {changed_count} 条；备份：{backup}；日报：{report}")

def cmd_enrich(args):
    db = database(); db.init()
    rows = db.projects_for_enrichment(args.project_no or "", args.notice_id, args.limit)
    if not rows:
        print("没有找到待补充文档的公告")
        return
    if (args.extra_page or args.attachment_url) and len(rows) != 1:
        raise SystemExit("--extra-page/--attachment-url 只能与唯一的 --notice-id 或 --project-no 结果一起使用")
    enricher = AttachmentEnricher(db)
    totals = {"projects": 0, "discovered": 0, "parsed": 0, "skipped": 0, "failed": 0}
    for row in rows:
        result = enricher.enrich_project(row, args.extra_page or [], args.attachment_url or [])
        totals["projects"] += 1
        for key in ("discovered", "parsed", "skipped", "failed"):
            totals[key] += result[key]
        print(
            f"公告 {row['id']} | {row['project_name']} | "
            f"发现 {result['discovered']}，解析 {result['parsed']}，跳过 {result['skipped']}，失败 {result['failed']}"
        )
    print(
        f"文档补充完成：公告 {totals['projects']}，发现附件 {totals['discovered']}，"
        f"解析 {totals['parsed']}，跳过 {totals['skipped']}，失败 {totals['failed']}"
    )

def cmd_documents(args):
    db = database(); db.init()
    rows = db.attachment_status(args.notice_id)
    if not rows:
        print("暂无附件记录")
        return
    for row in rows[: args.limit]:
        print(
            f"{row['id']} | 公告 {row['notice_id']} | {row['status']} | "
            f"{row['file_name']} | {row['page_count'] or 0}页 | {row['identifiers'] or '未提取编号'} | "
            f"{row['attachment_url']}"
        )

def cmd_document_search(args):
    db = database(); db.init()
    query = fts_query(args.query)
    if not query:
        raise SystemExit("请输入有效检索词")
    rows = db.document_search(query, args.limit)
    if not rows:
        print("全文库中未找到匹配文档")
        return
    for row in rows:
        print(
            f"公告 {row['notice_id']} | {row['project_name']} | {row['file_name']} | "
            f"{row['page_count']}页 | {row['snippet']} | {row['attachment_url']}"
        )

def cmd_cleanup_documents(args):
    db = database(); db.init()
    days = args.retention_days
    manager = DocumentRetentionManager(db)
    result = manager.cleanup(days, args.dry_run)
    action = "清理预览" if args.dry_run else "清理完成"
    print(
        f"附件{action}：保留 {days} 天，到期 {result['eligible']}，删除 {result['deleted']}，"
        f"文件已缺失 {result['missing']}，拒绝越界 {result['refused']}，失败 {result['failed']}；"
        "正文索引未删除"
    )

def cmd_export_obsidian(args):
    result = ObsidianExporter(DATA_DIR / "radar.sqlite3", Path(args.vault)).export()
    print(json.dumps(result, ensure_ascii=False))

def cmd_build_projects(args):
    service = ProjectEntityService(database())
    result = service.build()
    result["identifier_analysis"] = service.identifier_analysis()
    result["multi_notice_samples"] = service.multi_notice_samples(args.samples)
    print(json.dumps(result, ensure_ascii=False, indent=2))

def cmd_confirm_project(args):
    service = ProjectEntityService(database())
    entity_id = service.confirm_notices(
        args.notice_id,
        canonical_name=args.canonical_name or "",
        engineering_project_id=args.engineering_project_id,
    )
    print(f"人工关联完成：engineering_project {entity_id}；公告 {sorted(set(args.notice_id))}")

def cmd_build_project_candidates(args):
    service = ProjectCandidateService(database())
    result = service.build()
    result["top_candidates"] = service.list_candidates("PENDING", args.samples)
    result["historical_duplicate_analysis"] = service.historical_duplicate_analysis()
    print(json.dumps(result, ensure_ascii=False, indent=2))

def cmd_project_candidates(args):
    service = ProjectCandidateService(database())
    print(json.dumps(service.list_candidates(args.status, args.limit), ensure_ascii=False, indent=2))

def cmd_review_project_candidate(args):
    action = "CONFIRM" if args.confirm else "REJECT" if args.reject else "DEFER"
    result = ProjectCandidateService(database()).review(args.candidate_id, action, args.note)
    print(json.dumps(result, ensure_ascii=False, indent=2))

def cmd_project_identity_audit(args):
    print(json.dumps(ProjectCandidateService(database()).identity_audit(), ensure_ascii=False, indent=2))

def cmd_build_identity_facts(_):
    service = NoticeIdentityService(database())
    result = service.extract_all()
    result["difference_report"] = service.difference_report()
    print(json.dumps(result, ensure_ascii=False, indent=2))

def cmd_identity_facts(args):
    service = NoticeIdentityService(database())
    service.extract_notice(args.notice_id)
    print(json.dumps(service.facts(args.notice_id, active_only=not args.all_versions), ensure_ascii=False, indent=2))

def cmd_identity_diff(_):
    service = NoticeIdentityService(database())
    service.extract_all()
    print(json.dumps(service.difference_report(), ensure_ascii=False, indent=2))

def cmd_identity_review_queue(args):
    service = IdentityReviewService(database())
    result = {
        "statistics": service.queue_statistics(),
        "items": service.queue(fact_type=args.fact_type or "", priority=args.priority or "", limit=args.limit),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

def cmd_identity_review(args):
    print(json.dumps(IdentityReviewService(database()).review_detail(args.fact_id), ensure_ascii=False, indent=2))

def cmd_review_identity_fact(args):
    decision = "CONFIRM" if args.confirm else "REJECT"
    result = IdentityReviewService(database()).review_fact(
        args.fact_id, decision, note=args.note, reviewer=args.reviewer, dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))

def cmd_override_identity(args):
    result = IdentityReviewService(database()).override(
        args.notice_id, args.fact_type, args.value, note=args.note, reviewer=args.reviewer,
        identifier_type=args.identifier_type, identifier_namespace=args.namespace,
        identity_strength=args.strength, dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))

def cmd_clear_identity(args):
    result = IdentityReviewService(database()).clear(
        args.notice_id, args.fact_type, note=args.note, reviewer=args.reviewer, dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))

def cmd_identity_review_status(args):
    service = IdentityReviewService(database())
    print(json.dumps({
        "statistics": service.queue_statistics(),
        "formal_relation_conflicts": service.formal_relation_conflicts(),
        "review_history": service.review_history(args.notice_id),
    }, ensure_ascii=False, indent=2))

def cmd_project_clusters(args):
    clusters = ProjectCandidateService(database()).clusters()
    if args.limit:
        clusters = clusters[:args.limit]
    print(json.dumps(clusters, ensure_ascii=False, indent=2))

def cmd_review_project_cluster(args):
    service = ProjectCandidateService(database())
    if not any((args.confirm_notices, args.confirm_parent_candidates, args.reject_candidates)):
        result = service.get_cluster(args.cluster_id)
    else:
        result = service.review_cluster(
            args.cluster_id,
            confirm_notices=args.confirm_notices,
            confirm_parent_candidates=args.confirm_parent_candidates,
            reject_candidates=args.reject_candidates,
            note=args.note,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))

def cmd_build_project_events(_):
    print(json.dumps(ProjectEventService(database()).build(), ensure_ascii=False, indent=2))

def cmd_project_timeline(args):
    timeline = ProjectEventService(database()).timeline(
        args.engineering_project_id, show_sources=args.show_sources
    )
    project = timeline["project"]
    print(f"工程 {project['id']} | {project['canonical_name']}")
    if not timeline["events"]:
        print("暂无已确认事实事件")
        return
    for event in timeline["events"]:
        scope = event["event_scope"] or "全工程/范围未提取"
        party = event["party_name"] or "相关单位未提取"
        product = f" | 产品 {event['product']}" if event["product"] else ""
        print(
            f"{event['event_date'] or '日期未知'} [{event['event_date_source']}] | "
            f"{event['event_type']} | {scope} | {party}{product} | "
            f"Sources: {event['source_count']} | "
            f"公告 {event['notice_id']} {event['notice_name']} | 证据：{event['evidence_text']}"
        )
        if args.show_sources:
            for source in event["sources"]:
                print(
                    f"  - 来源事件 {source['project_event_id']} | 公告 {source['notice_id']} | "
                    f"{source['source_name']} | {source['event_date']} [{source['event_date_source']}] | "
                    f"{source['source_url']} | 证据：{source['evidence_text']}"
                )

def cmd_project_relation_candidates(args):
    service = EngineeringProjectRelationCandidateService(database())
    rows = service.candidates(args.min_score)
    if args.project_id:
        rows = [row for row in rows if args.project_id in {row["project_id_a"], row["project_id_b"]}]
    print(json.dumps(rows[:args.limit], ensure_ascii=False, indent=2))

def cmd_project_event_integrity(_):
    db = database(); db.init()
    print(json.dumps({
        "event_equivalence": ProjectEventService(db).equivalence_statistics(),
        "project_relation_candidates": EngineeringProjectRelationCandidateService(db).candidates(),
        "direct_unlinked_notices": direct_unlinked_notices(db),
    }, ensure_ascii=False, indent=2))

def cmd_project_lifecycle(args):
    service = ProjectLifecycleAggregator(database())
    result = service.lifecycle(args.engineering_project_id)
    print(f"工程 {result['engineering_project_id']} | {result['project_name']}")
    print(f"Current Observed Stage: {result['current_stage']}")
    print(f"Previous Stage: {result['previous_stage']}")
    period = result["stage_since"] or "日期未知"
    if result["stage_since_min"] and result["stage_since_min"] != result["stage_since_max"]:
        period = f"{result['stage_since_min']} 至 {result['stage_since_max']}"
    print(f"Stage Since: {period}")
    print(f"Coverage: {result['coverage_scope']} | {result['scope_level']}")
    if result["coverage_detail"]:
        print("Coverage Detail: " + "；".join(result["coverage_detail"]))
    print(f"Confidence: {result['stage_confidence']}（规则可信等级，不是概率）")
    print("Reason: " + result["stage_reason"])
    print(f"Evidence: {len(result['stage_evidence'])} Canonical Event(s)")
    for event in result["stage_evidence"]:
        print(
            f"  - {event['event_date'] or '日期未知'} | {event['event_type']} | "
            f"{event['event_scope'] or '范围未提取'} | {event['party_name'] or '相关单位未提取'} | "
            f"Sources: {event['source_count']} | {event['evidence_text']}"
        )
    if result["consistency_conflicts"]:
        print("Consistency Conflicts:")
        for conflict in result["consistency_conflicts"]:
            print(f"  - {conflict['code']} | {conflict['reason']}")
    if args.show_events:
        events = ProjectEventService(database()).canonical_events(
            args.engineering_project_id, read_only=True,
        )
        print("Canonical Event Timeline:")
        for event in events:
            print(
                f"  - {event['event_date'] or '日期未知'} | {event['event_type']} | "
                f"{event['event_scope'] or '范围未提取'} | Sources: {event['source_count']}"
            )
            for source in event["sources"]:
                print(
                    f"      Source Event {source['project_event_id']} | Notice {source['notice_id']} | "
                    f"{source['source_name']} | {source['source_url']} | {source['evidence_text']}"
                )

def cmd_project_lifecycle_audit(args):
    print(json.dumps(
        ProjectLifecycleAggregator(database()).audit(sample_limit=args.samples),
        ensure_ascii=False, indent=2,
    ))

def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    setup_logging(os.getenv("RADAR_LOG_LEVEL", "INFO"))
    parser = argparse.ArgumentParser(prog="radar", description="工程项目机会雷达 MVP")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(required=True)
    p = sub.add_parser("init"); p.set_defaults(func=cmd_init)
    p = sub.add_parser("login"); p.add_argument("source_id"); p.set_defaults(func=cmd_login)
    p = sub.add_parser("run"); p.add_argument("--mock", action="store_true", help="使用已启用来源的模拟公告"); p.add_argument("--source", action="append", choices=[s["id"] for s in enabled_sources()], help="仅运行指定且已启用的数据源，可重复使用"); p.set_defaults(func=cmd_run)
    p = sub.add_parser("report"); p.set_defaults(func=cmd_report)
    p = sub.add_parser("status"); p.set_defaults(func=cmd_status)
    p = sub.add_parser("followups", help="查看工程需要但等待后续专项采购的线索")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_followups)
    p = sub.add_parser("evaluate", help="运行人工标注的机会识别回归评测")
    p.add_argument("--fixture", default=str(Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "opportunity_cases.json"))
    p.add_argument("--min-precision", type=float, default=0.85)
    p.add_argument("--min-recall", type=float, default=0.85)
    p.set_defaults(func=cmd_evaluate)
    p = sub.add_parser("feedback", help="记录人工业务判断，供困难样本和规则回归使用")
    p.add_argument("notice_id", type=int, help="project 表中的公告 ID")
    p.add_argument("--label", required=True, choices=[
        "WORTH_TRACKING", "NOT_RELEVANT", "MISSED_PRODUCT", "WRONG_PRODUCT", "DUPLICATE",
    ])
    p.add_argument("--product", default="")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_feedback)
    p = sub.add_parser("reanalyze", help="用当前规则重评历史公告；默认仅预览")
    p.add_argument("--apply", action="store_true", help="备份数据库后写入重评结果")
    p.set_defaults(func=cmd_reanalyze)
    p = sub.add_parser("enrich", help="发现并解析公告附件，写入本地全文索引")
    p.add_argument("--project-no", default="", help="只处理指定交易/项目编号")
    p.add_argument("--notice-id", type=int, help="只处理指定公告 ID")
    p.add_argument("--limit", type=int, default=20, help="最多处理的公告数")
    p.add_argument("--extra-page", action="append", default=[], help="补充一个同项目官方资料页，可重复")
    p.add_argument("--attachment-url", action="append", default=[], help="补充一个已确认的官方附件直链，可重复")
    p.set_defaults(func=cmd_enrich)
    p = sub.add_parser("documents", help="查看已发现附件及解析状态")
    p.add_argument("--notice-id", type=int)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_documents)
    p = sub.add_parser("document-search", help="搜索附件全文和工程编号")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_document_search)
    p = sub.add_parser("cleanup-documents", help="删除到期附件原文件并保留正文全文索引")
    p.add_argument(
        "--retention-days", type=int,
        default=int(os.getenv("RADAR_DOCUMENT_RETENTION_DAYS", "2")),
        help="附件原文件保留天数，默认读取 RADAR_DOCUMENT_RETENTION_DAYS",
    )
    p.add_argument("--dry-run", action="store_true", help="只显示到期数量，不删除")
    p.set_defaults(func=cmd_cleanup_documents)
    p = sub.add_parser("export-obsidian", help="只读导出为 Obsidian 本地知识库")
    p.add_argument("--vault", required=True, help="Obsidian Vault 目录")
    p.set_defaults(func=cmd_export_obsidian)
    p = sub.add_parser("build-projects", help="建立高可信工程实体和公告关联")
    p.add_argument("--samples", type=int, default=10, help="输出多公告项目审核样本数")
    p.set_defaults(func=cmd_build_projects)
    p = sub.add_parser("confirm-project", help="人工确认多个公告属于同一工程")
    p.add_argument("--notice-id", action="append", type=int, required=True, help="公告ID，可重复")
    p.add_argument("--engineering-project-id", type=int, help="关联到已有工程实体")
    p.add_argument("--canonical-name", default="", help="人工确认的工程规范名称")
    p.set_defaults(func=cmd_confirm_project)
    p = sub.add_parser("build-project-candidates", help="生成只供人工审核的工程关联候选")
    p.add_argument("--samples", type=int, default=20, help="输出最高分候选数量")
    p.set_defaults(func=cmd_build_project_candidates)
    p = sub.add_parser("project-candidates", help="查看工程关联候选队列")
    p.add_argument("--status", default="PENDING", choices=["PENDING", "CONFIRMED", "REJECTED", "DEFERRED"])
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_project_candidates)
    p = sub.add_parser("review-project-candidate", help="人工审核工程关联候选")
    p.add_argument("candidate_id", type=int)
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument("--confirm", action="store_true")
    action.add_argument("--reject", action="store_true")
    action.add_argument("--defer", action="store_true")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_review_project_candidate)
    p = sub.add_parser("project-identity-audit", help="审计工程身份字段的来源和可靠性")
    p.set_defaults(func=cmd_project_identity_audit)
    p = sub.add_parser("build-identity-facts", help="为公告建立持久化、可审计的身份事实")
    p.set_defaults(func=cmd_build_identity_facts)
    p = sub.add_parser("identity-facts", help="查看一条公告的身份事实及来源")
    p.add_argument("notice_id", type=int)
    p.add_argument("--all-versions", action="store_true", help="包含历史Extractor版本")
    p.set_defaults(func=cmd_identity_facts)
    p = sub.add_parser("identity-diff", help="比较旧运行时解析与当前身份事实摘要")
    p.set_defaults(func=cmd_identity_diff)
    p = sub.add_parser("identity-review-queue", help="查看影响工程关联的身份事实人工审核队列")
    p.add_argument("--fact-type", default="")
    p.add_argument("--priority", choices=["P0", "P1", "P2", "P3"])
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_identity_review_queue)
    p = sub.add_parser("identity-review", help="查看身份事实上下文、审核历史和候选影响")
    p.add_argument("fact_id", type=int)
    p.set_defaults(func=cmd_identity_review)
    p = sub.add_parser("review-identity-fact", help="确认或否定机器身份事实")
    p.add_argument("fact_id", type=int)
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument("--confirm", action="store_true")
    action.add_argument("--reject", action="store_true")
    p.add_argument("--note", default="")
    p.add_argument("--reviewer", default="LOCAL_USER")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_review_identity_fact)
    p = sub.add_parser("override-identity", help="用人工高可信事实覆盖当前身份字段")
    p.add_argument("--notice-id", type=int, required=True)
    p.add_argument("--fact-type", required=True)
    p.add_argument("--value", required=True)
    p.add_argument("--identifier-type", default="")
    p.add_argument("--namespace", default="")
    p.add_argument("--strength", default="")
    p.add_argument("--note", default="")
    p.add_argument("--reviewer", default="LOCAL_USER")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_override_identity)
    p = sub.add_parser("clear-identity", help="人工声明当前无法确定某个身份字段")
    p.add_argument("--notice-id", type=int, required=True)
    p.add_argument("--fact-type", required=True)
    p.add_argument("--note", default="")
    p.add_argument("--reviewer", default="LOCAL_USER")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_clear_identity)
    p = sub.add_parser("identity-review-status", help="查看审核统计、历史和正式关系冲突")
    p.add_argument("--notice-id", type=int)
    p.set_defaults(func=cmd_identity_review_status)
    p = sub.add_parser("project-clusters", help="动态查看活动工程候选簇和内部关系图")
    p.add_argument("--limit", type=int, default=0, help="限制簇数量，0表示全部")
    p.set_defaults(func=cmd_project_clusters)
    p = sub.add_parser("review-project-cluster", help="查看或安全审核一个工程候选簇")
    p.add_argument("cluster_id", type=int, help="project-clusters显示的稳定簇ID")
    action = p.add_mutually_exclusive_group()
    action.add_argument("--confirm-notices", nargs="+", type=int, help="明确确认属于同一工程的公告子集")
    action.add_argument("--confirm-parent-candidates", nargs="+", type=int, help="确认SAME_PARENT_PROJECT候选边，不建立正式link")
    action.add_argument("--reject-candidates", nargs="+", type=int, help="明确拒绝的候选边ID")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_review_project_cluster)
    p = sub.add_parser("build-project-events", help="从正式关联公告构建可审计的工程事实事件")
    p.set_defaults(func=cmd_build_project_events)
    p = sub.add_parser("project-timeline", help="查看工程实体的事实事件时间轴")
    p.add_argument("engineering_project_id", type=int)
    p.add_argument("--show-sources", action="store_true", help="显示Canonical Event的全部来源公告")
    p.set_defaults(func=cmd_project_timeline)
    p = sub.add_parser("project-relation-candidates", help="只读查看工程实体之间的关系候选，不执行合并")
    p.add_argument("--project-id", type=int, help="只看涉及指定工程实体的候选")
    p.add_argument("--min-score", type=int, default=60)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_project_relation_candidates)
    p = sub.add_parser("project-event-integrity", help="查看事件等价、工程关系候选及未关联DIRECT公告")
    p.set_defaults(func=cmd_project_event_integrity)
    p = sub.add_parser("project-lifecycle", help="只读推导工程当前已观察生命周期阶段")
    p.add_argument("engineering_project_id", type=int)
    p.add_argument("--show-events", action="store_true", help="展开Canonical Event及全部Source Event")
    p.set_defaults(func=cmd_project_lifecycle)
    p = sub.add_parser("project-lifecycle-audit", help="只读统计全库生命周期分布和一致性冲突")
    p.add_argument("--samples", type=int, default=20, help="输出真实工程抽样数量")
    p.set_defaults(func=cmd_project_lifecycle_audit)
    args = parser.parse_args(); args.func(args)

if __name__ == "__main__": main()
