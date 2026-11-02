"""CSRF protection: double-submit cookie plus an Origin check.

Every unsafe request must carry the token from the CSRF cookie, either as the
`csrf_token` form field or the `X-CSRF-Token` header (HTMX sends the header).
Requests whose Origin (or Referer) is not our public origin are rejected
outright. API requests authenticated by API key are exempt because they carry
no ambient browser credentials.
"""

import hmac
import secrets
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import Response

from ..config import Settings

FORM_FIELD = "csrf_token"
HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def cookie_name(settings: Settings) -> str:
    return "__Host-mw_csrf" if settings.secure_cookies else "mw_csrf"


def get_or_create_token(request: Request, settings: Settings) -> tuple[str, bool]:
    token = request.cookies.get(cookie_name(settings))
    if token and len(token) >= 32:
        return token, False
    return secrets.token_urlsafe(32), True


def set_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        cookie_name(settings),
        token,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )


def origin_allowed(request: Request, settings: Settings) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        referer = request.headers.get("referer")
        if not referer:
            # Non-browser clients send neither; the token check still applies.
            return True
        parts = urlsplit(referer)
        origin = f"{parts.scheme}://{parts.netloc}"
    allowed = {settings.public_origin}
    # Reaching the app directly (LAN IP or localhost) is legitimate too.
    host = request.headers.get("host")
    if host:
        allowed.add(f"{request.url.scheme}://{host}")
    return origin in allowed


async def submitted_token(request: Request) -> str | None:
    header = request.headers.get(HEADER)
    if header:
        return header
    ctype = request.headers.get("content-type", "")
    if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        form = await request.form()
        value = form.get(FORM_FIELD)
        return value if isinstance(value, str) else None
    return None


def tokens_match(expected: str | None, submitted: str | None) -> bool:
    return bool(expected and submitted and hmac.compare_digest(expected, submitted))
