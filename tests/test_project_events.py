import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest

from opportunity_radar.ai import OpportunityAnalyzer
from opportunity_radar.db import Database
from opportunity_radar.models import Project
from opportunity_radar.project_events import EXTRACTOR_VERSION, ProjectEventService


CFG = {
    "products": [{
        "name": "管片", "direct_keywords": ["管片"], "keywords": ["盾构", "管片"],
        "project_types": ["轨道交通"],
    }],
    "rules": {"direct_product_score": 90, "max_score": 100},
}


def _db(tmp_path):
    db = Database(tmp_path / "events.db")
    db.init()
    return db


def _add_notice(
    db, index, title, *, progress="UNKNOWN", stage="", body="", publish_date="2026-09-05",
    linked=True, entity_id=None, analyze=False, source_site="测试来源",
):
    project = Project(
        name=title, publish_date=publish_date, stage=stage, raw_text=body,
        source_site=source_site, url=f"https://example.test/event/{index}",
        project_progress_signal=progress,
    )
    if analyze:
        project = OpportunityAnalyzer(CFG).apply(project)
        project.project_progress_signal = progress
    notice_id = db.upsert(project)
    if not linked:
        return notice_id, None
    with db.connect() as con:
        if entity_id is None:
            entity_id = int(con.execute(
                "INSERT INTO engineering_project(identity_key,canonical_name) VALUES(?,?)",
                (f"test:{index}", title),
            ).lastrowid)
        con.execute(
            """INSERT INTO project_notice_link(
            engineering_project_id,notice_id,match_method,match_score,confirmed_by_human)
            VALUES(?,?,?,100,1)""",
            (entity_id, notice_id, "HUMAN_CONFIRMED"),
        )
    return notice_id, entity_id


def _active_events(db):
    with db.connect() as con:
        con.row_factory = sqlite3.Row
        return [dict(row) for row in con.execute("SELECT * FROM project_event WHERE is_active=1 ORDER BY id")]


@pytest.mark.parametrize(
    "title,stage,progress,expected",
    [
        ("道路工程施工招标公告", "施工招标公告", "CONSTRUCTION_TENDER_OPEN", "TENDER_ANNOUNCED"),
        ("道路工程中标候选人公示", "中标候选人公示", "CONTRACTOR_CANDIDATE_SELECTED", "CONTRACTOR_CANDIDATE_SELECTED"),
        ("道路工程施工中标结果", "中标结果公告", "CONTRACTOR_SELECTED", "CONTRACTOR_SELECTED"),
    ],
)
def test_construction_progress_signals_become_factual_events(tmp_path, title, stage, progress, expected):
    db = _db(tmp_path)
    _add_notice(db, 1, title, stage=stage, progress=progress)
    ProjectEventService(db).build()
    assert [row["event_type"] for row in _active_events(db)] == [expected]


def test_product_procurement_announcement_is_per_product(tmp_path):
    db = _db(tmp_path)
    _add_notice(db, 1, "盾构管片采购公告", stage="采购公告", analyze=True)
    ProjectEventService(db).build()
    event = _active_events(db)[0]
    assert (event["event_type"], event["product"]) == ("PRODUCT_PROCUREMENT_ANNOUNCED", "管片")


def test_product_procurement_result_is_not_contractor_result(tmp_path):
    db = _db(tmp_path)
    _add_notice(
        db, 1, "盾构管片采购中标结果公告", stage="中标结果公告",
        progress="CONTRACTOR_SELECTED", body="中标人：中国构件有限公司", analyze=True,
    )
    ProjectEventService(db).build()
    event = _active_events(db)[0]
    assert event["event_type"] == "PRODUCT_PROCUREMENT_RESULT"
    assert event["party_role"] == "SUPPLIER"


