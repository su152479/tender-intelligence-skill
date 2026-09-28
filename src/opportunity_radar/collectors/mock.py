from datetime import date
from .base import BaseCollector
from ..models import Project

SAMPLES = {
 "cccc": ("某高速公路预制箱梁采购公告", "桥梁上部结构施工，采购预制箱梁", "天津市"),
 "cscec_yzw": ("某装配式住宅项目PC构件招标", "叠合板、预制楼梯及墙板供应", "北京市"),
 "crecg_luban": ("雄安轨道交通盾构区间材料采购", "盾构隧道预制混凝土管片", "河北省"),
 "crcc_ec": ("北京城市综合管廊顶管工程招标", "矩形顶管及圆形顶管施工", "北京市"),
 "powerchina": ("天津沿海风电场工程物资采购", "混凝土风塔及水工预制件", "天津市"),
 "beijing_ggzy": ("北京快速路桥梁工程施工招标公告", "桥梁上部结构及附属工程", "北京市"),
 "ccgp": ("河北污水管网工程施工招标公告", "污水管线及顶管施工", "河北省"),
 "hebei_ggzy": ("雄安新区综合管网工程施工招标公告", "雨污水管网及泵站工程", "河北省"),
 "tianjin_transport": ("天津港区桥梁工程施工招标公告", "桥梁及水工附属工程", "天津市"),
}
class MockCollector(BaseCollector):
    def collect(self):
        title, content, region = SAMPLES[self.source["id"]]
        self.set_funnel(
            request_success_count=1, raw_list_count=1, recent_count=1,
            region_recent_count=1, construction_count=1, detail_success_count=1,
            final_opportunity_count=1,
        )
        return [Project(name=title, publish_date=date.today().isoformat(), region=region, stage="招标", construction_content=content, source_site=self.source["name"], url=f"https://mock.invalid/{self.source['id']}/{date.today().isoformat()}", raw_text=f"{title}\n{content}")]
