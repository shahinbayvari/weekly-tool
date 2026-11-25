# Weekly Feedback Tool: v1 Plan

## Purpose
Weekly Start/Stop/Continue (S/S/C) feedback for one project and one fixed team.

## Users and roles
- **Owner:** creates the project, invites members, and sees who hasn't submitted.
- **Member:** submits weekly feedback and views the reveal.
- Login is email and password.

## Weekly cycle
- Fixed schedule: the window opens on a set day (e.g. Monday) and closes at a set deadline (e.g. Friday 5pm).
- Responses are hidden until the deadline, then revealed to everyone together.
- No owner override (no extending, closing early, or skipping a week).

## Submission
- Multiple short bullet items under each of Start, Stop and Continue, with no cap.
- Names show by default; a toggle lets a contributor stay anonymous.
- Members can edit, delete, or toggle anonymity until the window closes.

## Reveal
- View-only after the deadline. No reactions, history or action items.
- Anonymous items never show their author.

## Reminders
- Automatic emails (e.g. 24h before the deadline) to members who haven't submitted.
- The owner sees a list of who is missing. Anonymous authorship stays hidden.

## Tech and delivery
- Self-hosted Python (Flask/FastAPI) or Node, with SQLite.
- Build and run locally first. Email is logged to the console until deploy.

## Out of scope for v1
Multiple projects or workspaces, reactions, action items, history or archive, Slack/Teams, SSO or magic links, admin or viewer roles, owner override of the cycle.

## Open details to decide while building
- Exact open and close days and times, and timezone.
- Whether the owner is also a member who submits (assumed yes).
- Whether the missing-submitters list could expose anonymous authors on very small teams.
