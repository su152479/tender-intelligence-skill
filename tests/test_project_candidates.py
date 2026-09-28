import json

import pytest

from opportunity_radar.db import Database
from opportunity_radar.models import Project
from opportunity_radar.project_candidates import ALGORITHM_VERSION, ProjectCandidateService


def _notice(
    db, name, index, *, region="北京市", owner="北京市建设中心",
    publish_date="2026-09-01", source_site="中国政府采购网",
):
    return db.upsert(Project(
        name=name, region=region, owner=owner, publish_date=publish_date,
        source_site=source_site, url=f"https://example.test/candidate/{index}",
    ))


def _service(tmp_path):
    db = Database(tmp_path / "candidate.db"); db.init()
    return db, ProjectCandidateService(db)


def test_candidate_pair_is_canonical_and_idempotent(tmp_path):
    db, service = _service(tmp_path)
    ids = [_notice(db, "北运河综合治理工程施工招标公告", index) for index in (2, 1)]
    first = service.build(); second = service.build()
    with db.connect() as con:
        rows = con.execute("SELECT notice_id_a,notice_id_b,candidate_key FROM project_link_candidate").fetchall()
    assert len(rows) == 1
    assert rows[0] == (min(ids), max(ids), f"{min(ids)}:{max(ids)}")
    assert first["generated_candidates"] == second["generated_candidates"] == 1


def test_candidate_that_no_longer_meets_rules_leaves_active_queue(tmp_path):
    db, service = _service(tmp_path)
    _notice(db, "北运河综合治理工程施工招标公告", 1)
    _notice(db, "北运河综合治理工程中标结果公告", 2)
    service.build(); assert len(service.list_candidates()) == 1
    db.upsert(Project(
        name="完全不同的医院建设工程中标结果公告", region="北京市", owner="北京市建设中心",
        publish_date="2026-09-01", source_site="中国政府采购网",
        url="https://example.test/candidate/2",
    ))
    service.build()
    assert service.list_candidates() == []
    with db.connect() as con:
        assert con.execute("SELECT is_active FROM project_link_candidate").fetchone()[0] == 0


def test_matching_name_region_owner_is_high_candidate(tmp_path):
    db, service = _service(tmp_path)
    _notice(db, "北运河综合治理工程施工招标公告", 1)
    _notice(db, "北运河综合治理工程中标结果公告", 2)
    service.build()
    candidate = service.list_candidates()[0]
    assert candidate["evidence_level"] == "HIGH"
    assert candidate["evidence_score"] >= 90
    assert candidate["candidate_relation"] == "SAME_PROJECT"


def test_candidate_separates_project_identity_from_procurement_object(tmp_path):
    db, service = _service(tmp_path)
    _notice(db, "长石一标项目管片防水材料采购招标公告", 1)
    _notice(db, "长石一标项目管片螺栓采购招标公告", 2)
    service.build(); evidence = service.list_candidates()[0]["evidence"]
    assert evidence["core_name_a"] == evidence["core_name_b"] == "长石一标项目"
    assert evidence["signals"]["procurement_object_a"] == "管片防水材料采购"
    assert evidence["signals"]["procurement_object_b"] == "管片螺栓采购"


def test_different_functional_scopes_are_only_same_parent_candidate(tmp_path):
    db, service = _service(tmp_path)
    prefix = "孙河组团2902-73地块B4综合性商业金融服务业用地项目"
    _notice(db, f"{prefix}酒店装配式预制混凝土构件采购", 1, owner="")
    _notice(db, f"{prefix}商业、山姆装配式预制混凝土构件采购", 2, owner="")
    service.build(); candidate = service.list_candidates()[0]
    assert candidate["candidate_relation"] == "SAME_PARENT_PROJECT"
    assert candidate["evidence_score"] == 80
    assert candidate["evidence"]["signals"]["subproject_scope_a"] == "酒店"
    assert candidate["evidence"]["signals"]["subproject_scope_b"] == "商业、山姆"
    assert "子工程/功能分区不同" in candidate["evidence"]["negative_evidence"]


