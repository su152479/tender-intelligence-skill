"""Conservative Beijing-Tianjin-Hebei text matching helpers."""

REGION_MARKERS = {
    "北京市": ("北京", "京津冀"),
    "天津市": ("天津", "滨海新区", "京津冀"),
    "河北省": (
        "河北", "雄安", "石家庄", "唐山", "秦皇岛", "邯郸", "邢台", "保定",
        "张家口", "承德", "沧州", "廊坊", "衡水", "定州", "辛集", "京津冀",
    ),
}


def infer_region(text: str) -> str:
    value = text or ""
    matches = [region for region, markers in REGION_MARKERS.items() if any(marker in value for marker in markers)]
    return "、".join(matches)


def is_jing_jin_ji(text: str) -> bool:
    return bool(infer_region(text))

