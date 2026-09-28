import json

import pytest

from opportunity_radar.ai import OpportunityAnalyzer
from opportunity_radar.config import load_yaml
from opportunity_radar.db import Database
from opportunity_radar.identity import NoticeIdentityService
from opportunity_radar.models import Project
from opportunity_radar.obsidian import ObsidianExporter


ANALYZER = OpportunityAnalyzer(load_yaml("products.yaml"))


def evidence(result, product):
    return next(item for item in result["命中证据"] if item["产品"] == product)


def test_construction_award_closes_notice_not_product_follow_up():
    result = ANALYZER.analyze(Project(
        name="国道工程土建施工中标结果公告", stage="中标结果公告",
        raw_text="本标段桥梁上部结构采用预制箱梁。中标人:某施工有限公司",
    ))
    assert result["公告可参与性"] == "CLOSED"
    assert result["项目进展信号"] == "CONTRACTOR_SELECTED"
    assert result["当前商机分0-100"] == 0
    assert result["项目线索分0-100"] > 0
    assert evidence(result, "箱梁")["产品机会状态"] == "FOLLOW_UP"
    assert result["跟进优先级"] == "重点跟进"
    assert "具体产品供应链机会" in result["展示分类"]
    assert result["项目跟踪建议"] == "KEY_FOLLOW_UP"


def test_direct_box_girder_notice_stays_open_and_direct():
    result = ANALYZER.analyze(Project(
        name="预制箱梁采购招标公告", stage="招标公告",
        raw_text="本次采购预制混凝土箱梁。",
    ))
    assert result["公告可参与性"] == "OPEN"
    assert result["当前商机分0-100"] >= 85
    assert evidence(result, "箱梁")["产品机会状态"] == "DIRECT"


def test_company_name_does_not_hide_direct_product_action_in_title():
    result = ANALYZER.analyze(Project(
        name="中交路桥华北工程有限公司密云项目预制梁询价公告",
        raw_text="预制梁",
    ))
    item = evidence(result, "箱梁")
    assert item["采购窗口状态"] == "OPEN"
    assert item["产品机会状态"] == "DIRECT"


def test_direct_product_award_ends_only_that_product_procurement_window():
    result = ANALYZER.analyze(Project(
        name="预制箱梁采购中标结果公告", stage="中标结果公告",
        raw_text="本次采购预制混凝土箱梁，中标人:某构件有限公司。",
    ))
    assert result["公告可参与性"] == "CLOSED"
    assert result["当前商机分0-100"] == 0
    item = evidence(result, "箱梁")
    assert item["采购窗口状态"] == "ENDED"
    assert item["产品机会状态"] == "ENDED"
    assert result["项目跟踪建议"] == "KEY_FOLLOW_UP"


def test_steel_box_girder_is_out_of_scope_for_precast_concrete_box_girder():
    result = ANALYZER.analyze(Project(
        name="钢箱梁制作安装招标公告", raw_text="本次采购钢箱梁并进行安装。",
    ))
    item = evidence(result, "箱梁")
    assert item["产品机会状态"] == "OUT_OF_SCOPE"
    assert "箱梁" not in result["潜在预制产品"]


def test_plain_tunnel_is_weaker_than_shield_method_for_segments():
    plain = ANALYZER.analyze(Project(name="公路隧道工程施工招标公告", raw_text="隧道工程施工。"))
    shield = ANALYZER.analyze(Project(name="地铁盾构区间施工招标公告", raw_text="本区间采用盾构法施工。"))
    plain_item = evidence(plain, "管片")
    shield_item = evidence(shield, "管片")
    assert plain_item["等级"] == "工程方向推断"
    assert plain_item["产品机会状态"] == "NO_EVIDENCE"
    assert plain["项目跟踪建议"] == "ACTIVE_WATCH"
    assert shield_item["产品机会状态"] == "FOLLOW_UP"
    assert shield_item["等级"] == "明确施工方法"
    assert shield_item["项目线索分"] > plain_item["项目线索分"]