def test_same_title_result_pages_with_different_winners_are_not_same_project(tmp_path):
    db, service = _service(tmp_path)
    title = "G1306362600112001顺平县2025年水毁农村公路桥梁恢复重建工程(一、二、三标段)中标结果公告"
    for index, winner in ((1, "甲路桥公司"), (2, "乙建筑公司")):
        db.upsert(Project(
            name=title, region="河北省", owner="", publish_date="2026-09-01",
            source_site="京津冀公共资源交易协同专区",
            url=f"https://example.test/result/{index}", raw_text=f"中标人：{winner}\n中标金额：{index}00万元",
        ))
    service.build(); candidate = service.list_candidates()[0]
    assert candidate["candidate_relation"] == "SAME_PARENT_PROJECT"
    assert "同名结果页的中标人不同" in candidate["evidence"]["negative_evidence"]
    cluster = service.clusters()[0]
    with pytest.raises(ValueError, match="中标人/详情页冲突"):
        service.review_cluster(cluster["cluster_id"], confirm_notices=cluster["notice_ids"])


def test_different_lots_are_only_same_parent_candidate(tmp_path):
    db, service = _service(tmp_path)
    _notice(db, "高标准农田建设项目施工标段二（三次）招标公告", 1)
    _notice(db, "高标准农田建设项目施工标段三（三次）招标公告", 2)
    service.build()
    candidate = service.list_candidates()[0]
    assert candidate["candidate_relation"] == "SAME_PARENT_PROJECT"
    assert candidate["evidence_level"] == "MEDIUM"
    assert "标段不同" in candidate["evidence"]["negative_evidence"]


@pytest.mark.parametrize("field_a,field_b,kwargs", [
    ("北京市", "河北省", "region"),
    ("甲建设单位", "乙建设单位", "owner"),
])
def test_region_or_owner_conflict_is_not_candidate(tmp_path, field_a, field_b, kwargs):
    db, service = _service(tmp_path)
    options_a = {kwargs: field_a}; options_b = {kwargs: field_b}
    _notice(db, "开发区基础设施工程施工招标公告", 1, **options_a)
    _notice(db, "开发区基础设施工程中标结果公告", 2, **options_b)
    service.build()
    assert service.list_candidates() == []


@pytest.mark.parametrize("name_a,name_b", [
    ("轨道交通22号线东大桥站综合开发工程招标公告", "轨道交通22号线平谷站综合开发工程招标公告"),
    ("北运河治理工程一期施工招标公告", "北运河治理工程二期施工招标公告"),
    ("2025年道路养护工程施工招标公告", "2026年道路养护工程施工招标公告"),
])
def test_strong_identity_conflicts_do_not_enter_queue(tmp_path, name_a, name_b):
    db, service = _service(tmp_path)
    _notice(db, name_a, 1); _notice(db, name_b, 2)
    service.build()
    assert service.list_candidates() == []


def test_rejected_candidate_stays_rejected_after_rebuild(tmp_path):
    db, service = _service(tmp_path)
    _notice(db, "河道治理工程施工招标公告", 1)
    _notice(db, "河道治理工程中标结果公告", 2)
    service.build(); candidate_id = service.list_candidates()[0]["id"]
    service.review(candidate_id, "REJECT", "不是同一工程")
    service.build()
    assert service.list_candidates("PENDING") == []
    assert service.list_candidates("REJECTED")[0]["review_note"] == "不是同一工程"


def test_confirm_candidate_creates_human_formal_links(tmp_path):
    db, service = _service(tmp_path)
    ids = [
        _notice(db, "河道治理工程施工招标公告", 1),
        _notice(db, "河道治理工程中标结果公告", 2),
    ]
    service.build(); candidate_id = service.list_candidates()[0]["id"]
    result = service.review(candidate_id, "CONFIRM", "人工核对")
    with db.connect() as con:
        links = con.execute(
            "SELECT notice_id,match_method,confirmed_by_human FROM project_notice_link ORDER BY notice_id"
        ).fetchall()
    assert result["candidate_status"] == "CONFIRMED"
    assert links == [(ids[0], "HUMAN_CONFIRMED", 1), (ids[1], "HUMAN_CONFIRMED", 1)]


def test_confirm_review_reuses_project_entity_service(tmp_path, monkeypatch):
    db, service = _service(tmp_path)
    ids = [_notice(db, "桥梁改造工程施工招标公告", 1), _notice(db, "桥梁改造工程中标结果公告", 2)]
    service.build(); candidate_id = service.list_candidates()[0]["id"]
    called = {}

    def fake_confirm(instance, notice_ids, **kwargs):
        called["ids"] = notice_ids
        return 88

    monkeypatch.setattr("opportunity_radar.project_candidates.ProjectEntityService.confirm_notices", fake_confirm)
    result = service.review(candidate_id, "CONFIRM")
    assert called["ids"] == ids
    assert result["engineering_project_id"] == 88


