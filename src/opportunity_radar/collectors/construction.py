"""Construction-stage filtering and compact evidence extraction."""

import re
from functools import lru_cache

from ..config import load_profile

NON_CONSTRUCTION_TITLE_MARKERS = (
    "监理", "勘察", "设计", "检测", "监测", "咨询", "审计", "造价",
    "评估", "报告编制", "运维", "物业", "软件", "设备采购", "服务采购",
    "装修", "装饰", "拆除",
)

CONSTRUCTION_TITLE_MARKERS = (
    "施工", "工程总承包", "EPC", "建设工程", "改造工程", "新建工程",
    "扩建工程", "道路工程", "管线工程", "水利工程", "桥梁工程",
)

@lru_cache(maxsize=1)
def opportunity_markers() -> tuple[str, ...]:
    words: list[str] = []
    for item in load_profile().get("products", []):
        words.append(item["name"])
        for key in ("direct_keywords", "method_keywords", "direction_keywords", "keywords"):
            words.extend(item.get(key, []))
    return tuple(dict.fromkeys(words))


def is_construction_tender(title: str) -> bool:
    upper = title.upper()
    if any(marker.upper() in upper for marker in NON_CONSTRUCTION_TITLE_MARKERS):
        return False
    return any(marker.upper() in upper for marker in CONSTRUCTION_TITLE_MARKERS)


def has_opportunity_direction(text: str) -> bool:
    return any(marker.lower() in (text or "").lower() for marker in opportunity_markers())


def evidence_excerpt(text: str, limit: int = 600) -> str:
    clean = re.sub(r"\s+", " ", text or "").strip()
    pieces = re.split(r"(?<=[。；;])", clean)
    selected = [piece.strip() for piece in pieces if has_opportunity_direction(piece)]
    value = " ".join(selected[:5]) or clean
    return value[:limit]
