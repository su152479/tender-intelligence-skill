import json
from datetime import date, datetime, timedelta
from pathlib import Path
import requests
from opportunity_radar.ai import OpportunityAnalyzer
from opportunity_radar.db import Database
from opportunity_radar.models import Project
from opportunity_radar.collectors.cccc import CCCCCollector
from opportunity_radar.collectors.browser_resilience import (
    DiagnosticRecorder, LocatorCandidate, ResponseOutcome, SelectorRegistry,
    classify_document,
)
from opportunity_radar.collectors.luban import CRECGLubanCollector
from opportunity_radar.collectors.regions import infer_region, is_jing_jin_ji
from opportunity_radar.collectors.yzw import CSCECYunZhuCollector
from opportunity_radar.collectors.beijing_ggzy import BeijingGGZYCollector
from opportunity_radar.collectors.ccgp import CCGPCollector
from opportunity_radar.collectors.construction import is_construction_tender
from opportunity_radar.collectors.search_terms import configured_search_terms
from opportunity_radar.collectors.http import (
    HttpCache, PublicPageClient, SourceCircuitBreaker, SourceCircuitOpen,
)
from opportunity_radar.validation import ProjectValidator
from opportunity_radar.config import load_yaml
from opportunity_radar.evaluation import evaluate_fixture
from opportunity_radar.models import OpportunityAnalysis
from opportunity_radar.source_health import assess_source_health
from opportunity_radar.documents import (
    AttachmentEnricher, DocumentRetentionManager, extract_identifiers, fts_query,
)
from opportunity_radar.obsidian import ObsidianExporter, AUTO_END
from opportunity_radar.report import generate_daily, write_daily_observation_snapshot
from opportunity_radar.project_entities import ProjectEntityService, canonicalize_project_name
from pydantic import ValidationError

CFG = {"products": [{"name":"管片","keywords":["盾构","管片"],"project_types":["轨道交通"],"score":35}], "rules":{"title_keyword_bonus":20,"content_keyword_bonus":10,"max_score":100}}

def test_obsidian_export_is_private_and_preserves_manual_notes(tmp_path):
    database_path = tmp_path / "radar.db"
    db = Database(database_path); db.init()
    project = OpportunityAnalyzer(CFG).apply(Project(
        name="地铁管片采购", publish_date=date.today().isoformat(), region="北京市",
        source_site="测试来源", url="https://example.test/obsidian",
        raw_text="采购盾构管片 PRIVATE_COOKIE=must-not-export",
    ))
    db.upsert(project)
    vault = tmp_path / "vault"
    result = ObsidianExporter(database_path, vault).export()
    note = vault / "项目" / "项目-0001.md"
    assert result["projects"] == 1
    assert "PRIVATE_COOKIE" not in note.read_text(encoding="utf-8")
    note.write_text(note.read_text(encoding="utf-8") + "人工备注：下周联系。\n", encoding="utf-8")
    ObsidianExporter(database_path, vault).export()
    content = note.read_text(encoding="utf-8")
    assert content.count(AUTO_END) == 1
    assert "人工备注：下周联系。" in content
    assert (vault / "00 雷达首页.md").exists()
    assert (vault / ".obsidian" / "app.json").exists()

def test_analysis_json_contract():
    result = OpportunityAnalyzer(CFG).analyze(Project(name="盾构管片采购", raw_text="地铁盾构区间管片"))
    assert result["项目类型"] == "轨道交通"
    assert result["潜在预制产品"] == ["管片"]
    assert 0 <= result["机会评分0-100"] <= 100
    assert result["证据等级"] == "直接产品证据"
    assert result["分析器版本"] == "opportunity-analysis-v7.2"
    assert result["规则版本"].startswith("products-")
    assert result["命中证据"][0]["证据位置"] == ["项目名称", "施工方法"]
    assert result["命中证据"][0]["需求意图"] == "采购"
    assert result["产品证据分"]["管片"] == result["机会评分0-100"]
    assert result["项目线索分0-100"] >= result["当前商机分0-100"]
    assert result["收录类型"] == "当前直接商机"
    assert result["本次采购对象"]

def test_analysis_contract_rejects_score_out_of_range():
    payload = {
        "项目类型": "桥梁", "施工方向": "桥梁工程", "潜在预制产品": ["箱梁"],
        "匹配理由": "命中桥梁", "机会评分0-100": 101, "证据等级": "工程方向推断",
        "置信度": "中", "命中证据": [], "分析器版本": "v", "规则版本": "r",
    }
    try:
        OpportunityAnalysis.model_validate(payload)
        assert False, "评分超过 100 应被拒绝"
    except ValidationError:
        pass

def test_generic_precast_component_is_direct_pc_evidence():
    cfg = {
        "products": [{
            "name": "PC构件",
            "direct_keywords": ["PC构件", "预制构件", "小型预制构件", "预制混凝土构件"],
            "project_types": ["装配式建筑"],
        }],
        "rules": {"direct_product_score": 90, "title_evidence_bonus": 3, "max_score": 100},
    }
    result = OpportunityAnalyzer(cfg).analyze(Project(name="高速公路小型预制构件工程"))
    assert result["潜在预制产品"] == ["PC构件"]
    assert result["机会评分0-100"] <= 35
    assert result["项目线索分0-100"] >= 85
    assert result["收录类型"] == "前置项目线索"

def test_database_upsert(tmp_path):
    db = Database(tmp_path / "test.db"); db.init()
    p = OpportunityAnalyzer(CFG).apply(Project(name="管片采购", source_site="测试", url="https://example.test/1", raw_text="盾构管片"))
    db.upsert(p); db.upsert(p)
    rows = db.recent()
    assert len(rows) == 1
    assert json.loads(rows[0]["matched_products"]) == ["管片"]
    assert rows[0]["analyzer_version"] == "opportunity-analysis-v7.2"
    assessments = db.notice_product_assessments(rows[0]["id"])
    assert len(assessments) == 1
    assert assessments[0]["product"] == "管片"
    assert assessments[0]["demand_status"] == "CONFIRMED"
    assert assessments[0]["scope_status"] == "IN_SCOPE"
    assert assessments[0]["product_demand_evidence_level"] == "STRONG"
    assert assessments[0]["product_demand_evidence_type"] == "DIRECT_TARGET_PRODUCT"
    assert assessments[0]["procurement_window_status"] == "OPEN"
    assert json.loads(assessments[0]["procurement_window_evidence"])
    db.record_source_run({"id": "test", "name": "测试"}, datetime.now(), "success", 1,
                         run_id="run-1", collected_count=2, rejected_count=1)
    assert db.source_status()[0]["item_count"] == 1
    assert db.source_status()[0]["run_id"] == "run-1"
    assert db.source_status()[0]["rejected_count"] == 1


def _begin_test_run(db, source_id="source-a", source_name="来源A", batch="batch-1"):
    return db.begin_source_run(
        {"id": source_id, "name": source_name}, datetime.now(), run_id=batch,
        analyzer_version="test-analyzer", rules_version="test-rules",
    )


