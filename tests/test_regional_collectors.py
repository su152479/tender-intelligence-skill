from opportunity_radar.collectors.hebei_ggzy import HebeiGGZYCollector
from opportunity_radar.collectors.tianjin_transport import TianjinTransportCollector
from opportunity_radar.collectors.jjj_portal import JingJinJiPortalCollector


def test_hebei_detail_forces_hebei_region():
    collector = HebeiGGZYCollector({"name": "河北", "url": "https://example.test/"})
    html = '<h2 class="div-title">桥梁工程施工招标公告</h2><div class="newsCon">建设地点 石家庄市。新建桥梁并安装预制箱梁。</div>'
    project = collector.parse_detail(html, "https://example.test/a", "桥梁工程施工招标公告", "2026-08-14")
    assert project is not None
    assert project.region == "河北省"


def test_tianjin_list_and_detail_parsing():
    collector = TianjinTransportCollector({"name": "天津交通", "url": "https://example.test/", "list_url": "https://example.test/list/"})
    html = '''<div class="commonList-text-title">
      <a href="./202608/t20260814_1.html" title="天津港工程招标计划">天津港工程招标计划</a>
      </div>'''
    rows = collector.parse_list(html)
    assert rows == [("天津港工程招标计划", "2026-08-14", "https://example.test/list/202608/t20260814_1.html")]

    detail = '<div class="trs_editor_view">项目建设内容包括码头、护岸和给排水工程。</div>'
    project = collector.parse_detail(detail, rows[0][2], rows[0][0], rows[0][1])
    assert project is not None
    assert project.region == "天津市"
    assert project.stage == "招标计划"


def test_jjj_portal_parses_lifecycle_sections_and_deduplicates_mobile_copy():
    collector = JingJinJiPortalCollector({"id": "jjj_portal", "name": "京津冀协同", "url": "https://ggzyfw.beijing.gov.cn/"})
    html = '''
      <a href="/jyxxzbhxrgs/20260831/5691526.html" title="国道234道路工程土建工程(施工)中标候选人公示">项目</a>
      <a href="/jyxxzbhxrgs/20260831/5691526.html" title="国道234道路工程土建工程(施工)中标候选人公示">移动端重复</a>
      <a href="/xtgghbzbgg/20260901/5691814.html" title="胜利大街南延一期工程施工招标公告">项目</a>
      <a href="/jyxxcggg/20260901/other.html" title="设备采购公告">非工程栏目</a>
    '''
    rows = collector.parse_list(html)
    assert len(rows) == 2
    target = next(row for row in rows if "5691526" in row[2])
    assert target[1] == "2026-08-31"
    assert target[3] == "中标候选人公示"
    assert target[4] == "北京市"


def test_jjj_portal_parses_project_number_and_stage():
    collector = JingJinJiPortalCollector({"id": "jjj_portal", "name": "京津冀协同", "url": "https://ggzyfw.beijing.gov.cn/"})
    html = '''<div class="div-title">国道234道路工程土建工程(施工)中标候选人公示</div>
      <div class="newsCon">交易项目编号：S110000A001037484009，建设单位名称：北京市交通委员会怀柔公路分局，项目包含路基桥梁工程。</div>'''
    project = collector.parse_detail(
        html, "https://example.test/5691526.html", "国道234道路工程土建工程(施工)中标候选人公示",
        "2026-08-31", "中标候选人公示", "北京市",
    )
    assert project is not None
    assert project.project_no == "S110000A001037484009"
    assert project.stage == "中标候选人公示"
    assert project.owner == "北京市交通委员会怀柔公路分局"
