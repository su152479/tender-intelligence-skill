"""Polite, cache-aware HTTP access for public procurement pages.

This helper does not rotate proxies, hide automation, or retry access-control
responses. It reduces traffic through per-host pacing, a local SQLite cache,
and narrowly bounded retries for genuinely transient failures.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import email.utils
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
from urllib.parse import urlsplit

import requests

from ..config import DATA_DIR
from ..network import NetworkPolicy

log = logging.getLogger(__name__)


class SourceCircuitOpen(RuntimeError):
    pass


class HostRateLimiter:
    """Process-wide minimum interval shared by clients hitting the same host."""

    _lock = threading.Lock()
    _last_request_at: dict[str, float] = {}

    @classmethod
    def wait(cls, url: str, minimum_interval: float) -> None:
        host = (urlsplit(url).hostname or "unknown").lower()
        with cls._lock:
            now = time.monotonic()
            ready_at = max(now, cls._last_request_at.get(host, 0.0) + minimum_interval)
            cls._last_request_at[host] = ready_at
        if ready_at > now:
            time.sleep(ready_at - now)


class HttpCache:
    def __init__(self, path: Path | None = None):
        self.path = path or DATA_DIR / "http" / "responses.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS response_cache "
                "(url TEXT PRIMARY KEY, body TEXT NOT NULL, fetched_at REAL NOT NULL)"
            )

    def get(self, url: str, ttl_seconds: int) -> str | None:
        if ttl_seconds <= 0:
            return None
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT body, fetched_at FROM response_cache WHERE url = ?", (url,)
            ).fetchone()
        if not row or time.time() - float(row[1]) > ttl_seconds:
            return None
        return str(row[0])

    def put(self, url: str, body: str) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO response_cache(url, body, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(url) DO UPDATE SET body=excluded.body, fetched_at=excluded.fetched_at",
                (url, body, time.time()),
            )


class SourceCircuitBreaker:
    def __init__(self, source_id: str, root: Path | None = None):
        safe_id = "".join(char if char.isalnum() or char in "_-" else "_" for char in source_id)
        self.path = (root or DATA_DIR / "http" / "circuits") / f"{safe_id}.json"

    def block(self, status: int, *, retry_after: str = "") -> None:
        now = datetime.now()
        if status in {403, 418}:
            until = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
        elif status == 429:
            until = now + timedelta(seconds=self._retry_after_seconds(retry_after, default=21600))
        else:
            until = now + timedelta(minutes=30)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "source_id": self.path.stem,
            "status": status,
            "blocked_at": now.isoformat(timespec="seconds"),
            "blocked_until": until.isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def blocked_until(self, now: datetime | None = None) -> datetime | None:
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            until = datetime.fromisoformat(value["blocked_until"])
            return until if until > (now or datetime.now()) else None
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            return None

    def ensure_closed(self) -> None:
        until = self.blocked_until()
        if until:
            raise SourceCircuitOpen(f"来源处于冷却期，{until:%Y-%m-%d %H:%M} 后再试")

    @staticmethod
    def _retry_after_seconds(value: str, default: int) -> int:
        if value and value.strip().isdigit():
            return max(60, int(value.strip()))
        if value:
            try:
                target = email.utils.parsedate_to_datetime(value)
                now = datetime.now(target.tzinfo) if target.tzinfo else datetime.now()
                return max(60, int((target - now).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
        return default


class PublicPageClient:
    TRANSIENT_STATUSES = {500, 502, 503, 504}
    ACCESS_STATUSES = {403, 418, 429}

    def __init__(self, source_name: str, source_id: str | None = None):
        self.source_name = source_name
        self.source_id = source_id or source_name
        self.timeout = int(os.getenv("RADAR_COLLECTION_TIMEOUT_SECONDS", "60"))
        self.minimum_interval = float(os.getenv("RADAR_HTTP_MIN_INTERVAL_SECONDS", "2.5"))
        self.max_attempts = max(1, int(os.getenv("RADAR_HTTP_MAX_ATTEMPTS", "2")))
        self.list_cache_seconds = int(os.getenv("RADAR_HTTP_LIST_CACHE_SECONDS", "300"))
        self.detail_cache_seconds = int(os.getenv("RADAR_HTTP_DETAIL_CACHE_SECONDS", "86400"))
        self.cache = HttpCache()
        self.circuit = SourceCircuitBreaker(self.source_id)
        self.network = NetworkPolicy.from_env()
        self.session = self.network.requests_session()
        self.direct_session = self.network.requests_session(direct=True)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) EngineeringOpportunityRadar/0.2",
            "Accept": "text/html,application/xhtml+xml",
        }
        self.session.headers.update(headers)
        self.direct_session.headers.update(headers)

    def get_list_text(self, url: str, allow_windows_curl_fallback: bool = False) -> str:
        return self.get_text(url, allow_windows_curl_fallback, self.list_cache_seconds)

    def get_detail_text(self, url: str, allow_windows_curl_fallback: bool = False) -> str:
        return self.get_text(url, allow_windows_curl_fallback, self.detail_cache_seconds)

    def get_text(
        self,
        url: str,
        allow_windows_curl_fallback: bool = False,
        cache_ttl_seconds: int = 0,
    ) -> str:
        # A cached page must not make a blocked source look healthy in status
        # reporting, so the source circuit is checked before cache lookup.
        self.circuit.ensure_closed()
        cached = self.cache.get(url, cache_ttl_seconds)
        if cached is not None:
            log.debug("%s 使用 HTTP 缓存: %s", self.source_name, url)
            return cached
        routes = [("代理", self.session, False)]
        if self.network.has_direct_fallback:
            routes.append(("直连", self.direct_session, True))
        last_error: Exception | None = None
        last_status = 0
        last_retry_after = ""
        for route_index, (route_name, session, direct) in enumerate(routes):
            has_next_route = route_index + 1 < len(routes)
            for attempt in range(1, self.max_attempts + 1):
                HostRateLimiter.wait(url, self.minimum_interval)
                try:
                    response = session.get(url, timeout=self.timeout)
                    last_status = response.status_code
                    last_retry_after = response.headers.get("Retry-After", "")
                    if response.status_code in self.ACCESS_STATUSES:
                        last_error = RuntimeError(f"HTTP {response.status_code}")
                        if has_next_route:
                            log.warning(
                                "%s %s访问受限 HTTP %s，切换直连仅重试一次",
                                self.source_name, route_name, response.status_code,
                            )
                            break
                        self.circuit.block(response.status_code, retry_after=last_retry_after)
                        raise SourceCircuitOpen(
                            f"{self.source_name} 直连仍限制访问 HTTP {response.status_code}，已进入冷却且不再重试"
                        )
                    if response.status_code in self.TRANSIENT_STATUSES and attempt < self.max_attempts:
                        log.warning(
                            "%s %s临时故障 HTTP %s，有限重试 attempt=%s/%s",
                            self.source_name, route_name, response.status_code, attempt, self.max_attempts,
                        )
                        time.sleep(min(2 ** attempt, 5))
                        continue
                    if response.status_code in self.TRANSIENT_STATUSES:
                        last_error = RuntimeError(f"HTTP {response.status_code}")
                        if has_next_route:
                            log.warning("%s 代理连续临时故障，切换直连仅重试一次", self.source_name)
                            break
                        self.circuit.block(response.status_code)
                        raise SourceCircuitOpen(
                            f"{self.source_name} 直连连续临时故障 HTTP {response.status_code}，已暂停 30 分钟"
                        )
                    response.raise_for_status()
                    response.encoding = response.apparent_encoding or "utf-8"
                    self.cache.put(url, response.text)
                    return response.text
                except SourceCircuitOpen:
                    raise
                except requests.exceptions.SSLError as exc:
                    last_error = exc
                    if allow_windows_curl_fallback:
                        try:
                            text = self._windows_curl(url, direct=direct)
                            self.cache.put(url, text)
                            return text
                        except RuntimeError as curl_error:
                            last_error = curl_error
                    if has_next_route:
                        log.warning("%s 代理 TLS 失败，切换直连仅重试一次", self.source_name)
                        break
                    raise
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.HTTPError) as exc:
                    last_error = exc
                    if attempt < self.max_attempts and not isinstance(exc, requests.exceptions.HTTPError):
                        log.warning(
                            "%s %s网络临时故障，有限重试 attempt=%s/%s error=%s",
                            self.source_name, route_name, attempt, self.max_attempts, type(exc).__name__,
                        )
                        time.sleep(min(2 ** attempt, 5))
                        continue
                    if has_next_route:
                        log.warning("%s 代理请求失败，切换直连仅重试一次", self.source_name)
                        break
                    raise RuntimeError(f"{self.source_name} 直连请求失败：{last_error}") from exc
        if last_status in self.ACCESS_STATUSES:
            self.circuit.block(last_status, retry_after=last_retry_after)
        raise RuntimeError(f"{self.source_name} 代理及直连请求均失败：{last_error}")

    def _windows_curl(self, url: str, *, direct: bool = False) -> str:
        """Use Windows Schannel only for a Python/OpenSSL TLS incompatibility."""
        log.warning("%s Python TLS 不兼容，改用 Windows 系统 TLS: %s", self.source_name, url)
        command = [
            "curl.exe", "--fail", "--silent", "--show-error", "--location",
            "--ssl-no-revoke", "--max-time", str(self.timeout),
            "--user-agent", self.session.headers["User-Agent"],
        ]
        if direct:
            command.extend(["--noproxy", "*"])
        elif self.network.proxy_url:
            command.extend(["--proxy", self.network.proxy_url])
        command.append(url)
        completed = subprocess.run(command, capture_output=True, timeout=self.timeout + 5)
        if completed.returncode:
            error = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"{self.source_name} 系统 TLS 请求失败: {error}")
        return completed.stdout.decode("utf-8", errors="replace")