def test_notice_observation_first_repeat_and_business_change(tmp_path):
    db = Database(tmp_path / "observations.db"); db.init()
    first_run = _begin_test_run(db)
    notice = Project(
        name="管片采购公告", source_site="来源A", url="https://example.test/notices/1",
        raw_text="本次采购盾构管片。",
    )
    notice_id = db.upsert_observed(notice, first_run)
    first = db.notice_observations(notice_id)[0]
    assert first["is_first_seen"] == 1
    assert first["content_changed"] == 0

    second_run = _begin_test_run(db, batch="batch-2")
    db.upsert_observed(notice, second_run)
    second = db.notice_observations(notice_id)[1]
    assert second["is_first_seen"] == 0
    assert second["content_changed"] == 0

    third_run = _begin_test_run(db, batch="batch-3")
    changed = Project(
        name=notice.name, source_site=notice.source_site, url=notice.url,
        raw_text="本次采购盾构管片，并增加运输服务。",
    )
    db.upsert_observed(changed, third_run)
    third = db.notice_observations(notice_id)[2]
    assert third["is_first_seen"] == 0
    assert third["content_changed"] == 1
    assert len({first["content_hash"], second["content_hash"]}) == 1
    assert third["content_hash"] != second["content_hash"]


def test_notice_observation_is_idempotent_within_one_run(tmp_path):
    db = Database(tmp_path / "observations.db"); db.init()
    source_run_id = _begin_test_run(db)
    notice = Project(
        name="箱梁预制公告", source_site="来源A", url="https://example.test/notices/2",
        raw_text="箱梁预制。",
    )
    notice_id = db.upsert_observed(notice, source_run_id)
    assert db.upsert_observed(notice, source_run_id) == notice_id
    assert len(db.notice_observations(notice_id)) == 1


def test_notice_observation_keeps_sources_and_notices_isolated(tmp_path):
    db = Database(tmp_path / "observations.db"); db.init()
    run_a = _begin_test_run(db, "source-a", "来源A", "batch-a")
    run_b = _begin_test_run(db, "source-b", "来源B", "batch-b")
    notice_a = Project(
        name="同名公告", source_site="来源A", url="https://example.test/shared", raw_text="内容A",
    )
    notice_b = Project(
        name="同名公告", source_site="来源B", url="https://example.test/shared", raw_text="内容B",
    )
    notice_a_id = db.upsert_observed(notice_a, run_a)
    notice_b_id = db.upsert_observed(notice_b, run_b)
    assert notice_a_id != notice_b_id
    rows = db.notice_observations()
    assert [(row["run_id"], row["notice_id"], row["source_id"]) for row in rows] == [
        (run_a, notice_a_id, "source-a"), (run_b, notice_b_id, "source-b"),
    ]
    try:
        db.upsert_observed(notice_b, run_a)
        assert False, "不同来源公告不得关联到错误的 source_run"
    except ValueError as exc:
        assert "来源与采集运行不一致" in str(exc)


def test_notice_observation_ignores_presentation_only_changes(tmp_path):
    db = Database(tmp_path / "observations.db"); db.init()
    run_1 = _begin_test_run(db, batch="batch-1")
    first = Project(
        name="PC构件采购", source_site="来源A", url="https://example.test/notices/3",
        raw_text="<div class='random-123'>采购   PC构件</div>\n更新时间：2026-09-04 10:00:00",
    )
    notice_id = db.upsert_observed(first, run_1)
    run_2 = _begin_test_run(db, batch="batch-2")
    formatted = Project(
        name="PC构件采购", source_site="来源A", url=first.url,
        raw_text="<section data-random='999'>\n  采购 PC构件\n</section>\n浏览次数：12345",
    )
    db.upsert_observed(formatted, run_2)
    rows = db.notice_observations(notice_id)
    assert rows[0]["content_hash"] == rows[1]["content_hash"]
    assert rows[1]["content_changed"] == 0


def test_preexisting_notice_without_observation_is_not_first_seen(tmp_path):
    db = Database(tmp_path / "observations.db"); db.init()
    notice = Project(
        name="历史公告", source_site="来源A", url="https://example.test/notices/historical",
        raw_text="历史正文",
    )
    notice_id = db.upsert(notice)
    assert db.notice_observations(notice_id) == []
    source_run_id = _begin_test_run(db, batch="new-version-run")
    db.upsert_observed(notice, source_run_id)
    row = db.notice_observations(notice_id)[0]
    assert row["is_first_seen"] == 0
    assert row["content_changed"] == 0


def _finish_test_run(db, source_run_id, status="success"):
    db.finish_source_run(source_run_id, status, 1, collected_count=1)


def test_daily_summary_classifies_new_repeat_and_update_from_completed_runs(tmp_path):
    db = Database(tmp_path / "daily.db"); db.init()
    notice = Project(
        name="盾构管片采购", source_site="来源A", url="https://example.test/daily/1",
        raw_text="采购盾构管片。",
    )
    run_1 = _begin_test_run(db, batch="day-1")
    notice_id = db.upsert_observed(notice, run_1, datetime(2026, 9, 1, 9))
    _finish_test_run(db, run_1)
    assert [row["id"] for row in db.daily_notice_summary("2026-09-01")["new"]] == [notice_id]

    run_2 = _begin_test_run(db, batch="day-2")
    db.upsert_observed(notice, run_2, datetime(2026, 9, 2, 9))
    _finish_test_run(db, run_2)
    day_2 = db.daily_notice_summary("2026-09-02")
    assert day_2["new"] == []
    assert [row["id"] for row in day_2["seen_again"]] == [notice_id]

    run_3 = _begin_test_run(db, batch="day-3")
    changed = Project(
        name=notice.name, source_site=notice.source_site, url=notice.url,
        raw_text="采购盾构管片，并增加运输服务。",
    )
    db.upsert_observed(changed, run_3, datetime(2026, 9, 3, 9))
    _finish_test_run(db, run_3)
    day_3 = db.daily_notice_summary("2026-09-03")
    assert day_3["new"] == []
    assert [row["id"] for row in day_3["updated"]] == [notice_id]


def test_daily_summary_does_not_treat_preexisting_notice_as_new(tmp_path):
    db = Database(tmp_path / "daily.db"); db.init()
    notice = Project(
        name="上线前历史公告", source_site="来源A", url="https://example.test/daily/history",
        raw_text="历史正文",
    )
    notice_id = db.upsert(notice)
    run = _begin_test_run(db, batch="first-observation")
    db.upsert_observed(notice, run, datetime(2026, 9, 4, 10))
    _finish_test_run(db, run)
    summary = db.daily_notice_summary("2026-09-04")
    assert summary["new"] == []
    assert [row["id"] for row in summary["seen_again"]] == [notice_id]


