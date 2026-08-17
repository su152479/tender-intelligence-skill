from datetime import date
from .base import BaseCollector
from ..models import Project

SAMPLES = {
 "cccc": ("某高速公路预制箱梁采购公告", "桥梁上部结构施工，采购预制箱梁"),
 "cscec_yzw": ("某装配式住宅项目PC构件招标", "叠合板、预制楼梯及墙板供应"),
 "crecg_luban": ("城市轨道交通盾构区间材料采购", "盾构隧道预制混凝土管片"),
 "crcc_ec": ("城市综合管廊顶管工程招标", "矩形顶管及圆形顶管施工"),
 "powerchina": ("沿海风电场工程物资采购", "混凝土风塔及水工预制件"),
 "beijing_ggzy": ("某产业园钢结构厂房施工招标", "工业厂房钢梁及钢结构安装工程"),
 "ccgp": ("某污水处理厂机电设备采购公告", "采购水泵、风机、开关柜及配电柜"),
 "hebei_ggzy": ("某市给排水管网改造工程", "钢筋混凝土管、球墨铸铁管及顶管施工"),
 "tianjin_transport": ("某跨河桥梁建设工程", "钢箱梁吊装、预制梁架设及防水施工"),
}
class MockCollector(BaseCollector):
    def collect(self):
        title, content = SAMPLES.get(self.source["id"], ("某工程材料采购公告", "工程材料与设备采购"))
        return [Project(name=title, publish_date=date.today().isoformat(), region="示例地区", stage="招标", construction_content=content, source_site=self.source["name"], url=f"mock://{self.source['id']}/{date.today().isoformat()}", raw_text=f"{title}\n{content}")]
