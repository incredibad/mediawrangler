"""Fixing from the comparison pages: change which show an app thinks an item is (0.11)."""

import json

import httpx
import pytest
from sqlalchemy import select

from mediawrangler import writes
from mediawrangler.connectors.base import Connector, ConnectorConfig
from mediawrangler.connectors.jellyfin import JellyfinConnector
from mediawrangler.connectors.silo import SiloConnector
from mediawrangler.models import AuditLog, SourceItem

from .conftest import post
from .test_ingest import _group, world  # noqa: F401


class Apps:
    """Stands in for Jellyfin's and Silo's search and apply endpoints, and records calls."""

    def __init__(self):
        self.calls = []
        self.silo_updated = True
        self.silo_new_id = None
        self.jf_status = 204

    def handler(self, kind):
        def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else None
            self.calls.append(
                (kind, request.method, request.url.path, dict(request.url.params), body)
            )
            path = request.url.path
            if path.startswith("/Items/RemoteSearch/Apply/"):
                return httpx.Response(self.jf_status)
            if path.startswith("/Items/RemoteSearch/"):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "Name": "The Stand",
                            "PremiereDate": "2020-09-17T00:00:00.0000000Z",
                            "ProviderIds": {"Tvdb": "372051"},
                            "SearchProviderName": "TheTVDB",
                        },
                        {
                            "Name": "The Stand",
                            "ProductionYear": 1994,
                            "ProviderIds": {"Tvdb": "71267", "Imdb": "tt0108941"},
                            "SearchProviderName": "TheTVDB",
                            "Overview": "Miniseries.",
                        },
                    ],
                )
            if path.endswith("/match/search"):
                return httpx.Response(
                    200,
                    json={
                        "candidates": [
                            {
                                "title": "The Stand",
                                "year": 2020,
                                "provider_ids": {"tvdb": "372051"},
                                "match_reasons": ["title"],
                            },
                            {
                                "title": "The Stand",
                                "year": 1994,
                                "provider_ids": {"tvdb": "71267"},
                                "match_reasons": ["title", "year"],
                            },
                        ],
                        "truncated": False,
                    },
                )
            if path.endswith("/match/apply"):
                content = path.split("/")[-3]
                return httpx.Response(
                    200,
                    json={"content_id": self.silo_new_id or content, "updated": self.silo_updated},
                )
            return httpx.Response(404)

        return handle


@pytest.fixture
def apps(monkeypatch):
    apps = Apps()
    real_init = Connector.__init__

    def fake_init(self, config, client=None):
        real_init(
            self, config, httpx.Client(transport=httpx.MockTransport(apps.handler(self.type)))
        )

    monkeypatch.setattr(Connector, "__init__", fake_init)
    return apps


def _silo_stand(db, silo):
    return db.execute(
        select(SourceItem).where(
            SourceItem.source_id == silo.id,
            SourceItem.title == "The Stand",
            SourceItem.kind == "series",
        )
    ).scalar_one()


def test_jellyfin_search_and_apply_send_what_the_api_wants():
    seen = Apps()
    client = httpx.Client(transport=httpx.MockTransport(seen.handler("jellyfin")))
    jf = JellyfinConnector(ConnectorConfig("http://jf", "k"), client)
    found = jf.search_matches("abc", "series", "The Stand", 2020)
    assert found[0].year == 2020  # read from the premiere date when there is no year
    assert [(c.year, c.ids) for c in found][1] == (1994, {"tvdb": "71267", "imdb": "tt0108941"})
    kind, method, path, _q, body = seen.calls[0]
    assert (method, path) == ("POST", "/Items/RemoteSearch/Series")
    assert body["SearchInfo"] == {"Name": "The Stand", "Year": 2020} and body["ItemId"] == "abc"
    assert jf.match_call("abc", found[1])[1].endswith("/Apply/abc?replaceAllImages=true")
    assert jf.apply_match("abc", found[1]) == "abc"
    _k, method, path, query, body = seen.calls[1]
    assert (method, path, query) == (
        "POST",
        "/Items/RemoteSearch/Apply/abc",
        {"replaceAllImages": "true"},
    )
    assert body["ProviderIds"]["Tvdb"] == "71267"  # the search result goes back as it came


def test_silo_search_and_apply_use_the_content_id_and_library():
    seen = Apps()
    client = httpx.Client(transport=httpx.MockTransport(seen.handler("silo")))
    silo = SiloConnector(ConnectorConfig("http://silo", "k"), client)
    found = silo.search_matches("2/series-tvdb-372051", "series", "The Stand", None)
    assert found[1].ids == {"tvdb": "71267"} and found[1].note == "title, year"
    _k, _m, path, _q, body = seen.calls[0]
    assert path == "/api/v2/admin/items/series-tvdb-372051/match/search"
    assert body == {"title": "The Stand", "limit": 30, "library_id": "2"}
    assert silo.match_call("2/series-tvdb-372051", found[1]) == (
        "POST",
        "/api/v2/admin/items/series-tvdb-372051/match/apply",
        {"provider_ids": {"tvdb": "71267"}, "library_id": "2"},
    )
    assert silo.apply_match("2/series-tvdb-372051", found[1]) == "2/series-tvdb-372051"
    seen.silo_new_id = "series-tvdb-71267"
    assert silo.apply_match("2/series-tvdb-372051", found[1]) == "2/series-tvdb-71267"
    seen.silo_updated = False
    from mediawrangler.connectors.base import ItemGone

    with pytest.raises(ItemGone):
        silo.apply_match("2/series-tvdb-372051", found[1])


