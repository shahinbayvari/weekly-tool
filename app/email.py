"""Email sending behind one small interface.

Callers use `get_email_sender(settings)` and the `EmailSender` interface only;
they never import a concrete class. To add a backend: write a class with a
`send(to, subject, body)` method and add it to `BACKENDS`. `app.config`
derives its accepted `WEEKLY_EMAIL_BACKEND` values from `BACKENDS`, so that
is the single place to change.

This module must not import `app.config` at runtime (config imports it).
"""

import re
import sys
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.config import Settings

# Every character that splits a line (str.splitlines) plus the other ones
# that mail headers must never contain.
_LINE_BREAK_RE = re.compile("[\n\r\x0b\x0c\x1c\x1d\x1e\x85  ]")


class EmailSender(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


def validate_message(to: str, subject: str) -> None:
    """Raise ValueError for a header-unsafe or unusable recipient/subject.

    An empty or whitespace-only subject is allowed (the spec does not
    forbid it); the body is never validated.
    """
    if not to or not to.strip():
        raise ValueError("recipient must not be empty")
    if "@" not in to:
        raise ValueError("recipient must contain '@'")
    if _LINE_BREAK_RE.search(to):
        raise ValueError("recipient must not contain a line break")
    if _LINE_BREAK_RE.search(subject):
        raise ValueError("subject must not contain a line break")


class ConsoleEmailSender:
    """Prints each email to standard output."""

    def send(self, to: str, subject: str, body: str) -> None:
        validate_message(to, subject)
        # One write of the whole block, to the stdout current at call time.
        # If stdout cannot encode the text this raises UnicodeEncodeError
        # before anything is written (nothing is dropped or replaced silently).
        sys.stdout.write(f"--- EMAIL ---\nTo: {to}\nSubject: {subject}\n{body}\n--- END ---\n")


BACKENDS: dict[str, type[EmailSender]] = {"console": ConsoleEmailSender}


def get_email_sender(settings: "Settings") -> EmailSender:
    return BACKENDS[settings.email_backend]()
