"""
Tests: Time handling (TimezoneHandler, WorkingHours, HolidayCalendar)
"""

import pytest
from datetime import date, datetime, time, timedelta, timezone

from agent_system.core.time.handling import (
    TimezoneHandler,
    HolidayCalendar,
    WorkingHours,
    WorkUrgency,
    DEFAULT_TIMEOUTS,
    get_timeout_for,
    is_within_work_hours,
)


# ── TimezoneHandler ──

class TestTimezoneHandler:
    def test_utc_default(self):
        tz = TimezoneHandler("UTC")
        assert tz.tz_name == "UTC"
        assert tz.offset_hours() == 0

    def test_asia_shanghai_offset(self):
        tz = TimezoneHandler("Asia/Shanghai")
        # Shanghai is UTC+8
        assert tz.offset_hours() == 8.0

    def test_unknown_tz_falls_back_to_utc(self):
        tz = TimezoneHandler("Mars/Olympus_Mons")
        assert tz.tz_name == "Mars/Olympus_Mons"
        assert tz.tz == TimezoneHandler("UTC").tz

    def test_utc_to_local_conversion(self):
        tz = TimezoneHandler("Asia/Shanghai")
        utc_dt = datetime(2026, 6, 30, 10, 0, 0, tzinfo=timezone.utc)
        local = tz.to_local(utc_dt)
        assert local.hour == 18  # 10 + 8
        assert local.tzinfo is not None

    def test_local_to_utc_conversion(self):
        tz = TimezoneHandler("Asia/Shanghai")
        local_dt = datetime(2026, 6, 30, 18, 0, 0)  # naive local
        utc = tz.to_utc(local_dt)
        assert utc.hour == 10  # 18 - 8
        assert utc.tzinfo == timezone.utc

    def test_naive_to_utc_treats_as_local(self):
        tz = TimezoneHandler("Asia/Shanghai")
        naive = datetime(2026, 6, 30, 18, 0, 0)  # 6 PM Shanghai
        utc = tz.to_utc(naive)
        assert utc.hour == 10
        assert utc.tzinfo == timezone.utc

    def test_format_includes_timezone(self):
        tz = TimezoneHandler("UTC")
        dt = datetime(2026, 6, 30, 14, 30, 0, tzinfo=timezone.utc)
        s = tz.format(dt, fmt="%H:%M")
        assert s == "14:30"


# ── HolidayCalendar ──

class TestHolidayCalendar:
    def test_default_holidays_present(self):
        cal = HolidayCalendar()
        assert cal.is_holiday(date(2026, 1, 1))  # 元旦
        assert cal.is_holiday(date(2026, 10, 1))  # 国庆

    def test_add_holiday(self):
        cal = HolidayCalendar(holidays=[])
        assert not cal.is_holiday(date(2026, 7, 1))
        cal.add_holiday(date(2026, 7, 1))
        assert cal.is_holiday(date(2026, 7, 1))

    def test_is_workday_weekday(self):
        # 2026-06-30 is a Tuesday
        cal = HolidayCalendar(holidays=[])
        assert cal.is_workday(date(2026, 6, 30)) is True

    def test_is_workday_weekend(self):
        # 2026-06-27 is a Saturday
        cal = HolidayCalendar(holidays=[])
        assert cal.is_workday(date(2026, 6, 27)) is False
        assert cal.is_workday(date(2026, 6, 28)) is False  # Sunday

    def test_is_workday_holiday(self):
        # Even on a weekday, a holiday is not a workday
        cal = HolidayCalendar(holidays=[date(2026, 7, 1)])  # 2026-07-01 is Wed
        assert cal.is_workday(date(2026, 7, 1)) is False

    def test_count_workdays_between(self):
        cal = HolidayCalendar(holidays=[])
        # 2026-06-29 (Mon) to 2026-07-03 (Fri): 5 workdays
        count = cal.count_workdays_between(date(2026, 6, 29), date(2026, 7, 3))
        assert count == 5

        # Skip weekend
        count = cal.count_workdays_between(date(2026, 6, 27), date(2026, 6, 28))
        assert count == 0

    def test_count_workdays_with_holiday(self):
        cal = HolidayCalendar(holidays=[date(2026, 7, 1)])  # Wed
        # Mon-Wed-Fri: should be 2 (Wed is holiday)
        count = cal.count_workdays_between(date(2026, 6, 29), date(2026, 7, 3))
        assert count == 4  # Mon, Tue, Thu, Fri (Wed is holiday)