def test_different_lots_create_distinct_events(tmp_path):
    db = _db(tmp_path)
    _, entity_id = _add_notice(db, 6, "国道234工程第6标段施工中标结果", progress="CONTRACTOR_SELECTED")
    _add_notice(db, 7, "国道234工程第7标段施工中标结果", progress="CONTRACTOR_SELECTED", entity_id=entity_id)
    ProjectEventService(db).build()
    rows = _active_events(db)
    assert len(rows) == 2
    assert {row["lot"] for row in rows} == {"6", "7"}
    assert len({row["event_key"] for row in rows}) == 2


def test_repeated_build_is_idempotent(tmp_path):
    db = _db(tmp_path)
    _add_notice(db, 1, "道路工程施工招标公告", progress="CONSTRUCTION_TENDER_OPEN")
    service = ProjectEventService(db)
    service.build()
    first_id = _active_events(db)[0]["id"]
    service.build()
    rows = _active_events(db)
    assert len(rows) == 1 and rows[0]["id"] == first_id


def test_observation_time_and_event_date_are_separate(tmp_path):
    db = _db(tmp_path)
    notice_id, _ = _add_notice(
        db, 1, "道路工程施工中标结果", progress="CONTRACTOR_SELECTED",
        publish_date="2026-09-05",
    )
    run_id = db.begin_source_run({"id": "test", "name": "测试"}, datetime(2026, 9, 7, 9, 0))
    with db.connect() as con:
        con.execute(
            """INSERT INTO notice_observation(
            run_id,notice_id,source_id,observed_at,is_first_seen,content_hash,content_changed)
            VALUES(?,?,?,'2026-09-07T09:00:00',0,?,0)""",
            (run_id, notice_id, "test", "0" * 64),
        )
    ProjectEventService(db).build()
    event = _active_events(db)[0]
    assert event["event_date"] == "2026-09-05"
    assert event["event_date_source"] == "PUBLICATION_DATE"


def test_explicit_result_date_has_priority_over_publication_date(tmp_path):
    db = _db(tmp_path)
    _add_notice(
        db, 1, "道路工程施工中标结果", progress="CONTRACTOR_SELECTED",
        publish_date="2026-09-05", body="中标日期：2026年9月3日",
    )
    ProjectEventService(db).build()
    event = _active_events(db)[0]
    assert (event["event_date"], event["event_date_source"]) == ("2026-09-03", "NOTICE_EXPLICIT_DATE")


def test_result_party_is_reused_from_identity_fact(tmp_path):
    db = _db(tmp_path)
    _add_notice(
        db, 1, "道路工程施工中标结果", progress="CONTRACTOR_SELECTED",
        body="中标人：北京市政路桥股份有限公司",
    )
    ProjectEventService(db).build()
    event = _active_events(db)[0]
    assert (event["party_name"], event["party_role"]) == ("北京市政路桥股份有限公司", "CONTRACTOR")
    assert event["party_source_reference"].startswith("notice_identity_fact:")


def test_missing_result_party_is_not_invented(tmp_path):
    db = _db(tmp_path)
    _add_notice(db, 1, "道路工程施工中标结果", progress="CONTRACTOR_SELECTED")
    ProjectEventService(db).build()
    event = _active_events(db)[0]
    assert event["party_name"] == "" and event["party_role"] == ""


def test_unlinked_notice_does_not_create_event_or_entity(tmp_path):
    db = _db(tmp_path)
    _add_notice(db, 1, "道路工程施工招标公告", progress="CONSTRUCTION_TENDER_OPEN", linked=False)
    result = ProjectEventService(db).build()
    assert result["events"] == 0 and result["engineering_projects"] == 0


def test_same_parent_candidate_does_not_create_formal_event(tmp_path):
    db = _db(tmp_path)
    first, _ = _add_notice(db, 1, "甲工程施工招标公告", progress="CONSTRUCTION_TENDER_OPEN", linked=False)
    second, _ = _add_notice(db, 2, "甲工程施工中标公告", progress="CONTRACTOR_SELECTED", linked=False)
    with db.connect() as con:
        con.execute(
            """INSERT INTO project_link_candidate(
            notice_id_a,notice_id_b,candidate_key,candidate_relation,evidence_score,evidence_level,
            evidence_json,algorithm_version) VALUES(?,?,?,?,?,?,?,?)""",
            (first, second, f"{first}:{second}", "SAME_PARENT_PROJECT", 80, "MEDIUM", "{}", "test"),
        )
    assert ProjectEventService(db).build()["events"] == 0


