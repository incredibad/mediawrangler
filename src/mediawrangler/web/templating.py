"""Jinja2 templates and shared context."""

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from .. import __version__
from ..connectors import connector_types
from ..groups import get_master
from ..models import AppSetting
from ..scheduler import type_can_quick

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

# Fixed messages for ?msg= after a redirect, so no user text is ever reflected.
FLASH_MESSAGES = {
    "password_changed": ("ok", "Password changed. Other sessions were signed out."),
    "totp_disabled": ("warn", "Two-factor authentication is off."),
    "sessions_revoked": ("ok", "Other sessions were signed out."),
    "session_revoked": ("ok", "Session signed out."),
    "connector_saved": ("ok", "Connector saved."),
    "connector_deleted": ("ok", "Connector deleted."),
    "api_key_revoked": ("ok", "API key revoked."),
    "master_saved": ("ok", "Master changed. Matching was re-run against it."),
    "groups_saved": ("ok", "Libraries saved. They apply to the comparisons now."),
    "groups_moved": ("ok", "Library order saved. Tabs and the overview follow it."),
    "users_saved": ("ok", "User link saved."),
    "backup_made": ("ok", "Backup made."),
    "backup_deleted": ("ok", "Backup deleted."),
    "settings_imported": ("ok", "Settings imported. Run a sync so libraries and accounts fill in."),
}


def _master_id(request: Request, user) -> int | None:
    """The master media server, for the star shown before its name."""
    if user is None:
        return None
    with request.app.state.db.sessionmaker() as session:
        master = get_master(session)
        return master.id if master else None


# Sign-ins that closed the "two-factor is off" banner; not exported with the settings.
TOTP_BANNER_DISMISSED_KEY = "totp_banner_dismissed"


def _show_totp_banner(request: Request, user) -> bool:
    if user is None or user.totp_enabled:
        return False
    with request.app.state.db.sessionmaker() as session:
        row = session.get(AppSetting, TOTP_BANNER_DISMISSED_KEY)
        return user.id not in (row.value if row else [])


def _context(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    flash = FLASH_MESSAGES.get(request.query_params.get("msg", ""))
    return {
        "version": __version__,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "user": user,
        "show_totp_banner": _show_totp_banner(request, user),
        "master_id": _master_id(request, user),
        "flash": flash,
        "path": request.url.path,
    }


templates = Jinja2Templates(directory=str(TEMPLATE_DIR), context_processors=[_context])


def localtime(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Stored datetimes are naive UTC; show them in the configured TZ."""
    if value is None:
        return "—"
    from ..config import get_settings

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(get_settings().timezone).strftime(fmt)


def localtime_iso(value: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """localtime() for a timestamp stored as an ISO string (in a JSON setting)."""
    return localtime(datetime.fromisoformat(value), fmt) if value else "—"


def iso_local(value: str | None, fmt: str = "%d %b %H:%M:%S") -> str:
    """A UTC ISO time (as the live feed carries it) in the configured time zone."""
    if not value:
        return "—"
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return localtime(parsed.astimezone(UTC).replace(tzinfo=None), fmt)


def filesize(value: int | None) -> str:
    if not value:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def connector_label(type_: str) -> str:
    if type_ == "import":
        return "file import"
    if type_ == "local":
        return "MediaWrangler"
    cls = connector_types().get(type_)
    return cls.display_name if cls else type_


templates.env.globals["connector_label"] = connector_label
templates.env.filters["localtime"] = localtime
templates.env.filters["localtime_iso"] = localtime_iso
templates.env.filters["iso_local"] = iso_local
templates.env.filters["filesize"] = filesize
templates.env.globals["type_can_quick"] = type_can_quick
templates.env.globals["now"] = lambda: datetime.now(UTC)


def safe_next(value: str | None) -> str:
    """Only allow same-site relative redirects after login."""
    if not value:
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or not value.startswith("/") or value.startswith("//"):
        return "/"
    if "\\" in value:
        return "/"
    return value
