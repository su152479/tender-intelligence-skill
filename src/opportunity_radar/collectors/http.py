"""Small, polite HTTP helper for public government pages."""

import logging
import os
import random
import subprocess
import time

import requests

log = logging.getLogger(__name__)


class PublicPageClient:
    def __init__(self, source_name: str):
        self.source_name = source_name
        self.timeout = int(os.getenv("RADAR_COLLECTION_TIMEOUT_SECONDS", "60"))
        self.delay_min = float(os.getenv("RADAR_GOV_DELAY_MIN_SECONDS", "1.0"))
        self.delay_max = float(os.getenv("RADAR_GOV_DELAY_MAX_SECONDS", "2.5"))
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) EngineeringOpportunityRadar/0.2",
            "Accept": "text/html,application/xhtml+xml",
        })
        self._last_request_at = 0.0

    def get_text(self, url: str, allow_windows_curl_fallback: bool = False) -> str:
        self._pace()
        try:
            response = self.session.get(url, timeout=self.timeout)
            if response.status_code in (403, 429):
                if allow_windows_curl_fallback and response.status_code == 403:
                    return self._windows_curl(url)
                raise RuntimeError(f"{self.source_name} 限制访问 HTTP {response.status_code}，本次已停止，请稍后重试")
            response.raise_for_status()
            response.encoding = response.apparent_encoding or "utf-8"
            return response.text
        except requests.exceptions.SSLError:
            if not allow_windows_curl_fallback:
                raise
            return self._windows_curl(url)

    def _pace(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        delay = random.uniform(self.delay_min, self.delay_max)
        if self._last_request_at and elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request_at = time.monotonic()

    def _windows_curl(self, url: str) -> str:
        """Use Windows Schannel when Python/OpenSSL rejects a government site's EC chain.

        --ssl-no-revoke only disables the often-unavailable Windows revocation lookup;
        hostname and certificate validation remain enabled.
        """
        log.warning("%s Python TLS 不兼容，改用 Windows 系统 TLS: %s", self.source_name, url)
        command = [
            "curl.exe", "--fail", "--silent", "--show-error", "--location",
            "--ssl-no-revoke", "--max-time", str(self.timeout),
            "--user-agent", self.session.headers["User-Agent"], url,
        ]
        completed = subprocess.run(command, capture_output=True, timeout=self.timeout + 5)
        if completed.returncode:
            error = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"{self.source_name} 系统 TLS 请求失败: {error}")
        return completed.stdout.decode("utf-8", errors="replace")
