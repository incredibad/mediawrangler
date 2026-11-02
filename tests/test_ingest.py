"""Ingest, groups, matching and the presence matrix, end to end with fake APIs."""

import httpx
import pytest
from sqlalchemy import select

from mediawrangler.connectors import ConnectorConfig
from mediawrangler.connectors.jellyfin import JellyfinConnector
from mediawrangler.connectors.silo import SiloConnector, ids_from_content_id
from mediawrangler.fixes import references
from mediawrangler.groups import ensure_groups, get_master, set_master, source_order
from mediawrangler.ingest import apply_snapshot, previous_records
from mediawrangler.matching import run_matching
from mediawrangler.models import (
    CanonicalItem,
    Library,
    LibraryGroup,
    LibraryGroupMember,
    MediaFile,
    Source,
    SourceItem,
)
from mediawrangler.presence import build_group_view, filter_rows

from .conftest import filter_options, plain, post, summary_col, table_rows
from .fakes import SiloServer, jellyfin_handler


def test_silo_content_ids():
    assert ids_from_content_id("series-tvdb-411959") == {"tvdb": "411959"}
    assert ids_from_content_id("movie-imdb-tt37738006") == {"imdb": "tt37738006"}
    assert ids_from_content_id("movie-tmdb-949") == {"tmdb": "949"}
    assert ids_from_content_id("episode-tvdb-248742-3-17") == {}  # that's the series' ID
    assert ids_from_content_id("local-85d3b7d70fba47498f8202e7e541") == {}


def test_silo_title_in_two_libraries_is_two_items():
    """Silo uses one content ID for a film in both Movies and Movies 4K."""
    from mediawrangler.connectors.silo import card_record, content_id_of

    card = {"content_id": "movie-imdb-tt0113277", "title": "Heat", "year": 1995, "type": "movie"}
    a, b = card_record(card, "1"), card_record(card, "5")
    assert a.external_id != b.external_id
    assert content_id_of(a.external_id) == content_id_of(b.external_id) == "movie-imdb-tt0113277"


def test_jellyfin_fetch_pages_and_skips_virtual_episodes():
    client = httpx.Client(transport=httpx.MockTransport(jellyfin_handler(page_size=2)))
    snap = JellyfinConnector(ConnectorConfig("http://jf", "k"), client).fetch_full()
    assert [lib.kind for lib in snap.libraries] == ["series", "series", "movie", "series", "other"]
    eps = [i for i in snap.items if i.kind == "episode"]
    assert "jf-poi-v" not in {e.external_id for e in eps}
    assert len({e.external_id for e in eps}) == len(eps) == 4 + 1 + 5  # none lost between pages
    sherlock = next(i for i in snap.items if i.external_id == "jf-sherlock")
    assert (sherlock.tvdb, sherlock.tmdb, sherlock.year) == ("176941", "19885", 2010)
    assert any(i.external_id == "jf-heat" for i in snap.items)  # read as an admin user


def test_silo_fetch_reads_details_once_and_retries_429():
    server = SiloServer(page_size=2, rate_limit_first=True)
    client = httpx.Client(transport=httpx.MockTransport(server))
    conn = SiloConnector(ConnectorConfig("http://silo", "k"), client)
    snap = conn.fetch_full()
    stand = next(i for i in snap.items if i.external_id == "2/series-tvdb-372051")
    assert (stand.tvdb, stand.imdb, stand.path) == (
        "372051",
        "tt0108941",
        "/mnt/media/TV/The Stand (1994)",
    )
    heat = next(i for i in snap.items if i.kind == "movie")
    assert heat.tmdb == "949" and heat.path.endswith("Heat.mkv")
    assert len(server.detail_calls) == 7

    previous = {i.external_id: i for i in snap.items}
    server.detail_calls.clear()
    snap2 = SiloConnector(ConnectorConfig("http://silo", "k"), client).fetch_full(previous=previous)
    # series details are cached; a film's version files are read afresh every time
    assert server.detail_calls and all("movie" in call for call in server.detail_calls)
    assert (
        next(i for i in snap2.items if i.external_id == "2/series-tvdb-372051").imdb == "tt0108941"
    )


