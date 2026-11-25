from datetime import UTC, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.cycle import CLOSED, OPEN, Schedule, describe, describe_settings

AMS = ZoneInfo("Europe/Amsterdam")
NY = ZoneInfo("America/New_York")

DEFAULT = Schedule(AMS, 0, time(9, 0), 4, time(17, 0))
NON_DEFAULT = Schedule(NY, 2, time(12, 0), 3, time(12, 0))
WEEKEND = Schedule(AMS, 5, time(9, 0), 6, time(17, 0))  # Sat 09:00 - Sun 17:00


def ams(*args):
    return datetime(*args, tzinfo=AMS)


def ny(*args):
    return datetime(*args, tzinfo=NY)


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def window_hours(c):
    return (c.week_close.astimezone(UTC) - c.week_open.astimezone(UTC)) / timedelta(hours=1)


# 2026-11-23 is a Monday.


def test_monday_before_open_is_closed_previous_week():
    c = describe(ams(2026, 11, 23, 8, 59, 59), DEFAULT)
    assert c.phase == CLOSED
    assert c.week_id == "2026-11-16"


def test_monday_at_open_is_open_this_week():
    c = describe(ams(2026, 11, 23, 9, 0, 0), DEFAULT)
    assert c.phase == OPEN and c.is_open
    assert c.week_id == "2026-11-23"


def test_friday_last_microsecond_open_then_closed():
    assert describe(ams(2026, 11, 27, 16, 59, 59, 999999), DEFAULT).phase == OPEN
    c = describe(ams(2026, 11, 27, 17, 0, 0), DEFAULT)
    assert c.phase == CLOSED
    assert c.week_id == "2026-11-23"


@pytest.mark.parametrize("day", [28, 29])
def test_weekend_closed_with_monday_just_passed(day):
    c = describe(ams(2026, 11, day, 12, 0), DEFAULT)
    assert c.phase == CLOSED
    assert c.week_id == "2026-11-23"


def test_next_instants_before_window():
    c = describe(ams(2026, 11, 23, 8, 0), DEFAULT)
    assert c.next_open == ams(2026, 11, 23, 9, 0)
    assert c.next_close == ams(2026, 11, 27, 17, 0)


def test_next_instants_during_window():
    c = describe(ams(2026, 11, 25, 10, 0), DEFAULT)
    assert c.next_close == ams(2026, 11, 27, 17, 0)
    assert c.next_open == ams(2026, 11, 30, 9, 0)


def test_next_instants_after_window():
    c = describe(ams(2026, 11, 28, 10, 0), DEFAULT)
    assert c.next_open == ams(2026, 11, 30, 9, 0)
    assert c.next_close == ams(2026, 12, 4, 17, 0)


def test_own_week_open_and_close_before_during_and_after():
    before = describe(ams(2026, 11, 23, 8, 0), DEFAULT)
    assert before.week_open == ams(2026, 11, 16, 9, 0)
    assert before.week_close == ams(2026, 11, 20, 17, 0)
    for moment in (ams(2026, 11, 25, 10, 0), ams(2026, 11, 28, 10, 0)):
        c = describe(moment, DEFAULT)
        assert c.week_open == ams(2026, 11, 23, 9, 0)
        assert c.week_close == ams(2026, 11, 27, 17, 0)


def test_year_boundary_week_id():
    assert describe(ams(2027, 1, 1, 12, 0), DEFAULT).week_id == "2026-12-28"


def test_returned_instants_are_aware():
    c = describe(ams(2026, 11, 25, 10, 0), DEFAULT)
    for value in (c.week_open, c.week_close, c.next_open, c.next_close):
        assert value.tzinfo is not None and value.utcoffset() is not None


# Daylight saving


def test_window_spanning_spring_change_is_one_hour_shorter():
    # Clock change 2026-03-29 (Sunday)
    c = describe(ams(2026, 3, 28, 12, 0), WEEKEND)
    assert c.week_open == ams(2026, 3, 28, 9, 0)
    assert c.week_close == ams(2026, 3, 29, 17, 0)
    assert window_hours(c) == 31
    assert window_hours(describe(ams(2026, 3, 21, 12, 0), WEEKEND)) == 32


def test_window_spanning_autumn_change_is_one_hour_longer():
    # Clock change 2026-10-25 (Sunday)
    c = describe(ams(2026, 10, 25, 12, 0), WEEKEND)
    assert c.week_open == ams(2026, 10, 24, 9, 0)
    assert c.week_close == ams(2026, 10, 25, 17, 0)
    assert window_hours(c) == 33


def test_default_week_after_change_keeps_local_times():
    after = describe(ams(2026, 3, 31, 12, 0), DEFAULT)
    assert after.week_open.hour == 9 and after.week_open.utcoffset() == timedelta(hours=2)
    assert after.week_close.hour == 17
    before = describe(ams(2026, 3, 27, 12, 0), DEFAULT)
    assert before.week_open.utcoffset() == timedelta(hours=1)


