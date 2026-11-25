# Weekly Feedback Tool: v1 Backlog

Context for all tasks: a Start/Stop/Continue weekly feedback tool for one project and one fixed team (see `_docs/plan.md`). Assumed stack: Python, FastAPI, Jinja templates with HTMX, SQLite. Tasks are ordered roughly by dependency, but each description states what it needs.

## 1, Set up empty project with a passing test
Goal: A runnable empty FastAPI project with one passing test.
Description: Create the project skeleton with a dependency file, an app package that serves a `/health` endpoint returning 200, and a test runner (pytest) with one test that calls it. Add a `.gitignore` and a single documented command for running the tests and one for starting the server.

## 2, Add configuration module
Goal: Load all app settings from environment variables with sensible defaults.
Description: Create a settings module covering the database path, secret key, timezone, window open day and time, and deadline day and time (defaults: Monday 09:00 opens, Friday 17:00 closes, one configured IANA timezone). Validate values at startup and fail with a clear error if any are invalid. Include tests for defaults and for invalid input.

## 3, Create database layer and schema
Goal: Define the SQLite schema and a migration workflow.
Description: Set up an ORM and migrations, and create tables for users (email, password hash, role of owner or member), project (single row, name), invites, and feedback items (author, week, category of start, stop or continue, text, anonymous flag, timestamps). Provide a helper that gives tests an isolated in-memory or temp database. Test that migrations apply cleanly to an empty database.

## 4, Implement password hashing and user model
Goal: Store and verify user passwords securely.
Description: Add functions that hash passwords with argon2 or bcrypt and verify them, plus create-user and find-user-by-email functions that normalise email case. Reject duplicate emails and passwords shorter than a minimum length. Cover everything with unit tests; no web routes are needed.

## 5, Build login and logout
Goal: Let a user sign in with email and password and sign out.
Description: Add login and logout routes with a form, cookie-based signed sessions, and a dependency that returns the current user or redirects to login. Use one generic error message for wrong email or wrong password. Assumes a user model with hashed passwords exists; tests can create users directly.

## 6, Owner setup flow
Goal: Let the first owner create the account and the project.
Description: When no users exist, show a one-time setup page where someone enters their email, password and project name, which creates the owner account and the single project. Once an owner exists, the page returns 404. The owner is also treated as a member who submits feedback.

## 7, Add role-based access helpers
Goal: Restrict routes to owners or to any signed-in member.
Description: Create reusable dependencies for require-login and require-owner that return 401/redirect or 403 as appropriate. Add a placeholder owner-only route and a member route purely to test them. Cover signed-out, member and owner cases.

## 8, Owner invites members
Goal: Let the owner invite team members by email.
Description: Add an owner-only page with a form that creates an invite (email plus a random single-use token) and shows the invite link on screen, since email is logged for now. List pending and accepted invites, and prevent duplicate invites to the same email or to existing members.

## 9, Accept invite and set password
Goal: Let an invited person create their member account.
Description: Add a page at the invite link that checks the token is valid and unused, asks for a password, creates the member user, marks the invite used, and signs them in. Show a clear error for invalid, used or unknown tokens.

## 10, Weekly cycle calculator
Goal: Provide pure functions that say which week and phase any moment falls in.
Description: Given the configured timezone, open day/time and deadline day/time, return the current week identifier, whether the window is open or closed, and the next open and close instants. Handle daylight-saving changes and moments exactly at the boundaries. Pure logic with thorough unit tests and no web or database code.

## 11, Add submission service
Goal: Create, edit and delete feedback items for the current week.
Description: Write service functions that add a short text item to Start, Stop or Continue, edit its text, delete it, and toggle its anonymous flag (default is named). Only the item's author may change it, and empty or overlong text is rejected. Accept the current time and window state as inputs so the logic can be tested without the web layer; there is no cap on items.

## 12, Enforce the submission window
Goal: Block all item changes once the window is closed or before it opens.
Description: Make the submission service refuse add, edit, delete and anonymity changes when the weekly cycle says the window is not open, returning a clear error. Test the boundary instants just before and after the deadline. There is no owner override.

