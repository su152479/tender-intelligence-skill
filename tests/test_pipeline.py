import json
from datetime import datetime
from opportunity_radar.ai import OpportunityAnalyzer
from opportunity_radar.db import Database
from opportunity_radar.models import Project
from opportunity_radar.collectors.cccc import CCCCCollector
from opportunity_radar.collectors.luban import CRECGLubanCollector
from opportunity_radar.collectors.regions import infer_region, is_jing_jin_ji
from opportunity_radar.collectors.yzw import CSCECYunZhuCollector
from opportunity_radar.collectors.beijing_ggzy import BeijingGGZYCollector
from opportunity_radar.collectors.ccgp import CCGPCollector
from opportunity_radar.collectors.construction import is_construction_tender

CFG = {"products": [{"name":"管片","keywords":["盾构","管片"],"project_types":["轨道交通"],"score":35}], "rules":{"title_keyword_bonus":20,"content_keyword_bonus":10,"max_score":100}}

def test_analysis_json_contract():
    result = OpportunityAnalyzer(CFG).analyze(Project(name="盾构管片采购", raw_text="地铁盾构区间管片"))
    assert result["项目类型"] == "轨道交通"
    assert result["潜在预制产品"] == ["管片"]
    assert 0 <= result["机会评分0-100"] <= 100
    assert result["证据等级"] == "直接产品证据"

def test_database_upsert(tmp_path):
    db = Database(tmp_path / "test.db"); db.init()
    p = OpportunityAnalyzer(CFG).apply(Project(name="管片采购", source_site="测试", url="https://example.test/1", raw_text="盾构管片"))
    db.upsert(p); db.upsert(p)
    rows = db.recent()
    assert len(rows) == 1
    assert json.loads(rows[0]["matched_products"]) == ["管片"]
    db.record_source_run({"id": "test", "name": "测试"}, datetime.now(), "success", 1)
    assert db.source_status()[0]["item_count"] == 1

def test_cccc_html_to_text():
    assert CCCCCollector.html_to_text("<h2>项目概况</h2><p>盾构管片采购</p>") == "项目概况\n盾构管片采购"

def test_cccc_region_filter():
    collector = CCCCCollector({"name": "测试", "regions": ["北京市", "天津市", "河北省"]})
    assert collector._region_allowed("河北省")
    assert collector._region_allowed("北京市,河北省")
    assert not collector._region_allowed("江苏省")
    assert not collector._region_allowed("")

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
    assert not is_construction_tender("道路工程监理招标公告")
    assert not is_construction_tender("住宅楼室内精装修工程施工公告")