def test_deferred_candidate_is_preserved_after_rebuild(tmp_path):
    db, service = _service(tmp_path)
    _notice(db, "泵站更新工程施工招标公告", 1)
    _notice(db, "泵站更新工程中标结果公告", 2)
    service.build(); candidate_id = service.list_candidates()[0]["id"]
    service.review(candidate_id, "DEFER", "等待正式编号")
    service.build()
    assert service.list_candidates("PENDING") == []
    assert service.list_candidates("DEFERRED")[0]["review_note"] == "等待正式编号"


def test_deferred_candidate_reopens_only_after_evidence_changes(tmp_path):
    db, service = _service(tmp_path)
    _notice(db, "泵站更新工程施工招标公告", 1, owner="")
    _notice(db, "泵站更新工程中标结果公告", 2)
    service.build(); candidate_id = service.list_candidates()[0]["id"]
    service.review(candidate_id, "DEFER", "等待建设单位")
    db.upsert(Project(
        name="泵站更新工程施工招标公告", region="北京市", owner="北京市建设中心",
        publish_date="2026-09-01", source_site="中国政府采购网",
        url="https://example.test/candidate/1",
    ))
    service.build()
    candidate = service.list_candidates("PENDING")[0]
    assert candidate["id"] == candidate_id
    assert candidate["evidence_level"] == "HIGH"
    assert candidate["reviewed_at"] is None


def test_candidate_records_algorithm_version_and_readable_evidence(tmp_path):
    db, service = _service(tmp_path)
    _notice(db, "水库除险加固工程施工招标公告", 1)
    _notice(db, "水库除险加固工程中标结果公告", 2)
    service.build(); candidate = service.list_candidates()[0]
    assert candidate["algorithm_version"] == ALGORITHM_VERSION
    assert candidate["evidence"]["components"]
    assert candidate["evidence"]["evidence_score"] == candidate["evidence_score"]


def test_candidate_build_does_not_change_notice_observation(tmp_path):
    db, service = _service(tmp_path)
    notice_id = _notice(db, "水闸改造工程施工招标公告", 1)
    with db.connect() as con:
        run_id = con.execute(
            """INSERT INTO source_run(source_id,source_name,started_at,finished_at,status)
            VALUES('test','测试来源','2026-09-01T09:00:00','2026-09-01T09:01:00','success')"""
        ).lastrowid
        con.execute(
            """INSERT INTO notice_observation(run_id,notice_id,source_id,observed_at,is_first_seen,content_hash,content_changed)
            VALUES(?,?,?,'2026-09-01T09:00:30',1,?,0)""",
            (run_id, notice_id, "test", "a" * 64),
        )
        before = con.execute("SELECT * FROM notice_observation").fetchall()
    service.build()
    with db.connect() as con:
        after = con.execute("SELECT * FROM notice_observation").fetchall()
    assert after == before


def test_three_candidate_pairs_form_one_dynamic_cluster(tmp_path):
    db, service = _service(tmp_path)
    ids = [_notice(db, "同一河道治理工程施工招标公告", index) for index in range(3)]
    service.build(); clusters = service.clusters()
    assert len(clusters) == 1
    assert clusters[0]["notice_ids"] == ids
    assert len(clusters[0]["edges"]) == 3


def test_connected_cluster_with_end_to_end_conflict_cannot_confirm_all(tmp_path):
    db, service = _service(tmp_path)
    ids = [
        _notice(db, "北运河治理工程一期施工招标公告", 1),
        _notice(db, "北运河治理工程施工招标公告", 2),
        _notice(db, "北运河治理工程二期施工招标公告", 3),
    ]
    service.build(); cluster = service.clusters()[0]
    assert len(cluster["edges"]) == 2
    assert any("期次冲突" in item["conflicts"] for item in cluster["identity_conflicts"])
    with pytest.raises(ValueError, match="期次冲突"):
        service.review_cluster(cluster["cluster_id"], confirm_notices=ids)
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM project_notice_link").fetchone()[0] == 0