## 13, Build submission page
Goal: A member can manage their feedback on one page.
Description: Add a page with three sections (Start, Stop, Continue) where the signed-in member adds bullet items, edits or deletes them, and toggles anonymity per item, using HTMX to update without a full reload. Show only that member's own items and a message with the deadline when the window is closed. Assumes the submission service exists.

## 14, Reveal query with anonymity protection
Goal: Return all items for a week only after the deadline, hiding anonymous authors.
Description: Write a query function that returns nothing while the window for that week is still open, and after it closes returns every item grouped by category with the author name shown only for non-anonymous items. The author identifier must never appear in the returned data for anonymous items, and tests must check this explicitly.

## 15, Build reveal page
Goal: Show everyone the revealed feedback after the deadline.
Description: Add a view-only page, visible to all signed-in users, that shows the latest closed week's items in Start, Stop and Continue columns, with names or "Anonymous". Before the deadline, show a "results are hidden until <deadline>" message. No reactions, history or action items.

## 16, Missing-submitters list for the owner
Goal: Show the owner who has not submitted this week.
Description: Add an owner-only page or panel listing members with zero items in the current week, available during the open window. The list uses submission presence only and must not reveal anything about who wrote anonymous items. Write a function for the list separately so the reminder task can reuse it.

## 17, Small-team anonymity safeguard
Goal: Prevent the missing list from exposing anonymous authors on very small teams.
Description: Decide and implement a rule, such as hiding the list or showing only a count when the number of members or submitters falls below a threshold, and document the decision. Add tests with teams of two and three members. Assumes a missing-submitters function exists that returns the member list.

## 18, Email sender interface with console backend
Goal: Send emails through an interface that logs to the console for now.
Description: Define a small sender interface with a console implementation that prints recipient, subject and body, selected by configuration. Make it easy to add an SMTP implementation later without touching callers. Add tests that capture the output.

## 19, Reminder job logic
Goal: Compute and send reminders to members who have not submitted.
Description: Write a function that, about 24 hours before the deadline (configurable), finds members with no submission for the current week and sends each one a reminder via the email interface. Record each reminder sent per member per week so it never sends twice, even if run repeatedly. Test with a fake clock and a fake sender.

## 20, Schedule the reminder job
Goal: Run the reminder job automatically while the app is running.
Description: Start a scheduler (for example APScheduler) with the app that calls the reminder logic every few minutes, and add a command-line entry point that runs it once for use with an external cron. Ensure the scheduler can be disabled in tests and does not start twice. Assumes the reminder function exists.

## 21, Add CSRF protection and security headers
Goal: Protect all form posts and set basic response hardening.
Description: Add CSRF tokens to every state-changing form and HTMX request, and reject requests that lack a valid token. Set secure cookie flags and standard headers such as a content security policy and frame denial. Add tests showing a post without a token is rejected.

## 22, Page layout and navigation
Goal: A consistent base template and navigation for all pages.
Description: Create a base template with a header showing the project name, the signed-in user, a logout button, and links that depend on role (Submit, Reveal, and Invites/Missing for the owner). Add minimal responsive styling. Convert existing pages to use it without changing behaviour.

## 23, Dev seed script
Goal: Fill a local database with a demo team and sample feedback.
Description: Add a command that creates a project, an owner, several members, and sample items including some anonymous ones for a given week. Make it safe to rerun and refuse to run if the configuration marks the environment as production. Document how to use it.

## 24, End-to-end smoke test
Goal: One automated test walks through the whole weekly cycle.
Description: Write a test that sets up the owner, invites and accepts a member, submits items during an open window, checks that the reveal is hidden, advances a fake clock past the deadline, and checks the reveal shows items with anonymity respected. Use a controllable clock rather than real time.

## 25, README and run instructions
Goal: Document how to install, configure, run and test the app locally.
Description: Write a README covering the stack, setup steps, environment variables, how to start the server, run tests, seed demo data, and where console emails appear. Include a short section on how a future deploy would swap in real email and use an external scheduler.