def test_engineering_direction_does_not_become_product_procurement(tmp_path):
    db = _db(tmp_path)
    _add_notice(db, 1, "盾构区间施工招标公告", progress="CONSTRUCTION_TENDER_OPEN", analyze=True)
    ProjectEventService(db).build()
    assert _active_events(db)[0]["event_type"] == "TENDER_ANNOUNCED"


@pytest.mark.parametrize("product_status", ["FOLLOW_UP", "PRE_PROCUREMENT"])
def test_product_status_alone_does_not_create_predicted_event(tmp_path, product_status):
    db = _db(tmp_path)
    notice_id, _ = _add_notice(db, 1, "轨道交通工程信息", linked=True)
    with db.connect() as con:
        con.execute(
            """INSERT INTO notice_product_assessment(
            notice_id,product,product_opportunity_status,product_demand_evidence_level,
            product_demand_evidence_type,procurement_window_status,analyzer_version,rules_version)
            VALUES(?,?,?,?,?,?,?,?)""",
            (
                notice_id, "管片", product_status, "STRONG", "DIRECT_TARGET_PRODUCT",
                "UPCOMING" if product_status == "PRE_PROCUREMENT" else "UNKNOWN", "test", "test",
            ),
        )
    result = ProjectEventService(db).build()
    assert result["events"] == 0


def test_build_does_not_change_product_assessment_or_project_scores(tmp_path):
    db = _db(tmp_path)
    notice_id, _ = _add_notice(db, 1, "盾构管片采购公告", analyze=True)
    with db.connect() as con:
        before_project = con.execute(
            "SELECT ai_score,analysis_json,project_tracking_recommendation FROM project WHERE id=?",
            (notice_id,),
        ).fetchone()
        before_assessments = con.execute(
            "SELECT * FROM notice_product_assessment WHERE notice_id=?", (notice_id,)
        ).fetchall()
    ProjectEventService(db).build()
    with db.connect() as con:
        after_project = con.execute(
            "SELECT ai_score,analysis_json,project_tracking_recommendation FROM project WHERE id=?",
            (notice_id,),
        ).fetchone()
        after_assessments = con.execute(
            "SELECT * FROM notice_product_assessment WHERE notice_id=?", (notice_id,)
        ).fetchall()
    assert before_project == after_project
    assert before_assessments == after_assessments


def test_g234_four_lots_keep_parties_and_scope(tmp_path):
    db = _db(tmp_path)
    parties = {
        6: "北京市政路桥股份有限公司",
        7: "中交一公局第三工程有限公司",
        8: "中铁十六局集团第三工程有限公司",
        9: "北京城建集团有限责任公司",
    }
    entity_id = None
    for lot, party in parties.items():
        _, entity_id = _add_notice(
            db, lot, f"国道234（永宁—琉璃庙）道路工程第{lot}标段施工中标结果",
            progress="CONTRACTOR_SELECTED", body=f"中标人：{party}", entity_id=entity_id,
        )
    ProjectEventService(db).build()
    rows = _active_events(db)
    assert len(rows) == 4
    assert {(int(row["lot"]), row["party_name"]) for row in rows} == set(parties.items())
    assert [row["lot"] for row in ProjectEventService(db).timeline(entity_id)["events"]] == ["6", "7", "8", "9"]


def test_notice_observation_rows_are_unchanged(tmp_path):
    db = _db(tmp_path)
    notice_id, _ = _add_notice(db, 1, "道路工程施工招标公告", progress="CONSTRUCTION_TENDER_OPEN")
    run_id = db.begin_source_run({"id": "test", "name": "测试"}, datetime.now())
    with db.connect() as con:
        con.execute(
            "INSERT INTO notice_observation(run_id,notice_id,source_id,observed_at,content_hash) VALUES(?,?,?,?,?)",
            (run_id, notice_id, "test", "2026-09-07T09:00:00", "1" * 64),
        )
        before = con.execute("SELECT * FROM notice_observation").fetchall()
    ProjectEventService(db).build()
    with db.connect() as con:
        assert con.execute("SELECT * FROM notice_observation").fetchall() == before


