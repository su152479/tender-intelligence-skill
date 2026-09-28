import hashlib
import json
from types import SimpleNamespace

import pytest

from opportunity_radar.db import Database
from opportunity_radar.models import Project
from opportunity_radar.project_events import ProjectEventService
from opportunity_radar.project_lifecycle import ProjectLifecycleAggregator


def _db(tmp_path):
    db = Database(tmp_path / "lifecycle.db")
    db.init()
    return db


def _project(db, name="测试工程", project_id=None):
    with db.connect() as con:
        if project_id is None:
            return int(con.execute(
                "INSERT INTO engineering_project(identity_key,canonical_name) VALUES(?,?)",
                (f"test:{name}", name),
            ).lastrowid)
        con.execute(
            "INSERT INTO engineering_project(id,identity_key,canonical_name) VALUES(?,?,?)",
            (project_id, f"test:{project_id}", name),
        )
        return project_id


def _event(
    db, entity_id, index, event_type, event_date="2026-09-01", *, lot="",
    scope="", party="", confidence="HIGH", source="来源A", product="",
):
    notice = Project(
        name=f"测试公告{index}", publish_date=event_date, source_site=source,
        url=f"https://example.test/lifecycle/{index}/{source}", raw_text=f"事件{event_type}",
    )
    notice_id = db.upsert(notice)
    key = hashlib.sha256(f"{entity_id}|{index}|{source}|{event_type}".encode()).hexdigest()
    with db.connect() as con:
        con.execute(
            """INSERT INTO project_notice_link(
            engineering_project_id,notice_id,match_method,match_score,confirmed_by_human)
            VALUES(?,?,?,100,1)""",
            (entity_id, notice_id, "HUMAN_CONFIRMED"),
        )
        con.execute(
            """INSERT INTO project_event(
            engineering_project_id,notice_id,event_type,event_date,event_date_source,event_scope,
            event_subject,party_name,party_role,product,lot,phase,source_type,source_reference,
            evidence_text,confidence,extractor_version,event_key,is_active)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (
                entity_id, notice_id, event_type, event_date, "NOTICE_EXPLICIT_DATE", scope,
                event_type, party, "CONTRACTOR" if party else "", product, lot, "", source,
                notice.url, f"{event_type}证据", confidence, "test", key,
            ),
        )
    return notice_id


@pytest.mark.parametrize(
    "event_type,stage",
    [
        ("TENDER_ANNOUNCED", "TENDERING"),
        ("CONTRACTOR_CANDIDATE_SELECTED", "CONTRACTOR_CANDIDATE_SELECTED"),
        ("CONTRACTOR_SELECTED", "CONTRACTOR_SELECTED"),
        ("CONSTRUCTION_PROGRESS", "UNDER_CONSTRUCTION"),
    ],
)
def test_direct_event_to_stage_mapping(tmp_path, event_type, stage):
    db = _db(tmp_path); entity = _project(db)
    _event(db, entity, 1, event_type)
    result = ProjectLifecycleAggregator(db).lifecycle(entity)
    assert result["current_stage"] == stage
    assert result["stage_confidence"] in {"HIGH", "MEDIUM"}


def test_tender_candidate_selected_evolves_within_same_lot(tmp_path):
    db = _db(tmp_path); entity = _project(db)
    _event(db, entity, 1, "TENDER_ANNOUNCED", "2026-08-01", lot="6", scope="标段:6")
    _event(db, entity, 2, "CONTRACTOR_CANDIDATE_SELECTED", "2026-08-20", lot="6", scope="标段:6")
    _event(db, entity, 3, "CONTRACTOR_SELECTED", "2026-09-01", lot="6", scope="标段:6")
    result = ProjectLifecycleAggregator(db).lifecycle(entity)
    assert result["current_stage"] == "CONTRACTOR_SELECTED"
    assert result["previous_stage"] == "CONTRACTOR_CANDIDATE_SELECTED"
    assert result["stage_since"] == "2026-09-01"


def test_candidate_never_becomes_final_selected(tmp_path):
    db = _db(tmp_path); entity = _project(db)
    _event(db, entity, 1, "CONTRACTOR_CANDIDATE_SELECTED", lot="1", scope="标段:1")
    assert ProjectLifecycleAggregator(db).lifecycle(entity)["current_stage"] == "CONTRACTOR_CANDIDATE_SELECTED"


def test_later_tender_does_not_regress_selected_stage(tmp_path):
    db = _db(tmp_path); entity = _project(db)
    _event(db, entity, 1, "CONTRACTOR_SELECTED", "2026-08-01", lot="6", scope="标段:6")
    _event(db, entity, 2, "TENDER_ANNOUNCED", "2026-09-01", lot="7", scope="标段:7")
    assert ProjectLifecycleAggregator(db).lifecycle(entity)["current_stage"] == "CONTRACTOR_SELECTED"


def test_multiple_selected_lots_are_partial_coverage(tmp_path):
    db = _db(tmp_path); entity = _project(db)
    _event(db, entity, 1, "CONTRACTOR_SELECTED", lot="6", scope="标段:6")
    _event(db, entity, 2, "CONTRACTOR_SELECTED", lot="7", scope="标段:7")
    result = ProjectLifecycleAggregator(db).lifecycle(entity)
    assert result["coverage_scope"] == "PARTIAL_LOTS"
    assert result["coverage_detail"] == ["LOT 6,7"]


def test_single_lot_is_single_lot_coverage(tmp_path):
    db = _db(tmp_path); entity = _project(db)
    _event(db, entity, 1, "TENDER_ANNOUNCED", lot="1", scope="标段:1")
    result = ProjectLifecycleAggregator(db).lifecycle(entity)
    assert (result["scope_level"], result["coverage_scope"]) == ("LOT_LEVEL", "SINGLE_LOT")


def test_missing_timeline_is_unknown(tmp_path):
    db = _db(tmp_path); entity = _project(db)
    result = ProjectLifecycleAggregator(db).lifecycle(entity)
    assert result["current_stage"] == "UNKNOWN"
    assert not result["stage_evidence"]


def test_project_wide_termination_requires_unscoped_project_fact(tmp_path):
    db = _db(tmp_path); entity = _project(db)
    _event(db, entity, 1, "PROJECT_TERMINATED")
    result = ProjectLifecycleAggregator(db).lifecycle(entity)
    assert (result["current_stage"], result["coverage_scope"]) == ("TERMINATED", "PROJECT_WIDE")


def test_lot_termination_does_not_terminate_project(tmp_path):
    db = _db(tmp_path); entity = _project(db)
    _event(db, entity, 1, "PROJECT_TERMINATED", lot="1", scope="标段:1")
    result = ProjectLifecycleAggregator(db).lifecycle(entity)
    assert result["current_stage"] == "UNKNOWN"
    assert result["consistency_conflicts"][0]["code"] == "NARROW_TERMINATION_NOT_PROJECT_TERMINATION"


@pytest.mark.parametrize("event_type", ["PRODUCT_PROCUREMENT_ANNOUNCED", "PRODUCT_PROCUREMENT_RESULT", "PROCUREMENT_ANNOUNCED"])
def test_product_and_procurement_events_do_not_advance_lifecycle(tmp_path, event_type):
    db = _db(tmp_path); entity = _project(db)
    _event(db, entity, 1, event_type, product="管片")
    assert ProjectLifecycleAggregator(db).lifecycle(entity)["current_stage"] == "UNKNOWN"


def test_duplicate_source_events_are_consumed_once_as_canonical_evidence(tmp_path):
    db = _db(tmp_path); entity = _project(db)
    _event(db, entity, 1, "CONTRACTOR_SELECTED", lot="6", scope="标段:6", party="甲公司", source="来源A")
    _event(db, entity, 2, "CONTRACTOR_SELECTED", lot="6", scope="标段:6", party="甲公司", source="来源B")
    result = ProjectLifecycleAggregator(db).lifecycle(entity)
    assert len(result["stage_evidence"]) == 1
    assert result["stage_evidence"][0]["source_count"] == 2
    assert len(result["stage_source_events"]) == 2


def test_stage_since_reports_min_and_max_for_multiple_lots(tmp_path):
    db = _db(tmp_path); entity = _project(db)
    _event(db, entity, 1, "CONTRACTOR_SELECTED", "2026-09-01", lot="6", scope="标段:6")
    _event(db, entity, 2, "CONTRACTOR_SELECTED", "2026-09-03", lot="7", scope="标段:7")
    result = ProjectLifecycleAggregator(db).lifecycle(entity)
    assert result["stage_since"] == result["stage_since_min"] == "2026-09-01"
    assert result["stage_since_max"] == "2026-09-03"


def test_event_order_conflict_is_reported_only_for_same_known_scope(tmp_path):
    db = _db(tmp_path); entity = _project(db)
    _event(db, entity, 1, "CONTRACTOR_SELECTED", "2026-08-01", lot="6", scope="标段:6")
    _event(db, entity, 2, "TENDER_ANNOUNCED", "2026-09-01", lot="6", scope="标段:6")
    result = ProjectLifecycleAggregator(db).lifecycle(entity)
    assert result["consistency_conflicts"][0]["code"] == "LIFECYCLE_EVENT_ORDER_CONFLICT"


def test_different_lots_do_not_create_order_conflict(tmp_path):
    db = _db(tmp_path); entity = _project(db)
    _event(db, entity, 1, "CONTRACTOR_SELECTED", "2026-08-01", lot="6", scope="标段:6")
    _event(db, entity, 2, "TENDER_ANNOUNCED", "2026-09-01", lot="7", scope="标段:7")
    assert not ProjectLifecycleAggregator(db).lifecycle(entity)["consistency_conflicts"]


def test_project_52_and_59_are_calculated_independently(tmp_path):
    db = _db(tmp_path)
    first = _project(db, "国道234-009", 52); second = _project(db, "国道234-010", 59)
    _event(db, first, 1, "CONTRACTOR_CANDIDATE_SELECTED")
    for lot in (6, 7, 8, 9):
        _event(db, second, lot, "CONTRACTOR_SELECTED", lot=str(lot), scope=f"标段:{lot}")
    service = ProjectLifecycleAggregator(db)
    assert service.lifecycle(52)["current_stage"] == "CONTRACTOR_CANDIDATE_SELECTED"
    assert service.lifecycle(59)["current_stage"] == "CONTRACTOR_SELECTED"


def test_stage_evidence_is_traceable_to_canonical_and_source_events(tmp_path):
    db = _db(tmp_path); entity = _project(db)
    _event(db, entity, 1, "TENDER_ANNOUNCED", lot="1", scope="标段:1")
    result = ProjectLifecycleAggregator(db).lifecycle(entity)
    assert result["stage_evidence"][0]["canonical_event_key"]
    assert result["stage_source_events"][0]["project_event_id"]
    assert result["stage_source_events"][0]["source_url"]


def test_lifecycle_keeps_product_and_tracking_records_unchanged(tmp_path):
    db = _db(tmp_path); entity = _project(db)
    notice_id = _event(db, entity, 1, "CONTRACTOR_SELECTED", lot="1", scope="标段:1")
    with db.connect() as con:
        con.execute("UPDATE project SET project_tracking_recommendation='KEY_FOLLOW_UP' WHERE id=?", (notice_id,))
        con.execute(
            """INSERT INTO notice_product_assessment(
            notice_id,product,product_opportunity_status,product_demand_evidence_level,
            product_demand_evidence_type,procurement_window_status,analyzer_version,rules_version)
            VALUES(?,?,?,?,?,?,?,?)""",
            (notice_id, "管片", "FOLLOW_UP", "MEDIUM", "ACCESSORY_OR_SUPPORTING_PRODUCT", "UNKNOWN", "test", "test"),
        )
        before = (
            con.execute("SELECT project_tracking_recommendation,ai_score,analysis_json FROM project WHERE id=?", (notice_id,)).fetchone(),
            con.execute("SELECT * FROM notice_product_assessment WHERE notice_id=?", (notice_id,)).fetchall(),
        )
    ProjectLifecycleAggregator(db).lifecycle(entity)
    with db.connect() as con:
        after = (
            con.execute("SELECT project_tracking_recommendation,ai_score,analysis_json FROM project WHERE id=?", (notice_id,)).fetchone(),
            con.execute("SELECT * FROM notice_product_assessment WHERE notice_id=?", (notice_id,)).fetchall(),
        )
    assert before == after


def test_lifecycle_service_does_not_change_database_bytes(tmp_path):
    db = _db(tmp_path); entity = _project(db)
    _event(db, entity, 1, "TENDER_ANNOUNCED")
    before = hashlib.sha256(db.path.read_bytes()).hexdigest()
    ProjectLifecycleAggregator(db).lifecycle(entity)
    after = hashlib.sha256(db.path.read_bytes()).hexdigest()
    assert before == after


def test_lifecycle_does_not_update_persisted_lifecycle_stage(tmp_path):
    db = _db(tmp_path); entity = _project(db)
    _event(db, entity, 1, "CONTRACTOR_SELECTED", lot="1", scope="标段:1")
    with db.connect() as con:
        con.execute(
            "UPDATE engineering_project SET lifecycle_stage='LEGACY_VALUE' WHERE id=?",
            (entity,),
        )
    result = ProjectLifecycleAggregator(db).lifecycle(entity)
    with db.connect() as con:
        persisted = con.execute(
            "SELECT lifecycle_stage FROM engineering_project WHERE id=?", (entity,),
        ).fetchone()[0]
    assert result["current_stage"] == "CONTRACTOR_SELECTED"
    assert persisted == "LEGACY_VALUE"


def test_audit_is_read_only_and_reports_distributions(tmp_path):
    db = _db(tmp_path); a = _project(db, "A"); _project(db, "B")
    _event(db, a, 1, "TENDER_ANNOUNCED")
    result = ProjectLifecycleAggregator(db).audit(sample_limit=20)
    assert result["writes"] == 0
    assert result["engineering_projects"] == 2
    assert result["stage_distribution"] == {"TENDERING": 1, "UNKNOWN": 1}
    assert len(result["samples"]) == 2


def test_cli_lifecycle_show_events_is_read_only(tmp_path, monkeypatch, capsys):
    from opportunity_radar import cli

    db = _db(tmp_path); entity = _project(db)
    _event(db, entity, 1, "TENDER_ANNOUNCED", lot="1", scope="标段:1")
    before = hashlib.sha256(db.path.read_bytes()).hexdigest()
    monkeypatch.setattr(cli, "database", lambda: db)
    cli.cmd_project_lifecycle(SimpleNamespace(engineering_project_id=entity, show_events=True))
    output = capsys.readouterr().out
    assert "Current Observed Stage: TENDERING" in output
    assert "Canonical Event Timeline:" in output
    assert "Source Event" in output
    assert before == hashlib.sha256(db.path.read_bytes()).hexdigest()


def test_cli_lifecycle_audit_is_read_only(tmp_path, monkeypatch, capsys):
    from opportunity_radar import cli

    db = _db(tmp_path); entity = _project(db)
    _event(db, entity, 1, "TENDER_ANNOUNCED")
    before = hashlib.sha256(db.path.read_bytes()).hexdigest()
    monkeypatch.setattr(cli, "database", lambda: db)
    cli.cmd_project_lifecycle_audit(SimpleNamespace(samples=20))
    output = json.loads(capsys.readouterr().out)
    assert output["writes"] == 0
    assert output["stage_distribution"] == {"TENDERING": 1}
    assert before == hashlib.sha256(db.path.read_bytes()).hexdigest()


def test_read_only_canonical_query_does_not_change_database(tmp_path):
    db = _db(tmp_path); entity = _project(db)
    _event(db, entity, 1, "TENDER_ANNOUNCED")
    before = hashlib.sha256(db.path.read_bytes()).hexdigest()
    assert len(ProjectEventService(db).canonical_events(entity, read_only=True)) == 1
    assert before == hashlib.sha256(db.path.read_bytes()).hexdigest()
