"""Weekly cycle calculator: pure functions, the current time is always passed in.

A week is identified by the local date (``YYYY-MM-DD``) on which its window
opens. The window is open from open day/time (inclusive) until close day/time
(exclusive) in the configured timezone. The current week of a moment is the
most recent window that opened at or before it.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

from app.config import WEEKDAYS, Settings

OPEN = "open"
TIME_FORMAT = "%A %Y-%m-%d %H:%M %Z"
CLOSED = "closed"


@dataclass(frozen=True)
class Schedule:
    """The weekly window, in local wall-clock terms."""

    tz: tzinfo
    open_day: int  # 0 = Monday
    open_time: time
    close_day: int
    close_time: time

    def __post_init__(self) -> None:
        if (self.close_day, self.close_time) <= (self.open_day, self.open_time):
            raise ValueError("close must be later in the week than open")

    @classmethod
    def from_settings(cls, settings: Settings) -> "Schedule":
        return cls(
            tz=ZoneInfo(settings.timezone),
            open_day=WEEKDAYS.index(settings.open_day),
            open_time=time.fromisoformat(settings.open_time),
            close_day=WEEKDAYS.index(settings.close_day),
            close_time=time.fromisoformat(settings.close_time),
        )


@dataclass(frozen=True)
class Cycle:
    """Answers for one moment. All datetimes are timezone-aware."""

    phase: str  # OPEN or CLOSED
    week_id: str  # local date the week's window opens, YYYY-MM-DD
    week_open: datetime
    week_close: datetime
    next_open: datetime  # first opening strictly after the moment
    next_close: datetime  # first closing strictly after the moment

    @property
    def is_open(self) -> bool:
        return self.phase == OPEN


def _resolve(day: date, at: time, tz: tzinfo) -> datetime:
    """Local wall-clock `day` `at` in `tz` as an aware UTC instant.

    A time that occurs twice resolves to its first occurrence. A time that does
    not exist resolves to the first valid instant after it (the end of the gap).
    """
    naive = datetime.combine(day, at)
    utc = naive.replace(tzinfo=tz, fold=0).astimezone(UTC)
    if utc.astimezone(tz).replace(tzinfo=None) == naive:
        return utc
    # Nonexistent local time: find the instant the offset changes. Reading the
    # wall-clock time with each of the two offsets gives instants that bracket
    # the transition.
    old = naive.replace(tzinfo=tz, fold=0).utcoffset()
    new = naive.replace(tzinfo=tz, fold=1).utcoffset()
    lo = (naive - max(old, new)).replace(tzinfo=UTC)
    hi = (naive - min(old, new)).replace(tzinfo=UTC)
    # Transitions fall on whole seconds, so bisect on whole seconds.
    lo = lo.replace(microsecond=0)
    if hi.microsecond:
        hi = hi.replace(microsecond=0) + timedelta(seconds=1)
    before = lo.astimezone(tz).utcoffset()
    while hi - lo > timedelta(seconds=1):
        mid = lo + timedelta(seconds=((hi - lo) // timedelta(seconds=1)) // 2)
        if mid.astimezone(tz).utcoffset() == before:
            lo = mid
        else:
            hi = mid
    return hi


def _window(schedule: Schedule, open_date: date) -> tuple[datetime, datetime]:
    close_date = open_date + timedelta(days=schedule.close_day - schedule.open_day)
    return (
        _resolve(open_date, schedule.open_time, schedule.tz),
        _resolve(close_date, schedule.close_time, schedule.tz),
    )


def describe(now: datetime, schedule: Schedule) -> Cycle:
    """Phase, week identifier and relevant instants for the moment `now`.

    Raises ValueError if `now` is naive.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    instant = now.astimezone(UTC)
    local_day = instant.astimezone(schedule.tz).date()

    open_date = local_day - timedelta(days=(local_day.weekday() - schedule.open_day) % 7)
    week_open, week_close = _window(schedule, open_date)
    if week_open > instant:
        open_date -= timedelta(days=7)
        week_open, week_close = _window(schedule, open_date)

    next_open, next_close = _window(schedule, open_date + timedelta(days=7))
    if week_close > instant:
        next_close = week_close

    def local(value: datetime) -> datetime:
        return value.astimezone(schedule.tz)

    return Cycle(
        phase=OPEN if instant < week_close else CLOSED,
        week_id=open_date.isoformat(),
        week_open=local(week_open),
        week_close=local(week_close),
        next_open=local(next_open),
        next_close=local(next_close),
    )


def describe_settings(now: datetime, settings: Settings) -> Cycle:
    """`describe` using the application settings."""
    return describe(now, Schedule.from_settings(settings))


def format_moment(moment: datetime, settings: Settings) -> str:
    """A moment as shown to people, e.g. 'Friday 2026-11-27 17:00 CET (Europe/Amsterdam)'."""
    return f"{moment.astimezone(ZoneInfo(settings.timezone)).strftime(TIME_FORMAT)} ({settings.timezone})"