def test_identity_facts_and_reviews_are_unchanged(tmp_path):
    db = _db(tmp_path)
    notice_id, _ = _add_notice(
        db, 1, "道路工程第1标段施工中标结果", progress="CONTRACTOR_SELECTED",
        body="中标人：某建设有限公司",
    )
    with db.connect() as con:
        fact_id = con.execute(
            "SELECT id FROM notice_identity_fact WHERE notice_id=? AND fact_type='RESULT_PARTY'", (notice_id,)
        ).fetchone()[0]
        con.execute(
            """INSERT INTO notice_identity_review(
            notice_id,fact_type,target_fact_id,decision,review_note) VALUES(?,?,?,?,?)""",
            (notice_id, "RESULT_PARTY", fact_id, "CONFIRM", "test"),
        )
        before_facts = con.execute("SELECT * FROM notice_identity_fact").fetchall()
        before_reviews = con.execute("SELECT * FROM notice_identity_review").fetchall()
    ProjectEventService(db).build()
    with db.connect() as con:
        assert con.execute("SELECT * FROM notice_identity_fact").fetchall() == before_facts
        assert con.execute("SELECT * FROM notice_identity_review").fetchall() == before_reviews


def test_candidate_and_cluster_data_are_unchanged(tmp_path):
    db = _db(tmp_path)
    linked, _ = _add_notice(db, 1, "道路工程施工招标公告", progress="CONSTRUCTION_TENDER_OPEN")
    other, _ = _add_notice(db, 2, "道路工程信息", linked=False)
    a, b = sorted((linked, other))
    with db.connect() as con:
        con.execute(
            """INSERT INTO project_link_candidate(
            notice_id_a,notice_id_b,candidate_key,candidate_relation,evidence_score,evidence_level,
            evidence_json,algorithm_version) VALUES(?,?,?,?,?,?,?,?)""",
            (a, b, f"{a}:{b}", "UNCERTAIN", 70, "MEDIUM", json.dumps({"x": 1}), "test"),
        )
        before = con.execute("SELECT * FROM project_link_candidate").fetchall()
    ProjectEventService(db).build()
    with db.connect() as con:
        assert con.execute("SELECT * FROM project_link_candidate").fetchall() == before


def test_extractor_upgrade_preserves_auditable_history(tmp_path):
    db = _db(tmp_path)
    _add_notice(db, 1, "道路工程施工招标公告", progress="CONSTRUCTION_TENDER_OPEN")
    ProjectEventService(db, extractor_version="event-v1").build()
    ProjectEventService(db, extractor_version="event-v2").build()
    with db.connect() as con:
        rows = con.execute("SELECT extractor_version,is_active FROM project_event ORDER BY id").fetchall()
    assert rows == [("event-v1", 0), ("event-v2", 1)]


def test_timeline_returns_only_active_events_in_date_order(tmp_path):
    db = _db(tmp_path)
    _, entity_id = _add_notice(
        db, 1, "道路工程施工招标公告", progress="CONSTRUCTION_TENDER_OPEN", publish_date="2026-09-01"
    )
    _add_notice(
        db, 2, "道路工程施工中标结果", progress="CONTRACTOR_SELECTED",
        publish_date="2026-09-05", entity_id=entity_id,
    )
    service = ProjectEventService(db)
    service.build()
    timeline = service.timeline(entity_id)
    assert [row["event_date"] for row in timeline["events"]] == ["2026-09-01", "2026-09-05"]
    assert timeline["project"]["id"] == entity_id