@pytest.fixture
def world(app):
    """Both fake servers ingested, grouped and matched."""
    with app.state.db.sessionmaker() as db:
        jf = Source(type="jellyfin", name="Jellyfin", base_url="http://jf")
        silo = Source(type="silo", name="Silo", base_url="http://silo")
        db.add_all([jf, silo])
        db.commit()
        jf_snap = JellyfinConnector(
            ConnectorConfig("http://jf", "k"),
            httpx.Client(transport=httpx.MockTransport(jellyfin_handler())),
        ).fetch_full()
        silo_snap = SiloConnector(
            ConnectorConfig("http://silo", "k"),
            httpx.Client(transport=httpx.MockTransport(SiloServer())),
        ).fetch_full()
        apply_snapshot(db, jf, jf_snap)
        apply_snapshot(db, silo, silo_snap)
        ensure_groups(db)
        run_matching(db, source_order(db))
        yield db, jf, silo, jf_snap, silo_snap


def _group(db, name):
    return db.execute(select(LibraryGroup).where(LibraryGroup.name == name)).scalar_one()


def test_groups_from_master_and_folder_name_mapping(world):
    db, jf, silo, *_ = world
    assert get_master(db).id == jf.id
    groups = {g.name: g for g in db.execute(select(LibraryGroup)).scalars()}
    assert set(groups) == {"TV", "Anime", "Movies", "Sport"}  # boxsets ignored
    assert not groups["Sport"].presence_enabled
    members = db.execute(
        select(Library.name, Library.source_id, LibraryGroupMember.group_id).join(
            LibraryGroupMember
        )
    ).all()
    # Silo "Shows" (/mnt/media/TV) joins Jellyfin "TV" (/data/TV) by folder name.
    assert ("Shows", silo.id, groups["TV"].id) in members
    assert ("Anime", silo.id, groups["Anime"].id) in members


def test_presence_matrix_tv(world):
    db, jf, silo, *_ = world
    view = build_group_view(db, _group(db, "TV"))
    assert [c.id for c in view.columns] == [jf.id, silo.id]
    rows = {(r.canonical.title, r.canonical.year): r for r in view.rows}

    sherlock = rows[("Sherlock", 2010)]
    assert sherlock.cells[jf.id].episodes == 3 and sherlock.cells[jf.id].specials == 1
    assert sherlock.cells[silo.id].episodes == 2 and sherlock.cells[silo.id].delta == -1
    assert "episodes" in sherlock.flags
    # Specials are compared separately: Jellyfin has one, Silo none.
    assert sherlock.cells[silo.id].specials_delta == -1 and "specials" in sherlock.flags

    # Virtual (missing) Jellyfin episodes don't count.
    poi = rows[("Person of Interest", 2011)]
    assert poi.cells[jf.id].episodes == 1 and not poi.cells[silo.id].delta

    # The Stand: each side in one app, flagged as a possible mismatch.
    assert f"missing:{silo.id}" in rows[("The Stand", 1994)].flags
    assert "not_in_master" in rows[("The Stand", 2020)].flags
    assert len(view.possible_mismatches) == 1
    assert view.summaries[silo.id].missing == 1 and view.summaries[silo.id].extra == 1


def test_presence_anime_low_confidence_and_one_pace(world):
    db, jf, silo, *_ = world
    view = build_group_view(db, _group(db, "Anime"))
    low = filter_rows(view, "low")
    assert [r.canonical.title for r in low] == ["Prison School"]
    not_in_master = filter_rows(view, "not_in_master")
    assert sorted(r.canonical.title for r in not_in_master) == [
        "[One Pace][1-3] Romance Dawn [1080p]",
        "[One Pace][4-7] Orange Town [1080p]",
    ]
    assert view.summaries[silo.id].low == 1
    # Unconfirmed doesn't count as present.
    assert view.summaries[silo.id].present == 0


def test_movies_match_via_detail_ids(world):
    db, jf, silo, *_ = world
    view = build_group_view(db, _group(db, "Movies"))
    (row,) = view.rows
    assert row.cells[silo.id].state == "present" and not row.flags


