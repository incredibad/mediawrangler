"""First-run setup, login (with optional TOTP step) and logout."""

import re
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import lockout, passwords, sessions, totp
from ..backups import BackupError
from ..config import Settings
from ..crypto import SecretBox
from ..db import utcnow
from ..models import AdminUser, RecoveryCode, WebSession
from .deps import (
    admin_exists,
    current_web_session,
    get_box,
    get_client_ip,
    get_db,
    get_settings,
    require_user,
    verify_csrf,
)
from .templating import localtime, safe_next, templates

router = APIRouter(dependencies=[Depends(verify_csrf)])

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
ADMIN_ID = 1  # a fixed primary key makes a second admin impossible, even in a race


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# --- first run -------------------------------------------------------------


@router.get("/setup")
def setup_form(request: Request, db: Session = Depends(get_db)) -> Response:
    if admin_exists(db):
        return _redirect("/login")
    return templates.TemplateResponse(
        request, "auth/setup.html", {"min_len": passwords.MIN_PASSWORD_LENGTH}
    )


@router.post("/setup")
def setup_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    ip: str = Depends(get_client_ip),
) -> Response:
    if admin_exists(db):
        return _redirect("/login")
    username = username.strip()
    error = None
    if not USERNAME_RE.match(username):
        error = "Username may use letters, digits, '.', '_' and '-' (up to 64)."
    else:
        error = passwords.password_problem(password, confirm)
    if error:
        return templates.TemplateResponse(
            request,
            "auth/setup.html",
            {"error": error, "username": username, "min_len": passwords.MIN_PASSWORD_LENGTH},
            status_code=400,
        )
    user = AdminUser(
        id=ADMIN_ID, username=username, password_hash=passwords.hash_password(password)
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _redirect("/login")
    ua = request.headers.get("user-agent")
    lockout.record(db, ip=ip, username=username, step="setup", success=True, user_agent=ua)
    token = sessions.create(db, user, fully_authenticated=True, ip=ip, user_agent=ua)
    resp = _redirect("/setup/totp")
    sessions.set_cookie(resp, token, settings)
    return resp


@router.post("/setup/restore")
def setup_restore(
    request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)
) -> Response:
    """On a fresh install, bring in a backup instead of creating an admin."""
    if admin_exists(db):
        return _redirect("/login")
    from .routes_backups import restarting_page, stage_upload

    try:
        stage_upload(request.app.state.settings, file)
    except BackupError as exc:
        return templates.TemplateResponse(
            request,
            "auth/setup.html",
            {"restore_error": str(exc), "min_len": passwords.MIN_PASSWORD_LENGTH},
            status_code=400,
        )
    return restarting_page(request)


@router.get("/setup/totp")
def setup_totp_suggestion(request: Request, user: AdminUser = Depends(require_user)) -> Response:
    if user.totp_enabled:
        return _redirect("/")
    return templates.TemplateResponse(request, "auth/setup_totp.html", {})


# --- login -----------------------------------------------------------------


def _login_page(request: Request, error: str | None = None, status: int = 200, **ctx) -> Response:
    return templates.TemplateResponse(
        request, "auth/login.html", {"error": error, **ctx}, status_code=status
    )


def _locked_message(status: lockout.LockStatus) -> str:
    return f"Too many failed attempts. Try again after {localtime(status.until, '%H:%M')}."


