"""
Time handling — PLATFORM §31

  - TimezoneHandler: UTC storage, local display
  - WorkingHours: 9-18 weekdays, respects timezone + holidays
  - HolidayCalendar: load + check if a date is a holiday
  - Tasks scheduled at the right time

Designed to be timezone-aware everywhere. Never store naive datetimes.
"""

import logging
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ── Timezone helper ──

def _resolve_timezone(tz_name: str):
    """
    Resolve a timezone name. Prefers zoneinfo (full IANA DB) but falls
    back to fixed UTC offsets if zoneinfo / tzdata is missing.
    """
    # Common fixed-offset shortcuts
    fixed_offsets = {
        "UTC": timezone.utc,
        "Etc/UTC": timezone.utc,
        "Asia/Shanghai": timezone(timedelta(hours=8)),
        "Asia/Tokyo": timezone(timedelta(hours=9)),
        "Asia/Kolkata": timezone(timedelta(hours=5, minutes=30)),
        "Europe/London": timezone(timedelta(hours=0)),  # Approx; ignores DST
        "America/New_York": timezone(timedelta(hours=-5)),  # Approx; ignores DST
        "America/Los_Angeles": timezone(timedelta(hours=-8)),  # Approx
    }
    if tz_name in fixed_offsets:
        return fixed_offsets[tz_name]
    # Try zoneinfo for full IANA support (requires tzdata on Linux/Mac)
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo(tz_name)
    except Exception:
        logger.debug(f"zoneinfo unavailable for {tz_name}, using UTC")
        return timezone.utc


# ── Timezone handler ──

class TimezoneHandler:
    """
    Wraps a user/system timezone. Always stores in UTC, displays in local.
    """

    def __init__(self, tz_name: str = "UTC"):
        self.tz = _resolve_timezone(tz_name)
        self.tz_name = tz_name

    def now_utc(self) -> datetime:
        """Return current time in UTC."""
        return datetime.now(timezone.utc)

    def now_local(self) -> datetime:
        """Return current time in the configured local timezone."""
        return datetime.now(self.tz)

    def to_local(self, utc_dt: datetime) -> datetime:
        """Convert a UTC datetime to local timezone."""
        if utc_dt.tzinfo is None:
            utc_dt = utc_dt.replace(tzinfo=timezone.utc)
        return utc_dt.astimezone(self.tz)

    def to_utc(self, local_dt: datetime) -> datetime:
        """Convert a local datetime to UTC."""
        if local_dt.tzinfo is None:
            local_dt = local_dt.replace(tzinfo=self.tz)
        return local_dt.astimezone(timezone.utc)

    def format(self, utc_dt: datetime, fmt: str = "%Y-%m-%d %H:%M %Z") -> str:
        """Format a UTC datetime in local timezone."""
        return self.to_local(utc_dt).strftime(fmt)

    def offset_hours(self) -> float:
        """Current UTC offset for this timezone (e.g. 8.0 for Asia/Shanghai)."""
        return self.now_local().utcoffset().total_seconds() / 3600


# ── Holiday calendar ──

class HolidayCalendar:
    """
    Simple holiday calendar. Defaults to a small list of common Chinese
    holidays; pluggable via the holidays list.
    """

    def __init__(self, holidays: list[date] | None = None):
        self.holidays = set(holidays or self._default_holidays())

    def _default_holidays(self) -> list[date]:
        """A tiny default list. Production should load from DB / API."""
        return [
            # 2026 holidays (placeholder; real system should pull from a source)
            date(2026, 1, 1),    # 元旦
            date(2026, 2, 17),   # 春节
            date(2026, 4, 5),    # 清明
            date(2026, 5, 1),    # 劳动节
            date(2026, 10, 1),   # 国庆
        ]

    def add_holiday(self, holiday: date):
        self.holidays.add(holiday)

    def is_holiday(self, d: date) -> bool:
        return d in self.holidays

    def is_workday(self, d: date) -> bool:
        """A day is a workday if it's a weekday and not a holiday."""
        return d.weekday() < 5 and not self.is_holiday(d)

    def count_workdays_between(self, start: date, end: date) -> int:
        """Number of workdays in [start, end] inclusive."""
        if end < start:
            return 0
        count = 0
        d = start
        while d <= end:
            if self.is_workday(d):
                count += 1
            d += timedelta(days=1)
        return count


# ── Working hours ──

class WorkUrgency(str, Enum):
    """How urgent a notification is."""
    HIGH = "high"      # notify any time
    NORMAL = "normal"  # notify during work hours
    LOW = "low"        # wait until work hours


class WorkingHours:
    """
    Tracks when someone is "on the clock". Default is 9-18 weekdays in
    the configured timezone.
    """

    def __init__(
        self,
        tz_handler: TimezoneHandler,
        work_start: time = time(9, 0),
        work_end: time = time(18, 0),
        calendar: HolidayCalendar | None = None,
    ):
        self.tz = tz_handler
        self.work_start = work_start
        self.work_end = work_end
        self.calendar = calendar or HolidayCalendar()

    def is_working_time(self, when: datetime | None = None) -> bool:
        """Check if `when` is within work hours (in the configured timezone)."""
        when = when or self.tz.now_local()
        local = self.tz.to_local(when) if when.tzinfo else when
        if not self.calendar.is_workday(local.date()):
            return False
        return self.work_start <= local.time() <= self.work_end

    def should_notify_now(
        self,
        when: datetime | None = None,
        urgency: WorkUrgency = WorkUrgency.NORMAL,
    ) -> bool:
        """Decide whether to deliver a notification right now."""
        if urgency == WorkUrgency.HIGH:
            return True  # Always notify urgent stuff immediately
        return self.is_working_time(when)

    def next_working_moment(
        self,
        after: datetime | None = None,
    ) -> datetime:
        """
        Return the next working moment after `after`.
        Used to schedule deferred notifications.
        """
        after = after or self.tz.now_local()
        # If we're inside work hours on a workday, return now
        if self.is_working_time(after):
            return self.tz.to_utc(after)
        # Otherwise find the next workday 9 AM
        candidate = after
        for _ in range(14):  # cap at 2 weeks
            candidate = (candidate + timedelta(days=1)).replace(
                hour=self.work_start.hour,
                minute=self.work_start.minute,
                second=0,
                microsecond=0,
            )
            if self.calendar.is_workday(candidate.date()):
                return self.tz.to_utc(candidate)
        # Fallback: 1 day from now
        return self.tz.to_utc(after + timedelta(days=1))


# ── Task timeout integration ──

# Default timeout by task type (PLATFORM §31.5)
DEFAULT_TIMEOUTS = {
    "quick": 60,         # 1 min
    "standard": 300,      # 5 min
    "complex": 1800,     # 30 min
    "long": 7200,         # 2 hours
    "batch": 86400,       # 1 day
}


def get_timeout_for(task_type: str) -> int:
    return DEFAULT_TIMEOUTS.get(task_type, 300)


def is_within_work_hours(
    when: datetime | None = None,
    tz_name: str = "UTC",
    calendar: HolidayCalendar | None = None,
    work_start: time = time(9, 0),
    work_end: time = time(18, 0),
) -> bool:
    """Convenience: is now within work hours in a given timezone?"""
    tz = TimezoneHandler(tz_name)
    cal = calendar or HolidayCalendar()
    wh = WorkingHours(tz, work_start, work_end, cal)
    return wh.is_working_time(when)