def test_one_file_matched_to_two_films_is_a_possible_mismatch(world):
    """Jellyfin read the film's file as another title: the rows share no name, only the file."""
    db, jf, silo, *_ = world
    films = select(SourceItem).where(SourceItem.kind == "movie")
    jf_heat = db.execute(films.where(SourceItem.source_id == jf.id)).scalar_one()
    silo_heat = db.execute(films.where(SourceItem.source_id == silo.id)).scalar_one()
    wrong = CanonicalItem(key="movie:imdb:tt0", kind="movie", title="Cool Down")
    db.add(wrong)
    db.flush()
    jf_heat.canonical_id = wrong.id
    jf.path_map = [[jf_heat.path.rsplit("/", 1)[0], "/share"]]
    silo.path_map = [[silo_heat.path.rsplit("/", 1)[0], "/share/other"]]
    db.commit()
    view = build_group_view(db, _group(db, "Movies"))
    assert not any("mismatch" in r.flags for r in view.rows)  # different files
    silo.path_map = [[silo_heat.path.rsplit("/", 1)[0], "/share"]]
    db.commit()
    view = build_group_view(db, _group(db, "Movies"))
    assert sorted(r.canonical.title for r in view.rows if "mismatch" in r.flags) == [
        "Cool Down",
        "Heat",
    ]
    assert len(view.possible_mismatches) == 1
    refs = references(db, jf_heat)  # the search names Silo's version though the titles differ
    assert [(r.source, r.title) for r in refs] == [("Silo", silo_heat.title)]


def test_resync_marks_removed_and_keeps_details(world):
    db, jf, silo, jf_snap, silo_snap = world
    silo_snap.items = [i for i in silo_snap.items if i.external_id != "2/series-tvdb-176941"]
    stats = apply_snapshot(db, silo, silo_snap)
    assert stats.removed == 1 and stats.added == 0
    gone = db.execute(
        select(SourceItem).where(SourceItem.external_id == "2/series-tvdb-176941")
    ).scalar_one()
    assert gone.removed_at is not None
    prev = previous_records(db, silo)
    assert "2/series-tvdb-176941" not in prev
    assert prev["2/series-tvdb-372051"].details_fetched


def test_a_full_read_keeps_a_removal_made_while_it_ran(world):
    from datetime import timedelta

    from mediawrangler.db import utcnow

    db, jf, silo, jf_snap, silo_snap = world
    row = (
        db.execute(
            select(MediaFile).where(MediaFile.source_id == jf.id, MediaFile.removed_at.is_(None))
        )
        .scalars()
        .first()
    )
    began = utcnow()
    row.removed_at = began + timedelta(seconds=1)  # a delete confirmed during the read
    db.commit()
    apply_snapshot(db, jf, jf_snap, read_from=began)
    db.refresh(row)
    assert row.removed_at is not None
    # A read that began after the removal is believed.
    apply_snapshot(db, jf, jf_snap, read_from=row.removed_at + timedelta(seconds=1))
    db.refresh(row)
    assert row.removed_at is None


def test_switching_master_regroups_nothing_but_flips_missing(world):
    db, jf, silo, *_ = world
    set_master(db, silo.id)
    run_matching(db, source_order(db))
    view = build_group_view(db, _group(db, "TV"))
    assert view.master_id == silo.id
    rows = {(r.canonical.title, r.canonical.year): r for r in view.rows}
    assert f"missing:{jf.id}" in rows[("The Stand", 2020)].flags


def test_presence_page_and_decision(logged_in, app, world):
    db, jf, silo, *_ = world
    anime = _group(db, "Anime")
    page = logged_in.get(f"/presence?group={anime.id}&filter=low")
    assert page.status_code == 200 and "Prison School" in page.text and "Unconfirmed" in page.text

    prison = db.execute(
        select(SourceItem).where(SourceItem.external_id == "3/local-prison")
    ).scalar_one()
    from .conftest import post

    resp = post(
        logged_in,
        "/presence/decide",
        {"item_id": prison.id, "decision": "confirm", "back": f"/presence?group={anime.id}"},
    )
    assert resp.status_code == 303
    db.expire_all()
    view = build_group_view(db, anime)
    assert not filter_rows(view, "low")
    assert view.summaries[silo.id].present == 1

    overview = logged_in.get("/")
    assert overview.status_code == 200 and "Metadata" in overview.text
    assert logged_in.get("/settings/groups").status_code == 200
    assert logged_in.get("/settings/general", follow_redirects=False).status_code == 303


def test_sync_manager_runs_a_sync(app, monkeypatch):
    with app.state.db.sessionmaker() as db:
        src = Source(
            type="jellyfin",
            name="Jellyfin",
            base_url="http://jf",
            secret_enc=app.state.box.encrypt("k"),
        )
        db.add(src)
        db.commit()
        sid = src.id

    real_init = JellyfinConnector.__init__

    def fake_init(self, config, client=None):
        real_init(self, config, httpx.Client(transport=httpx.MockTransport(jellyfin_handler())))

    monkeypatch.setattr(JellyfinConnector, "__init__", fake_init)
    syncs = app.state.syncs
    run_id = syncs.start(sid)
    assert syncs.start(sid) == run_id  # already running or just finished: no duplicate queued
    syncs._pool.shutdown(wait=True)
    from mediawrangler.models import SyncRun

    with app.state.db.sessionmaker() as db:
        run = db.get(SyncRun, run_id)
        assert run.status == "ok", run.error
        assert run.stats["by_kind"]["series"] == 5
        assert db.execute(select(LibraryGroup)).scalars().first() is not None


