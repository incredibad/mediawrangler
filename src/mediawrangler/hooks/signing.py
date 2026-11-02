"""Checking the signature an app puts on a webhook.

Silo (and the Event Webhooks plugin for it) signs with the scheme Silo uses
for its own webhooks, which follows Stripe's: the header
``X-Silo-Signature: t=<unix seconds>,v1=<hex>`` where ``v1`` is the
HMAC-SHA256, keyed with the shared secret, of ``"<t>.<raw body>"``.
"""

import hashlib
import hmac

TOLERANCE_SECONDS = 300  # reject a signature older (or newer) than this


def sign(secret: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={digest.hexdigest()}"


def verify(secret: str, header: str | None, body: bytes, now: float) -> bool:
    if not secret or not header:
        return False
    try:
        parts = dict(part.split("=", 1) for part in header.split(","))
        timestamp = int(parts["t"])
        given = parts["v1"]
    except (ValueError, KeyError):
        return False
    if abs(now - timestamp) > TOLERANCE_SECONDS:
        return False
    expected = sign(secret, timestamp, body).split("v1=", 1)[1]
    return hmac.compare_digest(expected, given)


def token_matches(secret: str, given: str | None) -> bool:
    """The shared secret sent as a header, for apps that cannot sign."""
    return bool(secret) and bool(given) and hmac.compare_digest(secret.encode(), given.encode())
