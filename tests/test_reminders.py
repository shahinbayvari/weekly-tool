"""send_reminders at a fixed clock (Europe/Amsterdam, Mon 09:00 - Fri 17:00) with a fake sender."""

import logging
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.items import add_item, delete_item
from app.reminders import send_reminders
from app.users import create_user
from tests.fakes import FakeEmailSender

AMS = ZoneInfo("Europe/Amsterdam")
SETTINGS = Settings(
    timezone="Europe/Amsterdam", open_day="monday", open_time="09:00", close_day="friday", close_time="17:00"
)
PW = "correct horse"
WEEK = "2026-11-23"
DEADLINE = datetime(2026, 11, 27, 17, 0, tzinfo=AMS)
LEAD = DEADLINE - timedelta(hours=24)  # Thu 17:00
DEADLINE_TEXT = "Friday 2026-11-27 17:00 CET (Europe/Amsterdam)"
ALL = ["ann@example.com", "bob@example.com", "owner@example.com"]


@pytest.fixture
def users(db_engine):
    return {
        "owner": create_user("owner@example.com", PW, "owner", engine=db_engine),
        "bob": create_user("bob@example.com", PW, "member", engine=db_engine),
        "ann": create_user("ann@example.com", PW, "member", engine=db_engine),
    }


@pytest.fixture
def sender():
    return FakeEmailSender()


def run(engine, sender, now, settings=SETTINGS):
    return send_reminders(now, sender, settings, engine=engine)


def recipients(sender):
    return [to for to, _, _ in sender.sent]


def submit(engine, user, now):
    return add_item(user.id, now, "start", "text", False, engine=engine, settings=SETTINGS)


def recorded(engine, user_id=None):
    with engine.connect() as conn:
        if user_id is None:
            return conn.execute(text("SELECT count(*) FROM reminders_sent")).scalar_one()
        return conn.execute(
            text("SELECT count(*) FROM reminders_sent WHERE user_id = :u"), {"u": user_id}
        ).scalar_one()


# --- who and when ------------------------------------------------------------


def test_sends_one_email_to_each_missing_user_including_owner(db_engine, users, sender):
    assert run(db_engine, sender, LEAD) == 3
    assert sorted(recipients(sender)) == ALL
    assert recorded(db_engine) == 3


def test_each_email_goes_to_one_person_only(db_engine, users, sender):
    run(db_engine, sender, LEAD)
    assert all("," not in to and ";" not in to and " " not in to for to in recipients(sender))


def test_nothing_one_microsecond_before_lead(db_engine, users, sender):
    assert run(db_engine, sender, LEAD - timedelta(microseconds=1)) == 0
    assert sender.sent == [] and recorded(db_engine) == 0


def test_sends_exactly_at_lead(db_engine, users, sender):
    assert run(db_engine, sender, LEAD) == 3


def test_nothing_at_deadline_or_after(db_engine, users, sender):
    assert run(db_engine, sender, DEADLINE) == 0
    assert run(db_engine, sender, DEADLINE + timedelta(hours=1)) == 0
    assert sender.sent == []


def test_last_microsecond_before_deadline_sends(db_engine, users, sender):
    assert run(db_engine, sender, DEADLINE - timedelta(microseconds=1)) == 3


def test_nothing_when_window_closed(db_engine, users, sender):
    assert run(db_engine, sender, datetime(2026, 11, 29, 12, 0, tzinfo=AMS)) == 0  # weekend
    assert run(db_engine, sender, datetime(2026, 11, 23, 8, 59, tzinfo=AMS)) == 0  # before the window opens
    assert sender.sent == []


def test_long_lead_starts_when_window_opens(db_engine, users, sender):
    long_lead = replace(SETTINGS, reminder_hours_before=24 * 30)
    opens = datetime(2026, 11, 23, 9, 0, tzinfo=AMS)
    assert run(db_engine, sender, opens - timedelta(microseconds=1), long_lead) == 0
    assert run(db_engine, sender, opens, long_lead) == 3


def test_late_first_run_still_sends_to_everyone_missing(db_engine, users, sender):
    submit(db_engine, users["bob"], datetime(2026, 11, 24, 10, 0, tzinfo=AMS))
    assert run(db_engine, sender, DEADLINE - timedelta(hours=2)) == 2
    assert sorted(recipients(sender)) == ["ann@example.com", "owner@example.com"]


def test_aware_now_in_other_timezone_is_same_instant(db_engine, users, sender):
    assert run(db_engine, sender, LEAD.astimezone(ZoneInfo("UTC"))) == 3


# --- never twice -------------------------------------------------------------


def test_two_runs_send_once(db_engine, users, sender):
    assert run(db_engine, sender, LEAD) == 3
    assert run(db_engine, sender, LEAD + timedelta(minutes=5)) == 0
    assert len(sender.sent) == 3


def test_many_runs_send_each_member_exactly_once(db_engine, users, sender):
    total = 0
    now = LEAD
    while now < DEADLINE:
        total += run(db_engine, sender, now)
        now += timedelta(minutes=30)
    assert total == 3
    assert sorted(recipients(sender)) == ALL


def test_submit_before_reminder_time_gets_none(db_engine, users, sender):
    submit(db_engine, users["ann"], datetime(2026, 11, 24, 10, 0, tzinfo=AMS))
    run(db_engine, sender, LEAD)
    assert "ann@example.com" not in recipients(sender)
    assert recorded(db_engine) == 2


def test_submit_between_runs_gets_none(db_engine, users, sender):
    run(db_engine, sender, LEAD - timedelta(hours=1))  # too early: nothing
    submit(db_engine, users["ann"], LEAD - timedelta(minutes=1))
    run(db_engine, sender, LEAD)
    assert "ann@example.com" not in recipients(sender)