def test_confirm_subset_only_links_selected_notices(tmp_path):
    db, service = _service(tmp_path)
    ids = [_notice(db, "同一泵站改造工程施工招标公告", index) for index in range(3)]
    service.build(); cluster = service.clusters()[0]
    result = service.review_cluster(cluster["cluster_id"], confirm_notices=ids[:2], note="确认子集")
    with db.connect() as con:
        links = con.execute("SELECT notice_id FROM project_notice_link ORDER BY notice_id").fetchall()
        statuses = con.execute(
            "SELECT notice_id_a,notice_id_b,candidate_status FROM project_link_candidate ORDER BY id"
        ).fetchall()
    assert result["confirmed_notice_ids"] == ids[:2]
    assert links == [(ids[0],), (ids[1],)]
    assert (ids[0], ids[1], "CONFIRMED") in statuses
    assert all(row[2] == "PENDING" for row in statuses if ids[2] in row[:2])


def test_confirm_parent_group_records_edge_without_formal_link(tmp_path):
    db, service = _service(tmp_path)
    _notice(db, "高标准农田建设项目施工标段二招标公告", 1)
    _notice(db, "高标准农田建设项目施工标段三招标公告", 2)
    service.build(); cluster = service.clusters()[0]
    edge = cluster["edges"][0]
    assert edge["candidate_relation"] == "SAME_PARENT_PROJECT"
    result = service.review_cluster(
        cluster["cluster_id"], confirm_parent_candidates=[edge["candidate_id"]], note="同一上级项目"
    )
    with db.connect() as con:
        status = con.execute("SELECT candidate_status FROM project_link_candidate").fetchone()[0]
        link_count = con.execute("SELECT COUNT(*) FROM project_notice_link").fetchone()[0]
    assert result["engineering_project_id"] is None
    assert status == "CONFIRMED"
    assert link_count == 0


def test_pair_confirm_parent_also_does_not_create_formal_link(tmp_path):
    db, service = _service(tmp_path)
    _notice(db, "道路工程一标段招标公告", 1)
    _notice(db, "道路工程二标段招标公告", 2)
    service.build(); candidate = service.list_candidates()[0]
    result = service.review(candidate["id"], "CONFIRM")
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM project_notice_link").fetchone()[0] == 0
    assert result["engineering_project_id"] is None


def test_source_default_region_cannot_add_positive_region_evidence(tmp_path):
    db, service = _service(tmp_path)
    prefix = "中铁北京工程局集团有限公司甬绍干线天然气管道西段工程隧道施工一标项目"
    _notice(db, f"{prefix}减水剂采购", 1, region="北京市", owner="", source_site="中铁鲁班网")
    _notice(db, f"{prefix}土工布采购", 2, region="北京市", owner="", source_site="中铁鲁班网")
    service.build()
    assert service.list_candidates() == []
    audit = service.identity_audit()
    assert audit["region_source_counts"]["TEXT_INFERRED_LOW"] == 2


def test_contaminated_owner_does_not_receive_owner_bonus(tmp_path):
    db, service = _service(tmp_path)
    polluted = "某建设单位\n联系人：张三\n电话：12345678" * 8
    _notice(db, "河道治理工程施工招标公告", 1, owner=polluted)
    _notice(db, "河道治理工程中标结果公告", 2, owner=polluted)
    service.build(); candidate = service.list_candidates()[0]
    keys = {item["key"] for item in candidate["evidence"]["components"]}
    assert "owner_match" not in keys
    assert candidate["evidence"]["signals"]["owner_quality_a"] == "CONTAMINATED"
    assert candidate["evidence_level"] == "MEDIUM"


def test_structured_owner_still_receives_strong_bonus(tmp_path):
    db, service = _service(tmp_path)
    _notice(db, "河道治理工程施工招标公告", 1)
    _notice(db, "河道治理工程中标结果公告", 2)
    service.build(); candidate = service.list_candidates()[0]
    assert any(item["key"] == "owner_match" and item["score"] == 25 for item in candidate["evidence"]["components"])
    assert candidate["evidence_level"] == "HIGH"


def test_cluster_dynamic_rebuild_is_idempotent(tmp_path):
    db, service = _service(tmp_path)
    for index in range(3):
        _notice(db, "同一市政道路工程施工招标公告", index)
    service.build()
    first = service.clusters(); second = service.clusters()
    assert first == second