def test_products_keep_independent_statuses():
    result = ANALYZER.analyze(Project(
        name="综合工程材料采购公告",
        raw_text="本次采购预制混凝土管片；桥梁明确采用钢箱梁。",
    ))
    assert evidence(result, "管片")["产品机会状态"] == "DIRECT"
    assert evidence(result, "箱梁")["产品机会状态"] == "OUT_OF_SCOPE"


def test_explicit_project_termination_does_not_invent_product_ended_state():
    result = ANALYZER.analyze(Project(
        name="某工程终止公告", stage="终止公告", raw_text="本项目终止，不再实施。",
    ))
    assert all(item["产品机会状态"] != "ENDED" for item in result["命中证据"])


@pytest.mark.parametrize("lot,winner", [
    (6, "北京市政路桥股份有限公司"),
    (7, "中交一公局第三工程有限公司"),
    (8, "中铁十六局集团第三工程有限公司"),
    (9, "北京城建集团有限责任公司"),
])
def test_g234_award_replay_and_result_party_extraction(tmp_path, lot, winner):
    db = Database(tmp_path / f"g234-{lot}.db")
    db.init()
    project = Project(
        name=f"国道234（永宁-琉璃庙）道路工程土建工程第{lot}标段(施工)中标结果公告",
        project_no="S110000A001037484010", stage="中标结果公告",
        source_site="京津冀公共资源交易协同专区", url=f"https://example.test/g234/{lot}",
        raw_text=(
            f"确定第{lot}标段的中标人如下：\n一、中标人信息\n中标人:\n{winner}\n"
            "建设内容：包含桥梁和分离式隧道工程。"
        ),
    )
    result = ANALYZER.analyze(project)
    assert result["公告可参与性"] == "CLOSED"
    assert result["项目进展信号"] == "CONTRACTOR_SELECTED"
    assert result["当前商机分0-100"] == 0
    assert result["项目线索分0-100"] > 0
    assert evidence(result, "管片")["产品机会状态"] == "NO_EVIDENCE"
    assert result["项目跟踪建议"] == "KEY_FOLLOW_UP"
    assert "重点工程跟踪" in result["展示分类"]
    assert "具体产品供应链机会" not in result["展示分类"]
    notice_id = db.upsert(project)
    facts = NoticeIdentityService(db).facts(notice_id, active_only=True)
    parties = [row["raw_value"] for row in facts if row["fact_type"] == "RESULT_PARTY"]
    assert parties == [winner]
    assert "如下" not in parties


def test_assessment_versions_preserve_old_machine_judgment(tmp_path):
    db = Database(tmp_path / "history.db")
    db.init()
    old = Project(
        name="桥梁工程中标结果公告", source_site="测试", url="https://example.test/history",
        analyzer_version="opportunity-analysis-v6.3", rules_version=ANALYZER.rules_version,
        analysis_json=json.dumps({"命中证据": [{
            "产品": "箱梁", "评分": 55, "项目线索分": 55, "当前商机分": 0,
            "需求意图": "可能使用", "需求状态": "POSSIBLE", "工程需求": "POSSIBLE",
            "范围状态": "UNCERTAIN", "当前机会状态": "CLOSED", "等级": "工程方向推断",
            "证据关系": "工程方向", "证据句": ["桥梁工程"], "负向证据": [], "评分说明": [],
        }]}, ensure_ascii=False),
    )
    db.upsert(old)
    new = ANALYZER.apply(Project(
        name=old.name, stage="中标结果公告", source_site=old.source_site, url=old.url,
        raw_text="桥梁上部结构采用预制箱梁。",
    ))
    db.upsert(new)
    rows = db.notice_product_assessments()
    assert len(rows) == 2
    assert rows[0]["analyzer_version"] == "opportunity-analysis-v6.3"
    assert rows[0]["opportunity_status"] == "CLOSED"
    assert rows[1]["analyzer_version"] == "opportunity-analysis-v7.2"
    assert rows[1]["product_opportunity_status"] == "FOLLOW_UP"