def test_daily_summary_deduplicates_same_day_and_keeps_new_after_later_update(tmp_path):
    db = Database(tmp_path / "daily.db"); db.init()
    notice = Project(
        name="PC构件供应", source_site="来源A", url="https://example.test/daily/multi",
        raw_text="供应PC构件。",
    )
    morning = _begin_test_run(db, batch="morning")
    notice_id = db.upsert_observed(notice, morning, datetime(2026, 9, 5, 9))
    _finish_test_run(db, morning)
    noon = _begin_test_run(db, batch="noon")
    db.upsert_observed(notice, noon, datetime(2026, 9, 5, 12))
    _finish_test_run(db, noon)
    evening = _begin_test_run(db, batch="evening")
    changed = Project(
        name=notice.name, source_site=notice.source_site, url=notice.url,
        raw_text="供应PC构件，并负责安装。",
    )
    db.upsert_observed(changed, evening, datetime(2026, 9, 5, 18))
    _finish_test_run(db, evening)

    summary = db.daily_notice_summary("2026-09-05")
    assert len(summary["all"]) == 1
    assert [row["id"] for row in summary["new"]] == [notice_id]
    assert summary["updated"] == []
    assert summary["new"][0]["content_changed_today"] == 1
    assert summary["new"][0]["observation_count"] == 3


def test_daily_summary_excludes_running_and_failed_source_runs(tmp_path):
    db = Database(tmp_path / "daily.db"); db.init()
    for suffix, status in (("running", None), ("failed", "failed")):
        run = _begin_test_run(db, source_id=suffix, source_name=suffix, batch=suffix)
        db.upsert_observed(
            Project(name=suffix, source_site=suffix, url=f"https://example.test/{suffix}"),
            run, datetime(2026, 9, 6, 9),
        )
        if status:
            _finish_test_run(db, run, status)
    assert db.daily_notice_summary("2026-09-06")["all"] == []

    partial_run = _begin_test_run(db, source_id="partial", source_name="partial", batch="partial")
    partial_id = db.upsert_observed(
        Project(name="部分成功公告", source_site="partial", url="https://example.test/partial"),
        partial_run, datetime(2026, 9, 6, 10),
    )
    _finish_test_run(db, partial_run, "partial")
    assert [row["id"] for row in db.daily_notice_summary("2026-09-06")["new"]] == [partial_id]


def test_observation_source_id_matches_source_run(tmp_path):
    db = Database(tmp_path / "daily.db"); db.init()
    run = _begin_test_run(db, source_id="beijing", source_name="北京来源")
    notice_id = db.upsert_observed(
        Project(name="北京公告", source_site="北京来源", url="https://example.test/beijing"), run,
    )
    row = db.notice_observations(notice_id)[0]
    with db.connect() as con:
        source_run_source_id = con.execute("SELECT source_id FROM source_run WHERE id=?", (run,)).fetchone()[0]
    assert row["source_id"] == source_run_source_id == "beijing"


def test_daily_report_and_site_snapshot_use_observation_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_REPORT_MIN_SCORE", "0")
    db = Database(tmp_path / "daily.db"); db.init()
    new_notice = Project(name="新公告", source_site="来源A", url="https://example.test/new")
    old_notice = Project(name="旧公告", source_site="来源A", url="https://example.test/updated", raw_text="旧正文")
    prior_run = _begin_test_run(db, batch="prior-day")
    old_id = db.upsert_observed(old_notice, prior_run, datetime(2026, 9, 6, 9))
    _finish_test_run(db, prior_run)
    run = _begin_test_run(db)
    new_id = db.upsert_observed(new_notice, run, datetime(2026, 9, 7, 9))
    changed = Project(name=old_notice.name, source_site=old_notice.source_site, url=old_notice.url, raw_text="新正文")
    db.upsert_observed(changed, run, datetime(2026, 9, 7, 9, 1))
    _finish_test_run(db, run)
    summary = db.daily_notice_summary("2026-09-07")
    assert [row["id"] for row in summary["new"]] == [new_id]
    assert [row["id"] for row in summary["updated"]] == [old_id]

    report = generate_daily(summary, tmp_path / "reports")
    text = report.read_text(encoding="utf-8")
    assert "## 今日新发现商机" in text and "### 新公告" in text
    assert "## 今日更新公告" in text and "### 旧公告" in text
    assert text.count("### 新公告") == 1
    snapshot = write_daily_observation_snapshot(summary, tmp_path / "daily-observations.json")
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert payload["business_date"] == "2026-09-07"
    assert {item["status"] for item in payload["observations"]} == {"NEW", "UPDATED"}


def _entity_links(db):
    with db.connect() as con:
        con.row_factory = __import__("sqlite3").Row
        return con.execute("SELECT * FROM project_notice_link ORDER BY notice_id").fetchall()


def test_project_entity_same_formal_identifier_links_notices(tmp_path):
    db = Database(tmp_path / "entities.db"); db.init()
    for suffix in ("施工招标公告", "中标结果公告"):
        db.upsert(Project(
            name=f"某道路工程{suffix}", project_no="S110000A001000001001",
            region="北京市", source_site="北京市公共资源交易服务平台",
            url=f"https://example.test/{suffix}",
        ))
    result = ProjectEntityService(db).build()
    links = _entity_links(db)
    assert result["linked_notices"] == 2
    assert len({row["engineering_project_id"] for row in links}) == 1
    assert {row["match_method"] for row in links} == {"PROJECT_IDENTIFIER"}


def test_project_entity_does_not_treat_cccc_scheme_code_as_project_identifier(tmp_path):
    db = Database(tmp_path / "entities.db"); db.init()
    for index in range(2):
        db.upsert(Project(
            name=f"不同采购公告{index}", project_no="FA00000000123",
            region="北京市", source_site="中交集团供应链管理信息系统",
            url=f"https://example.test/cccc/{index}",
        ))
    ProjectEntityService(db).build()
    assert _entity_links(db) == []


def test_project_entity_does_not_treat_yunzhu_tender_code_as_project_identifier(tmp_path):
    db = Database(tmp_path / "entities.db"); db.init()
    for index in range(2):
        db.upsert(Project(
            name=f"不同招标公告{index}", project_no="cscec25000123",
            region="河北省", source_site="中建云筑网",
            url=f"https://example.test/yunzhu/{index}",
        ))
    ProjectEntityService(db).build()
    assert _entity_links(db) == []


def test_project_entity_exact_name_region_owner_links_notice_types(tmp_path):
    db = Database(tmp_path / "entities.db"); db.init()
    for suffix in ("施工招标公告", "中标结果公告"):
        db.upsert(Project(
            name=f"北运河综合治理工程{suffix}", region="北京市", owner="北京市水务建设中心",
            source_site="测试来源", url=f"https://example.test/name/{suffix}",
        ))
    ProjectEntityService(db).build()
    links = _entity_links(db)
    assert len(links) == 2
    assert len({row["engineering_project_id"] for row in links}) == 1
    assert {row["match_method"] for row in links} == {"NORMALIZED_NAME_OWNER_REGION"}


