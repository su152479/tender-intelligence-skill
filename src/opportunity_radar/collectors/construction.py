"""Construction-stage filtering and compact evidence extraction."""

import re

NON_CONSTRUCTION_TITLE_MARKERS = (
    "监理", "检测", "监测", "咨询", "审计", "造价",
    "评估", "报告编制", "运维", "物业", "软件", "设备采购", "服务采购",
    "装修", "装饰", "拆除", "规划编制", "专项规划", "规划咨询",
    "可行性研究", "可研报告", "防洪影响评价", "水土保持方案",
)

CONSTRUCTION_TITLE_MARKERS = (
    "施工", "工程总承包", "EPC", "建设工程", "改造工程", "新建工程",
    "扩建工程", "道路工程", "管线工程", "水利工程", "桥梁工程",
    "路基", "桥梁", "隧道", "盾构", "管网", "管廊", "泵站", "水闸",
    "堤防", "码头", "港口", "轨道交通", "市政工程", "水务工程",
    "附属工程", "土建工程", "主体工程", "招标计划",
    "城市更新", "城中村改造", "老旧小区改造", "棚户区改造", "危旧楼改建",
    "片区更新", "街区更新", "基础设施更新", "地下管网更新", "雨污分流",
    "河道治理", "河道整治", "防洪工程", "排涝工程", "灌区工程",
    "引调水", "供水工程", "水库工程", "渠系工程", "农田水利", "蓄滞洪区",
    "水生态修复", "水环境治理",
)

CITY_RENEWAL_MARKERS = (
    "城市更新", "城市更新改造", "城中村改造", "老旧小区改造", "棚户区改造",
    "危旧楼改建", "片区综合更新", "片区更新", "街区更新", "完整社区",
    "公共空间改造", "基础设施更新", "地下管网更新", "综合管廊", "雨污分流",
)

WATER_ENGINEERING_MARKERS = (
    "水利工程", "水务工程", "河道治理", "河道整治", "防洪", "排涝", "灌区",
    "泵站", "水闸", "闸站", "堤防", "蓄滞洪区", "引调水", "供水工程",
    "水库", "渠系", "农田水利", "水生态修复", "水环境治理", "护岸", "海绵城市",
)

OPPORTUNITY_MARKERS = (
    "箱梁", "预制梁", "桥梁", "高架", "管片", "盾构", "隧道", "地铁区间",
    "轨道交通", "PC构件", "装配式", "预制墙板", "叠合板", "住宅楼", "学校",
    "医院", "风塔", "混塔", "风电场", "塔筒", "顶管", "非开挖", "排水管网",
    "污水管", "雨水管", "供水管", "给水管", "再生水管", "综合管廊", "地下通道",
    "箱涵", "码头", "港池", "护岸", "水闸", "泵站", "堤防", "水利",
    *CITY_RENEWAL_MARKERS, *WATER_ENGINEERING_MARKERS,
)


def is_construction_tender(title: str) -> bool:
    upper = title.upper()
    if any(marker.upper() in upper for marker in NON_CONSTRUCTION_TITLE_MARKERS):
        return False
    if any(marker in title for marker in ("勘察", "设计")) and not any(
        marker in upper for marker in ("施工总承包", "工程总承包", "EPC")
    ):
        return False
    return any(marker.upper() in upper for marker in CONSTRUCTION_TITLE_MARKERS)


def has_opportunity_direction(text: str) -> bool:
    return any(marker.lower() in (text or "").lower() for marker in OPPORTUNITY_MARKERS)


def investigation_directions(text: str) -> list[str]:
    """Return broad government-project watch directions without implying product demand."""
    value = (text or "").lower()
    directions = []
    if any(marker.lower() in value for marker in CITY_RENEWAL_MARKERS):
        directions.append("城市更新")
    if any(marker.lower() in value for marker in WATER_ENGINEERING_MARKERS):
        directions.append("水利工程")
    return directions


def evidence_excerpt(text: str, limit: int = 600) -> str:
    clean = re.sub(r"\s+", " ", text or "").strip()
    pieces = re.split(r"(?<=[。；;])", clean)
    selected = [piece.strip() for piece in pieces if has_opportunity_direction(piece)]
    value = " ".join(selected[:5]) or clean
    return value[:limit]