def test_default_window_spanning_spring_change_has_shorter_length():
    # Open Saturday 2026-03-28 evening to Monday would need a longer config; use Fri-Mon.
    sched = Schedule(AMS, 4, time(17, 0), 6, time(17, 0))
    assert window_hours(describe(ams(2026, 3, 28, 12, 0), sched)) == 47
    assert window_hours(describe(ams(2026, 10, 24, 12, 0), sched)) == 49
    assert window_hours(describe(ams(2026, 11, 21, 12, 0), sched)) == 48


def test_spring_gap_open_time_resolves_to_first_valid_instant_after():
    sched = Schedule(AMS, 6, time(2, 30), 6, time(23, 0))  # Sunday 02:30 - 23:00
    c = describe(ams(2026, 3, 29, 12, 0), sched)
    assert c.week_open.astimezone(UTC) == utc(2026, 3, 29, 1, 0)
    assert (c.week_open.hour, c.week_open.minute) == (3, 0)
    assert describe(utc(2026, 3, 29, 0, 59, 59), sched).phase == CLOSED
    assert describe(utc(2026, 3, 29, 1, 0), sched).phase == OPEN


def test_spring_gap_close_time_resolves_to_first_valid_instant_after():
    sched = Schedule(AMS, 5, time(9, 0), 6, time(2, 30))  # Sat 09:00 - Sun 02:30
    c = describe(ams(2026, 3, 28, 12, 0), sched)
    assert c.week_close.astimezone(UTC) == utc(2026, 3, 29, 1, 0)
    assert describe(utc(2026, 3, 29, 0, 59, 59), sched).phase == OPEN
    assert describe(utc(2026, 3, 29, 1, 0), sched).phase == CLOSED


def test_autumn_fold_close_time_resolves_to_first_occurrence():
    sched = Schedule(AMS, 5, time(9, 0), 6, time(2, 30))  # 02:30 occurs twice on 2026-10-25
    c = describe(ams(2026, 10, 24, 12, 0), sched)
    assert c.week_close.astimezone(UTC) == utc(2026, 10, 25, 0, 30)  # first, CEST
    assert c.week_close.utcoffset() == timedelta(hours=2)


def test_autumn_fold_open_time_resolves_to_first_occurrence():
    sched = Schedule(AMS, 6, time(2, 30), 6, time(23, 0))
    c = describe(ams(2026, 10, 25, 12, 0), sched)
    assert c.week_open.astimezone(UTC) == utc(2026, 10, 25, 0, 30)


# Input zone independence


def test_same_moment_in_other_zones_gives_same_answer():
    moment = ams(2026, 11, 27, 16, 30)
    expected = describe(moment, DEFAULT)
    for zone in (UTC, NY, ZoneInfo("Asia/Tokyo"), timezone(timedelta(hours=-3, minutes=-30))):
        assert describe(moment.astimezone(zone), DEFAULT) == expected


def test_boundaries_given_in_utc():
    assert describe(utc(2026, 11, 23, 8, 0), DEFAULT).phase == OPEN  # 09:00 in Amsterdam
    assert describe(utc(2026, 11, 23, 7, 59, 59), DEFAULT).phase == CLOSED


# Input rules


def test_naive_datetime_raises():
    with pytest.raises(ValueError):
        describe(datetime(2026, 11, 25, 10, 0), DEFAULT)


def test_repeatable_and_independent_of_machine_timezone(monkeypatch):
    moment = ams(2026, 11, 25, 10, 0)
    first = describe(moment, DEFAULT)
    assert describe(moment, DEFAULT) == first
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    assert describe(moment, DEFAULT) == first


def test_schedule_requires_close_after_open():
    with pytest.raises(ValueError):
        Schedule(AMS, 4, time(17, 0), 0, time(9, 0))


# Non-default configuration: Wed 12:00 - Thu 12:00 America/New_York
# 2026-11-25 is a Wednesday.


def test_non_default_configuration():
    before = describe(ny(2026, 11, 25, 11, 59, 59), NON_DEFAULT)
    assert before.phase == CLOSED and before.week_id == "2026-11-18"
    start = describe(ny(2026, 11, 25, 12, 0), NON_DEFAULT)
    assert start.phase == OPEN and start.week_id == "2026-11-25"
    assert start.week_close == ny(2026, 11, 26, 12, 0)
    assert start.next_close == ny(2026, 11, 26, 12, 0)
    assert start.next_open == ny(2026, 12, 2, 12, 0)
    end = describe(ny(2026, 11, 26, 12, 0), NON_DEFAULT)
    assert end.phase == CLOSED and end.week_id == "2026-11-25"
    assert end.next_open == ny(2026, 12, 2, 12, 0)
    assert end.next_close == ny(2026, 12, 3, 12, 0)


def test_describe_settings_uses_settings_fields():
    moment = ams(2026, 11, 23, 9, 0)
    s = Settings(timezone="Europe/Amsterdam")
    assert describe_settings(moment, s) == describe(moment, DEFAULT)
