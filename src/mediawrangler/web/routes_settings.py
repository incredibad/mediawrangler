"""Settings: security (password, TOTP, sessions, sign-in log), connectors, API keys."""

import hashlib
import re
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import trash
from ..accounts import create_accounts, plan_accounts, set_account
from ..accounts import missing as missing_accounts
from ..auth import passwords, sessions, totp
from ..connectors import ConnectorConfig, connector_types, get_connector_class
from ..connectors.base import ConnectorError
from ..connectors.paths import PathMap
from ..crypto import SecretBox, SecretDecryptError
from ..db import utcnow
from ..groups import (
    fits_group,
    get_master,
    library_sections,
    media_server_sources,
    set_library_group,
    set_master,
)
from ..hooks.app import hook_kind
from ..importers import LANDINGS, guess_landing, landing_of
from ..ingest import recanonicalise
from ..models import (
    AdminUser,
    ApiKey,
    AppSetting,
    AppUser,
    Library,
    LibraryGroup,
    RecoveryCode,
    SigninEvent,
    Source,
    SourceAccountKey,
    SourceUser,
    UserSync,
    WebSession,
    WriteJob,
)
from ..sync import DISK_AUTOCREATE_KEY, connector_for
from ..watch import set_link, watch_counts
from ..writes import (
    ENABLED_KEY,
    PAUSED_KEY,
    PREVIEW_ROWS,
    flag,
    history_switches,
    plan_person,
    set_flag,
    set_history,
    status,
    writable,
)
from .deps import get_box, get_db, require_user, verify_csrf
from .routes_auth import use_recovery_code
from .routes_imports import import_context
from .templating import TOTP_BANNER_DISMISSED_KEY, templates

router = APIRouter(prefix="/settings", dependencies=[Depends(verify_csrf), Depends(require_user)])


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


@router.get("")
def settings_index() -> Response:
    return _redirect("/settings/connectors")


# --- security --------------------------------------------------------------


def _security_page(request: Request, db: Session, user: AdminUser, status: int = 200, **ctx):
    current = request.state.web_session
    web_sessions = (
        db.execute(
            select(WebSession)
            .where(WebSession.user_id == user.id, WebSession.fully_authenticated.is_(True))
            .order_by(WebSession.last_seen_at.desc())
        )
        .scalars()
        .all()
    )
    events = (
        db.execute(select(SigninEvent).order_by(SigninEvent.at.desc()).limit(100)).scalars().all()
    )
    codes_left = db.execute(
        select(func.count()).where(RecoveryCode.user_id == user.id, RecoveryCode.used_at.is_(None))
    ).scalar()
    return templates.TemplateResponse(
        request,
        "settings/security.html",
        {
            "web_sessions": web_sessions,
            "current_session_id": current.id,
            "events": events,
            "codes_left": codes_left,
            "min_len": passwords.MIN_PASSWORD_LENGTH,
            **ctx,
        },
        status_code=status,
    )


@router.get("/security")
def security(
    request: Request, db: Session = Depends(get_db), user: AdminUser = Depends(require_user)
):
    return _security_page(request, db, user)


@router.post("/security/password")
def change_password(
    request: Request,
    current: str = Form(...),
    new: str = Form(...),
    confirm: str = Form(...),
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_user),
):
    if not passwords.verify_password(user.password_hash, current):
        return _security_page(
            request,
            db,
            user,
            400,
            password_error="Current password is incorrect.",  # noqa: S106
        )
    problem = passwords.password_problem(new, confirm)
    if problem:
        return _security_page(request, db, user, 400, password_error=problem)
    user.password_hash = passwords.hash_password(new)
    user.password_changed_at = utcnow()
    db.commit()
    sessions.revoke_all(db, user.id, except_id=request.state.web_session.id)
    return _redirect("/settings/security?msg=password_changed")


@router.get("/security/totp")
def totp_setup(
    request: Request,
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
    user: AdminUser = Depends(require_user),
):
    if user.totp_enabled:
        return _redirect("/settings/security")
    # Keep the pending secret across reloads so an already-scanned QR stays valid.
    if user.totp_secret_enc:
        secret = box.decrypt(user.totp_secret_enc)
    else:
        secret = totp.new_secret()
        user.totp_secret_enc = box.encrypt(secret)
        db.commit()
    uri = totp.provisioning_uri(secret, user.username)
    return templates.TemplateResponse(
        request,
        "settings/totp_setup.html",
        {"qr_svg": totp.qr_svg(uri), "secret": secret},
    )


def _issue_recovery_codes(db: Session, user: AdminUser) -> list[str]:
    db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    codes = totp.new_recovery_codes()
    for code in codes:
        db.add(RecoveryCode(user_id=user.id, code_hash=totp.hash_recovery_code(code)))
    db.commit()
    return codes


