from opportunity_radar.collectors.hebei_ggzy import HebeiGGZYCollector
from opportunity_radar.collectors.tianjin_transport import TianjinTransportCollector


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
