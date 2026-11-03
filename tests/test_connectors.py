import httpx
from sqlalchemy import select

from mediawrangler.connectors import ConnectorConfig, connector_types
from mediawrangler.connectors.arr import RadarrConnector, SonarrConnector, SportarrConnector
from mediawrangler.connectors.base import ConnectionTest
from mediawrangler.connectors.jellyfin import JellyfinConnector
from mediawrangler.connectors.silo import SiloConnector
from mediawrangler.models import Source

from .conftest import post


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_registry_has_built_in_types():
    assert set(connector_types()) >= {"jellyfin", "silo", "sonarr", "radarr", "sportarr"}


def test_jellyfin_test_connection_sends_token_in_header():
    seen = {}

    def handler(request: httpx.Request):
        if request.url.path == "/System/Info/Public":
            return httpx.Response(200, json={"Version": "10.11.8"})
        seen["auth"] = request.headers.get("authorization")
        assert "api_key" not in str(request.url)  # never in the URL, where it gets logged
        return httpx.Response(200, json={"ServerName": "Home Server"})

    cfg = ConnectorConfig(base_url="http://jf:8096/", secret="k3y")
    result = JellyfinConnector(cfg, _client(handler)).test_connection()
    assert result == ConnectionTest(True, "Connected to Home Server", "10.11.8")
    assert 'Token="k3y"' in seen["auth"]


def test_jellyfin_rejected_key():
    def handler(request):
        if request.url.path == "/System/Info/Public":
            return httpx.Response(200, json={"Version": "10.11.8"})
        return httpx.Response(401)

    result = JellyfinConnector(ConnectorConfig("http://jf"), _client(handler)).test_connection()
    assert not result.ok and "rejected" in result.message


def test_silo_uses_bearer_and_native_api():
    def handler(request: httpx.Request):
        assert request.url.path.startswith("/api/v2/")
        if request.url.path == "/api/v2/system/info":
            return httpx.Response(200, json={"version": "0.9.1"})
        assert request.headers["authorization"] == "Bearer s1lo"
        return httpx.Response(200, json={"username": "user1"})

    result = SiloConnector(
        ConnectorConfig("http://silo:8090", "s1lo"), _client(handler)
    ).test_connection()
    assert result.ok and result.version == "0.9.1" and "user1" in result.message


def test_silo_explains_jellyfin_compat_port():
    def handler(request: httpx.Request):
        if request.url.path == "/System/Info/Public":
            return httpx.Response(
                200, json={"ProductName": "Jellyfin Server", "ServerName": "Silo"}
            )
        return httpx.Response(404)

    result = SiloConnector(
        ConnectorConfig("http://silo:8097", "k"), _client(handler)
    ).test_connection()
    assert not result.ok and "Jellyfin-compat port" in result.message


def test_silo_plain_404_stays_generic():
    result = SiloConnector(
        ConnectorConfig("http://elsewhere", "k"), _client(lambda r: httpx.Response(404))
    ).test_connection()
    assert not result.ok and result.message == "unexpected HTTP 404"


def test_silo_rate_limited():
    def handler(request):
        return httpx.Response(429)

    result = SiloConnector(ConnectorConfig("http://silo", "k"), _client(handler)).test_connection()
    assert not result.ok and "429" in result.message


def test_arr_status_and_sportarr_native_path():
    paths = []

    def handler(request: httpx.Request):
        paths.append(request.url.path)
        assert request.headers["x-api-key"] == "abc"
        return httpx.Response(200, json={"appName": "Radarr", "version": "5.9.1"})

    assert (
        RadarrConnector(ConnectorConfig("http://r", "abc"), _client(handler)).test_connection().ok
    )
    SportarrConnector(ConnectorConfig("http://s", "abc"), _client(handler)).test_connection()
    assert paths == ["/api/v3/system/status", "/api/system/status"]