def test_project_entity_similar_name_different_region_does_not_link(tmp_path):
    db = Database(tmp_path / "entities.db"); db.init()
    for index, region in enumerate(("北京市", "河北省")):
        db.upsert(Project(
            name="开发区基础设施项目施工招标公告", region=region, owner="同一建设单位",
            source_site="测试来源", url=f"https://example.test/region/{index}",
        ))
    ProjectEntityService(db).build()
    assert _entity_links(db) == []


def test_project_entity_similar_name_different_owner_does_not_link(tmp_path):
    db = Database(tmp_path / "entities.db"); db.init()
    for index, owner in enumerate(("甲建设单位", "乙建设单位")):
        db.upsert(Project(
            name="河道治理工程施工招标公告", region="河北省", owner=owner,
            source_site="测试来源", url=f"https://example.test/owner/{index}",
        ))
    ProjectEntityService(db).build()
    assert _entity_links(db) == []


def test_project_entity_same_line_different_station_does_not_link(tmp_path):
    db = Database(tmp_path / "entities.db"); db.init()
    for index, station in enumerate(("东大桥站", "平谷站")):
        db.upsert(Project(
            name=f"轨道交通22号线{station}综合开发工程招标公告", region="北京市", owner="轨道建设公司",
            source_site="测试来源", url=f"https://example.test/station/{index}",
        ))
    ProjectEntityService(db).build()
    assert _entity_links(db) == []


def test_project_entity_conservative_name_keeps_contract_section(tmp_path):
    assert canonicalize_project_name("国道234工程施工一标段招标公告") == "国道234工程施工一标段"
    assert canonicalize_project_name("国道234工程施工二标段中标结果公告") == "国道234工程施工二标段"
    db = Database(tmp_path / "entities.db"); db.init()
    for index, section in enumerate(("一标段", "二标段")):
        db.upsert(Project(
            name=f"国道234工程施工{section}招标公告", region="北京市", owner="公路建设单位",
            source_site="测试来源", url=f"https://example.test/section/{index}",
        ))
    ProjectEntityService(db).build()
    assert _entity_links(db) == []


def test_project_entity_build_is_idempotent(tmp_path):
    db = Database(tmp_path / "entities.db"); db.init()
    for index in range(2):
        db.upsert(Project(
            name=f"同一正式工程第{index + 1}份公告", project_no="S110000A001000002001",
            region="北京市", source_site="京津冀公共资源交易协同专区",
            url=f"https://example.test/idempotent/{index}",
        ))
    service = ProjectEntityService(db)
    first = service.build(); second = service.build()
    assert (first["engineering_projects"], first["linked_notices"]) == (1, 2)
    assert (second["engineering_projects"], second["linked_notices"]) == (1, 2)


def test_human_confirmed_project_link_has_priority_over_automation(tmp_path):
    db = Database(tmp_path / "entities.db"); db.init()
    notice_ids = [db.upsert(Project(
        name=f"人工确认公告{index}", region="天津市", source_site="测试来源",
        url=f"https://example.test/human/{index}",
    )) for index in range(2)]
    service = ProjectEntityService(db)
    entity_id = service.confirm_notices(notice_ids, canonical_name="人工确认工程")
    service.build()
    links = _entity_links(db)
    assert {row["engineering_project_id"] for row in links} == {entity_id}
    assert {row["match_method"] for row in links} == {"HUMAN_CONFIRMED"}
    assert all(row["confirmed_by_human"] == 1 for row in links)


def test_project_entity_without_observation_does_not_invent_first_seen(tmp_path):
    db = Database(tmp_path / "entities.db"); db.init()
    db.upsert(Project(
        name="历史工程公告", project_no="S110000A001000003001", region="北京市",
        source_site="北京市公共资源交易服务平台", url="https://example.test/historical/entity",
        publish_date="2020-01-01",
    ))
    ProjectEntityService(db).build()
    with db.connect() as con:
        row = con.execute("SELECT first_seen_at,last_seen_at FROM engineering_project").fetchone()
    assert row == (None, None)

def test_attachment_discovery_and_document_full_text_index(tmp_path):
    html = '''<a href="/files/design.pdf">初步设计PDF</a>
      <a href="/files/design.pdf">重复附件</a>
      <a href="/news/detail.html">普通页面</a>'''
    links = AttachmentEnricher.discover_links(html, "https://example.test/notices/1.html")
    assert [(item.file_name, item.url) for item in links] == [
        ("design.pdf", "https://example.test/files/design.pdf")
    ]

    db = Database(tmp_path / "documents.db"); db.init()
    project = OpportunityAnalyzer(CFG).apply(Project(
        name="国道234道路工程", project_no="S110000A001037484009",
        source_site="测试", url="https://example.test/project", raw_text="桥梁工程",
    ))
    db.upsert(project)
    notice_id = db.recent()[0]["id"]
    attachment_id = db.upsert_attachment(notice_id, links[0].url, links[0].file_name)
    identifiers = extract_identifiers("交易项目编号：S110000A001037484009 工程编号：2023-188LS")
    db.upsert_document(
        attachment_id, notice_id, "土建第5标段初步设计", "预制箱梁与桥梁下部结构 2023-188LS",
        174, "test", identifiers,
    )
    rows = db.document_search(fts_query("2023-188LS"))
    assert len(rows) == 1
    assert rows[0]["page_count"] == 174
    assert "初步设计" in rows[0]["title"]

def test_document_identifier_extraction_and_safe_host_policy():
    identifiers = extract_identifiers("交易项目编号：S110000A001037484009，工程编号：2023-188LS。")
    assert ("S110000A001037484009", "交易项目编号") in identifiers
    assert ("2023-188LS", "工程编号") in identifiers
    assert AttachmentEnricher._host_allowed(
        "https://ggzyfw.beijing.gov.cn/a", "https://jtw.beijing.gov.cn/file.pdf"
    )
    assert not AttachmentEnricher._host_allowed(
        "https://ggzyfw.beijing.gov.cn/a", "https://example.org/file.pdf"
    )

def test_document_cleanup_removes_binary_but_keeps_full_text_index(tmp_path):
    db = Database(tmp_path / "documents.db"); db.init()
    project = Project(
        name="桥梁初步设计", project_no="S110000A001037484009",
        source_site="测试", url="https://example.test/project", raw_text="桥梁工程",
    )
    db.upsert(project)
    notice_id = db.recent()[0]["id"]
    attachment_id = db.upsert_attachment(notice_id, "https://example.test/design.pdf", "design.pdf")
    documents_dir = tmp_path / "documents"; documents_dir.mkdir()
    local_file = documents_dir / f"{attachment_id}-design.pdf"
    local_file.write_bytes(b"pdf payload")
    db.update_attachment(
        attachment_id, status="PARSED", local_path=str(local_file),
        parsed_at="2026-08-01T09:00:00",
    )
    db.upsert_document(
        attachment_id, notice_id, "初步设计", "预制箱梁 2023-188LS", 174, "test",
        [("2023-188LS", "工程编号")],
    )

    result = DocumentRetentionManager(db, documents_dir).cleanup(
        retention_days=2, now=datetime(2026, 8, 4, 9, 0, 0),
    )

    assert result["deleted"] == 1
    assert not local_file.exists()
    assert db.attachment(attachment_id)["status"] == "PURGED"
    assert db.attachment(attachment_id)["local_path"] == ""
    assert len(db.document_search(fts_query("2023-188LS"))) == 1