def test_submit_after_reminder_still_one_only(db_engine, users, sender):
    run(db_engine, sender, LEAD)
    submit(db_engine, users["ann"], LEAD + timedelta(hours=1))
    run(db_engine, sender, LEAD + timedelta(hours=2))
    assert recipients(sender).count("ann@example.com") == 1


def test_add_then_delete_then_missing_gets_one(db_engine, users, sender):
    item = submit(db_engine, users["ann"], datetime(2026, 11, 24, 10, 0, tzinfo=AMS))
    delete_item(item.id, users["ann"].id, datetime(2026, 11, 24, 11, 0, tzinfo=AMS), engine=db_engine, settings=SETTINGS)
    run(db_engine, sender, LEAD)
    run(db_engine, sender, LEAD + timedelta(hours=1))
    assert recipients(sender).count("ann@example.com") == 1


def test_user_invited_after_reminder_time_gets_one_on_next_run(db_engine, users, sender):
    assert run(db_engine, sender, LEAD) == 3
    create_user("late@example.com", PW, "member", engine=db_engine)
    assert run(db_engine, sender, LEAD + timedelta(hours=1)) == 1
    assert recipients(sender)[-1] == "late@example.com"
    assert run(db_engine, sender, LEAD + timedelta(hours=2)) == 0


def test_user_added_after_deadline_gets_nothing(db_engine, users, sender):
    run(db_engine, sender, LEAD)
    create_user("late@example.com", PW, "member", engine=db_engine)
    assert run(db_engine, sender, DEADLINE) == 0
    assert "late@example.com" not in recipients(sender)


def test_next_week_sends_again(db_engine, users, sender):
    run(db_engine, sender, LEAD)
    assert run(db_engine, sender, LEAD + timedelta(days=7)) == 3
    assert len(sender.sent) == 6


# --- failures ----------------------------------------------------------------


class FlakySender(FakeEmailSender):
    def __init__(self, failing):
        super().__init__()
        self.failing = set(failing)

    def send(self, to, subject, body):
        if to in self.failing:
            raise RuntimeError(f"smtp down for {to} {body}")
        super().send(to, subject, body)


def test_failure_for_one_member_does_not_block_others_and_is_retried_alone(db_engine, users, caplog):
    sender = FlakySender({"bob@example.com"})
    with caplog.at_level(logging.WARNING, logger="app.reminders"):
        assert run(db_engine, sender, LEAD) == 2
    assert sorted(recipients(sender)) == ["ann@example.com", "owner@example.com"]
    assert recorded(db_engine) == 2
    assert recorded(db_engine, users["bob"].id) == 0
    # the warning is logged, names no address and carries no body
    assert caplog.records
    assert all("@" not in r.getMessage() and "/submit" not in r.getMessage() for r in caplog.records)

    sender.failing.clear()
    assert run(db_engine, sender, LEAD + timedelta(minutes=1)) == 1
    assert recipients(sender)[-1] == "bob@example.com"
    assert run(db_engine, sender, LEAD + timedelta(minutes=2)) == 0
    assert recorded(db_engine) == 3


def test_all_failing_records_nothing_and_does_not_raise(db_engine, users):
    sender = FlakySender(ALL)
    assert run(db_engine, sender, LEAD) == 0
    assert recorded(db_engine) == 0


def test_concurrent_duplicate_record_is_handled(db_engine, users):
    """Another run records bob while we are sending to him: no crash, not counted."""

    class RacingSender(FakeEmailSender):
        def send(self, to, subject, body):
            super().send(to, subject, body)
            if to == "bob@example.com":
                with db_engine.begin() as conn:
                    conn.execute(
                        text("INSERT INTO reminders_sent (user_id, week) VALUES (:u, :w)"),
                        {"u": users["bob"].id, "w": WEEK},
                    )

    sender = RacingSender()
    assert run(db_engine, sender, LEAD) == 2
    assert recorded(db_engine) == 3
    assert run(db_engine, sender, LEAD + timedelta(minutes=1)) == 0


def test_unique_and_foreign_key_constraints(db_engine, users):
    insert = text("INSERT INTO reminders_sent (user_id, week) VALUES (:u, :w)")
    with db_engine.begin() as conn:
        conn.execute(insert, {"u": users["bob"].id, "w": WEEK})
    with pytest.raises(IntegrityError, match="UNIQUE"):
        with db_engine.begin() as conn:
            conn.execute(insert, {"u": users["bob"].id, "w": WEEK})
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        with db_engine.begin() as conn:
            conn.execute(insert, {"u": 999, "w": WEEK})


# --- content -----------------------------------------------------------------


def test_subject_and_body(db_engine, users, sender):
    run(db_engine, sender, LEAD)
    for _, subject, body in sender.sent:
        assert subject == f"Reminder: submit your feedback by {DEADLINE_TEXT}"
        assert DEADLINE_TEXT in body
        assert "http://127.0.0.1:8000/submit" in body


def test_link_uses_configured_base_url(db_engine, users, sender):
    run(db_engine, sender, LEAD, replace(SETTINGS, base_url="https://w.example.com"))
    assert all("https://w.example.com/submit" in body and "com//" not in body for _, _, body in sender.sent)


def test_message_does_not_mention_others_or_anonymity(db_engine, users, sender):
    run(db_engine, sender, LEAD)
    for to, subject, body in sender.sent:
        content = (subject + body).lower()
        for other in ALL:
            if other != to:
                assert other not in content
        for word in ("anonym", "everyone", "others", "colleague", "teammate", "already submitted", "have submitted"):
            assert word not in content


def test_naive_now_rejected(db_engine, users, sender):
    with pytest.raises(ValueError):
        run(db_engine, sender, datetime(2026, 11, 26, 17, 0))
    assert sender.sent == []
