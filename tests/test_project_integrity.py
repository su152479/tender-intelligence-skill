from opportunity_radar.db import Database
from opportunity_radar.models import Project
from opportunity_radar.project_integrity import (
    EngineeringProjectRelationCandidateService,
    direct_unlinked_notices,
)


def test_split_formal_identifiers_only_create_relation_candidate(tmp_path):
    db = Database(tmp_path / "integrity.db")
    db.init()
    project_ids = []
    for index, code in enumerate(("S110000A001037484009", "S110000A001037484010"), start=1):
        notice_id = db.upsert(Project(
            name="国道234（永宁—琉璃庙）道路工程土建工程(施工)中标候选人公示",
            project_no=code, region="北京市", source_site="北京市公共资源交易服务平台",
            url=f"https://example.test/g234/{index}",
        ))
        with db.connect() as con:
            project_id = int(con.execute(
                "INSERT INTO engineering_project(identity_key,canonical_name,region) VALUES(?,?,?)",
                (f"id:{code}", "国道234（永宁—琉璃庙）道路工程土建工程(施工)", "北京市"),
            ).lastrowid)
            con.execute(
                "INSERT INTO project_notice_link(engineering_project_id,notice_id,match_method,match_score) VALUES(?,?,?,?)",
                (project_id, notice_id, "IDENTIFIER", 100),
            )
            con.execute(
                """INSERT INTO engineering_project_identifier(
                engineering_project_id,identifier_type,identifier_value,source_notice_id)
                VALUES(?,?,?,?)""",
                (project_id, "TRANSACTION_PROJECT_CODE", code, notice_id),
            )
        project_ids.append(project_id)
    with db.connect() as con:
        before = con.execute("SELECT COUNT(*) FROM project_notice_link").fetchone()[0]
    candidate = EngineeringProjectRelationCandidateService(db).candidate_for(*project_ids)
    with db.connect() as con:
        after = con.execute("SELECT COUNT(*) FROM project_notice_link").fetchone()[0]
    assert candidate["relation_candidate"] == "SAME_PARENT_PROJECT"
    assert "different_strong_identifier" in candidate["conflicts"]
    assert before == after == 2


def test_relation_candidate_reports_scope_conflicts_without_merging(tmp_path):
    db = Database(tmp_path / "scope.db")
    db.init()
    ids = []
    for index, lot in enumerate(("1", "2"), start=1):
        notice_id = db.upsert(Project(
            name=f"同一道路工程第{lot}标段施工", region="北京市", source_site="测试",
            url=f"https://example.test/scope/{index}",
        ))
        with db.connect() as con:
            project_id = int(con.execute(
                "INSERT INTO engineering_project(identity_key,canonical_name,region) VALUES(?,?,?)",
                (f"scope:{index}", "同一道路工程施工", "北京市"),
            ).lastrowid)
            con.execute(
                "INSERT INTO project_notice_link(engineering_project_id,notice_id,match_method,match_score) VALUES(?,?,?,?)",
                (project_id, notice_id, "TEST", 100),
            )
        ids.append(project_id)
    candidate = EngineeringProjectRelationCandidateService(db).candidate_for(*ids)
    assert candidate["relation_candidate"] == "SAME_PARENT_PROJECT"
    assert "different_lot" in candidate["conflicts"]


def test_relation_candidate_blocking_skips_unrelated_projects(tmp_path, monkeypatch):
    db = Database(tmp_path / "blocking.db")
    db.init()
    with db.connect() as con:
        for index in range(30):
            con.execute(
                "INSERT INTO engineering_project(identity_key,canonical_name,region) VALUES(?,?,?)",
                (f"unique:{index}", f"完全不同工程主体{index:02d}建设项目", "北京市"),
            )
    service = EngineeringProjectRelationCandidateService(db)
    calls = 0
    original = service._compare

    def counted(left, right):
        nonlocal calls
        calls += 1
        return original(left, right)

    monkeypatch.setattr(service, "_compare", counted)
    assert service.candidates() == []
    assert calls < (30 * 29) // 2


def test_relation_candidate_exposes_auditable_project_context(tmp_path):
    db = Database(tmp_path / "context.db")
    db.init()
    ids = []
    for index, lot in enumerate(("1", "2"), start=1):
        notice_id = db.upsert(Project(
            name=f"共同工程第{lot}标段施工", region="北京市", source_site=f"来源{index}",
            publish_date=f"2026-09-0{index}", url=f"https://example.test/context/{index}",
        ))
        with db.connect() as con:
            project_id = int(con.execute(
                "INSERT INTO engineering_project(identity_key,canonical_name,region) VALUES(?,?,?)",
                (f"context:{index}", "共同工程施工", "北京市"),
            ).lastrowid)
            con.execute(
                "INSERT INTO project_notice_link(engineering_project_id,notice_id,match_method,match_score) VALUES(?,?,?,?)",
                (project_id, notice_id, "TEST", 100),
            )
        ids.append(project_id)
    candidate = EngineeringProjectRelationCandidateService(db).candidate_for(*ids)
    assert candidate["project_a"]["regions"] == ["北京市"]
    assert candidate["project_a"]["source_platforms"] == ["来源1"]
    assert candidate["project_a"]["publication_window"] == ["2026-09-01"]


def test_direct_unlinked_notice_reports_identity_reasons(tmp_path):
    db = Database(tmp_path / "direct.db")
    db.init()
    notice_id = db.upsert(Project(
        name="某项目预制梁采购", region="北京市", source_site="测试来源",
        url="https://example.test/direct",
    ))
    with db.connect() as con:
        con.execute(
            """INSERT INTO notice_product_assessment(
            notice_id,product,opportunity_score,product_opportunity_status,
            analyzer_version,rules_version) VALUES(?,?,?,?,?,?)""",
            (notice_id, "箱梁", 95, "DIRECT", "test", "test"),
        )
    row = direct_unlinked_notices(db)[0]
    assert row["reason"] == "NO_FORMAL_IDENTIFIER"
    assert row["reasons"] == ["NO_FORMAL_IDENTIFIER", "OWNER_MISSING"]
