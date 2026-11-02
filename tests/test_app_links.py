"""Links to each app: column headings, and "View in <app>" on the folder page (0.12.87)."""

from mediawrangler.connectors.arr import RadarrConnector, SonarrConnector, SportarrConnector
from mediawrangler.connectors.base import ConnectorConfig, ConnectorError
from mediawrangler.connectors.jellyfin import JellyfinConnector
from mediawrangler.connectors.silo import SiloConnector

from .test_physical import share, synced  # noqa: F401
from .test_trash import orphan  # noqa: F401


def _make(cls, url="http://app:1234"):
    return cls(ConnectorConfig(base_url=url, secret="k"))


def test_each_app_links_to_its_own_page_for_a_title(monkeypatch):
    assert _make(JellyfinConnector).web_link("abc") == "http://app:1234/web/#/details?id=abc"
    assert (
        _make(SiloConnector).web_link("3/series-tvdb-80644")
        == "http://app:1234/item/series-tvdb-80644?libraryId=3"
    )
    asked = []

    def get_json(self, path, **params):
        asked.append(path)
        return {"titleSlug": "clannad"}

    monkeypatch.setattr(SonarrConnector, "_get_json", get_json)
    monkeypatch.setattr(RadarrConnector, "_get_json", get_json)
    assert _make(SonarrConnector).web_link("series-62") == "http://app:1234/series/clannad"
    assert _make(RadarrConnector).web_link("movie-7") == "http://app:1234/movie/clannad"
    assert asked == ["/api/v3/series/62", "/api/v3/movie/7"]
    assert _make(SportarrConnector).web_link("league-1") is None


def test_column_headings_link_to_the_apps(orphan, logged_in):  # noqa: F811
    db, group, *_ = orphan
    page = logged_in.get("/physical", params={"group": group.id, "filter": "all"}).text
    head = page.split("<thead>")[1].split("</thead>")[0]
    assert 'class="app-link" href="http://jf"' in head and 'target="_blank"' in head
    assert "Media share" in head and head.count("app-link") == head.count("<th") - 2


def test_the_folder_page_offers_view_in_each_app(orphan, logged_in, monkeypatch):  # noqa: F811
    db, group, view, folder, *_ = orphan
    page = logged_in.get("/physical/folder", params={"group": group.id, "folder": folder}).text
    assert 'hx-get="/physical/folder/links?' in page
    monkeypatch.setattr(SonarrConnector, "web_link", lambda self, ext: f"http://sonarr/{ext}")

    def down(self, ext):
        raise ConnectorError("unreachable")

    monkeypatch.setattr(SiloConnector, "web_link", down)
    links = logged_in.get(
        "/physical/folder/links", params={"group": group.id, "folder": folder}
    ).text
    assert "View in Jellyfin" in links and "View in Sonarr" in links
    assert "View in Silo" not in links and "Media share" not in links