def test_negative_evidence_excludes_product_from_current_opportunity():
    analyzer = OpportunityAnalyzer(load_yaml("products.yaml"))
    result = analyzer.analyze(Project(
        name="北京地铁区间施工公告",
        raw_text="本次招标不含管片，管片由甲方另行采购。",
    ))
    assert "管片" in result["潜在预制产品"]
    assert result["产品证据分"]["管片"] == 90
    segment = next(item for item in result["命中证据"] if item["产品"] == "管片")
    assert segment["需求意图"] == "另行采购"
    assert segment["需求状态"] == "SEPARATE_PROC"
    assert segment["工程需求"] == "YES"
    assert segment["范围状态"] == "SEPARATE_PROC"
    assert segment["当前机会状态"] == "MONITOR"
    assert segment["负向证据"]
    assert result["当前商机分0-100"] == 0
    assert result["收录类型"] == "前置项目线索"

def test_explicit_exclusion_closes_current_scope_without_claiming_engineering_need():
    analyzer = OpportunityAnalyzer(load_yaml("products.yaml"))
    result = analyzer.analyze(Project(name="施工公告", raw_text="本项目不含管片。"))
    segment = next(item for item in result["命中证据"] if item["产品"] == "管片")
    assert segment["评分"] == 0
    assert segment["需求状态"] == "EXCLUDED"
    assert segment["工程需求"] == "UNKNOWN"
    assert segment["范围状态"] == "OUT_OF_SCOPE"
    assert segment["当前机会状态"] == "CLOSED"

def test_direction_inference_is_monitorable_but_not_confirmed():
    analyzer = OpportunityAnalyzer(load_yaml("products.yaml"))
    result = analyzer.analyze(Project(name="天津港码头护岸工程", raw_text="码头和护岸施工。"))
    assert "水工预制件" in result["潜在预制产品"]
    item = next(item for item in result["命中证据"] if item["产品"] == "水工预制件")
    assert item["需求状态"] == "POSSIBLE"
    assert item["工程需求"] == "POSSIBLE"
    assert item["范围状态"] == "UNCERTAIN"
    assert item["当前机会状态"] == "UNKNOWN"
    assert item["产品机会状态"] == "NO_EVIDENCE"
    assert result["项目跟踪建议"] == "ACTIVE_WATCH"

def test_existing_product_inspection_is_not_a_new_demand():
    analyzer = OpportunityAnalyzer(load_yaml("products.yaml"))
    result = analyzer.analyze(Project(
        name="既有隧道检测项目",
        raw_text="对既有管片破损情况进行检测和修复。",
    ))
    assert "管片" not in result["潜在预制产品"]
    assert result["产品证据分"]["管片"] == 20
    assert result["机会评分0-100"] == 0

def test_award_stage_keeps_product_intelligence_but_closes_current_opportunity():
    analyzer = OpportunityAnalyzer(load_yaml("products.yaml"))
    result = analyzer.analyze(Project(
        name="国道道路工程土建工程(施工)中标候选人公示",
        stage="中标候选人公示",
        raw_text="项目包含路基桥梁工程，桥梁上部结构采用预制箱梁。",
    ))
    assert "箱梁" in result["潜在预制产品"]
    assert result["产品证据分"]["箱梁"] > 0
    assert result["机会评分0-100"] == 0
    assert result["公告可参与性"] == "CLOSED"
    assert result["项目进展信号"] == "CONTRACTOR_CANDIDATE_SELECTED"
    assert result["项目线索分0-100"] > 0
    assert result["跟进优先级"] == "重点跟进"
    item = next(item for item in result["命中证据"] if item["产品"] == "箱梁")
    assert item["当前机会状态"] == "MONITOR"
    assert item["产品机会状态"] == "FOLLOW_UP"

def test_multiple_products_do_not_inflate_each_other_scores():
    analyzer = OpportunityAnalyzer(load_yaml("products.yaml"))
    result = analyzer.analyze(Project(
        name="综合预制构件采购",
        raw_text="采购预制混凝土管片和预制楼梯。",
    ))
    assert result["产品证据分"]["管片"] == 92
    assert result["产品证据分"]["PC构件"] == 95  # 仅获得自身的标题证据奖励
    assert result["机会评分0-100"] == 95

def test_shield_machine_procurement_does_not_imply_segment_demand():
    analyzer = OpportunityAnalyzer(load_yaml("products.yaml"))
    result = analyzer.analyze(Project(
        name="盾构设备采购公告",
        raw_text="采购盾构机刀具及配套油脂。",
    ))
    assert "管片" not in result["潜在预制产品"]
    assert result["产品证据分"]["管片"] == 0

def test_source_health_flags_statistically_unusual_zero():
    health = assess_source_health("success", 0, [8, 7, 9, 6, 8])
    assert health.status == "warning"
    assert health.health_status == "SUSPECT"
    assert "假0条" in health.reason
    assert health.baseline_average == 7.6

def test_source_health_keeps_legitimate_low_volume_zero_normal():
    health = assess_source_health("success", 0, [0, 1, 0, 0, 1])
    assert health.status == "success"
    assert health.health_status == "HEALTHY"
    assert not health.reason

def test_database_records_false_zero_warning_from_history(tmp_path):
    db = Database(tmp_path / "health.db")
    db.init()
    source = {"id": "luban", "name": "鲁班"}
    for count in (8, 7, 9):
        db.record_source_run(source, datetime.now(), "success", count, collected_count=count)
    db.record_source_run(source, datetime.now(), "success", 0, collected_count=0)
    latest = db.source_status()[0]
    assert latest["status"] == "warning"
    assert latest["health_status"] == "SUSPECT"
    assert latest["baseline_average"] == 8
    assert "假0条" in latest["health_reason"]

def test_source_health_uses_upstream_funnel_before_final_opportunity_count():
    healthy = assess_source_health(
        "success", 0, [8, 7, 9], observed_count=50, previous_observed_counts=[55, 48, 52]
    )
    assert healthy.health_status == "HEALTHY"
    suspect = assess_source_health(
        "success", 0, [8, 7, 9], observed_count=0, previous_observed_counts=[55, 48, 52]
    )
    assert suspect.health_status == "SUSPECT"