@router.get("/login")
def login_form(
    request: Request,
    next: str = "/",
    db: Session = Depends(get_db),
    ws: WebSession | None = Depends(current_web_session),
) -> Response:
    if not admin_exists(db):
        return _redirect("/setup")
    if ws is not None and ws.fully_authenticated:
        return _redirect(safe_next(next))
    return _login_page(request, next=safe_next(next))


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    ip: str = Depends(get_client_ip),
) -> Response:
    username = username.strip()[:64]
    next = safe_next(next)
    ua = request.headers.get("user-agent")

    status = lockout.check_lock(db, ip, username)
    if status.locked:
        lockout.record(
            db,
            ip=ip,
            username=username,
            step="password",
            success=False,
            reason=status.reason,
            user_agent=ua,
        )
        return _login_page(request, _locked_message(status), 429, username=username, next=next)

    user = db.execute(
        select(AdminUser).where(func.lower(AdminUser.username) == username.lower())
    ).scalar()
    if not passwords.verify_password(user.password_hash if user else None, password):
        lockout.record(
            db,
            ip=ip,
            username=username,
            step="password",
            success=False,
            reason="bad_password",
            user_agent=ua,
        )
        return _login_page(
            request, "Incorrect username or password.", 401, username=username, next=next
        )

    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(password)
        db.commit()

    if user.totp_enabled:
        lockout.record(
            db,
            ip=ip,
            username=username,
            step="password",
            success=True,
            reason=lockout.PENDING_SECOND_FACTOR,
            user_agent=ua,
        )
        token = sessions.create(db, user, fully_authenticated=False, ip=ip, user_agent=ua)
        resp = _redirect("/login/totp?" + urlencode({"next": next}))
    else:
        lockout.record(db, ip=ip, username=username, step="password", success=True, user_agent=ua)
        token = sessions.create(db, user, fully_authenticated=True, ip=ip, user_agent=ua)
        resp = _redirect(next)
    sessions.set_cookie(resp, token, settings)
    return resp


@router.get("/login/totp")
def login_totp_form(
    request: Request,
    next: str = "/",
    ws: WebSession | None = Depends(current_web_session),
) -> Response:
    if ws is None:
        return _redirect("/login")
    if ws.fully_authenticated:
        return _redirect(safe_next(next))
    return templates.TemplateResponse(request, "auth/login_totp.html", {"next": safe_next(next)})


@router.post("/login/totp")
def login_totp_submit(
    request: Request,
    code: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    box: SecretBox = Depends(get_box),
    ip: str = Depends(get_client_ip),
    ws: WebSession | None = Depends(current_web_session),
) -> Response:
    next = safe_next(next)
    if ws is None or ws.fully_authenticated:
        return _redirect("/login")
    user = db.get(AdminUser, ws.user_id)
    if user is None or not user.totp_enabled or not user.totp_secret_enc:
        sessions.revoke(db, ws)
        return _redirect("/login")
    ua = request.headers.get("user-agent")

    status = lockout.check_lock(db, ip, user.username)
    if status.locked:
        lockout.record(
            db,
            ip=ip,
            username=user.username,
            step="totp",
            success=False,
            reason=status.reason,
            user_agent=ua,
        )
        sessions.revoke(db, ws)
        resp = _login_page(request, _locked_message(status), 429, next=next)
        sessions.clear_cookie(resp, settings)
        return resp

    step, ok = "totp", False
    if totp.looks_like_recovery_code(code):
        step = "recovery_code"
        ok = use_recovery_code(db, user, code)
    else:
        counter = totp.verify_code(box.decrypt(user.totp_secret_enc), code, user.totp_last_counter)
        if counter is not None:
            user.totp_last_counter = counter
            db.commit()
            ok = True

    if not ok:
        lockout.record(
            db,
            ip=ip,
            username=user.username,
            step=step,
            success=False,
            reason="bad_code",
            user_agent=ua,
        )
        return templates.TemplateResponse(
            request,
            "auth/login_totp.html",
            {"error": "That code didn't work. Try again.", "next": next},
            status_code=401,
        )

    lockout.record(db, ip=ip, username=user.username, step=step, success=True, user_agent=ua)
    # Issue a fresh token rather than promoting the pending one (no fixation).
    sessions.revoke(db, ws)
    token = sessions.create(db, user, fully_authenticated=True, ip=ip, user_agent=ua)
    resp = _redirect(next)
    sessions.set_cookie(resp, token, settings)
    return resp


def use_recovery_code(db: Session, user: AdminUser, code: str) -> bool:
    candidates = db.execute(
        select(RecoveryCode).where(RecoveryCode.user_id == user.id, RecoveryCode.used_at.is_(None))
    ).scalars()
    for row in candidates:
        if totp.recovery_code_matches(code, row.code_hash):
            row.used_at = utcnow()
            db.commit()
            return True
    return False


@router.post("/logout")
def logout(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    ws: WebSession | None = Depends(current_web_session),
) -> Response:
    if ws is not None:
        sessions.revoke(db, ws)
    resp = _redirect("/login")
    sessions.clear_cookie(resp, settings)
    return resp
