"""Request dependencies: database, settings, auth and CSRF."""

import hashlib
from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import csrf, sessions
from ..config import Settings
from ..crypto import SecretBox
from ..db import utcnow
from ..models import AdminUser, ApiKey, WebSession
from ..net import client_ip


class LoginRequired(Exception):
    pass


class SetupRequired(Exception):
    pass


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[Session]:
    yield from request.app.state.db.session()


def get_box(request: Request) -> SecretBox:
    return request.app.state.box


def get_client_ip(request: Request, settings: Settings = Depends(get_settings)) -> str:
    return client_ip(request, settings)


def admin_exists(db: Session) -> bool:
    return db.execute(select(AdminUser.id).limit(1)).first() is not None


def current_web_session(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> WebSession | None:
    if not hasattr(request.state, "web_session"):
        request.state.web_session = sessions.lookup(db, request, settings)
    return request.state.web_session


def require_user(
    request: Request,
    db: Session = Depends(get_db),
    ws: WebSession | None = Depends(current_web_session),
) -> AdminUser:
    if ws is None or not ws.fully_authenticated:
        if not admin_exists(db):
            raise SetupRequired()
        raise LoginRequired()
    user = db.get(AdminUser, ws.user_id)
    if user is None:
        raise LoginRequired()
    request.state.user = user
    return user


def require_api_access(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> None:
    """MediaWrangler's own API: a `Bearer mw_...` API key, or a logged-in browser."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        key = auth[7:].strip()
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        row = db.execute(select(ApiKey).where(ApiKey.key_hash == key_hash)).scalar()
        if row is None:
            raise HTTPException(401, "invalid API key")
        row.last_used_at = utcnow()
        db.commit()
        return
    ws = sessions.lookup(db, request, settings)
    if ws is None or not ws.fully_authenticated:
        raise HTTPException(401, "authentication required")


async def verify_csrf(request: Request, settings: Settings = Depends(get_settings)) -> None:
    if request.method in csrf.SAFE_METHODS:
        return
    if not csrf.origin_allowed(request, settings):
        raise HTTPException(403, "cross-origin request refused")
    expected = getattr(request.state, "csrf_token", None)
    if not csrf.tokens_match(expected, await csrf.submitted_token(request)):
        raise HTTPException(403, "missing or invalid CSRF token; reload the page and try again")
