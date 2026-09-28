"""Shared helpers for compact, source-specific portal searches."""


def configured_search_terms(source: dict) -> list[str]:
    """Prefer broader discovery terms while preserving legacy configurations."""
    values = source.get("search_keywords") or source.get("keywords") or []
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
