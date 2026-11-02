from fastapi.testclient import TestClient

from mediawrangler import __version__

from .conftest import extract, post


def test_health_is_public(client):
    resp = client.get("/api/health")
    assert resp.json() == {"status": "ok", "version": __version__}


def test_status_requires_auth(client):
    assert client.get("/api/v1/status").status_code == 401
    assert (
        client.get("/api/v1/status", headers={"Authorization": "Bearer mw_nope"}).status_code == 401
    )


def test_api_key_lifecycle(logged_in, app):
    resp = post(logged_in, "/settings/api-keys", {"name": "script"})
    key = extract(r'<code class="secret">(mw_[^<]+)</code>', resp.text)
    assert logged_in.get("/settings/api-keys").text.count(key) == 0  # shown once only

    with TestClient(app) as anon:
        ok = anon.get("/api/v1/status", headers={"Authorization": f"Bearer {key}"})
        assert ok.status_code == 200 and ok.json()["version"] == __version__

    key_id = extract(r"/settings/api-keys/(\d+)/revoke", logged_in.get("/settings/api-keys").text)
    post(logged_in, f"/settings/api-keys/{key_id}/revoke")
    with TestClient(app) as anon:
        assert (
            anon.get("/api/v1/status", headers={"Authorization": f"Bearer {key}"}).status_code
            == 401
        )


def test_logged_in_browser_can_read_status(logged_in):
    assert logged_in.get("/api/v1/status").status_code == 200


def test_unknown_page_404(logged_in):
    assert logged_in.get("/nope").status_code == 404