def test_unknown_notice_reports_reason_without_unknown_fact(tmp_path):
    db = _db(tmp_path)
    _add_notice(db, 1, "工程一般信息")
    result = ProjectEventService(db).build()
    assert result["events"] == 0
    assert result["notices_without_events"] == 1
    assert not _active_events(db)
    assert result["no_event_reasons"]["公告未表达受支持的可靠事实事件"] == 1


def test_explicit_project_termination_creates_fact(tmp_path):
    db = _db(tmp_path)
    _add_notice(db, 1, "某工程项目终止公告")
    ProjectEventService(db).build()
    assert _active_events(db)[0]["event_type"] == "PROJECT_TERMINATED"


def test_failed_tender_does_not_mean_project_terminated(tmp_path):
    db = _db(tmp_path)
    _add_notice(db, 1, "某工程设备采购流标公告")
    result = ProjectEventService(db).build()
    assert result["events"] == 0
    assert not _active_events(db)


def test_project_event_schema_has_versioned_event_key(tmp_path):
    db = _db(tmp_path)
    with db.connect() as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(project_event)")}
        indexes = {row[1] for row in con.execute("PRAGMA index_list(project_event)")}
    assert {"event_key", "extractor_version", "event_date_source", "product", "is_active"} <= columns
    assert any(name.startswith("sqlite_autoindex_project_event") for name in indexes)


def test_scope_rebuild_deactivates_old_event_and_keeps_one_active(tmp_path):
    db = _db(tmp_path)
    notice_id, entity_id = _add_notice(
        db, 1, "道路工程1#标段施工招标公告", progress="CONSTRUCTION_TENDER_OPEN"
    )
    with db.connect() as con:
        con.execute(
            """INSERT INTO project_event(
            engineering_project_id,notice_id,event_type,event_date,event_scope,event_subject,
            confidence,extractor_version,event_key) VALUES(?,?,?,?,?,?,?,?,?)""",
            (entity_id, notice_id, "TENDER_ANNOUNCED", "2026-09-05", "", "旧范围", "HIGH", "project-event-v1.0", "old-key"),
        )
    ProjectEventService(db).build()
    with db.connect() as con:
        old = con.execute("SELECT is_active FROM project_event WHERE event_key='old-key'").fetchone()[0]
        current = con.execute("SELECT lot,is_active FROM project_event WHERE extractor_version='project-event-v1.1'").fetchone()
    assert old == 0
    assert current == ("1", 1)


def test_two_sources_form_one_canonical_event_and_keep_sources(tmp_path):
    db = _db(tmp_path)
    _, entity_id = _add_notice(
        db, 1, "道路工程第6标段施工中标结果", progress="CONTRACTOR_SELECTED",
        body="中标人：某建设有限公司", source_site="来源A",
    )
    _add_notice(
        db, 2, "道路工程第6标段施工中标结果", progress="CONTRACTOR_SELECTED",
        body="中标人：某建设有限公司", source_site="来源B", entity_id=entity_id,
    )
    service = ProjectEventService(db)
    service.build()
    facts = service.canonical_events(entity_id)
    assert len(facts) == 1
    assert facts[0]["source_count"] == 2
    assert facts[0]["independent_reference_count"] == 2
    assert facts[0]["evidence_quality"] == "MULTI_SOURCE_CROSS_CHECKED"
    assert {source["source_type"] for source in facts[0]["sources"]} == {"来源A", "来源B"}
    assert {source["source_name"] for source in facts[0]["sources"]} == {"来源A", "来源B"}
    assert all(source["source_url"].startswith("https://example.test/") for source in facts[0]["sources"])


def test_canonical_event_does_not_merge_different_lots(tmp_path):
    db = _db(tmp_path)
    _, entity_id = _add_notice(db, 1, "道路工程第6标段施工中标结果", progress="CONTRACTOR_SELECTED", body="中标人：某建设有限公司", source_site="来源A")
    _add_notice(db, 2, "道路工程第7标段施工中标结果", progress="CONTRACTOR_SELECTED", body="中标人：某建设有限公司", source_site="来源B", entity_id=entity_id)
    ProjectEventService(db).build()
    assert len(ProjectEventService(db).canonical_events(entity_id)) == 2


