"""Yamtrack and Trakt history files: parsing, the extra Watch states column, and upload."""

import csv
import io
import json
import zipfile

import pytest
from sqlalchemy import select

from mediawrangler.animelist import AnimeEntry, AnimeList, fetch, parse, refresh_due
from mediawrangler.importers import (
    ImportFileError,
    apply_import,
    elsewhere,
    parse_trakt,
    parse_yamtrack,
)
from mediawrangler.models import WatchImport
from mediawrangler.watchdiff import build_watch_view

from .conftest import post, table_rows
from .test_watch import _group, _person, synced  # noqa: F401  (fixture)

HEADER = [
    "media_id", "source", "media_type", "title", "image", "season_number", "episode_number",
    "score", "status", "notes", "start_date", "end_date", "progress", "created_at", "progressed_at",
]  # fmt: skip


def _imported(view):
    return [c for c in view.columns if c.source.type == "import"][-1]


def _yamtrack(*rows):
    buf = io.StringIO()
    out = csv.writer(buf, quoting=csv.QUOTE_ALL)
    out.writerow(HEADER)
    for mid, source, kind, title, season, episode, status, end, progress in rows:
        out.writerow([mid, source, kind, title, "", season, episode, "", status, "", "", end,
                      progress, "", ""])  # fmt: skip
    return buf.getvalue()


YAMTRACK = _yamtrack(
    ("19885", "tmdb", "tv", "Sherlock", "", "", "Completed", "2020-01-01 00:00:00+00:00", "3"),
    ("19885", "tmdb", "episode", "Sherlock", "1", "1", "", "2020-01-02 10:00:00+00:00", ""),
    ("19885", "tmdb", "episode", "Sherlock", "1", "3", "", "2020-01-03 10:00:00+00:00", ""),
    ("999", "tmdb", "episode", "Gone Show", "1", "1", "", "2020-01-04 10:00:00+00:00", ""),
    ("1", "tmdb", "movie", "Some Film", "", "", "Completed", "2021-01-01 00:00:00+00:00", "1"),
    ("2", "tmdb", "movie", "Planned Film", "", "", "In progress", "", "0"),
    ("1", "mal", "anime", "Cowboy Bebop", "", "", "In progress", "2015-01-01 00:00:00+00:00", "8"),
)


def _trakt_zip(episodes=(), movies=()):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        history = [
            {
                "type": "episode",
                "watched_at": f"2020-01-0{n}T10:00:00.000Z",
                "episode": {"season": s, "number": e, "ids": {}},
                "show": {"title": t, "year": 2010, "ids": {"tvdb": tvdb, "tmdb": tmdb}},
            }
            for n, (t, tvdb, tmdb, s, e) in enumerate(episodes, 1)
        ]
        zf.writestr("watched-history-1.json", json.dumps(history))
        zf.writestr(
            "watched-movies-1.json",
            json.dumps(
                [
                    {
                        "last_watched_at": "2021-01-01T00:00:00.000Z",
                        "plays": 1,
                        "movie": {"title": t, "year": 2000, "ids": {"tmdb": tmdb, "imdb": None}},
                    }
                    for t, tmdb in movies
                ]
            ),
        )
        zf.writestr("_errors.json", "[]")
    return buf.getvalue()


def test_parse_yamtrack():
    recs = parse_yamtrack(YAMTRACK.encode())
    kinds = [(r.kind, r.title) for r in recs]
    assert kinds.count(("episode", "Sherlock")) == 2 and ("episode", "Gone Show") in kinds
    assert ("movie", "Some Film") in kinds and ("movie", "Planned Film") not in kinds
    (anime,) = [r for r in recs if r.kind == "anime"]
    assert (anime.mal, anime.progress, anime.title) == ("1", 8, "Cowboy Bebop")
    ep = next(r for r in recs if r.episode == 3)
    assert (ep.tmdb, ep.season, ep.watched_at.year) == ("19885", 1, 2020)
    assert [(p.action, p.at.day) for p in ep.plays] == [("yamtrack", 3)]


