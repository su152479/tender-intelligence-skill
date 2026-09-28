import sqlite3

import pytest

from opportunity_radar.db import Database
from opportunity_radar.identity import EXTRACTOR_VERSION, NoticeIdentityService, extract_lots
from opportunity_radar.models import Project


def _db(tmp_path):
    db = Database(tmp_path / "identity.db")
    db.init()
    return db


def _notice(db, name, index=1, **kwargs):
    return db.upsert(Project(
        name=name, source_site=kwargs.pop("source_site", "中国政府采购网"),
        url=f"https://example.test/identity/{index}", **kwargs,
    ))


def test_identity_facts_persist_with_provenance_and_multiple_sources(tmp_path):
    db = _db(tmp_path)
    notice_id = _notice(
        db, "保定市莲池区排水工程施工公告", region="河北省", owner="保定市排水中心",
        raw_text="建设地点：保定市莲池区复兴路\n建设单位：保定市排水中心",
    )
    facts = NoticeIdentityService(db).facts(notice_id, active_only=True)
    regions = [row for row in facts if row["fact_type"] == "REGION"]
    assert len(regions) >= 3
    assert {row["source_type"] for row in regions} >= {"API_STRUCTURED", "PAGE_STRUCTURED", "TITLE"}
    assert all(row["source_reference"] == f"project:{notice_id}" for row in regions)


def test_extractor_versions_are_historical_and_active_selection_is_current(tmp_path):
    db = _db(tmp_path)
    notice_id = _notice(db, "北京市排水工程施工公告", region="北京市")
    NoticeIdentityService(db, "identity-v2-test").extract_notice(notice_id)
    rows = NoticeIdentityService(db).facts(notice_id)
    assert {row["extractor_version"] for row in rows} == {EXTRACTOR_VERSION, "identity-v2-test"}
    assert not any(row["is_active"] for row in rows if row["extractor_version"] == EXTRACTOR_VERSION)
    assert all(row["is_active"] for row in rows if row["extractor_version"] == "identity-v2-test")
    assert NoticeIdentityService(db).current_identities()[notice_id].region_value == "北京市"


def test_low_region_and_contaminated_owner_are_not_usable(tmp_path):
    db = _db(tmp_path)
    notice_id = _notice(
        db, "中铁北京工程局甬绍天然气管线工程材料采购", region="北京市",
        owner="某单位\n联系人：张三\n电话：123456", source_site="中铁鲁班网",
    )
    identity = NoticeIdentityService(db).current_identities()[notice_id]
    assert identity.region_value == ""
    assert identity.region_confidence == "LOW"
    assert identity.owner == ""
    assert identity.owner_quality == "CONTAMINATED"


def test_structured_owner_stays_high_quality(tmp_path):
    db = _db(tmp_path)
    notice_id = _notice(db, "排水工程采购", owner="北京市水务建设中心")
    identity = NoticeIdentityService(db).current_identities()[notice_id]
    assert identity.owner == "北京市水务建设中心"
    assert identity.owner_quality == "STRUCTURED_HIGH"


@pytest.mark.parametrize("text,expected,kind", [
    ("长石1标项目管片采购", ("1",), "SINGLE_LOT"),
    ("工程（一、二、三标段）", ("1", "2", "3"), "MULTI_LOT"),
    ("工程第1-3标段", ("1", "2", "3"), "MULTI_LOT"),
    ("工程标段二", ("2",), "SINGLE_LOT"),
    ("工程施工一标项目", ("1",), "SINGLE_LOT"),
    ("工程01合同段", ("1",), "SINGLE_LOT"),
    ("工程SG-02", ("SG-2",), "SINGLE_LOT"),
    ("工程第TJ03标段", ("TJ3",), "SINGLE_LOT"),
])
def test_lot_forms_are_structured(tmp_path, text, expected, kind):
    db = _db(tmp_path)
    notice_id = _notice(db, text)
    identity = NoticeIdentityService(db).current_identities()[notice_id]
    assert identity.lots == expected
    assert identity.lot_kind == kind
    assert all(row["raw_value"] for row in NoticeIdentityService(db).facts(notice_id, active_only=True) if row["fact_type"] == "LOT")


@pytest.mark.parametrize("text,expected", [
    ("道路工程1#标段施工", ("1",)),
    ("道路工程2#标段施工", ("2",)),
    ("道路工程3#合同段施工", ("3",)),
    ("设备编号B-1#", ()),
])
def test_hash_lot_requires_explicit_contract_context(tmp_path, text, expected):
    db = _db(tmp_path)
    notice_id = _notice(db, text)
    assert NoticeIdentityService(db).current_identities()[notice_id].lots == expected