def test_sonarr_totals_episodes_with_files_and_shows_with_multi_episode_files():
    series = [
        {"id": 1, "title": "Cartoon", "path": "/tv/Cartoon", "statistics": {"episodeFileCount": 3}},
        {"id": 2, "title": "Drama", "path": "/tv/Drama", "statistics": {"episodeFileCount": 1}},
    ]
    files = {
        "1": [
            {"id": 10, "path": "/tv/Cartoon/S01E01-E02.mkv"},
            {"id": 11, "path": "/tv/Cartoon/S01E03.mkv"},
        ],
        "2": [{"id": 20, "path": "/tv/Drama/S01E01.mkv"}],
    }
    episodes = {
        "1": [
            {"seasonNumber": 1, "episodeNumber": 1, "episodeFileId": 10},
            {"seasonNumber": 1, "episodeNumber": 2, "episodeFileId": 10},
            {"seasonNumber": 1, "episodeNumber": 3, "episodeFileId": 11},
        ],
        "2": [{"seasonNumber": 1, "episodeNumber": 1, "episodeFileId": 20}],
    }

    def handler(request: httpx.Request):
        path, sid = request.url.path, request.url.params.get("seriesId")
        if path == "/api/v3/rootfolder":
            return httpx.Response(200, json=[{"id": 1, "path": "/tv"}])
        if path == "/api/v3/series":
            return httpx.Response(200, json=series)
        if path == "/api/v3/episodefile":
            return httpx.Response(200, json=files[sid])
        if path == "/api/v3/episode":
            return httpx.Response(200, json=episodes[sid])
        return httpx.Response(404)

    snap = SonarrConnector(ConnectorConfig("http://s", "k"), _client(handler)).fetch_full()
    assert len(snap.files) == 3
    assert snap.episodes_with_files == 4
    assert snap.multi_episode_series == [["Cartoon", 1, 2]]


def test_unreachable_host():
    def handler(request):
        raise httpx.ConnectError("refused")

    result = RadarrConnector(ConnectorConfig("http://r", "abc"), _client(handler)).test_connection()
    assert not result.ok and "cannot reach" in result.message


def _new_connector(client, **overrides):
    data = {
        "type": "sonarr",
        "name": "Sonarr",
        "base_url": "http://192.0.2.10:8989/",
        "secret": "super-secret-sonarr-key",
        "path_map": "/data/TV => /media/TV\n/data/Anime => /media/Anime",
        "schedule_mode": "every",
        "schedule_every": "1",
        "schedule_unit": "hours",
        "enabled": "on",
    }
    data.update(overrides)
    return post(client, "/settings/connectors/new", data)


def test_create_connector_encrypts_key_and_never_echoes_it(logged_in, app):
    resp = _new_connector(logged_in)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings/connectors?msg=connector_saved"
    with app.state.db.sessionmaker() as db:
        src = db.execute(select(Source)).scalar_one()
        page = logged_in.get(f"/settings/connectors/{src.id}")
        assert "super-secret-sonarr-key" not in page.text
        assert "A key is stored" in page.text
        assert src.base_url == "http://192.0.2.10:8989"
        assert "super-secret" not in src.secret_enc
        assert app.state.box.decrypt(src.secret_enc) == "super-secret-sonarr-key"
        assert src.path_map == [["/data/Anime", "/media/Anime"], ["/data/TV", "/media/TV"]]


def test_connector_validation(logged_in):
    assert _new_connector(logged_in, base_url="ftp://x").status_code == 400
    assert _new_connector(logged_in, path_map="/data/TV /media/TV").status_code == 400
    assert (
        _new_connector(
            logged_in, schedule_mode="every", schedule_every="1", schedule_unit="min"
        ).status_code
        == 400
    )
    assert _new_connector(logged_in, secret="").status_code == 400
    assert _new_connector(logged_in).status_code == 303
    resp = _new_connector(logged_in)
    assert resp.status_code == 400 and "already uses that name" in resp.text


