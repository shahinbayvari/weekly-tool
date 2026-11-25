"""Runs the reminder job (app.reminders.send_reminders) on a timer inside the web app.

- `start_scheduler(settings)` / `stop_scheduler()` are called from the FastAPI
  lifespan in app.main (never at import time).
- One scheduler per process: a module-level guard makes a second start reuse the
  running instance (reference counted, so it stops when the last user stops it).
- Single flight: APScheduler runs the job with max_instances=1 and coalesce=True,
  and `run_reminder_job` also holds a lock, so overlapping runs are skipped, never
  run in parallel (overlapping runs could double-send, see app.reminders).
- A failing run is logged (exception type only: no addresses, no bodies) and the
  schedule continues.
- Several web worker processes each run their own scheduler; the lock and the guard
  are per process only.
"""

import logging
import threading
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import Engine

from app.config import Settings, load_settings
from app.email import EmailSender, get_email_sender
from app.reminders import send_reminders

log = logging.getLogger(__name__)

FIRST_RUN_DELAY_SECONDS = 5
JOB_ID = "send_reminders"

_job_lock = threading.Lock()  # held while a reminder run is in progress
_guard = threading.Lock()  # protects _scheduler and _users
_scheduler: BackgroundScheduler | None = None
_users = 0  # how many start_scheduler calls are still active


def run_reminder_job(
    *,
    settings: Settings | None = None,
    sender: EmailSender | None = None,
    now: datetime | None = None,
    engine: Engine | None = None,
) -> int | None:
    """One scheduled run. Returns the number sent, or None if skipped or failed. Never raises."""
    if not _job_lock.acquire(blocking=False):
        log.warning("reminder run skipped: the previous run is still in progress")
        return None
    try:
        settings = settings or load_settings()
        sender = sender or get_email_sender(settings)
        return send_reminders(now or datetime.now(UTC), sender, settings, engine=engine)
    except Exception as exc:
        log.error("reminder run failed: %s", type(exc).__name__)
        return None
    finally:
        _job_lock.release()


def get_scheduler() -> BackgroundScheduler | None:
    """The running scheduler, or None."""
    return _scheduler


def start_scheduler(settings: Settings) -> bool:
    """Start the scheduler if enabled and none runs yet. True if the caller must call stop_scheduler()."""
    global _scheduler, _users
    if not settings.scheduler_enabled:
        return False
    with _guard:
        if _scheduler is None:
            scheduler = BackgroundScheduler(timezone=UTC)
            scheduler.add_job(
                run_reminder_job,
                "interval",
                minutes=settings.reminder_interval_minutes,
                next_run_time=datetime.now(UTC) + timedelta(seconds=FIRST_RUN_DELAY_SECONDS),
                id=JOB_ID,
                max_instances=1,
                coalesce=True,
            )
            scheduler.start()
            _scheduler = scheduler
        _users += 1
    return True


def stop_scheduler() -> None:
    """Release one start; the last release shuts the scheduler down without waiting for a running job."""
    global _scheduler, _users
    with _guard:
        if _users > 0:
            _users -= 1
        if _users == 0 and _scheduler is not None:
            scheduler, _scheduler = _scheduler, None
            scheduler.shutdown(wait=False)
