"""TOTP second factor and one-time recovery codes."""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime

import pyotp
import segno

ISSUER = "MediaWrangler"
RECOVERY_CODE_COUNT = 10


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=ISSUER)


def qr_svg(uri: str) -> str:
    """Inline SVG of the provisioning QR code (no external requests, CSP-safe)."""
    return segno.make(uri, error="m").svg_inline(scale=5, dark="#111", light="#fff")


def _now() -> datetime:
    """Current time; a seam for tests."""
    return datetime.now(UTC)


def verify_code(
    secret: str, code: str, last_counter: int | None = None, now: datetime | None = None
) -> int | None:
    """Return the matched time-step, or None. Allows one step of clock drift
    either way, and rejects any step at or before `last_counter` (replay)."""
    code = code.strip().replace(" ", "")
    if not (code.isdigit() and len(code) == 6):
        return None
    totp = pyotp.TOTP(secret)
    now = now or _now()
    current = totp.timecode(now)
    for counter in (current - 1, current, current + 1):
        if last_counter is not None and counter <= last_counter:
            continue
        if hmac.compare_digest(totp.generate_otp(counter), code):
            return counter
    return None


def looks_like_recovery_code(code: str) -> bool:
    return not code.strip().replace(" ", "").isdigit()


def new_recovery_codes() -> list[str]:
    """Codes like 'k3vq-7xm2-p9da'. Shown once; only hashes are stored."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = "".join(secrets.choice(alphabet) for _ in range(12))
        codes.append(f"{raw[:4]}-{raw[4:8]}-{raw[8:]}")
    return codes


def normalise_recovery_code(code: str) -> str:
    return code.strip().lower().replace(" ", "").replace("-", "")


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(normalise_recovery_code(code).encode()).hexdigest()


def recovery_code_matches(code: str, code_hash: str) -> bool:
    return hmac.compare_digest(hash_recovery_code(code), code_hash)
