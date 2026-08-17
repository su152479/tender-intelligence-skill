"""Construction-stage filtering and compact evidence extraction."""

import re

NON_CONSTRUCTION_TITLE_MARKERS = (
    "监理", "勘察", "设计", "检测", "监测", "咨询", "审计", "造价",
    "评估", "报告编制", "运维", "物业", "软件", "设备采购", "服务采购",
    "装修", "装饰", "拆除",
)

CONSTRUCTION_TITLE_MARKERS = (
    "施工", "工程总承包", "EPC", "建设工程", "改造工程", "新建工程",
    "扩建工程", "道路工程", "管线工程", "水利工程", "桥梁工程",
)

OPPORTUNITY_MARKERS = (
    "箱梁", "预制梁", "桥梁", "高架", "管片", "盾构", "隧道", "地铁区间",
    "轨道交通", "PC构件", "装配式", "预制墙板", "叠合板", "住宅楼", "学校",
    "医院", "风塔", "混塔", "风电场", "塔筒", "顶管", "非开挖", "排水管网",
    "污水管", "雨水管", "供水管", "给水管", "再生水管", "综合管廊", "地下通道",
    "箱涵", "码头", "港池", "护岸", "水闸", "泵站", "堤防", "水利",
)


def is_construction_tender(title: str) -> bool:
    upper = title.upper()
    if any(marker.upper() in upper for marker in NON_CONSTRUCTION_TITLE_MARKERS):
        return False
    return any(marker.upper() in upper for marker in CONSTRUCTION_TITLE_MARKERS)


def has_opportunity_direction(text: str) -> bool:
    return any(marker.lower() in (text or "").lower() for marker in OPPORTUNITY_MARKERS)


def evidence_excerpt(text: str, limit: int = 600) -> str:
    clean = re.sub(r"\s+", " ", text or "").strip()
    pieces = re.split(r"(?<=[。；;])", clean)
    selected = [piece.strip() for piece in pieces if has_opportunity_direction(piece)]
    value = " ".join(selected[:5]) or clean
    return value[:limit]
