"""Configuration-driven region matching helpers."""

import os
from functools import lru_cache

from ..config import load_yaml


@lru_cache(maxsize=1)
def region_catalog() -> dict:
    return load_yaml("regions.yaml").get("regions", {})


def target_regions(source_regions: list[str] | None = None) -> list[str]:
    override = os.getenv("RADAR_TARGET_REGIONS", "").strip()
    if override:
        return [item.strip() for item in override.split(",") if item.strip()]
    return list(source_regions or region_catalog().keys())


def infer_region(text: str, allowed_regions: list[str] | None = None) -> str:
    value = text or ""
    allowed = set(target_regions(allowed_regions))
    matches = []
    for name, details in region_catalog().items():
        if name not in allowed:
            continue
        markers = details.get("markers", [])
        if any(marker in value for marker in markers):
            matches.append(name)
    return "、".join(matches)


def is_target_region(text: str, allowed_regions: list[str] | None = None) -> bool:
    return bool(infer_region(text, allowed_regions))


def target_region_codes(source_regions: list[str] | None = None) -> list[str]:
    catalog = region_catalog()
    return [catalog[name]["code"] for name in target_regions(source_regions) if name in catalog]


def target_region_labels(source_regions: list[str] | None = None) -> list[str]:
    catalog = region_catalog()
    return [catalog[name]["label"] for name in target_regions(source_regions) if name in catalog]
