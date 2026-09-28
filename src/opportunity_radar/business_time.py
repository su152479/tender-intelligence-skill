"""Single business-time definition for Beijing-Tianjin-Hebei reporting."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


BUSINESS_TIMEZONE_NAME = "Asia/Shanghai"
try:
    BUSINESS_TIMEZONE = ZoneInfo(BUSINESS_TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    # Windows Python installations may not ship the IANA database. Shanghai has
    # used UTC+8 without daylight-saving transitions throughout this system's
    # operational date range, so this preserves the intended business boundary.
    BUSINESS_TIMEZONE = timezone(timedelta(hours=8), BUSINESS_TIMEZONE_NAME)


def business_now() -> datetime:
    """Return the current instant in the radar's business timezone."""
    return datetime.now(BUSINESS_TIMEZONE)


def business_today() -> date:
    return business_now().date()


def business_now_naive() -> datetime:
    """Return Shanghai wall-clock time in the repository's existing naive ISO format."""
    return business_now().replace(tzinfo=None)


def coerce_business_date(value: date | str | None = None) -> date:
    if value is None:
        return business_today()
    if isinstance(value, datetime):
        return value.astimezone(BUSINESS_TIMEZONE).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def business_day_bounds(value: date | str | None = None) -> tuple[str, str]:
    """Return naive ISO bounds matching the existing local-time SQLite format."""
    day = coerce_business_date(value)
    start = datetime.combine(day, time.min)
    return start.isoformat(timespec="seconds"), (start + timedelta(days=1)).isoformat(timespec="seconds")