def test_yamtrack_keeps_every_rewatch():
    csv_text = _yamtrack(
        ("19885", "tmdb", "episode", "Sherlock", "1", "1", "", "2020-01-02 10:00:00+00:00", ""),
        ("19885", "tmdb", "episode", "Sherlock", "1", "1", "", "2021-05-06 10:00:00+00:00", ""),
        ("19885", "tmdb", "episode", "Sherlock", "1", "1", "", "2021-05-06 10:00:00+00:00", ""),
        ("1", "tmdb", "movie", "Some Film", "", "", "Completed", "2021-01-01 00:00:00+00:00", "1"),
        ("1", "tmdb", "movie", "Some Film", "", "", "Repeating", "2022-02-02 00:00:00+00:00", "1"),
    )
    recs = parse_yamtrack(csv_text.encode())
    ep = next(r for r in recs if r.kind == "episode")
    assert [p.at.year for p in ep.plays] == [2020, 2021] and ep.watched_at.year == 2021
    (film,) = [r for r in recs if r.kind == "movie"]  # one film, two plays
    assert len(film.plays) == 2 and film.watched_at.year == 2022


def test_parse_rejects_wrong_files():
    with pytest.raises(ImportFileError):
        parse_yamtrack(b"a,b\n1,2\n")
    with pytest.raises(ImportFileError):
        parse_trakt(b"not a zip")
    with pytest.raises(ImportFileError):
        parse_trakt(_empty_zip())


def _empty_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("user-profile.json", "{}")
    return buf.getvalue()


def test_parse_trakt_dedupes_replays():
    data = _trakt_zip(
        episodes=[("Sherlock", 176941, 19885, 1, 1), ("Sherlock", 176941, 19885, 1, 1)],
        movies=[("Heat", 949)],
    )
    recs = parse_trakt(data)
    assert sorted(r.kind for r in recs) == ["episode", "movie"]
    ep = next(r for r in recs if r.kind == "episode")
    assert ep.tvdb == "176941" and ep.watched_at.day == 2  # the later play
    assert [p.action for p in ep.plays] == ["watch", "watch"]  # ... and both plays are kept


def test_import_is_another_column_and_replaces(synced):  # noqa: F811
    db, *_ = synced
    user1 = _person(db, "user1")
    recs = parse_trakt(
        _trakt_zip(
            episodes=[
                ("Sherlock", 176941, 19885, 1, 1),
                ("Sherlock", 176941, 19885, 1, 3),
                ("Not In Any App", 5, 5, 1, 1),
            ]
        )
    )
    imp = apply_import(db, user1.id, "trakt", "t.zip", recs)
    view = build_watch_view(db, user1, _group(db, "TV"))
    col = _imported(view)
    assert col.source.name == "Trakt" and col.source.type == "import"
    sherlock = next(r for r in view.rows if r.canonical.title == "Sherlock")
    cell = sherlock.cells[col.source.id]
    # Jellyfin played E1, E2 and a special; the file has E1 and E3.
    assert cell.played == 2 and cell.missing == 2 and cell.extra == 1
    assert "differs" in sherlock.flags and f"differs:{col.source.id}" in sherlock.flags
    # A show no app has is a row in the TV list, with "not in app" under every real app.
    orphan = next(r for r in view.rows if r.canonical.title == "Not In Any App")
    assert orphan.canonical.id < 0 and "elsewhere" in orphan.flags and "differs" not in orphan.flags
    assert orphan.cells[col.source.id].played == 1
    # Nothing in the apps, but the file's history is still what would be applied.
    assert orphan.apply_from.column is col
    assert not any(
        orphan.cells[c.source.id].present
        for c in [c for c in view.columns if c.source.type not in ("import", "local")]
    )
    assert "elsewhere" not in sherlock.flags
    imports, rows = elsewhere(db, user1.id, 1_000_000)
    assert [i.id for i in imports] == [imp.id] and rows == []  # nothing is left for the tab

    again = parse_trakt(_trakt_zip(movies=[("Heat", 949)]))
    apply_import(db, user1.id, "trakt", "again.zip", again)
    assert len(db.execute(select(WatchImport)).scalars().all()) == 1


