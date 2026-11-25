# Weekly Feedback Tool

A small self-hosted tool for weekly Start / Stop / Continue feedback for one project and one fixed team.

> **Status:** planning. No application code exists yet. The work is tracked as [GitHub issues](https://github.com/shahinbayvari/weekly-tool/issues), starting with issue 1.

## How it works

- An **owner** creates the project, invites members, and sees who hasn't submitted.
- **Members** add short bullet items under Start, Stop and Continue during a weekly window.
- Responses stay hidden until the deadline, then everyone sees them together.
- Contributors can choose to stay anonymous. Anonymous items never show their author.
- Members get an automatic email reminder before the deadline if they haven't submitted.

The full v1 scope, including what is out of scope, is in [_docs/plan.md](_docs/plan.md).

## Planned stack

Python, FastAPI, Jinja templates with HTMX, and SQLite. Email is logged to the console until the app is deployed. This is an assumption and may change.

## Getting started

Install, configuration, run and test instructions will be added once the project skeleton exists (issue 1) and finalised in the README issue (issue 25).

## Contributing

Pick an open issue. Each one is written to be done on its own in a single session, with a goal and a short description.
