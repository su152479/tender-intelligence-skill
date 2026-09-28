import sqlite3

from opportunity_radar.db import Database
from opportunity_radar.identity import NoticeIdentityService
from opportunity_radar.identity_reviews import IdentityReviewService
from opportunity_radar.models import Project
from opportunity_radar.project_candidates import ProjectCandidateService
from opportunity_radar.project_entities import ProjectEntityService


def _db(tmp_path):
    db = Database(tmp_path / "review.db")
    db.init()
    return db


def _notice(db, index=1, **kwargs):
    values = {
        "name": "北运河综合治理工程施工招标公告", "region": "北京市",
        "owner": "北京市水务建设中心", "source_site": "中国政府采购网",
        "url": f"https://example.test/review/{index}",
    }
    values.update(kwargs)
    return db.upsert(Project(**values))


def _fact(db, notice_id, fact_type, source_type=None):
    with db.connect() as con:
        sql = "SELECT * FROM notice_identity_fact WHERE notice_id=? AND fact_type=? AND is_active=1"
        params = [notice_id, fact_type]
        if source_type:
            sql += " AND source_type=?"
            params.append(source_type)
        sql += " ORDER BY id LIMIT 1"
        con.row_factory = sqlite3.Row
        return con.execute(sql, params).fetchone()


def test_confirm_preserves_machine_fact_and_enters_summary(tmp_path):
    db = _db(tmp_path)
    notice_id = _notice(db, source_site="中铁鲁班网", owner="", region="北京市")
    fact = _fact(db, notice_id, "REGION")
    result = IdentityReviewService(db).review_fact(fact["id"], "CONFIRM", note="人工核对")
    assert result["decision"] == "CONFIRM"
    assert _fact(db, notice_id, "REGION")["source_type"] != "HUMAN"
    assert NoticeIdentityService(db).current_identities()[notice_id].region_value == "北京市"


def test_reject_preserves_fact_but_removes_it_from_summary(tmp_path):
    db = _db(tmp_path)
    notice_id = _notice(db)
    fact = _fact(db, notice_id, "OWNER")
    IdentityReviewService(db).review_fact(fact["id"], "REJECT")
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM notice_identity_fact WHERE id=?", (fact["id"],)).fetchone()[0] == 1
    assert NoticeIdentityService(db).current_identities()[notice_id].owner == ""


def test_confirm_can_explicitly_accept_owner_previously_marked_contaminated(tmp_path):
    db = _db(tmp_path)
    notice_id = _notice(db, owner="某建设单位 联系人：张三")
    fact = _fact(db, notice_id, "OWNER")
    assert NoticeIdentityService(db).current_identities()[notice_id].owner == ""
    IdentityReviewService(db).review_fact(fact["id"], "CONFIRM")
    identity = NoticeIdentityService(db).current_identities()[notice_id]
    assert identity.owner == fact["raw_value"]
    assert identity.owner_quality == "HUMAN_CONFIRMED"


def test_override_creates_high_confidence_human_fact_and_wins(tmp_path):
    db = _db(tmp_path)
    notice_id = _notice(db)
    result = IdentityReviewService(db).override(notice_id, "OWNER", "人工确认建设单位")
    identity = NoticeIdentityService(db).current_identities()[notice_id]
    assert identity.owner == "人工确认建设单位"
    with db.connect() as con:
        fact = con.execute("SELECT source_type,confidence FROM notice_identity_fact WHERE id=?", (result["human_fact_id"],)).fetchone()
    assert fact == ("HUMAN", "HIGH")


def test_clear_empties_field_without_deleting_machine_facts(tmp_path):
    db = _db(tmp_path)
    notice_id = _notice(db)
    before = len(NoticeIdentityService(db).facts(notice_id))
    IdentityReviewService(db).clear(notice_id, "REGION", note="当前资料无法确认")
    assert NoticeIdentityService(db).current_identities()[notice_id].region_value == ""
    assert len(NoticeIdentityService(db).facts(notice_id)) == before


def test_extractor_upgrade_preserves_human_override_and_rejection(tmp_path):
    db = _db(tmp_path)
    notice_id = _notice(db)
    owner_fact = _fact(db, notice_id, "OWNER")
    service = IdentityReviewService(db)
    service.review_fact(owner_fact["id"], "REJECT")
    service.override(notice_id, "REGION", "天津市")
    NoticeIdentityService(db, "identity-v9-test").extract_notice(notice_id)
    identity = NoticeIdentityService(db).current_identities()[notice_id]
    assert identity.owner == ""
    assert identity.region_value == "天津市"
    assert any(row["source_type"] == "HUMAN" and row["is_active"] for row in NoticeIdentityService(db).facts(notice_id))


def test_extractor_upgrade_preserves_confirmed_old_machine_fact(tmp_path):
    db = _db(tmp_path)
    notice_id = _notice(db, name="地铁22号线东大桥站工程")
    station = _fact(db, notice_id, "STATION")
    IdentityReviewService(db).review_fact(station["id"], "CONFIRM", note="人工核对站名")
    NoticeIdentityService(db, "identity-v-next").extract_notice(notice_id)
    with db.connect() as con:
        active = con.execute("SELECT is_active FROM notice_identity_fact WHERE id=?", (station["id"],)).fetchone()[0]
    assert active == 1
    assert "东大桥站" in NoticeIdentityService(db).current_identities()[notice_id].stations


