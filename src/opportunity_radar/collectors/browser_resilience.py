"""Small, deterministic browser resilience primitives.

The daily path stays code-driven.  These helpers classify failures, remember
which of several reviewed selectors worked, and save local diagnostics.  They
do not bypass access controls, solve CAPTCHAs, or submit business forms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import json
from pathlib import Path
import re
from typing import Any, Callable

from ..config import DATA_DIR


class ResponseOutcome(str, Enum):
    OK = "OK"
    EMPTY_CONFIRMED = "EMPTY_CONFIRMED"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    WAF = "WAF"
    RATE_LIMIT = "RATE_LIMIT"
    STRUCTURE_CHANGED = "STRUCTURE_CHANGED"
    UPSTREAM_BROKEN = "UPSTREAM_BROKEN"
    ERROR = "ERROR"


def classify_document(status: int, title: str = "", body: str = "") -> ResponseOutcome:
    text = f"{title}\n{body}".lower()
    if status == 429 or any(word in text for word in ("too many requests", "访问频繁", "请求过于频繁")):
        return ResponseOutcome.RATE_LIMIT
    if status in {403, 418} or any(word in text for word in ("cloudwaf", "访问被拦截", "疑似攻击行为")):
        return ResponseOutcome.WAF
    if any(word in text for word in ("登录状态已失效", "请登录", "重新登录")):
        return ResponseOutcome.AUTH_EXPIRED
    if status >= 500:
        return ResponseOutcome.UPSTREAM_BROKEN
    if status >= 400 or status == 0:
        return ResponseOutcome.ERROR
    return ResponseOutcome.OK


@dataclass(frozen=True)
class LocatorCandidate:
    key: str
    resolve: Callable[[Any], Any] = field(repr=False, compare=False)


class SelectorRegistry:
    """Remember the last reviewed selector that worked for each page control."""

    def __init__(self, path: Path | None = None):
        self.path = path or DATA_DIR / "browser" / "selector-cache.json"

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, value: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    @staticmethod
    def cache_key(source_id: str, purpose: str) -> str:
        return f"{source_id}:{purpose}"

    def ordered(self, source_id: str, purpose: str, candidates: list[LocatorCandidate]) -> list[LocatorCandidate]:
        cached = self._load().get(self.cache_key(source_id, purpose))
        return sorted(candidates, key=lambda item: item.key != cached)

    def locate_visible(
        self,
        page: Any,
        source_id: str,
        purpose: str,
        candidates: list[LocatorCandidate],
        timeout_ms: int = 12000,
    ) -> Any:
        if not candidates:
            raise RuntimeError(f"没有配置页面控件候选项：{purpose}")
        each_timeout = max(700, min(3000, timeout_ms // len(candidates)))
        errors = []
        for candidate in self.ordered(source_id, purpose, candidates):
            try:
                locator = candidate.resolve(page).first
                locator.wait_for(state="visible", timeout=each_timeout)
                cache = self._load()
                cache[self.cache_key(source_id, purpose)] = candidate.key
                self._save(cache)
                return locator
            except Exception as exc:  # Playwright errors vary by browser/version.
                errors.append(f"{candidate.key}: {type(exc).__name__}")
        raise RuntimeError(f"页面结构已变化，未找到{purpose}（{'；'.join(errors)}）")


class DiagnosticRecorder:
    """Write scrubbed failure evidence locally under the ignored data directory."""

    SECRET_PATTERNS = (
        (re.compile(r"(?i)bearer\s+[a-z0-9._-]+"), "Bearer [REDACTED]"),
        (re.compile(r"(?i)(authorization|cookie|token|password|secret)(\s*[:=]\s*)[^\s,;]+"), r"\1\2[REDACTED]"),
    )

    def __init__(self, root: Path | None = None):
        self.root = root or DATA_DIR / "diagnostics"

    @classmethod
    def scrub(cls, value: str, limit: int = 3000) -> str:
        result = (value or "")[:limit]
        for pattern, replacement in cls.SECRET_PATTERNS:
            result = pattern.sub(replacement, result)
        return result

    def record(
        self,
        source_id: str,
        outcome: ResponseOutcome,
        *,
        url: str = "",
        status: int = 0,
        title: str = "",
        message: str = "",
        body_excerpt: str = "",
    ) -> Path:
        folder = self.root / re.sub(r"[^a-zA-Z0-9_-]", "_", source_id)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = folder / f"{stamp}-{outcome.value.lower()}.json"
        payload = {
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "source_id": source_id,
            "outcome": outcome.value,
            "url": url,
            "status": status,
            "title": self.scrub(title, 300),
            "message": self.scrub(message, 1000),
            "body_excerpt": self.scrub(body_excerpt),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def record_page(
        self,
        source_id: str,
        outcome: ResponseOutcome,
        page: Any,
        *,
        status: int = 0,
        message: str = "",
        screenshot: bool = False,
    ) -> Path:
        try:
            title = page.title()
        except Exception:
            title = ""
        try:
            body = page.locator("body").inner_text(timeout=3000)
        except Exception:
            body = ""
        path = self.record(
            source_id, outcome, url=getattr(page, "url", ""), status=status,
            title=title, message=message, body_excerpt=body,
        )
        if screenshot:
            try:
                page.screenshot(path=str(path.with_suffix(".png")), full_page=False)
            except Exception:
                pass
        return path