def test_database_persists_source_funnel(tmp_path):
    db = Database(tmp_path / "funnel.db"); db.init()
    db.record_source_run(
        {"id": "source", "name": "来源"}, datetime.now(), "success", 2,
        collected_count=2, funnel={"raw_list_count": 20, "region_recent_count": 4, "final_opportunity_count": 2},
    )
    row = db.source_status()[0]
    assert row["health_status"] == "HEALTHY"
    assert row["observed_count"] == 20
    assert json.loads(row["funnel_json"])["region_recent_count"] == 4

def test_database_records_human_feedback_for_regression_loop(tmp_path):
    db = Database(tmp_path / "feedback.db"); db.init()
    project = OpportunityAnalyzer(CFG).apply(Project(
        name="管片采购", source_site="测试", url="https://example.test/feedback", raw_text="采购管片",
    ))
    db.upsert(project)
    notice_id = db.recent()[0]["id"]
    feedback_id = db.add_feedback(notice_id, "WORTH_TRACKING", "管片", "人工确认值得跟踪")
    row = db.feedback(notice_id)[0]
    assert row["id"] == feedback_id
    assert row["label"] == "WORTH_TRACKING"
    assert row["product"] == "管片"

def test_separate_procurement_is_available_in_follow_up_pool(tmp_path):
    db = Database(tmp_path / "followups.db"); db.init()
    analyzer = OpportunityAnalyzer(load_yaml("products.yaml"))
    project = Project(
        name="盾构区间土建施工", source_site="测试",
        url="https://example.test/separate-segments", region="北京市",
        publish_date="2026-08-31", raw_text="本工程需要盾构管片，管片另行采购。",
    )
    db.upsert(analyzer.apply(project))
    rows = db.follow_up_assessments()
    assert len(rows) == 1
    assert rows[0]["product"] == "管片"
    assert rows[0]["scope_status"] == "SEPARATE_PROC"
    assert rows[0]["opportunity_status"] == "MONITOR"

def test_evaluation_fixture_meets_baseline():
    fixture = Path(__file__).parent / "fixtures" / "opportunity_cases.json"
    metrics = evaluate_fixture(OpportunityAnalyzer(load_yaml("products.yaml")), fixture)
    assert metrics["schema_pass_rate"] == 1
    assert metrics["product_precision"] >= 0.85
    assert metrics["product_recall"] >= 0.85
    assert metrics["candidate_precision"] == metrics["product_precision"]
    assert metrics["candidate_recall"] == metrics["product_recall"]
    assert metrics["demand_exact_match_rate"] == 1
    assert metrics["opportunity_status_exact_match_rate"] == 1
    assert metrics["direct_support"] > 0
    assert metrics["direct_precision"] == metrics["direct_recall"] == 1
    assert metrics["semantic_pass_rate"] == 1


def test_product_specific_weak_method_rule_comes_from_config():
    cfg = {
        "products": [{
            "name": "箱梁", "direct_keywords": ["箱梁"],
            "method_keywords": ["架梁"], "weak_method_keywords": ["架梁"],
            "project_types": ["桥梁"],
        }],
        "rules": {"product_candidate_min_score": 50, "construction_method_score": 75},
    }
    result = OpportunityAnalyzer(cfg).analyze(Project(name="桥梁架梁施工公告"))
    evidence = result["命中证据"][0]
    assert evidence["产品"] == "箱梁"
    assert evidence["产品需求证据等级"] == "WEAK"

def test_procurement_object_controls_current_opportunity_score():
    analyzer = OpportunityAnalyzer(load_yaml("products.yaml"))
    cases = [
        ("盾构施工安全监控系统大数据服务器采购竞谈公告", "项目用于盾构区间，本次采购大数据服务器。", 10, 70, "服务器"),
        ("轨道交通广告灯箱采购公告", "本次采购车站广告灯箱。", 5, 50, "灯箱"),
        ("顶管工程机械设备租赁公告", "采用泥水平衡顶管施工，本次租赁机械设备。", 20, 70, "机械设备"),
    ]
    for name, body, max_current, min_lead, subject in cases:
        result = analyzer.analyze(Project(name=name, raw_text=body))
        assert result["当前商机分0-100"] <= max_current
        assert result["项目线索分0-100"] >= min_lead
        assert result["收录类型"] == "前置项目线索"
        assert any(subject in value for value in result["本次采购对象"])

def test_direct_target_business_actions_rank_ahead_of_project_clues():
    analyzer = OpportunityAnalyzer(load_yaml("products.yaml"))
    direct = analyzer.analyze(Project(name="盾构管片采购公告", raw_text="采购预制混凝土管片。"))
    clue = analyzer.analyze(Project(name="盾构区间施工公告", raw_text="采用盾构法施工。"))
    assert direct["当前商机分0-100"] >= 90
    assert direct["收录类型"] == "当前直接商机"
    assert direct["命中证据"][0]["证据关系"] == "采购对象"
    assert clue["当前商机分0-100"] <= 20
    assert direct["当前商机分0-100"] > clue["当前商机分0-100"]

def test_product_modifier_is_adjacent_supply_not_direct_product_purchase():
    analyzer = OpportunityAnalyzer(load_yaml("products.yaml"))
    result = analyzer.analyze(Project(name="长石项目管片防水材料采购", raw_text="采购管片防水材料。"))
    item = next(item for item in result["命中证据"] if item["产品"] == "管片")
    assert item["证据关系"] == "上下游配套"
    assert item["等级"] == "工程需求证据"
    assert result["当前商机分0-100"] <= 10

def test_transaction_qualifiers_do_not_hide_direct_product_purchase():
    analyzer = OpportunityAnalyzer(load_yaml("products.yaml"))
    for name in ("预制构件竞价采购公告", "PC构件物资竞价采购公告"):
        result = analyzer.analyze(Project(name=name, raw_text=name))
        item = next(item for item in result["命中证据"] if item["产品"] == "PC构件")
        assert item["证据关系"] == "采购对象"
        assert result["当前商机分0-100"] >= 90

def test_cccc_html_to_text():
    assert CCCCCollector.html_to_text("<h2>项目概况</h2><p>盾构管片采购</p>") == "项目概况\n盾构管片采购"

def test_cccc_region_filter():
    collector = CCCCCollector({"name": "测试", "regions": ["北京市", "天津市", "河北省"]})
    assert collector._region_allowed("河北省")
    assert collector._region_allowed("北京市,河北省")
    assert not collector._region_allowed("江苏省")
    assert not collector._region_allowed("")

def test_cccc_browser_rows_parse_decrypted_public_table():
    collector = CCCCCollector({"name": "中交招采网", "public_url": "https://zjzcw.iccec.cn/", "regions": ["北京市", "天津市", "河北省"]})
    rows = [["1", "GK-1", "津沧高速桥梁工程招标公告", "天津市", "桥梁业务", "公开招标", "2099-01-02 10:00", "2099-01-07", "报名中"]]
    projects = collector.parse_browser_rows(rows, "桥梁")
    assert len(projects) == 1
    assert projects[0].project_no == "GK-1"
    assert projects[0].region == "天津市"
    assert "radarCode=GK-1" in projects[0].url