def test_review_history_is_append_only_and_repeat_is_idempotent(tmp_path):
    db = _db(tmp_path)
    notice_id = _notice(db)
    fact = _fact(db, notice_id, "OWNER")
    service = IdentityReviewService(db)
    first = service.review_fact(fact["id"], "CONFIRM", note="核对")
    repeated = service.review_fact(fact["id"], "CONFIRM", note="核对")
    rejected = service.review_fact(fact["id"], "REJECT", note="新资料")
    history = service.review_history(notice_id)
    assert repeated["id"] == first["id"] and repeated["idempotent"]
    assert len(history) == 2
    assert not history[0]["is_active"] and history[1]["id"] == rejected["id"]


def test_rejected_old_fact_does_not_block_new_extractor_evidence(tmp_path):
    db = _db(tmp_path)
    notice_id = _notice(db)
    old = _fact(db, notice_id, "OWNER")
    IdentityReviewService(db).review_fact(old["id"], "REJECT")
    _notice(db, owner="天津市水务建设中心")
    current = _fact(db, notice_id, "OWNER")
    assert current["id"] != old["id"]
    assert NoticeIdentityService(db).current_identities()[notice_id].owner == "天津市水务建设中心"


def test_multi_value_lots_can_all_be_confirmed(tmp_path):
    db = _db(tmp_path)
    notice_id = _notice(db, name="顺平县桥梁恢复工程（一、二、三标段）")
    service = IdentityReviewService(db)
    facts = [row for row in NoticeIdentityService(db).facts(notice_id, active_only=True) if row["fact_type"] == "LOT"]
    for fact in facts:
        service.review_fact(fact["id"], "CONFIRM")
    assert NoticeIdentityService(db).current_identities()[notice_id].lots == ("1", "2", "3")
    assert len([row for row in service.review_history(notice_id) if row["is_active"]]) == 3


def test_multiple_project_identifiers_can_coexist_and_be_confirmed(tmp_path):
    db = _db(tmp_path)
    notice_id = _notice(
        db, project_no="S110000A001037484009",
        source_site="北京市公共资源交易服务平台",
        raw_text="项目代码：2411-130202-89-01-637015",
    )
    service = IdentityReviewService(db)
    facts = [row for row in NoticeIdentityService(db).facts(notice_id, active_only=True) if row["fact_type"] == "PROJECT_IDENTIFIER"]
    for fact in facts:
        service.review_fact(fact["id"], "CONFIRM")
    assert len(NoticeIdentityService(db).current_identities()[notice_id].identifiers) == 2


def test_identifier_override_requires_and_records_semantics(tmp_path):
    db = _db(tmp_path)
    notice_id = _notice(db)
    IdentityReviewService(db).override(
        notice_id, "PROJECT_IDENTIFIER", "G13060020260001",
        identifier_type="PROJECT_CODE", identifier_namespace="HEBEI_GGZY",
        identity_strength="STRONG",
    )
    item = NoticeIdentityService(db).current_identities()[notice_id].identifiers[0]
    assert (item.identifier_type, item.namespace, item.strength) == (
        "PROJECT_CODE", "HEBEI_GGZY", "STRONG",
    )


def test_candidate_responds_to_reject_without_formal_link_mutation(tmp_path):
    db = _db(tmp_path)
    first = _notice(db, 1)
    _notice(db, 2, name="北运河综合治理工程中标结果公告")
    candidates = ProjectCandidateService(db)
    candidates.build()
    before = candidates.list_candidates()[0]["evidence_score"]
    formal_before = ProjectEntityService(db).statistics()["linked_notices"]
    IdentityReviewService(db).review_fact(_fact(db, first, "OWNER")["id"], "REJECT")
    candidates.build()
    after = candidates.list_candidates()[0]["evidence_score"]
    assert after < before
    assert ProjectEntityService(db).statistics()["linked_notices"] == formal_before == 0


def test_formal_relation_conflict_is_report_only(tmp_path):
    db = _db(tmp_path)
    notice_id = _notice(db)
    entity_id = ProjectEntityService(db).confirm_notices([notice_id], canonical_name="北运河工程")
    with db.connect() as con:
        con.execute("UPDATE engineering_project SET region='北京市' WHERE id=?", (entity_id,))
    IdentityReviewService(db).override(notice_id, "REGION", "天津市")
    conflicts = IdentityReviewService(db).formal_relation_conflicts()
    assert conflicts[0]["status"] == "FORMAL_RELATION_CONFLICT"
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM project_notice_link").fetchone()[0] == 1


def test_dry_run_does_not_write_review_or_change_formal_relations(tmp_path):
    db = _db(tmp_path)
    notice_id = _notice(db)
    fact = _fact(db, notice_id, "OWNER")
    result = IdentityReviewService(db).review_fact(fact["id"], "REJECT", dry_run=True)
    assert result["dry_run"]
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM notice_identity_review").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM project_notice_link").fetchone()[0] == 0


def test_review_queue_contains_context_priority_and_impact(tmp_path):
    db = _db(tmp_path)
    _notice(db, 1, owner="", raw_text="建设单位：北京市水务建设中心")
    _notice(db, 2, name="北运河综合治理工程中标结果公告")
    ProjectCandidateService(db).build()
    queue = IdentityReviewService(db).queue(fact_type="OWNER", limit=10)
    assert queue[0]["priority"] == "P0"
    assert queue[0]["title"] and queue[0]["source_location"]
    assert queue[0]["candidate_impact"]
