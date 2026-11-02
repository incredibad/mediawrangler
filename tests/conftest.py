import re

import pytest
from fastapi.testclient import TestClient

from mediawrangler.config import Settings, override_settings
from mediawrangler.main import create_app

PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def no_recheck_pause(monkeypatch):
    """No real waiting in tests: an app is read once, and a fix's fixed pauses are skipped.
    A test of catching up sets folder_sync.WAIT itself."""
    import mediawrangler.folder_sync as folder_sync
    import mediawrangler.sync as sync_mod

    monkeypatch.setattr(folder_sync, "POLL", 0)
    monkeypatch.setattr(folder_sync, "WAIT", 0)
    monkeypatch.setattr(folder_sync, "ARR_WAIT", 0)
    monkeypatch.setattr(sync_mod, "SETTLE_SCALE", 0)


@pytest.fixture
def settings(tmp_path):
    s = Settings(
        data_dir=tmp_path / "data",
        public_url="http://testserver",
        secret_key="test-master-key",
        webhook_port=0,
    )
    yield s
    override_settings(None)


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def csrf_of(client: TestClient) -> str:
    """The CSRF token the app issued as a cookie (fetching a page first if needed)."""
    token = client.cookies.get("mw_csrf")
    if not token:
        client.get("/login")
        token = client.cookies.get("mw_csrf")
    return token


def post(client: TestClient, url: str, data: dict | None = None, **kwargs):
    data = dict(data or {})
    data.setdefault("csrf_token", csrf_of(client))
    return client.post(url, data=data, follow_redirects=False, **kwargs)


def setup_admin(client: TestClient, username: str = "alex", password: str = PASSWORD):
    client.get("/setup")
    resp = post(client, "/setup", {"username": username, "password": password, "confirm": password})
    assert resp.status_code == 303, resp.text
    return resp


@pytest.fixture
def logged_in(client):
    setup_admin(client)
    return client


def extract(pattern: str, html: str) -> str:
    match = re.search(pattern, html)
    assert match, f"pattern {pattern!r} not found"
    return match.group(1)


def table_rows(html: str) -> list[tuple[str, str, bool]]:
    """(flags, search, hidden) for every row of a comparison table on a page."""
    found = re.findall(r'<tr data-flags="([^"]*)" data-search="([^"]*)"\s*(hidden)?\s*>', html)
    return [(flags, search, bool(hidden)) for flags, search, hidden in found]


def filter_options(html: str) -> list[tuple[str, str, bool]]:
    """(value, label, selected) for the filter dropdown of a comparison table."""
    select = re.search(r'<select name="filter"[^>]*>(.*?)</select>', html, re.S).group(1)
    return [
        (value, label, bool(sel))
        for value, sel, label in re.findall(
            r'<option value="([^"]+)"\s*(selected)?\s*>([^<]+)</option>', select
        )
    ]


def plain(html: str) -> str:
    """Visible text of an HTML fragment, whitespace collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def summary_col(html: str, name: str) -> str:
    """Visible text of one app's summary column on Watch, Presence or Physical media."""
    split = re.compile(r'<div class="summary-col(?: summary-box)?">')
    for col in split.split(html)[1:]:
        head = re.search(r'<div class="summary-head">(.*?)</div>', col, re.S).group(1)
        if plain(head).removeprefix("★ ").startswith(name):
            return plain(col.split("</section>")[0])
    raise AssertionError(f"no summary column for {name}")
