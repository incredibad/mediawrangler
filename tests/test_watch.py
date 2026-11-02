"""Users, the user map, watch-state ingest, change log and the per-user diff."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from mediawrangler.connectors import ConnectorConfig
from mediawrangler.connectors.base import parse_time
from mediawrangler.connectors.jellyfin import JellyfinConnector
from mediawrangler.connectors.silo import SiloConnector
from mediawrangler.models import (
    AppUser,
    LibraryGroup,
    Source,
    SourceUser,
    SyncRun,
    WatchEvent,
    WatchState,
)
from mediawrangler.watchdiff import build_watch_view, filter_rows, series_detail

from . import fakes
from .conftest import filter_options, post, summary_col, table_rows
from .fakes import SiloServer, jellyfin_handler


def _mock(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_parse_time():
    assert parse_time("2026-02-22T11:37:48.0000000Z").isoformat() == "2026-02-22T11:37:48"
    assert parse_time("2026-10-17T14:53:40.3536414Z").microsecond == 353641
    assert parse_time("2026-10-25T23:59:21.000Z").hour == 23
    assert parse_time("2026-10-25T23:59:21+10:00").hour == 13
    assert parse_time(None) is None and parse_time("nope") is None


def test_jellyfin_watch_reads_played_and_resumable():
    conn = JellyfinConnector(ConnectorConfig("http://jf", "k"), _mock(jellyfin_handler()))
    (alex,) = [u for u in conn.users() if u.name == "alex"]
    recs = {r.item_external_id: r for r in conn.fetch_watch(alex)}
    assert recs["jf-sh-1"].played and recs["jf-sh-1"].duration_seconds == 3600
    assert not recs["jf-poi-1"].played and recs["jf-poi-1"].position_seconds == 600


def test_silo_users_only_own_account_readable():
    conn = SiloConnector(ConnectorConfig("http://silo", "k"), _mock(SiloServer()))
    conn._ensure_profile()
    users = {u.name: u for u in conn.users()}
    assert users["alex"].watch_readable and not users["Jordan"].watch_readable
    recs = conn.fetch_watch(users["alex"])
    assert {r.item_external_id for r in recs} == {"episode-tvdb-176941-1-1", "movie-imdb-tt0113277"}
    assert conn._profile_id == "p-1"  # restored after acting as the profile


@pytest.fixture
def synced(app, monkeypatch):
    from mediawrangler.connectors.base import Connector

    handlers = {"jellyfin": jellyfin_handler, "silo": SiloServer}
    real_init = Connector.__init__

    def fake_init(self, config, client=None):
        real_init(self, config, _mock(handlers[self.type]()))

    monkeypatch.setattr(Connector, "__init__", fake_init)
    box = app.state.box
    with app.state.db.sessionmaker() as db:
        for typ, name in (("jellyfin", "Jellyfin"), ("silo", "Silo")):
            db.add(
                Source(type=typ, name=name, base_url=f"http://{typ}", secret_enc=box.encrypt("k"))
            )
        db.commit()

    def sync_all():
        syncs = app.state.syncs
        with app.state.db.sessionmaker() as db:
            ids = [s.id for s in db.execute(select(Source).order_by(Source.id)).scalars()]
        runs = [syncs.start(i) for i in ids]
        syncs._pool.shutdown(wait=True)
        syncs._pool = ThreadPoolExecutor(1)
        with app.state.db.sessionmaker() as db:
            out = []
            for rid in runs:
                run = db.get(SyncRun, rid)
                assert run.status == "ok", run.error
                out.append(run.stats)
            return out

    stats = sync_all()
    with app.state.db.sessionmaker() as db:
        yield db, sync_all, stats


def _person(db, name):
    return db.execute(select(AppUser).where(AppUser.name == name)).scalar_one()


def _group(db, name):
    return db.execute(select(LibraryGroup).where(LibraryGroup.name == name)).scalar_one()


def _source_user(db, source_type, name):
    return db.execute(
        select(SourceUser).join(Source).where(Source.type == source_type, SourceUser.name == name)
    ).scalar_one()


def test_users_linked_by_name_and_watch_stored(synced):
    db, _, stats = synced
    jf_stats, silo_stats = stats
    assert jf_stats["watch"]["users"] == 2 and jf_stats["watch"]["played"] == 5
    assert silo_stats["watch"]["readable"] == 1  # Jordan's Silo profile isn't readable
    alex = _person(db, "alex")
    links = db.execute(select(SourceUser).where(SourceUser.app_user_id == alex.id)).scalars().all()
    assert len(links) == 2
    assert {p.name for p in db.execute(select(AppUser)).scalars()} == {"alex", "Jordan"}
    # The first read is the baseline: nothing logged.
    assert db.execute(select(WatchEvent)).first() is None


def test_watch_diff_series(synced):
    db, *_ = synced
    view = build_watch_view(db, _person(db, "alex"), _group(db, "TV"))
    jf, silo = (c.source.id for c in view.columns if c.source.type != "local")
    rows = {r.canonical.title: r for r in view.rows}
    sherlock = rows["Sherlock"]
    # Specials count with the regular episodes: two episodes and the special.
    assert sherlock.cells[jf].played == 3 and sherlock.cells[jf].specials == 1
    # S01E02 is played in Jellyfin only; Silo has the episode, so it counts.
    assert sherlock.cells[silo].played == 1 and sherlock.cells[silo].missing == 1
    # Silo has no S00E01, so the special isn't a difference.
    assert sherlock.cells[silo].specials == 0
    assert "differs" in sherlock.flags
    # The source with the most played is the one to apply from, and Local wins
    # a tie with the master: it is the record the others are brought up to.
    assert sherlock.apply_from.column.source.type == "local" and sherlock.apply_from.played == 3
    assert sherlock.apply_from.second.source.id == jf
    poi = rows["Person of Interest"]
    assert poi.cells[jf].in_progress == 1 and "in_progress" in poi.flags
    assert [r.canonical.title for r in filter_rows(view, "diffs")] == ["Sherlock"]
    # Every title in the group is listed, watched or not; "watched" narrows it.
    assert "The Stand" in {r.canonical.title for r in view.rows}
    assert "The Stand" not in {r.canonical.title for r in filter_rows(view, "watched")}

    lines = {(ln.season, ln.episode): ln for ln in series_detail(db, view, sherlock.canonical.id)}
    assert lines[(1, 2)].differs and not lines[(1, 1)].differs
    assert lines[(0, 1)].cells[silo][0] is False  # not in Silo


def test_watch_diff_movies_agree(synced):
    db, *_ = synced
    view = build_watch_view(db, _person(db, "alex"), _group(db, "Movies"))
    (heat,) = view.rows
    assert all(c.played for c in heat.cells.values()) and "differs" not in heat.flags


def test_movie_watched_in_any_copy_counts(synced):
    """Jellyfin keeps 1080p and 4K copies as separate items; watching the 4K one
    means the film is watched, also in the Movies group."""
    from mediawrangler.models import Library, SourceItem

    db, *_ = synced
    jf_src = db.execute(select(Source).where(Source.type == "jellyfin")).scalar_one()
    heat = db.execute(
        select(SourceItem).where(
            SourceItem.source_id == jf_src.id, SourceItem.external_id == "jf-heat"
        )
    ).scalar_one()
    lib4k = Library(source_id=jf_src.id, external_id="jf-lib-4k", name="Movies 4K", kind="movie")
    db.add(lib4k)
    db.flush()
    db.add(
        SourceItem(
            source_id=jf_src.id,
            external_id="jf-heat-4k",
            library_id=lib4k.id,
            kind="movie",
            title="Heat",
            year=1995,
            canonical_id=heat.canonical_id,
        )
    )
    jf_tim = _source_user(db, "jellyfin", "alex")
    db.execute(
        WatchState.__table__.update()
        .where(WatchState.source_user_id == jf_tim.id, WatchState.item_external_id == "jf-heat")
        .values(item_external_id="jf-heat-4k")
    )
    db.commit()
    view = build_watch_view(db, _person(db, "alex"), _group(db, "Movies"))
    (row,) = view.rows
    assert all(c.played for c in row.cells.values()) and "differs" not in row.flags


def test_unreadable_silo_profile_reported(synced):
    db, *_ = synced
    view = build_watch_view(db, _person(db, "Jordan"), _group(db, "TV"))
    assert [c.source.name for c in view.columns] == ["Jellyfin", "Local"]
    assert [su.name for _src, su in view.unreadable] == ["Jordan"]


def age_holds(db, minutes=11):
    """Make every played item a read left out look like it was first missed a while ago."""
    for w in db.execute(select(WatchState).where(WatchState.missing_since.is_not(None))).scalars():
        w.missing_since -= timedelta(minutes=minutes)
    db.commit()


def test_change_log_on_second_sync(synced, monkeypatch):
    db, sync_all, _ = synced
    tim_watch = [w for w in fakes.JF_WATCH["u-alex"] if w["Id"] != "jf-sh-2"]  # unwatched
    tim_watch.append(fakes._jf_watch("jf-sh-3"))  # newly watched
    monkeypatch.setitem(fakes.JF_WATCH, "u-alex", tim_watch)
    sync_all()
    db.expire_all()
    events = {(e.item_external_id, e.kind) for e in db.execute(select(WatchEvent)).scalars()}
    assert events == {("jf-sh-3", "played")}  # the missing one waits for a second read
    sync_all()  # straight away again: too soon to confirm
    db.expire_all()
    assert {
        (e.item_external_id, e.kind) for e in db.execute(select(WatchEvent)).scalars()
    } == events
    age_holds(db)
    sync_all()
    db.expire_all()
    events = {(e.item_external_id, e.kind) for e in db.execute(select(WatchEvent)).scalars()}
    assert events == {("jf-sh-2", "unplayed"), ("jf-sh-3", "played")}
    jf_tim = _source_user(db, "jellyfin", "alex")
    stored = {
        w.item_external_id
        for w in db.execute(
            select(WatchState).where(WatchState.source_user_id == jf_tim.id)
        ).scalars()
    }
    assert "jf-sh-2" not in stored and "jf-sh-3" in stored


def test_deleted_item_is_not_an_unwatch(synced, monkeypatch):
    db, sync_all, _ = synced
    items = dict(fakes.JF_ITEMS)
    items[("jf-lib-movies", "Movie")] = []  # Heat's file deleted
    monkeypatch.setattr(fakes, "JF_ITEMS", items)
    tim_watch = [w for w in fakes.JF_WATCH["u-alex"] if w["Id"] != "jf-heat"]
    monkeypatch.setitem(fakes.JF_WATCH, "u-alex", tim_watch)
    sync_all()
    db.expire_all()
    assert not db.execute(
        select(WatchEvent).where(WatchEvent.item_external_id == "jf-heat")
    ).first()


def test_relinking_a_user(synced, logged_in):
    db, *_ = synced
    al_jf = _source_user(db, "jellyfin", "Jordan")
    alex = _person(db, "alex")
    resp = post(logged_in, f"/settings/users/link/{al_jf.id}", {"app_user_id": str(alex.id)})
    assert resp.status_code == 303
    db.expire_all()
    su = db.get(SourceUser, al_jf.id)
    assert su.app_user_id == alex.id and su.link_assigned


def test_watch_pages_render(synced, logged_in):
    db, *_ = synced
    alex, tv, movies = _person(db, "alex"), _group(db, "TV"), _group(db, "Movies")
    for flt in ("all", "watched", "diffs", "in_progress", "elsewhere"):
        resp = logged_in.get(f"/watch?user={alex.id}&group={tv.id}&filter={flt}")
        assert resp.status_code == 200, flt
    page = logged_in.get(f"/watch?user={alex.id}&group={tv.id}")
    assert "Sherlock" in page.text and "−1" in page.text
    # The difference sits right after Silo's total, colour coded, with a tooltip.
    assert 'class="delta bad"' in page.text and "played in Jellyfin but not here" in page.text
    assert 'class="pbar some' in page.text and "Apply from" in page.text
    assert logged_in.get(f"/watch?user={alex.id}&group={movies.id}&filter=all").status_code == 200
    view = build_watch_view(db, alex, tv)
    sherlock = next(r for r in view.rows if r.canonical.title == "Sherlock")
    url = f"/watch/series?user={alex.id}&group={tv.id}&canonical={sherlock.canonical.id}"
    detail = logged_in.get(url)
    assert detail.status_code == 200 and "S01E02" in detail.text
    assert logged_in.get("/settings/users").status_code == 200


def test_filter_order_and_default():
    from mediawrangler.watchdiff import DEFAULT_FILTER, FILTERS

    keys = list(FILTERS)
    assert keys[0] == "diffs" and keys[-1] == "all"
    assert FILTERS["diffs"].label == "All differences" and FILTERS["all"].label == "All titles"
    assert DEFAULT_FILTER == "diffs"


def test_all_differences_can_be_narrowed_to_one_app(synced):
    db, *_ = synced
    view = build_watch_view(db, _person(db, "alex"), _group(db, "Anime"))
    everything = {r.canonical.title for r in filter_rows(view, "diffs")}
    silo = view.columns[1].source.id
    # A specific app narrows every flag in the filter to that app.
    assert {r.canonical.title for r in filter_rows(view, "diffs", source_id=silo)} == everything


def test_search_folding():
    from mediawrangler.filters import fold, search_text

    assert fold("Pokémon") == "pokemon"
    assert fold("Law & Order: SVU") == "law and order svu"
    assert search_text("The Stand") == "the stand stand"
    assert search_text("Sherlock") == "sherlock"


def test_watch_page_lists_every_row_and_hides_the_rest(synced, logged_in):
    db, *_ = synced
    alex, tv = _person(db, "alex"), _group(db, "TV")
    page = logged_in.get(f"/watch?user={alex.id}&group={tv.id}")
    # All differences is first and the default; All titles is last.
    options = filter_options(page.text)
    assert options[0] == ("diffs", "All differences", True)
    assert options[-1] == ("all", "All titles", False)
    assert [o[0] for o in options] == [
        "diffs",
        "differs_local",
        "in_progress",
        "watched",
        "elsewhere",
        "all",
    ]
    # Every title is in the page; those the filter excludes are only hidden.
    rows = table_rows(page.text)
    assert len(rows) == len(build_watch_view(db, alex, tv).rows) >= 3
    by_search = {search: (flags, hidden) for flags, search, hidden in rows}
    assert by_search["the stand stand"][1] is True  # unwatched everywhere: hidden
    sherlock_flags, sherlock_hidden = by_search["sherlock"]
    assert not sherlock_hidden and "differs" in sherlock_flags
    assert any(f.startswith("differs:") for f in sherlock_flags.split())  # per-app flag
    # No pager: all records are viewable at once.
    assert "Previous" not in page.text and "Page 1 of" not in page.text
    assert f"{sum(1 for r in rows if not r[2])} of {len(rows)} titles" in page.text


def test_watch_page_starts_from_the_url(synced, logged_in):
    db, *_ = synced
    alex, tv = _person(db, "alex"), _group(db, "TV")
    everything = logged_in.get(f"/watch?user={alex.id}&group={tv.id}&filter=all")
    assert ("all", "All titles", True) in filter_options(everything.text)
    assert not any(hidden for _f, _s, hidden in table_rows(everything.text))
    searched = logged_in.get(f"/watch?user={alex.id}&group={tv.id}&filter=all&q=sherl")
    shown = [s for _f, s, hidden in table_rows(searched.text) if not hidden]
    assert shown == ["sherlock"]
    # Old links with a page number still work (there are no pages any more).
    assert logged_in.get(f"/watch?user={alex.id}&group={tv.id}&page=3").status_code == 200


def test_watch_page_has_no_inline_handlers(synced, logged_in):
    """The content-security policy blocks inline handlers; behaviour comes from /static."""
    db, *_ = synced
    alex, tv = _person(db, "alex"), _group(db, "TV")
    page = logged_in.get(f"/watch?user={alex.id}&group={tv.id}")
    assert "onchange=" not in page.text and "onclick=" not in page.text
    assert "data-person-form" in page.text and "data-table-filter" in page.text
    assert '<script src="/static/table-filter.js' in page.text
    js = logged_in.get("/static/table-filter.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert "script-src 'self'" in page.headers["content-security-policy"]


def test_view_cache_reuses_until_version_moves():
    from mediawrangler.viewcache import ViewCache

    cache, built = ViewCache(), []

    def build(tag):
        def make():
            built.append(tag)
            return object()

        return make

    first = cache.get(("watch", 1, 2), 1, build("a"))
    assert cache.get(("watch", 1, 2), 1, build("b")) is first and built == ["a"]
    assert cache.get(("watch", 1, 2), 2, build("c")) is not first
    assert built == ["a", "c"]
    small = ViewCache(max_entries=1)
    small.get("x", 1, object)
    small.get("y", 1, object)
    assert len(small._entries) == 1


def test_changes_through_the_ui_invalidate_the_cache(synced, logged_in, app):
    db, *_ = synced
    alex, tv = _person(db, "alex"), _group(db, "TV")
    url = f"/watch?user={alex.id}&group={tv.id}&filter=all"
    before = app.state.syncs.version
    logged_in.get(url)
    assert app.state.syncs.version == before  # reading doesn't invalidate
    cached = app.state.views._entries[("watch", alex.id, tv.id)][1]
    al_jf = _source_user(db, "jellyfin", "Jordan")
    post(logged_in, f"/settings/users/link/{al_jf.id}", {"app_user_id": str(alex.id)})
    assert app.state.syncs.version > before
    logged_in.get(url)
    assert app.state.views._entries[("watch", alex.id, tv.id)][1] is not cached


def test_syncs_invalidate_the_cache(synced, app):
    db, sync_all, _ = synced
    before = app.state.syncs.version
    sync_all()
    assert app.state.syncs.version > before


def test_blank_or_junk_numbers_in_the_address_mean_not_set(synced, logged_in):
    """A form sends `source=` for "Any app"; that used to be a raw 422 JSON error."""
    db, *_ = synced
    alex, tv = _person(db, "alex"), _group(db, "TV")
    urls = [
        "/watch?source=",
        "/watch?user=&group=&filter=diffs&source=&q=",
        f"/watch?user={alex.id}&group={tv.id}&source=abc",
        "/presence?source=",
        "/presence?group=&source=&filter=missing",
        "/physical?source=",
        "/physical?group=&source=&filter=count",
    ]
    for url in urls:
        resp = logged_in.get(url)
        assert resp.status_code == 200, (url, resp.status_code, resp.text[:120])
        assert "int_parsing" not in resp.text
    # A real number still narrows the filter.
    jf_silo = logged_in.get(f"/watch?user={alex.id}&group={tv.id}&filter=diffs&source=999")
    assert jf_silo.status_code == 200


def test_watch_summary_is_a_small_table_per_app(synced, logged_in):
    db, *_ = synced
    alex, tv = _person(db, "alex"), _group(db, "TV")
    page = logged_in.get(f"/watch?user={alex.id}&group={tv.id}").text
    jf, silo = summary_col(page, "Jellyfin"), summary_col(page, "Silo")
    for label in ("Played", "Not played", "Total", "In progress", "Read"):
        assert label in jf and label in silo
    assert jf.index("Not played") < jf.index("Total") < jf.index("In progress")
    assert "In progress 1" in jf and "In progress 0" in silo
    # No +/- numbers or reminder text up here any more.
    assert "compare each app with" not in page and "played only in" not in page


def test_watch_names_apps_as_star_name_type(synced, logged_in):
    db, *_ = synced
    alex, tv = _person(db, "alex"), _group(db, "TV")
    page = logged_in.get(f"/watch?user={alex.id}&group={tv.id}").text
    assert summary_col(page, "Jellyfin").startswith("★ Jellyfin Jellyfin ")
    assert summary_col(page, "Silo").startswith("Silo Silo ")
    assert '<span class="tag">reference</span>' not in page
