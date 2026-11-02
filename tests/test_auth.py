from datetime import UTC, datetime, timedelta

import pyotp
from sqlalchemy import select

from mediawrangler.auth import lockout
from mediawrangler.auth import totp as totp_mod
from mediawrangler.config import Settings
from mediawrangler.models import AdminUser, SigninEvent
from mediawrangler.net import client_ip
from mediawrangler.web.templating import safe_next

from .conftest import PASSWORD, extract, post, setup_admin


def test_fresh_install_redirects_to_setup(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/setup"
    assert client.get("/login", follow_redirects=False).headers["location"] == "/setup"


def test_setup_creates_admin_and_suggests_totp(client):
    resp = setup_admin(client)
    assert resp.headers["location"] == "/setup/totp"
    page = client.get("/setup/totp")
    assert "two-factor" in page.text.lower()
    assert client.get("/").status_code == 200


def test_setup_enforces_password_policy(client):
    client.get("/setup")
    resp = post(client, "/setup", {"username": "user1", "password": "short", "confirm": "short"})
    assert resp.status_code == 400
    assert "at least 10" in resp.text


def test_setup_closes_once_admin_exists(client, app):
    setup_admin(client)
    client.cookies.clear()
    assert client.get("/setup", follow_redirects=False).headers["location"] == "/login"
    resp = post(client, "/setup", {"username": "eve", "password": PASSWORD, "confirm": PASSWORD})
    assert resp.headers["location"] == "/login"
    with app.state.db.sessionmaker() as db:
        assert [u.username for u in db.execute(select(AdminUser)).scalars()] == ["user1"]


def test_protected_page_redirects_to_login_with_next(client):
    setup_admin(client)
    client.cookies.clear()
    resp = client.get("/settings/connectors", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=%2Fsettings%2Fconnectors"


def test_htmx_requests_get_hx_redirect(client):
    setup_admin(client)
    client.cookies.clear()
    resp = client.get(
        "/settings/connectors", headers={"HX-Request": "true"}, follow_redirects=False
    )
    assert resp.headers["HX-Redirect"].startswith("/login")


def test_login_logout(client):
    setup_admin(client)
    client.cookies.clear()
    resp = post(client, "/login", {"username": "USER1", "password": PASSWORD, "next": "/settings"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings"
    assert client.get("/").status_code == 200
    assert post(client, "/logout").headers["location"] == "/login"
    assert client.get("/", follow_redirects=False).status_code == 303


def test_bad_password_is_rejected_and_logged(client, app):
    setup_admin(client)
    client.cookies.clear()
    resp = post(client, "/login", {"username": "user1", "password": "nope-nope-nope-nope"})
    assert resp.status_code == 401
    assert "Incorrect username or password" in resp.text
    resp = post(client, "/login", {"username": "nobody", "password": "nope-nope-nope-nope"})
    assert resp.status_code == 401
    with app.state.db.sessionmaker() as db:
        reasons = [
            e.reason
            for e in db.execute(select(SigninEvent).where(SigninEvent.success.is_(False))).scalars()
        ]
    assert reasons == ["bad_password", "bad_password"]


def test_account_lockout(client):
    setup_admin(client)
    client.cookies.clear()
    for _ in range(lockout.MAX_FAILURES_PER_ACCOUNT):
        assert (
            post(
                client, "/login", {"username": "user1", "password": "wrong-wrong-wrong"}
            ).status_code
            == 401
        )
    resp = post(client, "/login", {"username": "user1", "password": PASSWORD})
    assert resp.status_code == 429
    assert "Too many failed attempts" in resp.text


def test_lock_lifts_after_window(app):
    with app.state.db.sessionmaker() as db:
        for _ in range(lockout.MAX_FAILURES_PER_ACCOUNT):
            lockout.record(
                db,
                ip="10.0.0.1",
                username="user1",
                step="password",
                success=False,
                reason="bad_password",
            )
        assert lockout.check_lock(db, "10.0.0.9", "user1").locked
        later = lockout.utcnow() + lockout.WINDOW + timedelta(seconds=1)
        assert not lockout.check_lock(db, "10.0.0.9", "user1", now=later).locked


def test_ip_lockout_spans_usernames(app):
    with app.state.db.sessionmaker() as db:
        for i in range(lockout.MAX_FAILURES_PER_IP):
            lockout.record(db, ip="10.0.0.1", username=f"user{i}", step="password", success=False)
        status = lockout.check_lock(db, "10.0.0.1", "someone-else")
        assert status.locked and status.reason == "locked_ip"
        assert not lockout.check_lock(db, "10.0.0.2", "someone-else").locked


def test_csrf_token_required(client):
    client.get("/setup")
    resp = client.post(
        "/setup",
        data={"username": "user1", "password": PASSWORD, "confirm": PASSWORD},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    resp = post(
        client,
        "/setup",
        {"username": "user1", "password": PASSWORD, "confirm": PASSWORD, "csrf_token": "x" * 43},
    )
    assert resp.status_code == 403


def test_cross_origin_post_refused(client):
    client.get("/setup")
    resp = post(
        client,
        "/setup",
        {"username": "user1", "password": PASSWORD, "confirm": PASSWORD},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403


def test_security_headers(client):
    resp = client.get("/login")
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["cache-control"] == "no-store"
    assert "strict-transport-security" not in resp.headers  # http public URL = LAN mode


def test_session_cookie_flags(client):
    resp = setup_admin(client)
    cookie = resp.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie


def _enable_totp(client) -> tuple[str, list[str]]:
    page = client.get("/settings/security/totp")
    secret = extract(r'<code class="secret">([A-Z2-7]+)</code>', page.text)
    resp = post(client, "/settings/security/totp", {"code": pyotp.TOTP(secret).now()})
    assert resp.status_code == 200, resp.text
    codes = [
        c
        for c in extract(r'<ul class="codes">(.*?)</ul>', resp.text.replace("\n", "")).split(
            "<code>"
        )[1:]
    ]
    return secret, [c.split("</code>")[0] for c in codes]


def test_totp_enable_and_login(client, app, monkeypatch):
    setup_admin(client)
    secret, codes = _enable_totp(client)
    assert len(codes) == 10
    assert "Two-factor authentication is off" not in client.get("/").text

    client.cookies.clear()
    resp = post(client, "/login", {"username": "user1", "password": PASSWORD, "next": "/settings"})
    assert resp.headers["location"] == "/login/totp?next=%2Fsettings"
    # Password alone doesn't grant access.
    assert client.get("/", follow_redirects=False).status_code == 303

    assert post(client, "/login/totp", {"code": "000000"}).status_code == 401

    # The code used to enable TOTP was consumed (no replay), so move the clock
    # to the next time-step and use that step's code.
    with app.state.db.sessionmaker() as db:
        last = db.get(AdminUser, 1).totp_last_counter
    gen = pyotp.TOTP(secret)
    next_step = datetime.fromtimestamp((last + 1) * gen.interval + 1, UTC)
    monkeypatch.setattr(totp_mod, "_now", lambda: next_step)

    resp = post(client, "/login/totp", {"code": gen.at(next_step), "next": "/settings"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings"
    assert client.get("/").status_code == 200

    # The same code can't be used twice.
    client.cookies.clear()
    post(client, "/login", {"username": "user1", "password": PASSWORD})
    assert post(client, "/login/totp", {"code": gen.at(next_step)}).status_code == 401


def test_recovery_code_is_single_use(client):
    setup_admin(client)
    _secret, codes = _enable_totp(client)

    client.cookies.clear()
    post(client, "/login", {"username": "user1", "password": PASSWORD})
    assert post(client, "/login/totp", {"code": codes[0]}).status_code == 303

    client.cookies.clear()
    post(client, "/login", {"username": "user1", "password": PASSWORD})
    assert post(client, "/login/totp", {"code": codes[0]}).status_code == 401


def test_password_step_success_does_not_reset_totp_failures(client):
    setup_admin(client)
    _enable_totp(client)
    client.cookies.clear()
    for _ in range(lockout.MAX_FAILURES_PER_ACCOUNT):
        post(client, "/login", {"username": "user1", "password": PASSWORD})
        post(client, "/login/totp", {"code": "000000"})
    resp = post(client, "/login", {"username": "user1", "password": PASSWORD})
    assert resp.status_code == 429


def test_change_password_signs_out_other_sessions(client, app):
    from fastapi.testclient import TestClient

    setup_admin(client)
    with TestClient(app) as other:
        post(other, "/login", {"username": "user1", "password": PASSWORD})
        assert other.get("/").status_code == 200
        new = "an even longer passphrase here"
        resp = post(
            client, "/settings/security/password", {"current": PASSWORD, "new": new, "confirm": new}
        )
        assert resp.headers["location"] == "/settings/security?msg=password_changed"
        assert other.get("/", follow_redirects=False).status_code == 303
    assert client.get("/").status_code == 200


def test_safe_next():
    assert safe_next("/settings") == "/settings"
    assert safe_next("https://evil.example/") == "/"
    assert safe_next("//evil.example") == "/"
    assert safe_next("/\\evil.example") == "/"
    assert safe_next(None) == "/"


class _Req:
    def __init__(self, peer, xff=None):
        self.client = type("C", (), {"host": peer})()
        self.headers = {"x-forwarded-for": xff} if xff else {}


def test_client_ip_ignores_xff_from_untrusted_peer():
    s = Settings(trusted_proxies="192.168.1.2")
    assert client_ip(_Req("203.0.113.9", "1.2.3.4"), s) == "203.0.113.9"


def test_client_ip_uses_rightmost_untrusted_hop():
    s = Settings(trusted_proxies="192.168.1.2, 10.0.0.0/8")
    # A client spoofing XFF can only prepend; the proxy appends the real address.
    assert client_ip(_Req("192.168.1.2", "6.6.6.6, 203.0.113.9"), s) == "203.0.113.9"
    assert client_ip(_Req("192.168.1.2", "203.0.113.9, 10.1.1.1"), s) == "203.0.113.9"
