import dataclasses
import importlib

import pytest

from app.config import DEV_SECRET_KEY, ConfigError, Settings, load_settings


def test_defaults_with_empty_environment():
    s = load_settings({})
    assert s == Settings()
    assert (s.db_path, s.secret_key, s.timezone) == ("weekly.db", DEV_SECRET_KEY, "UTC")
    assert (s.open_day, s.open_time) == ("monday", "09:00")
    assert (s.close_day, s.close_time) == ("friday", "17:00")
    assert s.env == "development"


def test_overrides_do_not_touch_real_environment(monkeypatch):
    monkeypatch.setenv("WEEKLY_DB_PATH", "from-real-env.db")
    assert load_settings({"WEEKLY_DB_PATH": "x.db"}).db_path == "x.db"


def test_reads_process_environment_by_default(monkeypatch):
    monkeypatch.setenv("WEEKLY_DB_PATH", "real.db")
    assert load_settings().db_path == "real.db"


def test_settings_are_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        load_settings({}).db_path = "other"


def test_valid_timezone_accepted():
    assert load_settings({"WEEKLY_TIMEZONE": "Europe/Amsterdam"}).timezone == "Europe/Amsterdam"


def test_unknown_timezone_rejected():
    with pytest.raises(ConfigError, match=r"WEEKLY_TIMEZONE.*Mars/Base"):
        load_settings({"WEEKLY_TIMEZONE": "Mars/Base"})


def test_day_is_case_insensitive():
    assert load_settings({"WEEKLY_CLOSE_DAY": "Friday"}).close_day == "friday"


@pytest.mark.parametrize("var", ["WEEKLY_OPEN_DAY", "WEEKLY_CLOSE_DAY"])
def test_bad_day_rejected(var):
    with pytest.raises(ConfigError, match=rf"{var}.*funday"):
        load_settings({var: "funday"})


@pytest.mark.parametrize("var", ["WEEKLY_OPEN_TIME", "WEEKLY_CLOSE_TIME"])
@pytest.mark.parametrize("bad", ["25:00", "9am", "", "12:60", "9:00"])
def test_bad_time_rejected(var, bad):
    with pytest.raises(ConfigError, match=rf"{var}.*{bad!r}"):
        load_settings({var: bad})


@pytest.mark.parametrize(
    "env",
    [
        # equal to the default open moment
        {"WEEKLY_CLOSE_DAY": "monday", "WEEKLY_CLOSE_TIME": "09:00"},
        # close before open, would wrap past the end of the week
        {"WEEKLY_CLOSE_DAY": "sunday", "WEEKLY_CLOSE_TIME": "08:00",
         "WEEKLY_OPEN_DAY": "sunday", "WEEKLY_OPEN_TIME": "09:00"},
        {"WEEKLY_OPEN_DAY": "wednesday", "WEEKLY_CLOSE_DAY": "tuesday"},
        # earlier than default open, same day
        {"WEEKLY_CLOSE_DAY": "monday", "WEEKLY_CLOSE_TIME": "08:00"},
        # open is sunday 09:00 by default close friday -> wraps
        {"WEEKLY_OPEN_DAY": "sunday"},
    ],
)
def test_close_must_be_after_open(env):
    with pytest.raises(ConfigError, match="WEEKLY_CLOSE_DAY"):
        load_settings(env)


def test_close_later_same_day_is_ok():
    s = load_settings({"WEEKLY_CLOSE_DAY": "monday", "WEEKLY_CLOSE_TIME": "09:01"})
    assert s.close_time == "09:01"


def test_empty_secret_rejected():
    with pytest.raises(ConfigError, match="WEEKLY_SECRET_KEY"):
        load_settings({"WEEKLY_SECRET_KEY": ""})


def test_production_rejects_default_secret():
    with pytest.raises(ConfigError, match="WEEKLY_SECRET_KEY") as exc:
        load_settings({"WEEKLY_ENV": "production"})
    assert DEV_SECRET_KEY not in str(exc.value)


def test_production_rejects_explicit_default_secret():
    with pytest.raises(ConfigError, match="WEEKLY_SECRET_KEY"):
        load_settings({"WEEKLY_ENV": "production", "WEEKLY_SECRET_KEY": DEV_SECRET_KEY})


def test_production_with_real_secret_ok():
    s = load_settings({"WEEKLY_ENV": "production", "WEEKLY_SECRET_KEY": "s3cret"})
    assert s.env == "production"


def test_unknown_env_rejected():
    with pytest.raises(ConfigError, match=r"WEEKLY_ENV.*staging"):
        load_settings({"WEEKLY_ENV": "staging"})


def test_secret_never_in_other_errors():
    with pytest.raises(ConfigError) as exc:
        load_settings({"WEEKLY_SECRET_KEY": "topsecret", "WEEKLY_TIMEZONE": "Mars/Base"})
    assert "topsecret" not in str(exc.value)


def test_app_import_fails_with_bad_settings(monkeypatch):
    import app.main

    monkeypatch.setenv("WEEKLY_ENV", "bogus")
    with pytest.raises(ConfigError, match="WEEKLY_ENV"):
        importlib.reload(app.main)
    monkeypatch.undo()
    importlib.reload(app.main)


def test_secret_not_in_settings_repr():
    s = load_settings({"WEEKLY_SECRET_KEY": "topsecret"})
    assert "topsecret" not in repr(s)
    assert "topsecret" not in str(s)