def test_the_search_puts_the_candidate_with_the_other_apps_id_first(world, logged_in, apps):  # noqa: F811
    db, jf, silo, *_ = world
    item = _silo_stand(db, silo)
    page = logged_in.get(f"/fix/{item.id}/search")
    assert page.status_code == 200 and "Change which show Silo thinks this is" in page.text
    assert page.text.index("(2020)") < page.text.index(
        "(1994)"
    )  # the 2020 one agrees with Jellyfin
    assert "Same ID as Jellyfin" in page.text
    assert logged_in.get(f"/fix/{item.id}/search?q=Stand&year=").status_code == 200
    assert apps.calls[-1][4]["title"] == "Stand" and "year" not in apps.calls[-1][4]


def test_the_preview_shows_the_exact_call_and_never_the_key(world, logged_in, apps):  # noqa: F811
    db, jf, silo, *_ = world
    item = _silo_stand(db, silo)
    page = logged_in.get(f"/fix/{item.id}/search").text
    candidate = page.split('name="candidate" value=\'')[1].split("'>")[0]
    resp = post(logged_in, f"/fix/{item.id}/preview", {"candidate": candidate})
    assert resp.status_code == 200
    assert (
        "POST</strong> http://silo/api/v2/admin/items/series-tvdb-372051/match/apply" in resp.text
    )
    assert "71267" in resp.text and "Change in Silo" in resp.text
    assert "Writes to your apps are off" in resp.text
    assert not [c for c in apps.calls if c[2].endswith("/match/apply")]  # nothing sent yet


def test_apply_needs_the_write_switch_and_is_logged(world, logged_in, apps, app, monkeypatch):  # noqa: F811
    db, jf, silo, *_ = world
    item = _silo_stand(db, silo)
    page = logged_in.get(f"/fix/{item.id}/search").text
    candidate = page.split('name="candidate" value=\'')[1].split("'>")[0]

    refused = post(logged_in, f"/fix/{item.id}/apply", {"candidate": candidate})
    assert "switched off" in refused.text
    assert not [c for c in apps.calls if c[2].endswith("/match/apply")]

    import mediawrangler.sync as sync_mod

    monkeypatch.setattr(sync_mod, "SETTLE_SCALE", 0)
    writes.set_flag(db, writes.ENABLED_KEY, True)
    done = post(logged_in, f"/fix/{item.id}/apply", {"candidate": candidate})
    assert "data-close-now" in done.text  # the dialog closes; the work goes on in the background
    app.state.syncs.wait_idle()
    assert [c for c in apps.calls if c[2].endswith("/match/apply")]
    (entry,) = db.execute(select(AuditLog).where(AuditLog.op == "match")).scalars()
    assert entry.result == "applied" and entry.source_name == "Silo"
    assert entry.before == "tvdb 372051" and entry.after == "tvdb 71267"
    assert "Changed which show Silo thinks" in entry.why

    apps.silo_updated = False
    failed = post(logged_in, f"/fix/{item.id}/apply", {"candidate": candidate})
    assert "data-close-now" in failed.text
    app.state.syncs.wait_idle()
    assert any("did not change anything" in j["error"] for j in app.state.syncs.jobs())
    errors = db.execute(select(AuditLog).where(AuditLog.op == "match", AuditLog.result == "error"))
    assert len(errors.scalars().all()) == 1
    assert "Changed match" in logged_in.get("/activity?tab=audit").text


def test_a_garbled_choice_is_refused_cleanly(world, logged_in, apps):  # noqa: F811
    db, jf, silo, *_ = world
    item = _silo_stand(db, silo)
    resp = post(logged_in, f"/fix/{item.id}/preview", {"candidate": "not json"})
    assert resp.status_code == 200 and "could not be read" in resp.text
    assert logged_in.get("/fix/999999/search").status_code == 404


def test_presence_rows_get_a_fix_menu_where_it_applies(world, logged_in, apps):  # noqa: F811
    db, jf, silo, *_ = world
    tv = _group(db, "TV")
    page = logged_in.get(f"/presence?group={tv.id}&filter=all").text
    assert "Change which show Silo thinks this is" in page
    assert "Change which show Jellyfin thinks this is" in page
    anime = _group(db, "Anime")
    low = logged_in.get(f"/presence?group={anime.id}&filter=low").text
    assert "Tell MediaWrangler these are the same show" in low