def test_update_with_blank_key_keeps_stored_key(logged_in, app):
    _new_connector(logged_in)
    with app.state.db.sessionmaker() as db:
        src = db.execute(select(Source)).scalar_one()
        old = src.secret_enc
    resp = post(
        logged_in,
        f"/settings/connectors/{src.id}",
        {
            "name": "Sonarr TV",
            "base_url": "http://192.0.2.10:8989",
            "secret": "",
            "path_map": "",
            "enabled": "on",
        },
    )
    assert resp.status_code == 303
    with app.state.db.sessionmaker() as db:
        src = db.get(Source, src.id)
        assert src.secret_enc == old and src.name == "Sonarr TV" and src.schedule_minutes is None


def test_test_connection_uses_the_form_and_records_the_result(logged_in, app, monkeypatch):

    seen = {}

    def fake(self):
        seen["url"], seen["key"] = self.config.base_url, self.config.secret
        return ConnectionTest(True, "Connected to Sonarr", "4.0.15")

    monkeypatch.setattr(SonarrConnector, "test_connection", fake)
    _new_connector(logged_in)
    with app.state.db.sessionmaker() as db:
        sid = db.execute(select(Source.id)).scalar_one()
    # A new address in the form is tested without saving; a blank key means the stored one.
    resp = post(
        logged_in,
        "/settings/connectors/test",
        {"source_id": str(sid), "base_url": "http://other:8989", "secret": "", "path_map": ""},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200 and "Works" in resp.text and "Connected to" not in resp.text
    assert seen["url"] == "http://other:8989" and seen["key"]
    with app.state.db.sessionmaker() as db:
        row = db.get(Source, sid)
        assert row.last_test_ok is True and row.base_url != "http://other:8989"


def test_test_connection_on_a_new_connector_reports_failure(logged_in, monkeypatch):

    monkeypatch.setattr(
        SonarrConnector, "test_connection", lambda self: ConnectionTest(False, "401 Unauthorized")
    )
    resp = post(
        logged_in,
        "/settings/connectors/test",
        {"type": "sonarr", "base_url": "http://s:8989", "secret": "k"},
    )
    assert "Failed" in resp.text and "401 Unauthorized" in resp.text


def test_delete_connector(logged_in, app):
    _new_connector(logged_in)
    with app.state.db.sessionmaker() as db:
        sid = db.execute(select(Source.id)).scalar_one()
    assert post(logged_in, f"/settings/connectors/{sid}/delete").status_code == 303
    with app.state.db.sessionmaker() as db:
        assert db.get(Source, sid) is None


def test_arr_rescans_name_the_one_title():

    # Radarr ignores "movieIds" on RescanMovie and rescans every film.
    assert RadarrConnector.rescan_body(None, "movie-12") == {"name": "RescanMovie", "movieId": 12}
    assert SonarrConnector.rescan_body(None, "series-7") == {"name": "RescanSeries", "seriesId": 7}


def test_jellyfin_finds_a_folder_entry_and_refreshes_just_that_folder():
    from mediawrangler.connectors.base import Task
    from mediawrangler.connectors.jellyfin import JellyfinConnector

    items = {
        "copy": {"Id": "copy", "ParentId": "dir", "Path": "/data/Movies/The Core [2003]/a.mkv"},
        "dir": {"Id": "dir", "ParentId": "lib", "Path": "/data/Movies/The Core [2003]"},
    }

    class Fake(JellyfinConnector):
        def __init__(self):
            pass

        def _get_json(self, path, **params):
            return {"Items": [items[params["Ids"]]] if params.get("Ids") in items else []}

    fake = Fake()
    assert fake.folder_entry("/data/Movies/The Core [2003]/", ["gone", "copy"]) == "dir"
    method, path, body = fake.task_call(Task("scan", path="/data/Movies/x", item_id="dir"))
    assert (method, body) == ("POST", None)
    assert path.startswith("/Items/dir/Refresh?Recursive=true&MetadataRefreshMode=Default")
    assert fake.task_call(Task("scan", path="/data/Movies/x"))[1] == "/Library/Media/Updated"