@router.post("/security/totp")
def totp_enable(
    request: Request,
    code: str = Form(...),
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
    user: AdminUser = Depends(require_user),
):
    if user.totp_enabled or not user.totp_secret_enc:
        return _redirect("/settings/security")
    secret = box.decrypt(user.totp_secret_enc)
    counter = totp.verify_code(secret, code)
    if counter is None:
        uri = totp.provisioning_uri(secret, user.username)
        return templates.TemplateResponse(
            request,
            "settings/totp_setup.html",
            {
                "qr_svg": totp.qr_svg(uri),
                "secret": secret,
                "error": "That code didn't match. Check your authenticator's clock and try again.",
            },
            status_code=400,
        )
    user.totp_enabled = True
    user.totp_last_counter = counter
    db.commit()
    codes = _issue_recovery_codes(db, user)
    return templates.TemplateResponse(
        request, "settings/recovery_codes.html", {"codes": codes, "just_enabled": True}
    )


@router.post("/security/totp/disable")
def totp_disable(
    request: Request,
    password: str = Form(...),
    code: str = Form(...),
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
    user: AdminUser = Depends(require_user),
):
    if not user.totp_enabled:
        return _redirect("/settings/security")
    ok = passwords.verify_password(user.password_hash, password)
    if ok:
        if totp.looks_like_recovery_code(code):
            ok = use_recovery_code(db, user, code)
        else:
            ok = (
                totp.verify_code(box.decrypt(user.totp_secret_enc), code, user.totp_last_counter)
                is not None
            )
    if not ok:
        return _security_page(request, db, user, 400, totp_error="Password or code is incorrect.")
    user.totp_enabled = False
    user.totp_secret_enc = None
    user.totp_last_counter = None
    db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    db.commit()
    return _redirect("/settings/security?msg=totp_disabled")


@router.post("/security/totp-banner/dismiss")
def dismiss_totp_banner(
    request: Request, db: Session = Depends(get_db), user: AdminUser = Depends(require_user)
):
    row = db.get(AppSetting, TOTP_BANNER_DISMISSED_KEY)
    if row is None:
        row = AppSetting(key=TOTP_BANNER_DISMISSED_KEY, value=[])
        db.add(row)
    if user.id not in row.value:
        row.value = [*row.value, user.id]
    db.commit()
    if request.headers.get("hx-request"):
        return Response("")
    # Back to the page it was closed on; only a path on this site, never another host.
    back = urlsplit(request.headers.get("referer") or "")
    path = back.path if back.path.startswith("/") and not back.path.startswith("//") else "/"
    return _redirect(path + (f"?{back.query}" if back.query else ""))


@router.post("/security/recovery-codes")
def regenerate_recovery_codes(
    request: Request,
    password: str = Form(...),
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_user),
):
    if not user.totp_enabled:
        return _redirect("/settings/security")
    if not passwords.verify_password(user.password_hash, password):
        return _security_page(request, db, user, 400, codes_error="Password is incorrect.")
    codes = _issue_recovery_codes(db, user)
    return templates.TemplateResponse(request, "settings/recovery_codes.html", {"codes": codes})


@router.post("/security/sessions/{session_id}/revoke")
def revoke_session(
    request: Request,
    session_id: int,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_user),
):
    ws = db.get(WebSession, session_id)
    if ws is not None and ws.user_id == user.id:
        sessions.revoke(db, ws)
    if session_id == request.state.web_session.id:
        return _redirect("/login")
    return _redirect("/settings/security?msg=session_revoked")


@router.post("/security/sessions/revoke-others")
def revoke_other_sessions(
    request: Request, db: Session = Depends(get_db), user: AdminUser = Depends(require_user)
):
    sessions.revoke_all(db, user.id, except_id=request.state.web_session.id)
    return _redirect("/settings/security?msg=sessions_revoked")


# --- connectors ------------------------------------------------------------


def _config_for(source: Source, box: SecretBox) -> ConnectorConfig:
    secret = box.decrypt(source.secret_enc) if source.secret_enc else None
    return ConnectorConfig(
        base_url=source.base_url,
        secret=secret,
        path_map=PathMap.from_pairs(source.path_map or []),
        options=source.options or {},
    )


def _validate_location(url: str, location_kind: str = "url") -> str | None:
    if location_kind == "path":
        if not url.startswith("/") or "\x00" in url:
            return "Enter an absolute folder path inside the container, e.g. /media."
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return "URL must start with http:// or https:// and include a host."
    if parts.query or parts.fragment:
        return "URL must not include a query string or fragment."
    return None


@router.get("/connectors")
def connectors(request: Request, db: Session = Depends(get_db)):
    sources = db.execute(select(Source).order_by(Source.type, Source.name)).scalars().all()
    return templates.TemplateResponse(
        request,
        "settings/connectors.html",
        {
            "sources": sources,
            "types": connector_types(),
            "servers": media_server_sources(db),
            "master": get_master(db),
            **import_context(request, db),
        },
    )