def test_upload_needs_a_person_and_shows_the_column(synced, logged_in):  # noqa: F811
    db, *_ = synced
    page = logged_in.get("/settings/connectors")
    assert page.status_code == 200 and "Choose a person" in page.text
    files = {"file": ("trakt.zip", _trakt_zip(episodes=[("Sherlock", 176941, 19885, 1, 3)]))}
    no_person = logged_in.post(
        "/settings/imports", data={"kind": "trakt", "csrf_token": _csrf(logged_in)}, files=files
    )
    assert no_person.status_code == 400 and "which person" in no_person.text
    bad = logged_in.post(
        "/settings/imports",
        data={
            "person": str(_person(db, "user1").id),
            "kind": "yamtrack",
            "csrf_token": _csrf(logged_in),
        },
        files=files,
    )
    assert bad.status_code == 400 and "Yamtrack" in bad.text
    ok = logged_in.post(
        "/settings/imports",
        data={
            "person": str(_person(db, "user1").id),
            "kind": "trakt",
            "csrf_token": _csrf(logged_in),
        },
        files=files,
        follow_redirects=False,
    )
    assert ok.status_code == 303
    assert "Trakt" in logged_in.get("/settings/connectors").text
    # A Yamtrack file for the same person sits beside it, as its own column.
    yam = {"file": ("y.csv", YAMTRACK.encode())}
    data = {
        "person": str(_person(db, "user1").id),
        "kind": "yamtrack",
        "csrf_token": _csrf(logged_in),
    }
    assert logged_in.post("/settings/imports", data=data, files=yam).status_code == 200  # follows
    assert len(db.execute(select(WatchImport)).scalars().all()) == 2
    both = logged_in.get("/watch?filter=all").text
    assert "Trakt" in both and "Yamtrack" in both
    watch = logged_in.get("/watch?filter=all").text
    assert "file import" in watch and table_rows(watch)
    imp = db.execute(select(WatchImport).where(WatchImport.kind == "trakt")).scalar_one()
    page = logged_in.get(f"/watch/elsewhere?user={_person(db, 'user1').id}").text
    rows = table_rows(page)
    assert rows and all("other" in flags for flags, _s, _h in rows)  # only what can't be placed
    tv = table_rows(logged_in.get("/watch?filter=elsewhere").text)
    assert any(
        "elsewhere" in flags and "gone show" in search and not hid for flags, search, hid in tv
    )
    assert "Not in any app" in logged_in.get("/watch").text
    assert "Not in any app" not in logged_in.get("/settings/connectors").text
    assert post(logged_in, f"/settings/imports/{imp.id}/delete").status_code == 303
    after = logged_in.get("/watch?filter=all").text
    assert "Trakt [" not in after and "Yamtrack" in after  # only that file's data is gone


def _csrf(client):
    from .conftest import csrf_of

    return csrf_of(client)


ANIME_LIST = [
    {"type": "TV", "mal_id": 21, "tvdb_id": 81797, "themoviedb_id": {"tv": 37854},
     "imdb_id": ["tt0388629"], "season": {"tvdb": 1, "tmdb": 1}},
    {"type": "MOVIE", "mal_id": 5, "themoviedb_id": {"movie": 949}, "imdb_id": ["tt0113277"]},
    {"type": "TV", "mal_id": 6, "season": {"tvdb": 1}},  # no ids to place it by
    {"type": "TV", "mal_id": 9, "tvdb_id": 81797, "themoviedb_id": {"tv": 37854}},  # no season
    {"type": "OVA", "mal_id": 10, "tvdb_id": 81797},  # no season, and not a TV series
    {"type": "TV", "mal_id": [7, 8], "tvdb_id": [111], "themoviedb_id": 222, "season": {"tvdb": 2}},
]  # fmt: skip


def test_anime_list_parse_tolerates_the_lists_shapes():
    entries = parse(ANIME_LIST)
    assert entries["21"] == AnimeEntry("TV", "81797", 1, "37854", None, ("tt0388629",))
    assert entries["5"].tmdb_movie == "949" and entries["8"].tvdb == "111"
    assert entries["8"].tmdb_tv == "222" and entries["8"].tvdb_season == 2