@pytest.mark.parametrize("title", [
    "东石桥220千伏变电站新建联络工程",
    "2026年山区村庄供水站标准化改造工程",
    "轨道交通线路污水泵站改造工程",
])
def test_non_rail_facilities_are_not_stations(tmp_path, title):
    db = _db(tmp_path)
    notice_id = _notice(db, title)
    assert NoticeIdentityService(db).current_identities()[notice_id].stations == ()


def test_explicit_metro_station_remains_station(tmp_path):
    db = _db(tmp_path)
    notice_id = _notice(db, "北京地铁22号线东大桥站至平谷站区间工程")
    identity = NoticeIdentityService(db).current_identities()[notice_id]
    assert {"东大桥站", "平谷站"} <= set(identity.stations)


def test_railway_and_lettered_line_stations_remain_stations(tmp_path):
    db = _db(tmp_path)
    r1 = _notice(db, "永清县R1线临空站片区工程", 1)
    railway = _notice(db, "铁路北京东综合站区提升工程", 2)
    identities = NoticeIdentityService(db).current_identities()
    assert "临空站" in identities[r1].stations
    assert "北京东综合站" in identities[railway].stations


@pytest.mark.parametrize("text", [
    "2026年高标准农田建设项目", "地铁22号线工程", "孙河2902-73地块工程",
    "项目编号S110000A001037484009",
])
def test_unscoped_numbers_are_not_lots(text):
    assert extract_lots(text) == []


def test_phase_year_station_and_range_semantics(tmp_path):
    db = _db(tmp_path)
    first = _notice(db, "石家庄供热十期工程二次招标", 1)
    second = _notice(db, "2026年高标准农田建设项目西二旗站～龙泽站区间K1+200-K2+300施工", 2)
    identities = NoticeIdentityService(db).current_identities()
    assert identities[first].phases == ("10",)
    assert identities[first].lots == ()
    assert identities[second].years == ("2026",)
    assert {"西二旗站", "龙泽站"} <= {
        row["raw_value"] for row in NoticeIdentityService(db).facts(second, active_only=True)
        if row["fact_type"] == "STATION"
    }
    assert identities[second].ranges


@pytest.mark.parametrize("source,project_no,expected_type,expected_namespace,strength", [
    ("北京市公共资源交易服务平台", "S110000A001037484009", "TRANSACTION_PROJECT_CODE", "BEIJING_GGZY", "STRONG"),
    ("中建云筑网", "TENDER-2026-001", "YUNZHU_TENDER_CODE", "YUNZHU", "BUSINESS_ONLY"),
    ("中交集团供应链管理信息系统", "FA00000012345", "CCCC_SCHEME_CODE", "CCCC", "BUSINESS_ONLY"),
])
def test_identifier_semantics_namespace_and_strength(
    tmp_path, source, project_no, expected_type, expected_namespace, strength,
):
    db = _db(tmp_path)
    notice_id = _notice(db, "道路工程采购", project_no=project_no, source_site=source)
    identifier = NoticeIdentityService(db).current_identities()[notice_id].identifiers[0]
    assert (identifier.identifier_type, identifier.namespace, identifier.strength) == (
        expected_type, expected_namespace, strength,
    )


def test_document_identifier_can_be_imported_without_reanalysis(tmp_path):
    db = _db(tmp_path)
    notice_id = _notice(db, "道路工程施工")
    with db.connect() as con:
        attachment_id = con.execute(
            "INSERT INTO notice_attachment(notice_id,attachment_url) VALUES(?,?)",
            (notice_id, "https://example.test/a.pdf"),
        ).lastrowid
        document_id = con.execute(
            "INSERT INTO document_text(attachment_id,notice_id) VALUES(?,?)", (attachment_id, notice_id),
        ).lastrowid
        con.execute(
            "INSERT INTO document_identifier(document_id,identifier,identifier_type) VALUES(?,?,?)",
            (document_id, "2023-188LS", "工程编号"),
        )
    NoticeIdentityService(db).extract_notice(notice_id)
    facts = NoticeIdentityService(db).facts(notice_id, active_only=True)
    imported = [row for row in facts if row["source_type"] == "ATTACHMENT"]
    assert imported[0]["source_reference"] == f"document:{document_id}"
    assert imported[0]["identifier_type"] == "ENGINEERING_CODE"


def test_fact_schema_has_foreign_key_and_expected_indexes(tmp_path):
    db = _db(tmp_path)
    with db.connect() as con:
        assert con.execute("PRAGMA foreign_key_list(notice_identity_fact)").fetchall()
        indexes = {row[1] for row in con.execute("PRAGMA index_list(notice_identity_fact)")}
    assert "idx_notice_identity_fact_current" in indexes
    assert "idx_notice_identity_fact_identifier" in indexes
