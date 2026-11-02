"""Login lockout by IP and by account, driven by the sign-in log."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import SigninEvent

WINDOW = timedelta(minutes=15)
MAX_FAILURES_PER_IP = 10
MAX_FAILURES_PER_ACCOUNT = 5


@dataclass
class LockStatus:
    locked: bool
    reason: str | None = None
    until: datetime | None = None


def _failures_since(db: Session, since: datetime, **filters) -> tuple[int, datetime | None]:
    q = select(func.count(), func.min(SigninEvent.at)).where(
        SigninEvent.success.is_(False), SigninEvent.at >= since
    )
    for column, value in filters.items():
        q = q.where(getattr(SigninEvent, column) == value)
    count, oldest = db.execute(q).one()
    return count, oldest


# A correct password on a TOTP account is logged as a success with this reason,
# but must not reset the failure count, or TOTP could be brute-forced by
# re-entering the password between guesses.
PENDING_SECOND_FACTOR = "totp_pending"


def _last_success(db: Session, **filters) -> datetime | None:
    q = select(func.max(SigninEvent.at)).where(
        SigninEvent.success.is_(True),
        (SigninEvent.reason.is_(None)) | (SigninEvent.reason != PENDING_SECOND_FACTOR),
    )
    for column, value in filters.items():
        q = q.where(getattr(SigninEvent, column) == value)
    return db.execute(q).scalar()


def check_lock(db: Session, ip: str, username: str, now: datetime | None = None) -> LockStatus:
    """Locked when too many failures fall inside the window. A success resets the
    count for that IP or account; the lock lifts when the oldest failure ages out."""
    now = now or utcnow()
    window_start = now - WINDOW

    for key, limit, reason in (
        ({"ip": ip}, MAX_FAILURES_PER_IP, "locked_ip"),
        ({"username": username.lower()}, MAX_FAILURES_PER_ACCOUNT, "locked_account"),
    ):
        since = max(filter(None, [window_start, _last_success(db, **key)]))
        count, oldest = _failures_since(db, since, **key)
        if count >= limit:
            return LockStatus(True, reason, (oldest or now) + WINDOW)
    return LockStatus(False)


def record(
    db: Session,
    *,
    ip: str,
    username: str,
    step: str,
    success: bool,
    reason: str | None = None,
    user_agent: str | None = None,
) -> None:
    db.add(
        SigninEvent(
            ip=ip,
            username=username.lower()[:64],
            step=step,
            success=success,
            reason=reason,
            user_agent=(user_agent or "")[:255] or None,
        )
    )
    db.commit()