def _anime_csv(*rows):
    when = "2020-01-01 00:00:00+00:00"
    return _yamtrack(*[(m, "mal", "anime", t, "", "", st, when, p) for m, t, st, p in rows])


def test_a_whole_show_entry_without_a_season_is_season_one():
    from mediawrangler.importers import ImportRec, anime_target

    anime = parse(ANIME_LIST)
    tv = anime_target(anime, ImportRec("anime", "x", mal="9", progress=4))
    assert tv[0] == "series" and tv[1].season == 1 and tv[1].progress == 4
    assert anime_target(anime, ImportRec("anime", "x", mal="10", progress=1)) is None
    assert anime_target(anime, ImportRec("anime", "x", mal="6", progress=1)) is None


def test_anime_progress_counts_as_the_first_episodes(synced):  # noqa: F811
    db, *_ = synced
    user1 = _person(db, "user1")
    recs = parse_yamtrack(
        _anime_csv(
            ("21", "One Piece", "In progress", "3"),
            ("6", "No Ids", "Completed", "12"),
            ("999", "Unlisted", "Completed", "12"),
            ("5", "Heat", "Completed", "1"),
        ).encode()
    )
    imp = apply_import(db, user1.id, "yamtrack", "y.csv", recs)
    anime = parse(ANIME_LIST)
    view = build_watch_view(db, user1, _group(db, "Anime"), anime)
    col = _imported(view)
    one_piece = next(r for r in view.rows if r.canonical.title == "One Piece")
    assert one_piece.cells[col.source.id].played == 3
    # Without the list, nothing is guessed.
    bare = build_watch_view(db, user1, _group(db, "Anime"))
    op = next(r for r in bare.rows if r.canonical.title == "One Piece")
    assert op.cells[_imported(bare).source.id].played == 0
    movies = build_watch_view(db, user1, _group(db, "Movies"), anime)
    heat = next(r for r in movies.rows if r.canonical.title == "Heat")
    assert heat.cells[_imported(movies).source.id].played == 1
    imports, rows = elsewhere(db, user1.id, 1_000_000, anime)
    left = {r.title: r.kind for r in rows}
    assert left == {"No Ids": "other", "Unlisted": "other"}
    assert imports[0].id == imp.id


def test_anime_list_download_keeps_the_old_copy_on_failure(app, tmp_path):
    import httpx

    big = [{"type": "TV", "mal_id": i, "tvdb_id": i, "season": {"tvdb": 1}} for i in range(1, 1500)]
    good = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=big)))
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    lst = AnimeList(tmp_path)
    db = app.state.db
    assert fetch(db, lst, good) and len(lst.entries()) == 1499
    assert not fetch(db, lst, bad) and len(lst.entries()) == 1499  # old copy kept
    tiny = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    assert not fetch(db, lst, tiny) and len(lst.entries()) == 1499


def test_anime_list_refreshes_daily(app, tmp_path, monkeypatch):
    from datetime import timedelta

    from mediawrangler import animelist
    from mediawrangler.db import utcnow

    calls = []
    monkeypatch.setattr(animelist, "fetch", lambda db, lst, client=None: calls.append(1) or True)
    lst = AnimeList(tmp_path)
    db = app.state.db
    now = utcnow()
    assert refresh_due(db, lst, now) and calls  # never downloaded
    animelist._record(db, fetched_at=now.isoformat(), attempted_at=now.isoformat(), error=None)
    lst.path.write_text("[]")
    calls.clear()
    assert not refresh_due(db, lst, now + timedelta(hours=20)) and not calls
    assert refresh_due(db, lst, now + timedelta(hours=25)) and calls
    # A failure is retried after hours, not on every check.
    animelist._record(db, attempted_at=now.isoformat(), error="boom")
    calls.clear()
    assert not refresh_due(db, lst, now + timedelta(hours=1)) and not calls
    assert refresh_due(db, lst, now + timedelta(hours=7)) and calls


