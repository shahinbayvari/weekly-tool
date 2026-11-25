"""HTML error pages (403 and 404) for browsers.

A request gets the HTML page only when its Accept header prefers text/html (browsers do;
`*/*` clients such as curl, HTMX and the test client do not). Everyone else keeps the
earlier responses: plain text "Not allowed" for 403 and FastAPI's JSON for 404.
"""

from fastapi.exception_handlers import http_exception_handler as default_http_exception_handler
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from app.auth import templates

PAGES = {
    403: ("Not allowed", "You do not have access to this page."),
    404: ("Page not found", "There is nothing at this address."),
}


def _quality(params: list[str]) -> float:
    for param in params:
        name, _, value = param.strip().partition("=")
        if name.strip() == "q":
            try:
                return float(value)
            except ValueError:
                return 0.0
    return 1.0


def prefers_html(request: Request) -> bool:
    """True when text/html (or text/*) is accepted with a higher quality than */*, or equal and listed."""
    html = star = 0.0
    for part in request.headers.get("accept", "").split(","):
        media, *params = part.split(";")
        media = media.strip().lower()
        if media in ("text/html", "text/*"):
            html = max(html, _quality(params))
        elif media == "*/*":
            star = max(star, _quality(params))
    return html > 0 and html >= star


def error_response(request: Request, status_code: int) -> Response | None:
    """The HTML error page for 403/404 if the client prefers HTML, else None."""
    if status_code not in PAGES or not prefers_html(request):
        return None
    heading, message = PAGES[status_code]
    return templates.TemplateResponse(request, "error.html", {"heading": heading, "message": message}, status_code=status_code)


async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
    return error_response(request, exc.status_code) or await default_http_exception_handler(request, exc)