def test_canonical_event_does_not_merge_candidate_and_final(tmp_path):
    db = _db(tmp_path)
    _, entity_id = _add_notice(db, 1, "道路工程第6标段中标候选人公示", progress="CONTRACTOR_CANDIDATE_SELECTED", source_site="来源A")
    _add_notice(db, 2, "道路工程第6标段施工中标结果", progress="CONTRACTOR_SELECTED", source_site="来源B", entity_id=entity_id)
    ProjectEventService(db).build()
    assert len(ProjectEventService(db).canonical_events(entity_id)) == 2


def test_canonical_event_does_not_merge_different_parties(tmp_path):
    db = _db(tmp_path)
    _, entity_id = _add_notice(db, 1, "道路工程第6标段施工中标结果", progress="CONTRACTOR_SELECTED", body="中标人：甲公司有限公司", source_site="来源A")
    _add_notice(db, 2, "道路工程第6标段施工中标结果", progress="CONTRACTOR_SELECTED", body="中标人：乙公司有限公司", source_site="来源B", entity_id=entity_id)
    ProjectEventService(db).build()
    assert len(ProjectEventService(db).canonical_events(entity_id)) == 2


def test_explicit_date_wins_over_cross_source_publication_date(tmp_path):
    db = _db(tmp_path)
    _, entity_id = _add_notice(
        db, 1, "道路工程第6标段施工中标结果", progress="CONTRACTOR_SELECTED",
        body="中标人：某建设有限公司\n中标日期：2026年9月1日", publish_date="2026-09-05", source_site="来源A",
    )
    _add_notice(
        db, 2, "道路工程第6标段施工中标结果", progress="CONTRACTOR_SELECTED",
        body="中标人：某建设有限公司", publish_date="2026-09-05", source_site="来源B", entity_id=entity_id,
    )
    ProjectEventService(db).build()
    fact = ProjectEventService(db).canonical_events(entity_id)[0]
    assert fact["event_date"] == "2026-09-01"
    assert fact["event_date_source"] == "NOTICE_EXPLICIT_DATE"
    assert fact["source_count"] == 2


def test_canonical_timeline_is_idempotent_and_sources_are_optional(tmp_path):
    db = _db(tmp_path)
    _, entity_id = _add_notice(db, 1, "道路工程施工招标公告", progress="CONSTRUCTION_TENDER_OPEN")
    service = ProjectEventService(db)
    service.build()
    assert service.timeline(entity_id) == service.timeline(entity_id)
    assert "sources" not in service.timeline(entity_id)["events"][0]
    assert len(service.timeline(entity_id, show_sources=True)["events"][0]["sources"]) == 1


def test_cli_show_sources_prints_source_events(tmp_path, monkeypatch, capsys):
    from opportunity_radar import cli

    db = _db(tmp_path)
    notice_id, entity_id = _add_notice(db, 1, "道路工程施工招标公告", progress="CONSTRUCTION_TENDER_OPEN")
    ProjectEventService(db).build()
    monkeypatch.setattr(cli, "database", lambda: db)
    cli.cmd_project_timeline(SimpleNamespace(engineering_project_id=entity_id, show_sources=True))
    output = capsys.readouterr().out
    assert "来源事件" in output
    assert f"公告 {notice_id}" in output
    assert "Sources: 1" in output
    assert "证据：" in output


def test_cli_default_timeline_prints_canonical_source_count(tmp_path, monkeypatch, capsys):
    from opportunity_radar import cli

    db = _db(tmp_path)
    _, entity_id = _add_notice(db, 1, "道路工程施工招标公告", progress="CONSTRUCTION_TENDER_OPEN")
    ProjectEventService(db).build()
    monkeypatch.setattr(cli, "database", lambda: db)
    cli.cmd_project_timeline(SimpleNamespace(engineering_project_id=entity_id, show_sources=False))
    output = capsys.readouterr().out
    assert "Sources: 1" in output
    assert "来源事件" not in output
