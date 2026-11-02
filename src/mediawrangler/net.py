"""Client IP resolution behind a trusted reverse proxy."""

import ipaddress

from starlette.requests import Request

from .config import Settings


def _is_trusted(ip: str, settings: Settings) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in settings.trusted_proxy_networks)


def client_ip(request: Request, settings: Settings) -> str:
    """The peer address, or the first untrusted hop of X-Forwarded-For when the
    peer is a trusted proxy. Walking from the right stops a client from spoofing
    its address by sending its own X-Forwarded-For."""
    peer = request.client.host if request.client else "unknown"
    if not _is_trusted(peer, settings):
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [h.strip() for h in forwarded.split(",") if h.strip()]
    for hop in reversed(hops):
        if not _is_trusted(hop, settings):
            return hop
    return hops[0] if hops else peer
