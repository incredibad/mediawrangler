"""Server-side browser sessions."""

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response

from ..config import Settings
from ..db import utcnow
from ..models import AdminUser, WebSession

IDLE_TIMEOUT = timedelta(days=7)
ABSOLUTE_TIMEOUT = timedelta(days=30)
PENDING_TOTP_TIMEOUT = timedelta(minutes=10)
TOUCH_INTERVAL = timedelta(minutes=1)


def cookie_name(settings: Settings) -> str:
    # __Host- binds the cookie to this exact origin; it requires Secure.
    return "__Host-mw_session" if settings.secure_cookies else "mw_session"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create(
    db: Session,
    user: AdminUser,
    *,
    fully_authenticated: bool,
    ip: str,
    user_agent: str | None,
) -> str:
    token = secrets.token_urlsafe(32)
    db.add(
        WebSession(
            user_id=user.id,
            token_hash=_hash(token),
            fully_authenticated=fully_authenticated,
            ip=ip,
            user_agent=(user_agent or "")[:255] or None,
        )
    )
    db.commit()
    return token


def set_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        cookie_name(settings),
        token,
        max_age=int(ABSOLUTE_TIMEOUT.total_seconds()),
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )


def clear_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(cookie_name(settings), path="/", secure=settings.secure_cookies)


def lookup(db: Session, request: Request, settings: Settings) -> WebSession | None:
    token = request.cookies.get(cookie_name(settings))
    if not token:
        return None
    ws = db.execute(select(WebSession).where(WebSession.token_hash == _hash(token))).scalar()
    if ws is None:
        return None
    now = utcnow()
    expired = (
        now - ws.last_seen_at > IDLE_TIMEOUT
        or now - ws.created_at > ABSOLUTE_TIMEOUT
        or (not ws.fully_authenticated and now - ws.created_at > PENDING_TOTP_TIMEOUT)
    )
    if expired:
        db.delete(ws)
        db.commit()
        return None
    if now - ws.last_seen_at > TOUCH_INTERVAL:
        ws.last_seen_at = now
        db.commit()
    return ws


def promote(db: Session, ws: WebSession) -> None:
    ws.fully_authenticated = True
    ws.last_seen_at = utcnow()
    db.commit()


def revoke(db: Session, ws: WebSession) -> None:
    db.delete(ws)
    db.commit()


def revoke_all(db: Session, user_id: int, except_id: int | None = None) -> None:
    q = delete(WebSession).where(WebSession.user_id == user_id)
    if except_id is not None:
        q = q.where(WebSession.id != except_id)
    db.execute(q)
    db.commit()


def purge_expired(db: Session) -> None:
    now = utcnow()
    db.execute(
        delete(WebSession).where(
            (WebSession.last_seen_at < now - IDLE_TIMEOUT)
            | (WebSession.created_at < now - ABSOLUTE_TIMEOUT)
        )
    )
    db.commit()