def test_settings_page_copes_with_an_import_stored_before_anime_counts(synced, logged_in):  # noqa: F811
    db, *_ = synced
    imp = apply_import(db, _person(db, "user1").id, "yamtrack", "old.csv", [])
    imp.stats = {"movies": 1, "episodes": 2, "other": 3}  # as 0.6.0 to 0.6.2 stored it
    db.commit()
    page = logged_in.get("/settings/connectors")
    assert page.status_code == 200 and "old.csv" in page.text


def test_unknown_titles_land_in_the_right_list(synced):  # noqa: F811
    db, *_ = synced
    user1 = _person(db, "user1")
    anime = parse([{"type": "TV", "mal_id": 1, "tvdb_id": 4242, "season": {"tvdb": 1}}])
    recs = parse_trakt(
        _trakt_zip(
            episodes=[
                ("Some Anime", 4242, 1, 1, 1),  # the anime list knows this show
                ("Some Drama", 7, 8, 1, 1),  # it doesn't
            ],
            movies=[("Some Film", 55)],
        )
    )
    apply_import(db, user1.id, "trakt", "t.zip", recs)
    titles = {}
    for group in ("TV", "Anime", "Movies"):
        view = build_watch_view(db, user1, _group(db, group), anime)
        titles[group] = {r.canonical.title for r in view.rows if r.canonical.id < 0}
    assert titles == {"TV": {"Some Drama"}, "Anime": {"Some Anime"}, "Movies": {"Some Film"}}
    assert elsewhere(db, user1.id, 1_000_000, anime)[1] == []

    # A group set to take nothing leaves its titles for the "Not in any app" tab.
    _group(db, "Anime").import_landing = "none"
    db.commit()
    view = build_watch_view(db, user1, _group(db, "Anime"), anime)
    assert not [r for r in view.rows if r.canonical.id < 0]
    assert [r.title for r in elsewhere(db, user1.id, 1_000_000, anime)[1]] == ["Some Anime"]


def test_landing_setting_and_guess(synced, logged_in):  # noqa: F811
    from mediawrangler.importers import guess_landing, landing_groups

    db, *_ = synced
    assert [guess_landing(_group(db, n)) for n in ("TV", "Anime", "Movies")] == [
        "show",
        "anime",
        "movie",
    ]
    assert set(landing_groups(db)) == {"show", "anime", "movie"}
    tv = _group(db, "TV")
    resp = post(
        logged_in,
        f"/settings/groups/{tv.id}",
        {"name": "TV", "presence_enabled": "on", "import_landing": "none"},
    )
    assert resp.status_code == 303
    db.expire_all()
    assert tv.import_landing == "none" and "show" not in landing_groups(db)
    bad = post(logged_in, f"/settings/groups/{tv.id}", {"name": "TV", "import_landing": "x"})
    assert bad.status_code == 400
    assert "Automatic (Anime)" in logged_in.get("/settings/groups").text


def test_every_play_is_stored_and_shown(synced, logged_in):  # noqa: F811
    from mediawrangler.models import WatchImportPlay

    db, *_ = synced
    user1 = _person(db, "user1")
    recs = parse_trakt(
        _trakt_zip(
            episodes=[("Sherlock", 176941, 19885, 1, 1), ("Sherlock", 176941, 19885, 1, 1)],
            movies=[("Heat", 949)],
        )
    )
    imp = apply_import(db, user1.id, "trakt", "t.zip", recs)
    assert imp.stats["plays"] == 3  # two plays of the episode, one of the film
    assert len(db.execute(select(WatchImportPlay)).scalars().all()) == 3
    view = build_watch_view(db, user1, _group(db, "TV"))
    sherlock = next(r for r in view.rows if r.canonical.title == "Sherlock")
    assert sherlock.cells[_imported(view).source.id].plays == 2
    assert sherlock.cells[view.columns[0].source.id].plays == 0  # apps don't report plays here
    page = logged_in.get(f"/watch?user={user1.id}&group={_group(db, 'TV').id}&filter=all").text
    assert "2 plays recorded" in page
    # Removing the import removes its plays with it.
    assert post(logged_in, f"/settings/imports/{imp.id}/delete").status_code == 303
    assert db.execute(select(WatchImportPlay)).first() is None