def test_silo_versions_go_to_the_library_holding_them():
    from mediawrangler.connectors.silo import SiloConnector

    detail = {
        "imdb_id": "tt0113277",
        "versions": [
            {"file_path": "/mnt/media/Movies/Heat (1995)/Heat.mkv"},
            {"file_path": "/mnt/media/Movies4K/Heat (1995)/Heat 2160p.mkv"},
        ],
    }
    libs = [
        {"id": "1", "name": "Movies", "type": "movies", "paths": ["/mnt/media/Movies"]},
        {"id": "5", "name": "Movies 4K", "type": "movies", "paths": ["/mnt/media/Movies4K"]},
    ]
    card = {"content_id": "movie-imdb-tt0113277", "title": "Heat", "year": 1995, "type": "movie"}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v2/profiles":
            return httpx.Response(200, json={"items": [{"id": "p", "is_primary": True}]})
        if path == "/api/v2/libraries":
            return httpx.Response(200, json={"items": libs})
        if path == "/api/v2/catalog":
            return httpx.Response(200, json={"items": [card], "page": {"has_more": False}})
        if path.startswith("/api/v2/catalog/items/"):
            return httpx.Response(200, json=detail)
        return httpx.Response(200, json={"items": [], "page": {"has_more": False}})

    snap = SiloConnector(
        ConnectorConfig("http://silo", "k"), httpx.Client(transport=httpx.MockTransport(handler))
    ).fetch_full()
    by_path = {f.path: f.library_external_id for f in snap.files}
    assert by_path["/mnt/media/Movies/Heat (1995)/Heat.mkv"] == "1"
    assert by_path["/mnt/media/Movies4K/Heat (1995)/Heat 2160p.mkv"] == "5"


def test_presence_page_has_every_row_and_instant_filters(world, logged_in):
    db, jf, silo, *_ = world
    tv = _group(db, "TV")
    view = build_group_view(db, tv)
    page = logged_in.get(f"/presence?group={tv.id}")
    options = filter_options(page.text)
    assert options[0] == ("issues", "All issues", True) and options[-1][0] == "all"
    rows = table_rows(page.text)
    assert len(rows) == len(view.rows) > 3
    by_search = {search: (flags, hidden) for flags, search, hidden in rows}
    # Titles with an issue are shown, clean ones are only hidden (still on the page).
    assert by_search["person of interest"][1] is True  # agrees everywhere: hidden
    assert by_search["sherlock"][1] is False  # episode count differs: shown
    not_in_master = [(f, h) for f, s, h in rows if "not_in_master" in f.split()]
    assert len(not_in_master) == 1 and not not_in_master[0][1]  # The Stand (2020)
    # The Stand (1994): in the master only, so "missing" in Silo, per app and overall.
    (missing_row,) = [f.split() for f, _s, _h in rows if "missing" in f.split()]
    assert f"missing:{silo.id}" in missing_row
    assert "Previous" not in page.text and "Page 1 of" not in page.text
    # Straight to a filter, with an app, from the address.
    missing = logged_in.get(f"/presence?group={tv.id}&filter=missing&source={silo.id}")
    assert [s for f, s, h in table_rows(missing.text) if not h] == ["the stand stand"]
    everything = logged_in.get(f"/presence?group={tv.id}&filter=all")
    assert not any(h for _f, _s, h in table_rows(everything.text))
    # Old paged links still work, and nothing relies on an inline handler.
    assert logged_in.get(f"/presence?group={tv.id}&page=2").status_code == 200
    assert "onchange=" not in page.text and 'src="/static/table-filter.js' in page.text


def test_presence_has_no_separate_mismatch_card(world, logged_in):
    db, *_ = world
    tv = _group(db, "TV")
    for flt in ("issues", "all", "low"):
        page = logged_in.get(f"/presence?group={tv.id}&filter={flt}")
        assert "<h2>Possible mismatches</h2>" not in page.text