# ── WorkingHours ──

class TestWorkingHours:
    def setup_method(self):
        self.tz = TimezoneHandler("UTC")
        self.wh = WorkingHours(self.tz)

    def test_inside_work_hours(self):
        # 14:00 Tuesday is work time
        dt = datetime(2026, 6, 30, 14, 0, 0, tzinfo=timezone.utc)  # Tuesday
        assert self.wh.is_working_time(dt) is True

    def test_at_work_start(self):
        # 9:00 sharp is work time
        dt = datetime(2026, 6, 30, 9, 0, 0, tzinfo=timezone.utc)
        assert self.wh.is_working_time(dt) is True

    def test_at_work_end(self):
        # 18:00 sharp is work time (inclusive)
        dt = datetime(2026, 6, 30, 18, 0, 0, tzinfo=timezone.utc)
        assert self.wh.is_working_time(dt) is True

    def test_before_work(self):
        dt = datetime(2026, 6, 30, 8, 0, 0, tzinfo=timezone.utc)
        assert self.wh.is_working_time(dt) is False

    def test_after_work(self):
        dt = datetime(2026, 6, 30, 19, 0, 0, tzinfo=timezone.utc)
        assert self.wh.is_working_time(dt) is False

    def test_weekend_off(self):
        # 2026-06-27 is Saturday
        dt = datetime(2026, 6, 27, 14, 0, 0, tzinfo=timezone.utc)
        assert self.wh.is_working_time(dt) is False

    def test_high_urgency_always_notify(self):
        # Even on weekend
        dt = datetime(2026, 6, 27, 14, 0, 0, tzinfo=timezone.utc)  # Saturday
        assert self.wh.should_notify_now(dt, urgency=WorkUrgency.HIGH) is True

    def test_normal_urgency_defers(self):
        # On weekend, normal defers
        dt = datetime(2026, 6, 27, 14, 0, 0, tzinfo=timezone.utc)  # Saturday
        assert self.wh.should_notify_now(dt, urgency=WorkUrgency.NORMAL) is False

    def test_next_working_moment_after_hours(self):
        # Currently Saturday 14:00 — next working moment should be Monday 9:00
        dt = datetime(2026, 6, 27, 14, 0, 0, tzinfo=timezone.utc)  # Saturday
        next_moment = self.wh.next_working_moment(dt)
        # Should be Monday 2026-06-29 at 09:00 UTC
        assert next_moment.weekday() == 0  # Monday
        assert next_moment.hour == 9

    def test_timezone_aware_working_hours(self):
        # Beijing 9:00 = UTC 1:00. Verify timezone handling.
        tz = TimezoneHandler("Asia/Shanghai")
        wh = WorkingHours(tz)
        # UTC 1:00 = Shanghai 9:00 — should be work time
        dt_utc = datetime(2026, 6, 30, 1, 0, 0, tzinfo=timezone.utc)
        assert wh.is_working_time(dt_utc) is True
        # UTC 0:00 = Shanghai 8:00 — before work
        dt_utc2 = datetime(2026, 6, 30, 0, 0, 0, tzinfo=timezone.utc)
        assert wh.is_working_time(dt_utc2) is False


# ── Default timeouts ──

class TestTimeouts:
    def test_default_values(self):
        assert DEFAULT_TIMEOUTS["quick"] == 60
        assert DEFAULT_TIMEOUTS["standard"] == 300
        assert DEFAULT_TIMEOUTS["complex"] == 1800
        assert DEFAULT_TIMEOUTS["long"] == 7200
        assert DEFAULT_TIMEOUTS["batch"] == 86400

    def test_get_timeout_for(self):
        assert get_timeout_for("quick") == 60
        assert get_timeout_for("standard") == 300
        # Unknown defaults to 300 (standard)
        assert get_timeout_for("unknown") == 300


# ── Convenience ──

class TestIsWithinWorkHours:
    def test_convenience_function(self):
        # Use a known workday
        dt = datetime(2026, 6, 30, 14, 0, 0, tzinfo=timezone.utc)  # Tuesday
        assert is_within_work_hours(when=dt, tz_name="UTC") is True

    def test_convenience_function_outside_hours(self):
        dt = datetime(2026, 6, 30, 22, 0, 0, tzinfo=timezone.utc)
        assert is_within_work_hours(when=dt, tz_name="UTC") is False