def test_cccc_reports_waf_block_without_misclassifying_as_zero_results():
    assert "WAF" in CCCCCollector.block_reason(418)
    assert "限流" in CCCCCollector.block_reason(429)

def test_cccc_waf_circuit_breaker_only_blocks_same_day(tmp_path):
    collector = CCCCCollector({"id": "cccc", "name": "中交招采网"})
    collector.diagnostics = DiagnosticRecorder(tmp_path)
    collector.diagnostics.record("cccc", ResponseOutcome.WAF, status=418)
    assert collector.waf_blocked_today(date.today())
    assert not collector.waf_blocked_today(date.today() + timedelta(days=1))

def test_response_classifier_distinguishes_access_and_upstream_failures():
    assert classify_document(418, "访问被拦截！") == ResponseOutcome.WAF
    assert classify_document(429) == ResponseOutcome.RATE_LIMIT
    assert classify_document(503) == ResponseOutcome.UPSTREAM_BROKEN
    assert classify_document(200, body="请登录") == ResponseOutcome.AUTH_EXPIRED
    assert classify_document(200) == ResponseOutcome.OK

def test_selector_registry_prefers_cached_reviewed_candidate(tmp_path):
    registry = SelectorRegistry(tmp_path / "selectors.json")
    candidates = [LocatorCandidate("first", lambda page: page), LocatorCandidate("second", lambda page: page)]
    registry._save({registry.cache_key("source", "search"): "second"})
    assert [item.key for item in registry.ordered("source", "search", candidates)] == ["second", "first"]

def test_diagnostic_recorder_scrubs_secrets(tmp_path):
    recorder = DiagnosticRecorder(tmp_path)
    path = recorder.record(
        "source", ResponseOutcome.ERROR,
        message="Authorization: Bearer abc.def token=secret-value",
    )
    text = path.read_text(encoding="utf-8")
    assert "abc.def" not in text
    assert "secret-value" not in text
    assert "REDACTED" in text

def test_project_validator_rejects_out_of_region_and_bad_url():
    validator = ProjectValidator({"regions": ["北京市", "天津市", "河北省"]})
    valid = Project(name="雄安管片采购", region="河北省", source_site="测试", url="https://example.test/1")
    invalid = Project(name="上海项目", region="上海市", source_site="测试", url="javascript:void(0)")
    accepted, rejected = validator.validate([valid, invalid])
    assert accepted == [valid]
    assert len(rejected) == 1
    assert "URL无效" in rejected[0].reasons
    assert "地区不在来源允许范围" in rejected[0].reasons

def test_jing_jin_ji_text_filter():
    assert infer_region("雄安新区盾构管片") == "河北省"
    assert is_jing_jin_ji("天津地铁PC构件")
    assert not is_jing_jin_ji("上海地铁管片")

def test_luban_homepage_filter():
    html = '''<div class="luban-notice-row"><a title="雄安新区管片采购" href="https://eproport.crecgec.com/assets/tmp/redirect.html?x=1">项目</a><div class="date">2099-01-01</div></div>'''
    collector = CRECGLubanCollector({"name": "中铁鲁班网", "url": "https://www.crecgec.com/", "keywords": ["管片"]})
    rows = collector.parse_homepage(html)
    assert len(rows) == 1
    assert rows[0].region == "河北省"

def test_luban_accepts_bridge_direction_without_exact_product_name():
    html = '''<div class="luban-notice-row"><a title="京密高速公路第6标段主路路基及桥梁土方、主路附属工程招标公告" href="https://eproport.crecgec.com/assets/tmp/redirect.html?x=2">项目</a><div class="date">2099-01-01</div></div>'''
    collector = CRECGLubanCollector({"name": "中铁鲁班网", "url": "https://www.crecgec.com/", "keywords": ["箱梁"]})
    rows = collector.parse_homepage(html)
    assert len(rows) == 1
    assert rows[0].region == "北京市"
    assert rows[0].stage == "招标采购公告"

def test_luban_public_history_parser_filters_region_and_direction():
    text = '''历史公告\n京密高速公路（六环路至西统路段）工程第6标段主路路基及桥梁土方、主路附属工程招标公告\n华北区\n中铁六局集团有限公司北京铁路建设有限公司\n2099-01-02\n上海办公用品采购公告\n华东区\n某公司\n2099-01-01'''
    collector = CRECGLubanCollector({"name": "中铁鲁班网", "url": "https://www.crecgec.com/", "keywords": ["箱梁"]})
    rows = collector.parse_portal_text(text, "京密")
    assert len(rows) == 1
    assert "京密高速公路" in rows[0].name
    assert rows[0].region == "北京市"

def test_yzw_api_region_filter():
    collector = CSCECYunZhuCollector({"id": "cscec_yzw", "name": "中建云筑网"})
    body = {"code": 200, "data": {"records": [
        {"name": "雄安管片采购", "area": "河北省雄安新区", "tenderCode": "T-1", "source": 3, "tenantId": "cscec", "publishDate": "2099-01-01 10:00:00"},
        {"name": "武汉管片采购", "area": "湖北省武汉市", "tenderCode": "T-2"},
    ]}}
    rows = collector._projects_from_api(body, "管片")
    assert [row.project_no for row in rows] == ["T-1"]

def test_beijing_ggzy_parsers():
    collector = BeijingGGZYCollector({"name": "北京公共资源", "url": "https://ggzyfw.beijing.gov.cn/"})
    list_html = '''<ul><li><a class="divtitlejy" href="/jyxx/1.html" title="某道路及管线工程施工资格预审公告">项目</a><div>2026-08-14</div></li></ul>'''
    assert collector.parse_list(list_html) == [("某道路及管线工程施工资格预审公告", "2026-08-14", "https://ggzyfw.beijing.gov.cn/jyxx/1.html")]
    detail_html = '''<div class="div-title">某道路及管线工程施工资格预审公告<p>交易项目编号：S110001</p></div><div class="newsCon">招标人为 北京市某建设中心，建设地点 大兴区。建设规模为雨水管线1000米，招标范围为道路工程、给水工程。</div>'''
    project = collector.parse_detail(detail_html, "https://example.test/1", "某道路及管线工程施工资格预审公告", "2026-08-14")
    assert project.project_no == "S110001"
    assert project.region.startswith("北京市")
    assert "雨水管线" in project.construction_content

def test_ccgp_parsers_and_jjj_mapping():
    collector = CCGPCollector({"name": "中国政府采购网", "region_labels": ["北京", "天津", "河北"]})
    html = '''<ul class="c_list_bid"><li><a href="./202608/t1.htm" title="污水管网工程施工公开招标公告">公告</a>发布时间：<em>2026-08-14 10:00</em> 地域：<em>河北</em> 采购人：<em>某水务局</em></li></ul>'''
    rows = collector.parse_list(html)
    assert rows[0][2:4] == ("河北", "某水务局")
    detail = collector.parse_detail('<div class="vF_detail_content">项目编号：HB-1 本工程采用顶管施工。</div>', rows[0][4], *rows[0][:4])
    assert detail.region == "河北省"
    assert detail.project_no == "HB-1"