def test_presence_decision_returns_to_the_live_filter(world, logged_in):
    db, *_ = world
    anime = _group(db, "Anime")
    page = logged_in.get(f"/presence?group={anime.id}&filter=low")
    # The return address is also refreshed by the script when the form is sent.
    assert '<input type="hidden" name="back" value="/presence?group=' in page.text
    prison = db.execute(
        select(SourceItem).where(SourceItem.external_id == "3/local-prison")
    ).scalar_one()
    resp = post(
        logged_in,
        "/presence/decide",
        {
            "item_id": prison.id,
            "decision": "confirm",
            "back": f"/presence?group={anime.id}&filter=all&q=prison",
        },
    )
    assert resp.headers["location"] == f"/presence?group={anime.id}&filter=all&q=prison"


def test_presence_views_are_cached_and_overview_reuses_them(world, logged_in, app):
    db, *_ = world
    tv = _group(db, "TV")
    logged_in.get("/")
    cached = app.state.views._entries[("presence", tv.id)][1]
    logged_in.get(f"/presence?group={tv.id}")
    assert app.state.views._entries[("presence", tv.id)][1] is cached
    app.state.syncs.bump()
    logged_in.get(f"/presence?group={tv.id}")
    assert app.state.views._entries[("presence", tv.id)][1] is not cached


def test_presence_summary_shows_difference_from_the_master(world, logged_in):
    db, *_ = world
    anime = logged_in.get(f"/presence?group={_group(db, 'Anime').id}").text
    # Jellyfin (master) has 2 titles, Silo 3: one more.
    assert summary_col(anime, "Jellyfin").startswith("★ Jellyfin Jellyfin Total 2")
    silo = summary_col(anime, "Silo")
    assert silo.startswith("Silo Silo Total 3 +1") and 'class="diff warn"' in anime
    assert "1 more than Jellyfin" in anime
    # The directional counts carry their sign too: titles Silo has that the master doesn't.
    assert "Missing −1" in silo  # One Piece, in the master only
    assert "Not in master +2" in silo  # the two One Pace folders
    tv = logged_in.get(f"/presence?group={_group(db, 'TV').id}").text
    assert summary_col(tv, "Silo").startswith("Silo Silo Total 3 ✓")  # same count as the master
    assert "same as Jellyfin" in tv
    assert "compare each app with" not in tv


def test_overview_shows_difference_from_the_master(world, logged_in):
    db, jf, silo, *_ = world
    page = logged_in.get("/").text
    row = next(r for r in page.split("<tr>") if f"/settings/connectors/{silo.id}" in r)
    cells = [plain(c) for c in row.split("<td")[1:]]
    assert cells[1].endswith("6 +1")  # series: Silo 6 vs Jellyfin 5
    jf_row = next(r for r in page.split("<tr>") if f"/settings/connectors/{jf.id}" in r)
    assert "diff" not in jf_row.split("<td")[2]  # the master itself shows no badge
    # Present in the presence table is compared with what the master has.
    # TV: 2 of the master's 3 titles are present (The Stand is mismatched): 1 fewer.
    # Anime: 0 of the master's 2 are present: 2 fewer.
    assert "1 fewer than Jellyfin" in page and "2 fewer than Jellyfin" in page


def test_a_title_match_is_confirmed_when_the_episodes_are_the_same(world):
    db, jf, silo, *_ = world

    def show(source, ext, title, numbers):
        db.add(SourceItem(source_id=source.id, external_id=ext, kind="series", title=title))
        for season, episode in numbers:
            db.add(
                SourceItem(
                    source_id=source.id,
                    external_id=f"{ext}-{season}-{episode}",
                    kind="episode",
                    title=f"Match {episode}",
                    parent_external_id=ext,
                    season=season,
                    episode=episode,
                )
            )

    cup = [(2026, 1), (2026, 2), (2026, 3)]
    show(jf, "jf-cup", "Zzz Sport Cup", cup)
    show(silo, "silo-cup", "Zzz Sport Cup", cup)
    show(jf, "jf-liga", "Zzz Sport Liga", [(2026, 1), (2026, 2)])
    show(silo, "silo-liga", "Zzz Sport Liga", [(2026, 1), (2026, 5)])
    db.commit()
    run_matching(db, source_order(db))

    def row(ext):
        return db.execute(select(SourceItem).where(SourceItem.external_id == ext)).scalar_one()

    assert (row("silo-cup").match_method, row("silo-cup").match_confidence) == ("episodes", "high")
    assert row("silo-cup").canonical_id == row("jf-cup").canonical_id
    assert row("silo-liga").match_confidence == "low"  # the episodes differ: still unconfirmed
