"""Test doubles shared across test files."""


class FakeEmailSender:
    """Records every email instead of sending it: `sent` is a list of (to, subject, body)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject, body))