@pytest.mark.parametrize("name,raw_text,product,demand,evidence_type,window,status", [
    ("盾构管片采购公告", "本次采购盾构管片。", "管片", "STRONG", "DIRECT_TARGET_PRODUCT", "OPEN", "DIRECT"),
    ("盾构管片后续采购计划", "盾构管片后续采购。", "管片", "STRONG", "DIRECT_TARGET_PRODUCT", "UPCOMING", "PRE_PROCUREMENT"),
    ("管片螺栓采购公告", "本次采购管片螺栓。", "管片", "MEDIUM", "ACCESSORY_OR_SUPPORTING_PRODUCT", "UNKNOWN", "FOLLOW_UP"),
    ("隧道工程施工公告", "建设内容包括隧道工程。", "管片", "WEAK", "ENGINEERING_DIRECTION", "UNKNOWN", "NO_EVIDENCE"),
])
def test_v72_two_step_product_opportunity_model(
    name, raw_text, product, demand, evidence_type, window, status,
):
    item = evidence(ANALYZER.analyze(Project(name=name, raw_text=raw_text)), product)
    assert item["产品需求证据等级"] == demand
    assert item["产品需求证据类型"] == evidence_type
    assert item["采购窗口状态"] == window
    assert item["产品机会状态"] == status


def test_obsidian_keeps_construction_award_out_of_non_target_bucket(tmp_path):
    db = Database(tmp_path / "obsidian.db")
    db.init()
    project = ANALYZER.apply(Project(
        name="道路桥梁工程施工中标结果公告", stage="中标结果公告",
        source_site="测试", url="https://example.test/award-obsidian",
        raw_text="桥梁上部结构采用预制箱梁。中标人:某施工有限公司",
    ))
    notice_id = db.upsert(project)
    vault = tmp_path / "vault"
    ObsidianExporter(db.path, vault).export()
    follow_up = (vault / "02 具体产品供应链机会.md").read_text(encoding="utf-8")
    tracking = (vault / "03 重点工程跟踪.md").read_text(encoding="utf-8")
    progress = (vault / "04 项目最新进展.md").read_text(encoding="utf-8")
    excluded = (vault / "06 非目标或排除.md").read_text(encoding="utf-8")
    link = f"项目-{notice_id:04d}"
    assert link in follow_up
    assert link in tracking
    assert link in progress
    assert link not in excluded


@pytest.mark.parametrize("name,raw_text,product,expected", [
    ("普通公路隧道工程施工招标公告", "建设内容包括隧道工程。", "管片", "NO_EVIDENCE"),
    ("轨道交通广告灯箱采购公告", "本次采购广告灯箱。", "管片", "NO_EVIDENCE"),
    ("钢箱梁制作安装公告", "本工程仅采用钢箱梁。", "箱梁", "OUT_OF_SCOPE"),
    ("地铁盾构区间施工公告", "本区间采用盾构法施工。", "管片", "FOLLOW_UP"),
    ("管片螺栓采购公告", "本次采购管片螺栓。", "管片", "FOLLOW_UP"),
    ("管片防水密封材料采购公告", "本次采购管片防水密封垫。", "管片", "FOLLOW_UP"),
    ("预制构件灌浆套筒采购公告", "本次采购预制构件灌浆套筒。", "PC构件", "FOLLOW_UP"),
    ("排水管线非开挖施工公告", "本工程采用非开挖方式施工。", "圆形顶管", "NO_EVIDENCE"),
])
def test_v71_product_specific_status_gate(name, raw_text, product, expected):
    result = ANALYZER.analyze(Project(name=name, raw_text=raw_text))
    assert evidence(result, product)["产品机会状态"] == expected


@pytest.mark.parametrize("raw_text", [
    "投标人资格要求：轨道交通工程除外。",
    "招标代理机构为天津轨道交通咨询有限公司。",
    "中标人为江西省路桥隧道工程有限公司。",
    "中标人为河北省水利工程局集团有限公司。",
    "请按照水利工程模块操作手册提交文件。",
])
def test_v71_non_business_positions_do_not_create_product_evidence(raw_text):
    result = ANALYZER.analyze(Project(name="一般服务公告", raw_text=raw_text))
    assert result["潜在预制产品"] == []
    assert result["命中证据"] == []
