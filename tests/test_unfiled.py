"""Titles only an import knows live in Local, and join an app's copy when it arrives."""

# ruff: noqa: F811  (the `synced` fixture is shared from test_watch)

from sqlalchemy import select

from mediawrangler.groups import source_order
from mediawrangler.importers import apply_import, parse_trakt
from mediawrangler.matching import run_matching
from mediawrangler.models import CanonicalItem, LocalEvent, LocalPlay, LocalState
from mediawrangler.unfiled import adopt_imports, adopt_new_imports, reunite

from .test_imports import _trakt_zip
from .test_watch import _person
from .test_watch import synced as synced  # re-exported: pytest finds fixtures by module


def _import_unknown_show(db, user1):
    recs = parse_trakt(
        _trakt_zip(
            episodes=[("Not In Any App", 5, 55, 1, 1), ("Not In Any App", 5, 55, 1, 2)],
            movies=[("Not In Any Film", 777)],
        )
    )
    apply_import(db, user1.id, "trakt", "t.zip", recs)


def _held(db, user1, title):
    cid = db.execute(select(CanonicalItem.id).where(CanonicalItem.title == title)).scalar()
    states = db.execute(
        select(LocalState).where(LocalState.app_user_id == user1.id, LocalState.canonical_id == cid)
    ).scalars()
    return cid, sorted((s.season, s.episode, s.played) for s in states)


def test_a_title_no_app_has_gets_a_canonical_item_and_plays_in_local(synced):
    db, *_ = synced
    user1 = _person(db, "user1")
    _import_unknown_show(db, user1)
    assert adopt_imports(db, user1, {}) == 2  # the show and the film
    cid, states = _held(db, user1, "Not In Any App")
    assert cid > 0 and states == [(1, 1, True), (1, 2, True)]
    assert db.execute(select(CanonicalItem.key).where(CanonicalItem.id == cid)).scalar() == (
        "series:tvdb:5"
    )
    assert _held(db, user1, "Not In Any Film")[1] == [(0, 0, True)]
    plays = db.execute(select(LocalPlay).where(LocalPlay.canonical_id == cid)).scalars().all()
    assert plays and all(p.sources == "Trakt" for p in plays)
    # Running it again changes nothing.
    n = len(db.execute(select(LocalPlay)).scalars().all())
    assert adopt_imports(db, user1, {}) == 0
    assert len(db.execute(select(LocalPlay)).scalars().all()) == n


def test_a_new_import_is_folded_in_once(synced):
    db, *_ = synced
    user1 = _person(db, "user1")
    assert adopt_new_imports(db, user1, {}) == 0  # no files yet
    _import_unknown_show(db, user1)
    assert adopt_new_imports(db, user1, {}) == 2
    assert adopt_new_imports(db, user1, {}) == 0  # same files: nothing to do
    assert _held(db, user1, "Not In Any App")[1]


def test_matching_does_not_clean_away_what_local_holds(synced):
    db, *_ = synced
    user1 = _person(db, "user1")
    _import_unknown_show(db, user1)
    adopt_imports(db, user1, {})
    before = _held(db, user1, "Not In Any App")
    run_matching(db, source_order(db))  # no app has the show, so nothing points at it
    assert _held(db, user1, "Not In Any App") == before


def test_local_follows_when_an_app_gets_the_title_under_another_key(synced):
    db, *_ = synced
    user1 = _person(db, "user1")
    sherlock = db.execute(select(CanonicalItem).where(CanonicalItem.title == "Sherlock")).scalar()
    ghost = CanonicalItem(
        key="series:import:sherlock:2010",
        kind="series",
        title="Sherlock",
        year=2010,
        tvdb=sherlock.tvdb,
    )
    db.add(ghost)
    db.flush()
    db.add(
        LocalState(
            app_user_id=user1.id, kind="ep", canonical_id=ghost.id, season=9, episode=9, played=True
        )
    )
    db.add(
        LocalEvent(
            app_user_id=user1.id,
            kind="ep",
            canonical_id=ghost.id,
            season=9,
            episode=9,
            change="played",
            reason="import",
            at=sherlock.updated_at,
        )
    )
    db.commit()
    assert reunite(db) == 1
    db.commit()
    assert db.get(CanonicalItem, ghost.id) is None
    moved = db.execute(
        select(LocalState).where(LocalState.canonical_id == sherlock.id, LocalState.season == 9)
    ).scalar_one()
    assert moved.played
    assert db.execute(
        select(LocalEvent).where(LocalEvent.canonical_id == sherlock.id, LocalEvent.season == 9)
    ).scalar_one()
