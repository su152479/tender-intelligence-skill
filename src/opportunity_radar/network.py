"""Explicit, bounded proxy-to-direct routing for collectors."""

from __future__ import annotations

from dataclasses import dataclass
import os

import requests
from dotenv import load_dotenv


load_dotenv()


def _enabled(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class NetworkPolicy:
    proxy_url: str = ""
    direct_fallback: bool = True

    @classmethod
    def from_env(cls) -> "NetworkPolicy":
        return cls(
            proxy_url=os.getenv("RADAR_PROXY_URL", "").strip(),
            direct_fallback=_enabled("RADAR_DIRECT_FALLBACK", True),
        )

    @property
    def has_direct_fallback(self) -> bool:
        return bool(self.proxy_url and self.direct_fallback)

    def requests_session(self, *, direct: bool = False) -> requests.Session:
        session = requests.Session()
        # Do not inherit HTTP(S)_PROXY after choosing a route. Otherwise a
        # supposed direct retry may silently reuse the failing proxy.
        session.trust_env = False
        if self.proxy_url and not direct:
            session.proxies.update({"http": self.proxy_url, "https": self.proxy_url})
        return session

    def browser_launch_options(self, *, direct: bool = False) -> dict:
        if direct:
            return {"args": ["--no-proxy-server"]}
        if self.proxy_url:
            return {"proxy": {"server": self.proxy_url}}
        return {}

    @staticmethod
    def may_retry_direct(error: Exception) -> bool:
        message = str(error)
        blocked = ("登录状态" in message or "验证码" in message or "login" in message.lower())
        return not blocked
