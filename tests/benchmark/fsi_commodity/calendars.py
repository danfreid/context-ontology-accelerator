# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Per-party business calendars, time zones and working hours.

Holiday lists are our own approximations for 2025 and 2026, enough to make business-day arithmetic
differ by jurisdiction. They are not an authoritative source.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

COVERED_YEARS = (2025, 2026)


@dataclass(frozen=True)
class Calendar:
    """A jurisdiction's weekend, holidays, time zone and working hours."""

    code: str
    tz: str
    holidays: frozenset[date]
    open_time: time = time(9, 0)
    close_time: time = time(17, 30)


def _dates(*items: str) -> frozenset[date]:
    return frozenset(date.fromisoformat(i) for i in items)


_US = _dates(
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-05-26", "2025-06-19", "2025-07-04",
    "2025-09-01", "2025-10-13", "2025-11-11", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-10-12", "2026-11-11", "2026-11-26", "2026-12-25",
)  # fmt: skip

CALENDARS: dict[str, Calendar] = {
    "AU-NSW": Calendar(
        "AU-NSW",
        "Australia/Sydney",
        _dates(
            "2025-01-01",
            "2025-01-27",
            "2025-04-18",
            "2025-04-21",
            "2025-04-25",
            "2025-06-09",
            "2025-10-06",
            "2025-12-25",
            "2025-12-26",
            "2026-01-01",
            "2026-01-26",
            "2026-04-03",
            "2026-04-06",
            "2026-06-08",
            "2026-10-05",
            "2026-12-25",
            "2026-12-28",
        ),  # fmt: skip
    ),
    "US-TX": Calendar("US-TX", "America/Chicago", _US),
    "US-NY": Calendar("US-NY", "America/New_York", _US),
    "GB": Calendar(
        "GB",
        "Europe/London",
        _dates(
            "2025-01-01",
            "2025-04-18",
            "2025-04-21",
            "2025-05-05",
            "2025-05-26",
            "2025-08-25",
            "2025-12-25",
            "2025-12-26",
            "2026-01-01",
            "2026-04-03",
            "2026-04-06",
            "2026-05-04",
            "2026-05-25",
            "2026-08-31",
            "2026-12-25",
            "2026-12-28",
        ),  # fmt: skip
    ),
    "AE": Calendar(
        "AE", "Asia/Dubai", _dates("2025-01-01", "2025-12-02", "2025-12-03", "2026-01-01", "2026-12-02", "2026-12-03")
    ),
}


def _check(d: date) -> None:
    if d.year not in COVERED_YEARS:
        raise ValueError(f"calendar data covers {COVERED_YEARS}, got {d}")


def is_business_day(cal: Calendar, d: date) -> bool:
    """Return True when ``d`` is a weekday and not a holiday in ``cal``."""
    _check(d)
    return d.weekday() < 5 and d not in cal.holidays


def next_business_day(cal: Calendar, d: date) -> date:
    """Return ``d`` if it is a business day, otherwise the next one."""
    while not is_business_day(cal, d):
        d += timedelta(days=1)
    return d


def add_business_days(cal: Calendar, d: date, n: int) -> date:
    """Return the date ``n`` business days after (or before, if negative) ``d``."""
    step = 1 if n >= 0 else -1
    remaining = abs(n)
    while remaining:
        d += timedelta(days=step)
        if is_business_day(cal, d):
            remaining -= 1
    return d


def local_datetime(cal: Calendar, d: date, minute_of_day: int) -> datetime:
    """Return an aware datetime on ``d`` at ``minute_of_day`` minutes after local midnight."""
    return datetime.combine(d, time(minute_of_day // 60, minute_of_day % 60), tzinfo=ZoneInfo(cal.tz))


def working_minutes(cal: Calendar) -> tuple[int, int]:
    """Return (open, close) as minutes after local midnight."""
    return cal.open_time.hour * 60 + cal.open_time.minute, cal.close_time.hour * 60 + cal.close_time.minute


def in_working_hours(cal: Calendar, moment: datetime) -> bool:
    """Return True when ``moment`` falls on a business day inside ``cal``'s working hours."""
    local = moment.astimezone(ZoneInfo(cal.tz))
    start, end = working_minutes(cal)
    minute = local.hour * 60 + local.minute
    return is_business_day(cal, local.date()) and start <= minute <= end
