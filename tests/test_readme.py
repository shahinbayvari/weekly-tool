"""The README must stay in step with the code: settings table, commands, links."""

import re
from pathlib import Path

import pytest

from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")

# attribute on Settings -> environment variable name (checked against config.py below)
FIELD_TO_VAR = {
    "db_path": "WEEKLY_DB_PATH",
    "secret_key": "WEEKLY_SECRET_KEY",
    "timezone": "WEEKLY_TIMEZONE",
    "open_day": "WEEKLY_OPEN_DAY",
    "open_time": "WEEKLY_OPEN_TIME",
    "close_day": "WEEKLY_CLOSE_DAY",
    "close_time": "WEEKLY_CLOSE_TIME",
    "env": "WEEKLY_ENV",
    "email_backend": "WEEKLY_EMAIL_BACKEND",
    "reminder_hours_before": "WEEKLY_REMINDER_HOURS_BEFORE",
    "base_url": "WEEKLY_BASE_URL",
    "reminder_interval_minutes": "WEEKLY_REMINDER_INTERVAL_MINUTES",
    "scheduler_enabled": "WEEKLY_SCHEDULER_ENABLED",
    "cookie_secure": "WEEKLY_COOKIE_SECURE",
    "setup_token": "WEEKLY_SETUP_TOKEN",
}


def config_var_names() -> set[str]:
    source = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
    return set(re.findall(r"WEEKLY_[A-Z_]+", source)) - {"WEEKLY_CLOSE_DAY/WEEKLY_CLOSE_TIME"}


def readme_table() -> dict[str, list[str]]:
    """Rows of the settings table: variable name -> [default, example, description]."""
    rows = {}
    for line in README.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) == 4 and re.fullmatch(r"`WEEKLY_[A-Z_]+`", cells[0]):
            rows[cells[0].strip("`")] = cells[1:]
    return rows


def code_blocks() -> list[str]:
    return [m.strip() for m in re.findall(r"```[a-z]*\n(.*?)```", README, flags=re.S)]


def test_table_variables_match_config_exactly():
    table = set(readme_table())
    code = config_var_names()
    assert table - code == set(), f"in README but not in app/config.py: {sorted(table - code)}"
    assert code - table == set(), f"in app/config.py but not in README: {sorted(code - table)}"


def test_field_map_covers_every_setting():
    # a new Settings field without a README row must be noticed
    assert set(FIELD_TO_VAR) == set(Settings.__dataclass_fields__)
    assert set(FIELD_TO_VAR.values()) == config_var_names()


def test_table_defaults_match_code():
    defaults = Settings()
    table = readme_table()
    for field, var in FIELD_TO_VAR.items():
        value = getattr(defaults, field)
        expected = str(value).lower() if isinstance(value, bool) else str(value)
        shown = "none" if value == "" else f"`{expected}`"  # an unset-by-default setting reads "none"
        assert table[var][0] == shown, f"{var}: README default {table[var][0]} != {shown}"


def test_table_rows_are_complete():
    for var, (default, example, what) in readme_table().items():
        assert default and example and what, f"{var} has an empty cell"


@pytest.mark.parametrize(
    "command",
    [
        "uv sync",
        "uv run pytest",
        "uv run uvicorn app.main:app --reload",
        "uv run python -m app.seed",
        "uv run python -m app.reminders",
        "uv run python -m alembic upgrade head",
    ],
)
def test_command_is_in_its_own_code_block(command):
    assert command in code_blocks()


def test_variable_setting_examples_for_both_shells():
    assert '$env:WEEKLY_TIMEZONE = "Europe/Berlin"' in code_blocks()
    assert "export WEEKLY_TIMEZONE=Europe/Berlin" in code_blocks()


def test_stale_phrases_are_gone():
    lowered = README.lower()
    for phrase in ("status:", "planning", "no application code", "issue 25", "will be finalised"):
        assert phrase not in lowered


def test_relative_links_point_to_existing_files():
    links = re.findall(r"\[[^\]]*\]\(([^)]+)\)", README)
    relative = [link for link in links if not re.match(r"[a-z]+:", link) and not link.startswith("#")]
    assert relative
    for link in relative:
        assert (ROOT / link.split("#")[0]).is_file(), f"broken link: {link}"


def test_links_to_plan_and_anonymity_docs():
    assert "](_docs/plan.md)" in README
    assert "](_docs/anonymity.md)" in README


def test_anchor_links_have_a_heading():
    headings = {re.sub(r"[^a-z0-9 -]", "", h.lower()).replace(" ", "-") for h in re.findall(r"^#+ (.+)$", README, flags=re.M)}
    for anchor in re.findall(r"\]\(#([^)]+)\)", README):
        assert anchor in headings


def test_required_sections_exist():
    headings = re.findall(r"^#+ (.+)$", README, flags=re.M)
    for required in ("Requirements", "Getting started", "First run", "Configuration", "Demo data", "Deploying later"):
        assert required in headings


def test_requirements_line_matches_pyproject():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'requires-python\s*=\s*">=(\d+\.\d+)"', pyproject).group(1)
    assert f"Python {version} or newer" in README
    assert "https://docs.astral.sh/uv/getting-started/installation/" in README


def test_key_facts_present():
    for text in (
        "--- EMAIL ---",
        "--- END ---",
        "Reminders sent: N",
        "demo-password",
        "/setup",
        "/invites",
        "WEEKLY_COOKIE_SECURE=true",
        "WEEKLY_SCHEDULER_ENABLED=false",
        "$env:",
        "tzdata",
    ):
        assert text in README
    assert "duplicate reminders" in README
    assert "overlapping runs can" in README
