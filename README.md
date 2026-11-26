# Weekly Feedback Tool

A small self-hosted tool for weekly Start / Stop / Continue feedback for one project and one fixed team.

Members add short Start, Stop and Continue items, anonymous or named. Items stay hidden until the weekly deadline, then everyone sees them together. The product plan, including what is out of scope, is in [_docs/plan.md](_docs/plan.md). How anonymity works is explained in [_docs/anonymity.md](_docs/anonymity.md).

Stack: Python, FastAPI, Jinja templates with HTMX, and SQLite.

## How it works

- An **owner** creates the project, invites members, and sees who has not submitted.
- **Members** add short items under Start, Stop and Continue during a weekly window.
- Responses stay hidden until the deadline, then everyone sees them together.
- Contributors can choose to stay anonymous. Anonymous items never show their author.
- Members get an email reminder before the deadline if they have not submitted.

By default the window opens Monday 09:00 and closes Friday 17:00 in `WEEKLY_TIMEZONE` (default UTC).

## Requirements

- Python 3.11 or newer
- uv, see its [install instructions](https://docs.astral.sh/uv/getting-started/installation/)

On Windows, Python's `zoneinfo` relies on the `tzdata` package for time zones. It is declared in `pyproject.toml`, so `uv sync` installs it and nothing extra is needed.

## Getting started

Install the dependencies:

```
uv sync
```

Create the database (needed once before the first run, and again after an update that adds migrations; safe to repeat):

```
uv run python -m alembic upgrade head
```

Run the tests:

```
uv run pytest
```

Start the server (then open http://127.0.0.1:8000/):

```
uv run uvicorn app.main:app --reload
```

Fill the database with demo data (optional, see [Demo data](#demo-data)):

```
uv run python -m app.seed
```

Troubleshooting: if Windows blocks the `uv run pytest` or `uv run uvicorn` launchers (Windows Application Control, os error 4551), run them through Python instead: `uv run python -m pytest` and `uv run python -m uvicorn app.main:app --reload`.

The app does not create or migrate the database itself (the seed command does, see below). The database file is `weekly.db` in the current directory unless `WEEKLY_DB_PATH` says otherwise.

## First run

1. Open http://127.0.0.1:8000/setup and create the owner account. This page only works until the first user exists.
2. Sign in at `/login`, then open `/invites` and invite members by email. The invite link is shown on screen, once: copy it and send it to the member. The member opens the link and chooses a password.
3. Members add items at `/submit` and read the revealed results at `/reveal` after the deadline. The owner sees who has not submitted at `/missing`.

Key pages: `/setup`, `/login`, `/submit`, `/reveal`, and for the owner `/invites` and `/missing`.

## Configuration

Settings are read from environment variables when the app starts. An invalid value stops the app with a message naming the variable.

PowerShell:

```powershell
$env:WEEKLY_TIMEZONE = "Europe/Berlin"
```

bash:

```bash
export WEEKLY_TIMEZONE=Europe/Berlin
```

| Variable | Default | Example | What it does |
| --- | --- | --- | --- |
| `WEEKLY_DB_PATH` | `weekly.db` | `/var/lib/weekly/weekly.db` | Path of the SQLite database file. |
| `WEEKLY_SECRET_KEY` | `dev-only-insecure-secret-key` | `a-long-random-string` | Secret used to sign cookies. Must not be empty. The default is for development only. |
| `WEEKLY_TIMEZONE` | `UTC` | `Europe/Berlin` | IANA time zone in which the weekly window is defined. |
| `WEEKLY_OPEN_DAY` | `monday` | `tuesday` | Weekday on which the window opens. |
| `WEEKLY_OPEN_TIME` | `09:00` | `08:30` | Time (HH:MM, 24-hour) at which the window opens. |
| `WEEKLY_CLOSE_DAY` | `friday` | `thursday` | Weekday on which the window closes (the deadline). Must be later in the week than the open day and time. |
| `WEEKLY_CLOSE_TIME` | `17:00` | `16:00` | Time (HH:MM, 24-hour) of the deadline. |
| `WEEKLY_ENV` | `development` | `production` | `development` or `production`. Production refuses the default secret key, and the seed command refuses to run. |
| `WEEKLY_EMAIL_BACKEND` | `console` | `console` | How email is sent. Only `console` exists today. |
| `WEEKLY_REMINDER_HOURS_BEFORE` | `24` | `12` | How many hours before the deadline reminders start (positive whole number). |
| `WEEKLY_BASE_URL` | `http://127.0.0.1:8000` | `https://weekly.example.com` | Public address of the app, used for links in emails. Must start with http:// or https://. |
| `WEEKLY_REMINDER_INTERVAL_MINUTES` | `5` | `10` | How often the in-app scheduler runs the reminder job (positive whole number). |
| `WEEKLY_SCHEDULER_ENABLED` | `true` | `false` | `true` or `false`. Whether the web server runs the reminder job on a schedule. |
| `WEEKLY_COOKIE_SECURE` | `false` | `true` | `true` or `false`. Marks cookies Secure so they are only sent over https. Default is false so local http works. |

## Email and reminders

Email is printed to the console for now. Look in the terminal that runs the server: each email appears between `--- EMAIL ---` and `--- END ---`.

Members who have not submitted get one reminder per week, starting `WEEKLY_REMINDER_HOURS_BEFORE` hours before the deadline. The server runs the reminder job on a schedule (every `WEEKLY_REMINDER_INTERVAL_MINUTES` minutes) unless `WEEKLY_SCHEDULER_ENABLED=false`.

Run the reminder job once by hand:

```
uv run python -m app.reminders
```

It uses the configured email backend and prints `Reminders sent: N` as the last line on stdout. On failure it prints `Error: ...` on stderr and exits with code 1.

### Scheduler limit: use one web worker

A reminder is recorded only after the email was sent. Runs that do not overlap never send the same reminder twice, but overlapping runs can. With several web workers, or one worker plus an external cron job, runs can overlap and a member may receive duplicate reminders. Run a single web worker, or turn the in-app scheduler off (`WEEKLY_SCHEDULER_ENABLED=false`) and call `python -m app.reminders` from one external cron job.

## Demo data

```
uv run python -m app.seed
```

This creates a "Demo Project", the owner `owner@example.com` and members `member1@example.com` to `member5@example.com`, with sample items. All six use the password `demo-password`. That password is public: use the seed for local demos only.

- `--week YYYY-MM-DD` fills a specific week instead of the current one. The date must be a day on which the window opens, for example a Monday.
- It applies the database migrations itself.
- It refuses to run when `WEEKLY_ENV=production`, or when the database already holds any user other than the six seed users.
- It is safe to run again. A different `--week` adds only that week's items.
- `member5` submits nothing, so the owner's `/missing` page has someone to show.

Items are written straight into the database, so a week whose window has closed works too: sign in as any seed user and open `/reveal`. For example `uv run python -m app.seed --week 2026-11-16`.

## Deploying later

Not a full deployment guide, only what to change before real use:

- Email: only the `console` backend exists today. A real email backend would be selected through `WEEKLY_EMAIL_BACKEND`.
- Scheduler: set `WEEKLY_SCHEDULER_ENABLED=false` and call `python -m app.reminders` from cron (see the scheduler limit above).
- Secrets: set a real `WEEKLY_SECRET_KEY` and `WEEKLY_ENV=production`. Production refuses the default development key.
- https: set `WEEKLY_COOKIE_SECURE=true` when serving over https. `WEEKLY_ENV=production` does not turn on Secure cookies by itself.
- Set `WEEKLY_BASE_URL` to the public address so links in emails are correct.

## Contributing

Pick an open issue. Each one is written to be done on its own in a single session, with a goal and a short description.
