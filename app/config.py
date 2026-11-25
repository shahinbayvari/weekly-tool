"""Application settings, read from environment variables."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEV_SECRET_KEY = "dev-only-insecure-secret-key"  # development only

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
ENVIRONMENTS = ("development", "production")
_TIME_RE = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


class ConfigError(ValueError):
    """Raised when a setting is invalid. The message names the variable."""


@dataclass(frozen=True)
class Settings:
    db_path: str = "weekly.db"
    secret_key: str = field(default=DEV_SECRET_KEY, repr=False)
    timezone: str = "UTC"
    open_day: str = "monday"
    open_time: str = "09:00"
    close_day: str = "friday"
    close_time: str = "17:00"
    env: str = "development"


def _day(name: str, value: str) -> str:
    day = value.strip().lower()
    if day not in WEEKDAYS:
        raise ConfigError(f"{name}: {value!r} is not a weekday name")
    return day


def _time(name: str, value: str) -> str:
    if not _TIME_RE.fullmatch(value):
        raise ConfigError(f"{name}: {value!r} is not a time in HH:MM (24-hour) format")
    return value


def _moment(day: str, hhmm: str) -> tuple[int, time]:
    return WEEKDAYS.index(day), time.fromisoformat(hhmm)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from `env` (default: the process environment)."""
    if env is None:
        env = os.environ
    defaults = Settings()

    def get(name: str, default: str) -> str:
        return env.get(name, default)

    db_path = get("WEEKLY_DB_PATH", defaults.db_path)

    secret_key = get("WEEKLY_SECRET_KEY", defaults.secret_key)
    if not secret_key:
        raise ConfigError("WEEKLY_SECRET_KEY: must not be empty")

    tz = get("WEEKLY_TIMEZONE", defaults.timezone)
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ConfigError(f"WEEKLY_TIMEZONE: {tz!r} is not a known IANA timezone") from None

    open_day = _day("WEEKLY_OPEN_DAY", get("WEEKLY_OPEN_DAY", defaults.open_day))
    open_time = _time("WEEKLY_OPEN_TIME", get("WEEKLY_OPEN_TIME", defaults.open_time))
    close_day = _day("WEEKLY_CLOSE_DAY", get("WEEKLY_CLOSE_DAY", defaults.close_day))
    close_time = _time("WEEKLY_CLOSE_TIME", get("WEEKLY_CLOSE_TIME", defaults.close_time))

    if _moment(close_day, close_time) <= _moment(open_day, open_time):
        raise ConfigError(
            f"WEEKLY_CLOSE_DAY/WEEKLY_CLOSE_TIME: close {close_day} {close_time} "
            f"must be later in the week than open {open_day} {open_time}"
        )

    app_env = get("WEEKLY_ENV", defaults.env)
    if app_env not in ENVIRONMENTS:
        raise ConfigError(f"WEEKLY_ENV: {app_env!r} must be one of {', '.join(ENVIRONMENTS)}")
    if app_env == "production" and secret_key == DEV_SECRET_KEY:
        raise ConfigError("WEEKLY_SECRET_KEY: the default development key is not allowed in production")

    return Settings(
        db_path=db_path,
        secret_key=secret_key,
        timezone=tz,
        open_day=open_day,
        open_time=open_time,
        close_day=close_day,
        close_time=close_time,
        env=app_env,
    )