@router.get("/connectors/new")
def connector_new(request: Request, type: str | None = None):
    types = connector_types()
    if type not in types:
        return templates.TemplateResponse(request, "settings/connector_pick.html", {"types": types})
    return templates.TemplateResponse(
        request,
        "settings/connector_form.html",
        {
            "cls": types[type],
            "source": None,
            "form": {
                "enabled": True,
                "schedule_mode": "manual",
                "quick_mode": "manual",
                "schedule_unit": "min",
                "quick_unit": "min",
            },
        },
    )


def schedule_form(
    schedule_mode: str = Form("manual"),
    schedule_at: str = Form(""),
    schedule_every: str = Form(""),
    schedule_unit: str = Form("min"),
    quick_mode: str = Form("manual"),
    quick_at: str = Form(""),
    quick_every: str = Form(""),
    quick_unit: str = Form("min"),
) -> dict:
    return {
        "schedule_mode": schedule_mode,
        "schedule_at": schedule_at.strip(),
        "schedule_every": schedule_every.strip(),
        "schedule_unit": schedule_unit,
        "quick_mode": quick_mode,
        "quick_at": quick_at.strip(),
        "quick_every": quick_every.strip(),
        "quick_unit": quick_unit,
    }


def schedule_values(source: Source) -> dict:
    """The schedule fields of the connector form, from what is stored."""
    out = {}
    for prefix, minutes, at in (
        ("schedule", source.schedule_minutes, source.schedule_at),
        ("quick", source.quick_minutes, source.quick_at),
    ):
        hours = bool(minutes) and minutes % 60 == 0
        out[f"{prefix}_mode"] = "at" if at else "every" if minutes else "manual"
        out[f"{prefix}_at"] = at or ""
        out[f"{prefix}_every"] = (minutes // 60 if hours else minutes) or ""
        out[f"{prefix}_unit"] = "hours" if hours else "min"
    return out


def _parse_schedule(values: dict, prefix: str, label: str):
    """(error, minutes, at) for one sync's schedule fields."""
    mode = values[f"{prefix}_mode"]
    if mode == "at":
        at = values[f"{prefix}_at"]
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
            return f"{label}: enter the time as 24-hour HH:MM, for example 04:00.", None, None
        return None, None, at
    if mode == "every":
        every, unit = values[f"{prefix}_every"], values[f"{prefix}_unit"]
        if not every.isdigit() or unit not in ("min", "hours"):
            return f"{label}: enter how often as a whole number.", None, None
        minutes = int(every) * (60 if unit == "hours" else 1)
        if not 5 <= minutes <= 10080:
            return f"{label}: use between 5 minutes and 7 days.", None, None
        return None, minutes, None
    return None, None, None


def _form_values(name, base_url, path_map, enabled, schedule: dict) -> dict:
    return {
        "name": name.strip(),
        "base_url": base_url.strip().rstrip("/"),
        "path_map": path_map,
        "enabled": enabled == "on",
        **schedule,
    }


def _validate_source_form(
    db: Session, values: dict, source_id: int | None, location_kind: str = "url"
) -> tuple[str | None, PathMap | None, tuple | None]:
    if not values["name"] or len(values["name"]) > 64:
        return "Name is required (up to 64 characters).", None, None
    clash = db.execute(select(Source.id).where(Source.name == values["name"])).scalar()
    if clash is not None and clash != source_id:
        return "Another connector already uses that name.", None, None
    url_error = _validate_location(values["base_url"], location_kind)
    if url_error:
        return url_error, None, None
    try:
        path_map = PathMap.parse(values["path_map"])
    except ValueError as exc:
        return f"Path map: {exc}", None, None
    schedules = []
    for prefix, label in (("schedule", "Full sync"), ("quick", "Watch-state sync")):
        error, minutes, at = _parse_schedule(values, prefix, label)
        if error:
            return error, None, None
        schedules.append((minutes, at))
    return None, path_map, tuple(schedules)


@router.post("/connectors/new")
def connector_create(
    request: Request,
    type: str = Form(...),
    name: str = Form(...),
    base_url: str = Form(...),
    secret: str = Form(""),
    path_map: str = Form(""),
    schedule: dict = Depends(schedule_form),
    enabled: str | None = Form(None),
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
):
    cls = connector_types().get(type)
    if cls is None:
        raise HTTPException(400, "unknown connector type")
    values = _form_values(name, base_url, path_map, enabled, schedule)
    error, pmap, minutes = _validate_source_form(db, values, None, cls.location_kind)
    if not error and cls.needs_secret and not secret.strip():
        error = "An API key is required."
    if error:
        return templates.TemplateResponse(
            request,
            "settings/connector_form.html",
            {"cls": cls, "source": None, "form": values, "error": error},
            status_code=400,
        )
    source = Source(
        type=type,
        name=values["name"],
        base_url=values["base_url"],
        secret_enc=box.encrypt(secret.strip()) if secret.strip() else None,
        path_map=pmap.to_pairs(),
        # A map typed in by hand is kept; an empty one is derived from groups.
        options={"path_map_auto": not pmap.rules},
        schedule_minutes=minutes[0][0],
        schedule_at=minutes[0][1],
        quick_minutes=minutes[1][0],
        quick_at=minutes[1][1],
        enabled=values["enabled"],
    )
    db.add(source)
    db.commit()
    return _redirect("/settings/connectors?msg=connector_saved")


def _get_source(db: Session, source_id: int) -> Source:
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(404, "connector not found")
    return source


def _webhook_context(request: Request, source: Source) -> dict | None:
    """The "receive webhooks" card of a connector, for apps that can send them."""
    kind = hook_kind(source.type)
    if kind is None:
        return None
    settings = request.app.state.settings
    host = urlsplit(settings.public_url).hostname or "localhost"
    return {
        "kind": kind,
        "listening": bool(settings.webhook_port),
        "url": f"http://{host}:{settings.webhook_port}/hooks/{kind}/{source.id}",
        "secret_set": bool(source.webhook_secret_enc),
    }


KEY_LABEL = "MediaWrangler"


def _silo_keys_context(db: Session, source: Source) -> dict | None:
    """Silo's login accounts and whether MediaWrangler can read each one's
    profiles: with the connector's own key (its own account), with a key made
    for the account, or not at all yet."""
    if source.type != "silo":
        return None
    keys = {
        k.account_id: k
        for k in db.execute(
            select(SourceAccountKey).where(SourceAccountKey.source_id == source.id)
        ).scalars()
    }
    accounts: dict[str, dict] = {}
    for su in db.execute(
        select(SourceUser)
        .where(
            SourceUser.source_id == source.id,
            SourceUser.removed_at.is_(None),
            SourceUser.account_id.is_not(None),
        )
        .order_by(SourceUser.name)
    ).scalars():
        a = accounts.setdefault(
            su.account_id, {"id": su.account_id, "profiles": [], "readable": False}
        )
        a["profiles"].append(su.name)
        a["readable"] = a["readable"] or su.watch_readable
    rows = []
    for aid, a in accounts.items():
        key = keys.get(aid)
        if key is not None:
            a["state"] = "key" if key.state == "ok" else "gone"
        else:
            a["state"] = "own" if a["readable"] else "missing"
        a["made"] = key.created_at if key else None
        rows.append(a)
    rows.sort(key=lambda r: (r["state"] != "own", r["profiles"][0].lower()))
    return {
        "accounts": rows,
        "missing": [r for r in rows if r["state"] in ("missing", "gone")],
        "held_ids": {k.key_id for k in keys.values()},
    }


def _key_plan(ctx: dict, silo_keys: list[dict], only: str | None) -> list[tuple[dict, list[dict]]]:
    """For each account that needs a key: Silo's own leftover keys with our label
    that we do not hold (deleted first, so there is one) and then the new one."""
    plan = []
    for acct in ctx["missing"]:
        if only and acct["id"] != only:
            continue
        stale = [
            k
            for k in silo_keys
            if k["user_id"] == acct["id"]
            and k["label"] == KEY_LABEL
            and k["id"] not in ctx["held_ids"]
        ]
        plan.append((acct, stale))
    return plan


def _edit_response(
    request: Request,
    source: Source,
    new_secret: str | None = None,
    db: Session | None = None,
    keys_preview: dict | None = None,
    keys_result: dict | None = None,
):
    cls = get_connector_class(source.type)
    form = {
        "name": source.name,
        "base_url": source.base_url,
        "path_map": PathMap.from_pairs(source.path_map or []).to_text(),
        **schedule_values(source),
        "enabled": source.enabled,
    }
    return templates.TemplateResponse(
        request,
        "settings/connector_form.html",
        {
            "cls": cls,
            "source": source,
            "form": form,
            "webhook": _webhook_context(request, source),
            "new_secret": new_secret,
            "silo_keys": _silo_keys_context(db, source) if db is not None else None,
            "keys_preview": keys_preview,
            "keys_result": keys_result,
            "key_label": KEY_LABEL,
        },
    )


@router.get("/connectors/{source_id}")
def connector_edit(request: Request, source_id: int, db: Session = Depends(get_db)):
    return _edit_response(request, _get_source(db, source_id), db=db)


@router.post("/connectors/{source_id}/silo-keys/preview")
def silo_keys_preview(
    request: Request,
    source_id: int,
    account_id: str = Form(""),
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
):
    """Show the calls that making the missing keys (all, or one account's) would
    send. Sends nothing; the only call it makes is a read of Silo's key list."""
    source = _get_source(db, source_id)
    if source.type != "silo":
        raise HTTPException(400, "only Silo has account keys")
    ctx = _silo_keys_context(db, source)
    errors = []
    try:
        with connector_for(source, box) as connector:
            silo_keys = connector.list_keys()
    except ConnectorError as exc:
        silo_keys, errors = [], [str(exc)]
    plan = _key_plan(ctx, silo_keys, account_id or None)
    return _edit_response(
        request,
        source,
        db=db,
        keys_preview={"plan": plan, "only": account_id},
        keys_result={"made": [], "errors": errors} if errors else None,
    )


@router.post("/connectors/{source_id}/silo-keys/create")
def silo_keys_create(
    request: Request,
    source_id: int,
    account_id: str = Form(""),
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
):
    """Make a key in Silo for every account that needs one (or just one), store
    each encrypted, then read their watch state. The one place MediaWrangler
    writes to Silo. A key Silo lost is replaced; a leftover one of ours that we
    do not hold (its secret is gone) is deleted first."""
    source = _get_source(db, source_id)
    if source.type != "silo":
        raise HTTPException(400, "only Silo has account keys")
    ctx = _silo_keys_context(db, source)
    made, errors = [], []
    try:
        with connector_for(source, box) as connector:
            names = {a["id"]: a["name"] for a in connector.accounts()}
            for acct, stale in _key_plan(ctx, connector.list_keys(), account_id or None):
                name = names.get(acct["id"]) or acct["profiles"][0]
                try:
                    for k in stale:
                        connector.delete_key(k["id"])
                    key_id, key = connector.create_account_key(acct["id"], KEY_LABEL)
                except ConnectorError as exc:
                    errors.append(f"{name}: {exc}")
                    continue
                db.execute(
                    delete(SourceAccountKey).where(
                        SourceAccountKey.source_id == source.id,
                        SourceAccountKey.account_id == acct["id"],
                    )
                )
                db.add(
                    SourceAccountKey(
                        source_id=source.id,
                        account_id=acct["id"],
                        account_name=name,
                        key_id=key_id,
                        key_enc=box.encrypt(key),
                        state="ok",
                    )
                )
                db.commit()  # keep each key the moment Silo shows it: it is never shown again
                made.append(name)
    except ConnectorError as exc:
        errors.append(str(exc))
    if made:
        request.app.state.syncs.start(source.id, "manual", "watch")
    return _edit_response(request, source, db=db, keys_result={"made": made, "errors": errors})


@router.post("/connectors/{source_id}/silo-keys/{account_id}/revoke")
def silo_key_revoke(
    request: Request,
    source_id: int,
    account_id: str,
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
):
    """Delete the account's key in Silo and forget it here."""
    source = _get_source(db, source_id)
    key = db.execute(
        select(SourceAccountKey).where(
            SourceAccountKey.source_id == source.id, SourceAccountKey.account_id == account_id
        )
    ).scalar()
    if key is None:
        raise HTTPException(404, "no key for that account")
    errors = []
    try:
        with connector_for(source, box) as connector:
            connector.delete_key(key.key_id)
    except ConnectorError as exc:
        errors.append(f"{key.account_name}: {exc}")
    else:
        db.delete(key)
        db.commit()
    return _edit_response(
        request, source, db=db, keys_result={"made": [], "errors": errors, "revoked": not errors}
    )


@router.post("/connectors/test")
def connector_test(
    request: Request,
    type: str = Form(""),
    source_id: str = Form(""),
    base_url: str = Form(""),
    secret: str = Form(""),
    path_map: str = Form(""),
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
):
    """Test the connection as the form stands (nothing is saved). A blank key
    means the stored one; when the connector exists the result is recorded."""
    source = db.get(Source, int(source_id)) if source_id.isdigit() else None
    cls = get_connector_class(source.type if source else type)
    ok, message = False, ""
    try:
        if _validate_location(base_url.strip(), cls.location_kind):
            raise ConnectorError("Check the URL.")
        key = secret or (box.decrypt(source.secret_enc) if source and source.secret_enc else None)
        config = ConnectorConfig(
            base_url=base_url.strip(),
            secret=key,
            path_map=PathMap.parse(path_map),
            options=(source.options if source else None) or {},
        )
        with cls(config) as connector:
            result = connector.test_connection()
        ok, message = result.ok, result.message
    except SecretDecryptError:
        message = "Stored key can't be decrypted; re-enter it."
    except (ConnectorError, ValueError) as exc:
        message = str(exc)
    if source is not None:
        source.last_test_at = utcnow()
        source.last_test_ok = ok
        source.last_test_message = message[:255]
        db.commit()
    return templates.TemplateResponse(
        request, "settings/_test_inline.html", {"ok": ok, "message": message}
    )


@router.post("/connectors/{source_id}/webhook-secret")
def connector_webhook_secret(
    request: Request,
    source_id: int,
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
):
    """Make a new shared secret for the app's webhooks. It is shown once."""
    source = _get_source(db, source_id)
    if hook_kind(source.type) is None:
        raise HTTPException(400, "this app doesn't send webhooks to MediaWrangler")
    secret = secrets.token_hex(24)
    source.webhook_secret_enc = box.encrypt(secret)
    db.commit()
    return _edit_response(request, source, new_secret=secret, db=db)


@router.post("/connectors/{source_id}")
def connector_update(
    request: Request,
    source_id: int,
    name: str = Form(...),
    base_url: str = Form(...),
    secret: str = Form(""),
    path_map: str = Form(""),
    schedule: dict = Depends(schedule_form),
    enabled: str | None = Form(None),
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
):
    source = _get_source(db, source_id)
    cls = get_connector_class(source.type)
    values = _form_values(name, base_url, path_map, enabled, schedule)
    error, pmap, minutes = _validate_source_form(db, values, source.id, cls.location_kind)
    if error:
        return templates.TemplateResponse(
            request,
            "settings/connector_form.html",
            {"cls": cls, "source": source, "form": values, "error": error},
            status_code=400,
        )
    source.name = values["name"]
    if source.base_url != values["base_url"]:
        source.last_test_ok = None
    source.base_url = values["base_url"]
    if secret.strip():  # blank keeps the stored key; it is never sent back to the browser
        source.secret_enc = box.encrypt(secret.strip())
        source.last_test_ok = None
    map_changed = pmap.to_pairs() != (source.path_map or [])
    if map_changed:
        source.path_map = pmap.to_pairs()
        # Edited by hand: stop deriving it. Cleared: derive it again.
        source.options = {**(source.options or {}), "path_map_auto": not pmap.rules}
    (source.schedule_minutes, source.schedule_at), (source.quick_minutes, source.quick_at) = minutes
    source.enabled = values["enabled"]
    db.commit()
    if map_changed:
        recanonicalise(db, source)
        request.app.state.syncs.rematch()
    return _redirect("/settings/connectors?msg=connector_saved")


@router.post("/connectors/{source_id}/delete")
def connector_delete(source_id: int, db: Session = Depends(get_db)):
    source = _get_source(db, source_id)
    if source.type == "disk":
        # Don't re-add the media share on the next start.
        row = db.get(AppSetting, DISK_AUTOCREATE_KEY)
        if row is None:
            db.add(AppSetting(key=DISK_AUTOCREATE_KEY, value=False))
        else:
            row.value = False
    db.delete(source)
    db.commit()
    return _redirect("/settings/connectors?msg=connector_deleted")


# --- API keys --------------------------------------------------------------


@router.get("/api-keys")
def api_keys(request: Request, db: Session = Depends(get_db)):
    keys = db.execute(select(ApiKey).order_by(ApiKey.created_at.desc())).scalars().all()
    return templates.TemplateResponse(request, "settings/api_keys.html", {"keys": keys})


@router.post("/api-keys")
def api_key_create(request: Request, name: str = Form(...), db: Session = Depends(get_db)):
    name = name.strip()[:64] or "API key"
    key = "mw_" + secrets.token_urlsafe(32)
    db.add(ApiKey(name=name, prefix=key[:10], key_hash=hashlib.sha256(key.encode()).hexdigest()))
    db.commit()
    keys = db.execute(select(ApiKey).order_by(ApiKey.created_at.desc())).scalars().all()
    return templates.TemplateResponse(
        request, "settings/api_keys.html", {"keys": keys, "new_key": key, "new_key_name": name}
    )


@router.post("/api-keys/{key_id}/revoke")
def api_key_revoke(key_id: int, db: Session = Depends(get_db)):
    row = db.get(ApiKey, key_id)
    if row is not None:
        db.delete(row)
        db.commit()
    return _redirect("/settings/api-keys?msg=api_key_revoked")


# --- master ---------------------------------------------------------------


@router.get("/general")
def general_moved():
    return _redirect("/settings/connectors")


@router.post("/connectors/master")
def master_save(request: Request, master_id: int = Form(...), db: Session = Depends(get_db)):
    if master_id not in {s.id for s in media_server_sources(db)}:
        raise HTTPException(400, "the master must be an enabled media server")
    set_master(db, master_id)
    request.app.state.syncs.rematch()
    return _redirect("/settings/connectors?msg=master_saved")


# --- library groups --------------------------------------------------------


@router.get("/groups")
def groups_page(request: Request, db: Session = Depends(get_db)):
    sections, left_out = library_sections(db)
    return templates.TemplateResponse(
        request,
        "settings/groups.html",
        {
            "sections": sections,
            "left_out": left_out,
            "master": get_master(db),
            "landings": LANDINGS,
            "guess": {s.group.id: guess_landing(s.group) for s in sections},
            "landing": {s.group.id: landing_of(s.group) for s in sections},
        },
    )


def _place(
    request: Request, db: Session, library_id: int, group_id: int | None
) -> RedirectResponse:
    lib = db.get(Library, library_id)
    group = db.get(LibraryGroup, group_id) if group_id else None
    if lib is None or (group_id and group is None):
        raise HTTPException(404, "library or group not found")
    if group is not None and not fits_group(group, lib):
        raise HTTPException(400, "a library can only join a group of the same kind")
    set_library_group(db, library_id, group_id)
    request.app.state.syncs.rematch()  # path rules follow the libraries
    return _redirect("/settings/groups?msg=groups_saved")


@router.post("/groups/library/{library_id}")
def groups_assign(
    request: Request, library_id: int, group_id: str = Form(""), db: Session = Depends(get_db)
):
    return _place(request, db, library_id, int(group_id) if group_id.isdigit() else None)


@router.post("/groups/{group_id}/add")
def groups_add_library(
    request: Request, group_id: int, library_id: int = Form(...), db: Session = Depends(get_db)
):
    return _place(request, db, library_id, group_id)


@router.post("/groups/{group_id}/move")
def groups_move(group_id: int, direction: str = Form(...), db: Session = Depends(get_db)):
    """Swap a library with its neighbour; the order sets the tabs and the overview tables."""
    groups = list(
        db.execute(select(LibraryGroup).order_by(LibraryGroup.position, LibraryGroup.id)).scalars()
    )
    at = next((i for i, g in enumerate(groups) if g.id == group_id), None)
    if at is None:
        raise HTTPException(404, "group not found")
    to = at - 1 if direction == "up" else at + 1
    if 0 <= to < len(groups):
        groups[at], groups[to] = groups[to], groups[at]
    for i, g in enumerate(groups):
        g.position = i
    db.commit()
    return _redirect(f"/settings/groups?msg=groups_moved#group-{group_id}")


@router.post("/groups/{group_id}")
def groups_update(
    group_id: int,
    name: str = Form(...),
    presence_enabled: str | None = Form(None),
    import_landing: str = Form(""),
    db: Session = Depends(get_db),
):
    group = db.get(LibraryGroup, group_id)
    if group is None:
        raise HTTPException(404, "group not found")
    name = name.strip()[:64]
    clash = db.execute(select(LibraryGroup.id).where(LibraryGroup.name == name)).scalar()
    if not name or (clash is not None and clash != group_id):
        raise HTTPException(400, "group names must be unique and not empty")
    group.name = name
    group.presence_enabled = presence_enabled == "on"
    if import_landing and import_landing not in LANDINGS:
        raise HTTPException(400, "unknown import setting")
    group.import_landing = import_landing or None
    db.commit()
    return _redirect("/settings/groups?msg=groups_saved")


# --- users ------------------------------------------------------------------


@router.get("/users")
def users_page(request: Request, db: Session = Depends(get_db)):
    people = db.execute(select(AppUser).order_by(func.lower(AppUser.name))).scalars().all()
    source_users = db.execute(
        select(SourceUser, Source)
        .join(Source, Source.id == SourceUser.source_id)
        .where(SourceUser.removed_at.is_(None))
        .order_by(Source.id, func.lower(SourceUser.name))
    ).all()
    by_person: dict[int | None, list] = {}
    for su, src in source_users:
        by_person.setdefault(su.app_user_id, []).append((su, src))
    return templates.TemplateResponse(
        request,
        "settings/users.html",
        {
            "people": people,
            "by_person": by_person,
            "counts": watch_counts(db),
            "servers": [s for s in media_server_sources(db)],
            **_accounts_context(db),
        },
    )


def _accounts_context(db: Session) -> dict:
    switched = {
        (u.app_user_id, u.source_id)
        for u in db.execute(select(UserSync).where(UserSync.sync_account.is_(True))).scalars()
    }
    gaps = [
        {"person": p, "source": src, "on": (p.id, src.id) in switched}
        for p, src, _origin in missing_accounts(db)
    ]
    return {"gaps": gaps, "planned": plan_accounts(db), "results": None}


def _accounts_partial(request: Request, db: Session, results=None):
    ctx = _accounts_context(db)
    ctx["results"] = results
    return templates.TemplateResponse(request, "settings/_accounts.html", ctx)


@router.post("/users/account/{person_id}/{source_id}")
def users_account_switch(
    request: Request,
    person_id: int,
    source_id: int,
    on: str = Form(""),
    db: Session = Depends(get_db),
):
    person = db.get(AppUser, person_id)
    if person is None or db.get(Source, source_id) is None:
        raise HTTPException(404, "not found")
    if (person_id, source_id) not in {(p.id, s.id) for p, s, _o in missing_accounts(db)}:
        raise HTTPException(404, "that account is not missing")
    set_account(db, person, source_id, bool(on))
    db.commit()
    return _accounts_partial(request, db)


@router.post("/users/accounts/create")
async def users_accounts_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    passwords = {}
    for key, value in form.items():
        if key.startswith("pw_") and isinstance(value, str):
            _, pid, sid = key.split("_")
            passwords[(int(pid), int(sid))] = value
    results = create_accounts(db, request.app.state.box, passwords)
    if any(r.ok for r in results):
        request.app.state.syncs.bump()
    return _accounts_partial(request, db, results)


@router.post("/users/link/{source_user_id}")
def users_link(source_user_id: int, app_user_id: str = Form(""), db: Session = Depends(get_db)):
    target = int(app_user_id) if app_user_id.isdigit() else None
    if target is not None and db.get(AppUser, target) is None:
        raise HTTPException(404, "person not found")
    if db.get(SourceUser, source_user_id) is None:
        raise HTTPException(404, "user not found")
    set_link(db, source_user_id, target)
    return _redirect("/settings/users?msg=users_saved")


# --- Writes ----------------------------------------------------------------


def _writes_context(db: Session) -> dict:
    people = []
    servers: dict[int, str] = {}
    for p in db.execute(select(AppUser).order_by(AppUser.name)).scalars():
        switches = history_switches(db, p)
        apps = []
        for _su, src in writable(db, p):
            servers[src.id] = src.name
            apps.append({"id": src.id, "name": src.name, "on": src.id in switches})
        people.append({"p": p, "apps": apps})
    able = [a for r in people for a in r["apps"]]
    return {
        "enabled": flag(db, ENABLED_KEY),
        "paused": flag(db, PAUSED_KEY),
        "status": status(db),
        "people": people,
        "servers": sorted(servers.items(), key=lambda kv: kv[0]),
        "all_on": bool(able) and all(a["on"] for a in able),
        "files_on": trash.enabled(db),
        "readonly": trash.unwritable(db),
        "share_writable": not trash.unwritable(db),
    }


def _writes_partial(request: Request, db: Session, kick: bool = True):
    if kick:
        request.app.state.syncs.writes.kick()
    if not request.headers.get("hx-request"):
        return _redirect("/settings/writes")
    return templates.TemplateResponse(
        request, "settings/_writes_controls.html", _writes_context(db)
    )


@router.get("/writes")
def writes_page(
    request: Request,
    preview: int | None = None,
    all: int = 0,
    db: Session = Depends(get_db),
):
    preview_ctx = None
    if preview is not None:
        person = db.get(AppUser, preview)
        if person is not None:
            planned = plan_person(db, person)
            counts: dict[tuple[str, str], int] = {}
            for item in planned:
                counts[(item.source_name, item.op)] = counts.get((item.source_name, item.op), 0) + 1
            grouped: dict[tuple, dict] = {}
            for item in planned:  # one row per record, listing every app it is written to
                row = grouped.setdefault((item.key, item.op), {"first": item, "apps": []})
                row["apps"].append(item.source_name)
            records = [{"item": g["first"], "apps": g["apps"]} for g in grouped.values()]
            queued = db.execute(
                select(func.count()).where(
                    WriteJob.app_user_id == person.id, WriteJob.status == "pending"
                )
            ).scalar_one()
            preview_ctx = {
                "person": person,
                "queued": queued,
                "counts": sorted(counts.items()),
                "total": len(planned),
                "records": len(records),
                "rows": records if all else records[:PREVIEW_ROWS],
                "all": bool(all),
                "person_id": person.id,
            }
    return templates.TemplateResponse(
        request,
        "settings/writes.html",
        {
            **_writes_context(db),
            "preview": preview_ctx,
            "preview_rows": PREVIEW_ROWS,
        },
    )


@router.get("/writes/status")
def writes_status(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "settings/_writes_live.html",
        {
            "status": status(db),
            "enabled": flag(db, ENABLED_KEY),
            "paused": flag(db, PAUSED_KEY),
            "oob": True,
        },
    )


@router.post("/writes/switch/{name}")
def writes_switch(request: Request, name: str, on: str = Form(""), db: Session = Depends(get_db)):
    if name not in ("enabled", "paused", "files"):
        raise HTTPException(400, "unknown switch")
    key = {"enabled": ENABLED_KEY, "paused": PAUSED_KEY, "files": trash.FILES_KEY}[name]
    set_flag(db, key, bool(on))
    return _writes_partial(request, db)


@router.post("/writes/all")
def writes_all(request: Request, on: str = Form(""), db: Session = Depends(get_db)):
    for person in db.execute(select(AppUser)).scalars():
        for _su, src in writable(db, person):
            set_history(db, person, src.id, bool(on))
    db.commit()
    return _writes_partial(request, db)


@router.post("/writes/user/{person_id}/app/{source_id}")
def writes_user_app(
    request: Request,
    person_id: int,
    source_id: int,
    on: str = Form(""),
    db: Session = Depends(get_db),
):
    person = db.get(AppUser, person_id)
    if person is None:
        raise HTTPException(404, "no such user")
    if source_id not in {src.id for _su, src in writable(db, person)}:
        raise HTTPException(404, "that app cannot be written to for this user")
    set_history(db, person, source_id, bool(on))
    db.commit()
    return _writes_partial(request, db)


@router.post("/writes/user/{person_id}/filter")
def writes_user_filter(
    request: Request,
    person_id: int,
    title_filter: str = Form(""),
    db: Session = Depends(get_db),
):
    person = db.get(AppUser, person_id)
    if person is None:
        raise HTTPException(404, "no such user")
    lines = [line.strip() for line in title_filter.splitlines() if line.strip()]
    person.writes_filter = "\n".join(lines)[:4000] or None
    db.commit()
    request.app.state.syncs.writes.kick()
    return Response(status_code=204)
