"""CSRF protection and security headers, as two small pure-ASGI middlewares.

CSRF token scheme
    The token is `secrets.token_urlsafe(32)` (32 random bytes) stored in the signed
    Starlette session cookie under SESSION_CSRF_KEY. It is created lazily the first
    time a page that needs it is rendered, so it also exists before sign-in: the
    session cookie set when /login, /setup or /invite/<token> is first shown carries it
    (the "token tied to a cookie set when the form is shown"). Because the session
    cookie is signed with the app secret key, an attacker cannot inject a cookie
    value of their choosing (the classic weakness of unsigned double-submit cookies),
    and a token from one session never validates in another. Sign-in, setup, invite
    acceptance and logout call `session.clear()`, which drops the token, so a new one
    is issued on the next page: the token changes after sign-in and after logout.

    The session cookie is HttpOnly and SameSite=Lax (and Secure when
    WEEKLY_COOKIE_SECURE=true); see app.main.

    A state-changing request (POST, PUT, PATCH, DELETE) must carry the token in the
    `X-CSRF-Token` header (HTMX: set with hx-headers on <body>) or in a `csrf_token`
    form field (urlencoded or multipart). Anything else gets 403 and the request never
    reaches the application. Comparison uses hmac.compare_digest.

Body replay
    When the token is not in the header, the body is buffered (at most MAX_BODY_BYTES;
    larger bodies are refused with 413 before any handler runs), parsed with
    Starlette's own form parser to find the field, and then replayed to the
    downstream application through a fresh `receive`, so handlers still parse the
    form themselves.
"""

import hmac
import logging
import secrets

from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings

logger = logging.getLogger("app.security")

SESSION_CSRF_KEY = "csrf"
FORM_FIELD = "csrf_token"
HEADER_NAME = "x-csrf-token"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MAX_BODY_BYTES = 1024 * 1024

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def get_csrf_token(session: dict) -> str:
    """The session's CSRF token, created on first use."""
    token = session.get(SESSION_CSRF_KEY)
    if not isinstance(token, str) or len(token) < 43:  # token_urlsafe(32) is 43 characters
        token = secrets.token_urlsafe(32)
        session[SESSION_CSRF_KEY] = token
    return token


def csrf_context(request: Request) -> dict[str, str]:
    """Jinja2 context processor: makes `csrf_token` available in every template."""
    return {"csrf_token": get_csrf_token(request.session)}


def _valid(expected: object, given: object) -> bool:
    if not isinstance(expected, str) or not isinstance(given, str) or not given:
        return False
    return hmac.compare_digest(expected.encode(), given.encode())


class _TooLarge(Exception):
    pass


class CsrfMiddleware:
    """Rejects state-changing requests without a valid token. Must sit inside SessionMiddleware."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return

        expected = scope.get("session", {}).get(SESSION_CSRF_KEY)
        request = Request(scope)
        header = request.headers.get(HEADER_NAME)

        if header is not None:
            if _valid(expected, header):
                await self.app(scope, receive, send)
            else:
                await self._reject(scope, receive, send, 403)
            return

        try:
            declared = int(request.headers.get("content-length", "0"))
        except ValueError:
            declared = 0
        if declared > MAX_BODY_BYTES:
            await self._reject(scope, receive, send, 413)
            return

        try:
            chunks = await self._buffer(receive)
        except _TooLarge:
            await self._reject(scope, receive, send, 413)
            return

        parse_chunks = list(chunks)
        replay_chunks = list(chunks)

        async def receive_for_parse() -> Message:
            if parse_chunks:
                return parse_chunks.pop(0)
            return {"type": "http.disconnect"}

        async def replay() -> Message:
            if replay_chunks:
                return replay_chunks.pop(0)
            return await receive()  # e.g. http.disconnect

        token = None
        try:
            form = await Request(scope, receive_for_parse).form()
            values = form.getlist(FORM_FIELD)
            await form.close()
            if len(values) == 1:
                token = values[0]
        except Exception:  # malformed body: no usable token
            token = None

        if not _valid(expected, token):
            await self._reject(scope, receive, send, 403)
            return
        await self.app(scope, replay, send)

    @staticmethod
    async def _buffer(receive: Receive) -> list[Message]:
        chunks: list[Message] = []
        size = 0
        while True:
            message = await receive()
            chunks.append(message)
            if message["type"] != "http.request":
                return chunks
            size += len(message.get("body", b""))
            if size > MAX_BODY_BYTES:
                raise _TooLarge
            if not message.get("more_body", False):
                return chunks

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, status: int) -> None:
        text = "Request body too large" if status == 413 else "CSRF token missing or invalid"
        await PlainTextResponse(text, status_code=status)(scope, receive, send)


class SecurityHeadersMiddleware:
    """Adds the security headers to every response, including ones from errors.

    Must be the outermost middleware. An unhandled exception is turned into a plain
    500 here (and logged) so that even that response carries the headers.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            if started:
                raise
            logger.exception("Unhandled exception for %s %s", scope["method"], scope["path"])
            await PlainTextResponse("Internal Server Error", status_code=500)(scope, receive, send_with_headers)


def install(app, settings: Settings) -> None:
    """Add session, CSRF and security-header middleware to a Starlette/FastAPI app.

    Order matters (the last one added is outermost): CSRF needs the session, and the
    headers middleware wraps everything. The session cookie is HttpOnly (always, set
    by SessionMiddleware) and SameSite=Lax; it is Secure when WEEKLY_COOKIE_SECURE=true.
    """
    app.add_middleware(CsrfMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="weekly_session",
        same_site="lax",
        https_only=settings.cookie_secure,
    )
    app.add_middleware(SecurityHeadersMiddleware)