def test_construction_stage_filter_excludes_service_notices():
    assert is_construction_tender("道路及管线工程施工资格预审公告")
    assert is_construction_tender("主路路基及桥梁土方、主路附属工程招标公告")
    assert is_construction_tender("轨道交通工程设计施工总承包招标公告")
    assert not is_construction_tender("道路工程监理招标公告")
    assert not is_construction_tender("桥梁工程设计招标公告")
    assert not is_construction_tender("住宅楼室内精装修工程施工公告")


def test_city_renewal_and_water_projects_are_government_watch_directions():
    from opportunity_radar.collectors.construction import investigation_directions

    assert is_construction_tender("某片区城市更新项目施工总承包招标公告")
    assert is_construction_tender("某河道治理及防洪排涝工程施工招标公告")
    assert investigation_directions("老旧小区改造及地下管网更新工程") == ["城市更新"]
    assert investigation_directions("河道治理、泵站和水闸工程") == ["水利工程"]
    assert investigation_directions("城市更新片区河道治理工程") == ["城市更新", "水利工程"]
    assert not is_construction_tender("城市更新专项规划编制咨询服务")
    assert not is_construction_tender("水利工程防洪影响评价服务公告")

def test_broader_search_terms_override_legacy_product_terms_and_deduplicate():
    source = {
        "keywords": ["箱梁"],
        "search_keywords": ["箱梁", "桥梁", "盾构", "桥梁", ""],
    }
    assert configured_search_terms(source) == ["箱梁", "桥梁", "盾构"]

def _http_response(status: int, text: str = "") -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = text.encode("utf-8")
    response.url = "https://example.test/notices"
    response.encoding = "utf-8"
    return response

def test_http_cache_obeys_ttl(tmp_path):
    cache = HttpCache(tmp_path / "cache.sqlite3")
    assert cache.get("https://example.test/1", 60) is None
    cache.put("https://example.test/1", "公告正文")
    assert cache.get("https://example.test/1", 60) == "公告正文"
    assert cache.get("https://example.test/1", 0) is None

def test_http_418_opens_circuit_without_retry(monkeypatch, tmp_path):
    monkeypatch.setenv("RADAR_PROXY_URL", "")
    client = PublicPageClient("测试来源", "test_source")
    client.minimum_interval = 0
    client.max_attempts = 2
    client.cache = HttpCache(tmp_path / "cache.sqlite3")
    client.circuit = SourceCircuitBreaker("test_source", tmp_path / "circuits")
    calls = []
    client.session.get = lambda *args, **kwargs: calls.append(args[0]) or _http_response(418)
    try:
        client.get_text("https://example.test/notices")
        assert False, "HTTP 418 should open the source circuit"
    except SourceCircuitOpen:
        pass
    assert len(calls) == 1
    assert client.circuit.blocked_until() is not None

def test_http_transient_503_has_bounded_retry_and_caches(monkeypatch, tmp_path):
    monkeypatch.setenv("RADAR_PROXY_URL", "")
    client = PublicPageClient("测试来源", "test_source")
    client.minimum_interval = 0
    client.max_attempts = 2
    client.cache = HttpCache(tmp_path / "cache.sqlite3")
    client.circuit = SourceCircuitBreaker("test_source", tmp_path / "circuits")
    responses = iter([_http_response(503), _http_response(200, "正常公告")])
    calls = []
    client.session.get = lambda *args, **kwargs: calls.append(args[0]) or next(responses)
    monkeypatch.setattr("opportunity_radar.collectors.http.time.sleep", lambda _: None)
    assert client.get_text("https://example.test/notices", cache_ttl_seconds=60) == "正常公告"
    assert len(calls) == 2
    assert client.get_text("https://example.test/notices", cache_ttl_seconds=60) == "正常公告"
    assert len(calls) == 2

def test_http_repeated_503_opens_short_circuit(monkeypatch, tmp_path):
    monkeypatch.setenv("RADAR_PROXY_URL", "")
    client = PublicPageClient("测试来源", "test_source")
    client.minimum_interval = 0
    client.max_attempts = 2
    client.cache = HttpCache(tmp_path / "cache.sqlite3")
    client.circuit = SourceCircuitBreaker("test_source", tmp_path / "circuits")
    calls = []
    client.session.get = lambda *args, **kwargs: calls.append(args[0]) or _http_response(503)
    monkeypatch.setattr("opportunity_radar.collectors.http.time.sleep", lambda _: None)
    try:
        client.get_text("https://example.test/notices")
        assert False, "Repeated HTTP 503 should open a short source circuit"
    except SourceCircuitOpen:
        pass
    assert len(calls) == 2
    assert client.circuit.blocked_until() is not None

def test_proxy_access_error_falls_back_to_direct_once(monkeypatch, tmp_path):
    monkeypatch.setenv("RADAR_PROXY_URL", "http://127.0.0.1:7890")
    monkeypatch.setenv("RADAR_DIRECT_FALLBACK", "true")
    client = PublicPageClient("测试来源", "proxy_fallback")
    client.minimum_interval = 0
    client.max_attempts = 1
    client.cache = HttpCache(tmp_path / "cache.sqlite3")
    client.circuit = SourceCircuitBreaker("proxy_fallback", tmp_path / "circuits")
    calls = []
    client.session.get = lambda *args, **kwargs: calls.append("proxy") or _http_response(418)
    client.direct_session.get = lambda *args, **kwargs: calls.append("direct") or _http_response(200, "直连成功")
    assert client.get_text("https://example.test/notices") == "直连成功"
    assert calls == ["proxy", "direct"]
    assert client.circuit.blocked_until() is None

def test_proxy_and_direct_access_error_opens_circuit(monkeypatch, tmp_path):
    monkeypatch.setenv("RADAR_PROXY_URL", "http://127.0.0.1:7890")
    monkeypatch.setenv("RADAR_DIRECT_FALLBACK", "true")
    client = PublicPageClient("测试来源", "proxy_fallback_blocked")
    client.minimum_interval = 0
    client.max_attempts = 1
    client.cache = HttpCache(tmp_path / "cache.sqlite3")
    client.circuit = SourceCircuitBreaker("proxy_fallback_blocked", tmp_path / "circuits")
    calls = []
    client.session.get = lambda *args, **kwargs: calls.append("proxy") or _http_response(418)
    client.direct_session.get = lambda *args, **kwargs: calls.append("direct") or _http_response(403)
    try:
        client.get_text("https://example.test/notices")
        assert False, "代理和直连均受限时应熔断"
    except SourceCircuitOpen:
        pass
    assert calls == ["proxy", "direct"]
    assert client.circuit.blocked_until() is not None
