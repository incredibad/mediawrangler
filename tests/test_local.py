"""Local: MediaWrangler's own record of what each person has watched."""

# ruff: noqa: F811  (the `synced` fixture is shared from test_watch)

from datetime import datetime

from sqlalchemy import select

from mediawrangler import local
from mediawrangler.db import utcnow
from mediawrangler.models import LocalEvent, LocalPlay, LocalState
from mediawrangler.watchdiff import build_watch_view

from . import fakes
from .conftest import filter_options
from .test_watch import _group, _person, age_holds
from .test_watch import synced as synced  # re-exported: pytest finds fixtures by module


def _sherlock(db, person):
    view = build_watch_view(db, person, _group(db, "TV"))
    return view, next(r for r in view.rows if r.canonical.title == "Sherlock")


def _local_id(view):
    return next(c.source.id for c in view.columns if c.source.type == "local")


def test_local_is_seeded_from_every_readable_source_once(synced):
    db, *_ = synced
    user1, user2 = _person(db, "user1"), _person(db, "user2")
    assert user1.local_seeded_at is not None and user2.local_seeded_at is not None
    view, sherlock = _sherlock(db, user1)
    # The most progress wins: S01E02 is played in Jellyfin only, and Local has it.
    assert sherlock.cells[_local_id(view)].played == 3
    assert "differs_local" in sherlock.flags  # ... which Silo doesn't
    assert db.execute(select(LocalEvent).where(LocalEvent.reason != "seed")).first() is None
    plays = db.execute(select(LocalPlay).where(LocalPlay.app_user_id == user1.id)).scalars().all()
    assert plays and all(p.sources for p in plays)
    # user2's Silo profile can't be read: they are seeded from Jellyfin alone.
    names = [c.source.name for c in build_watch_view(db, user2, _group(db, "TV")).columns]
    assert names == ["Jellyfin", "Local"]


def test_an_unwatch_reaches_local_and_keeps_the_play(synced, monkeypatch):
    db, sync_all, _ = synced
    user1 = _person(db, "user1")
    before = db.execute(select(LocalPlay).where(LocalPlay.app_user_id == user1.id)).scalars().all()
    watched = [w for w in fakes.JF_WATCH["u-user1"] if w["Id"] != "jf-sh-2"]  # unwatched
    watched.append(fakes._jf_watch("jf-sh-3"))  # newly watched
    monkeypatch.setitem(fakes.JF_WATCH, "u-user1", watched)
    sync_all()
    db.expire_all()
    view, sherlock = _sherlock(db, user1)
    assert sherlock.cells[_local_id(view)].played == 4  # the unwatch waits for a second read
    age_holds(db)
    sync_all()
    db.expire_all()
    view, sherlock = _sherlock(db, user1)
    assert sherlock.cells[_local_id(view)].played == 3  # one off, one on
    changes = {
        (e.change, e.reason)
        for e in db.execute(select(LocalEvent).where(LocalEvent.reason != "seed")).scalars()
    }
    assert changes == {("unplayed", "Jellyfin"), ("played", "Jellyfin")}
    after = db.execute(select(LocalPlay).where(LocalPlay.app_user_id == user1.id)).scalars().all()
    assert len(after) == len(before) + 1  # the unwatched episode's play is still there


def test_a_mass_unwatch_is_applied_and_never_blocks(synced, monkeypatch):
    db, sync_all, _ = synced
    user1 = _person(db, "user1")
    plays = db.execute(select(LocalPlay).where(LocalPlay.app_user_id == user1.id)).scalars().all()
    monkeypatch.setitem(fakes.JF_WATCH, "u-user1", [])  # everything unwatched in Jellyfin
    sync_all()
    db.expire_all()
    view, sherlock = _sherlock(db, user1)
    assert sherlock.cells[_local_id(view)].played > 0  # held until a second read agrees
    age_holds(db)
    sync_all()
    db.expire_all()
    view, sherlock = _sherlock(db, user1)
    assert sherlock.cells[_local_id(view)].played == 0
    after = db.execute(select(LocalPlay).where(LocalPlay.app_user_id == user1.id)).scalars().all()
    assert len(after) == len(plays)  # the plays are all still there
    monkeypatch.setitem(fakes.JF_WATCH, "u-user1", [fakes._jf_watch("jf-sh-3")])
    sync_all()  # and later changes still arrive
    db.expire_all()
    view, sherlock = _sherlock(db, user1)
    assert sherlock.cells[_local_id(view)].played == 1


def test_plays_of_one_title_on_one_day_are_one_play(synced):
    db, *_ = synced
    user1 = _person(db, "user1")
    store = local._Store(db, user1.id)
    key = ("mv", 999_999, 0, 0)
    when = datetime(2026, 10, 1, 20, 0)
    store.add_play(key, when, "Jellyfin")
    store.add_play(key, datetime(2026, 10, 1, 21, 30), "Silo")
    store.add_play(key, datetime(2026, 10, 2, 9, 0), "Silo")  # next day: the same play
    store.add_play(key, datetime(2026, 10, 5, 9, 0), "Silo")
    store.add_play(key, None, "Trakt")
    plays = sorted(
        (p for p in store.plays.values() if p.canonical_id == 999_999), key=lambda p: p.day
    )
    assert [(p.day, p.sources) for p in plays] == [
        ("", "Trakt"),
        ("2026-10-01", "Jellyfin, Silo"),
        ("2026-10-05", "Silo"),
    ]


def test_watch_page_has_local_column_and_filter(synced, logged_in):
    db, *_ = synced
    user1, tv = _person(db, "user1"), _group(db, "TV")
    page = logged_in.get(f"/watch?user={user1.id}&group={tv.id}&filter=differs_local")
    assert page.status_code == 200 and "MediaWrangler" in page.text and "≠L" in page.text
    assert ("differs_local", "Differs from Local", True) in filter_options(page.text)


def test_an_unwatch_of_one_copy_is_ignored_while_another_copy_is_played(synced):
    from mediawrangler.models import Source, SourceItem, SourceUser, WatchEvent, WatchState

    db, *_ = synced
    user1 = _person(db, "user1")
    jf = db.execute(select(Source).where(Source.type == "jellyfin")).scalar_one()
    su = db.execute(
        select(SourceUser).where(SourceUser.source_id == jf.id, SourceUser.app_user_id == user1.id)
    ).scalar_one()
    ep = db.execute(
        select(SourceItem).where(SourceItem.source_id == jf.id, SourceItem.external_id == "jf-sh-2")
    ).scalar_one()
    db.add(  # the file was replaced: a second Jellyfin item for the same episode
        SourceItem(
            source_id=jf.id,
            external_id="jf-sh-2b",
            kind="episode",
            title=ep.title,
            library_id=ep.library_id,
            parent_external_id=ep.parent_external_id,
            season=ep.season,
            episode=ep.episode,
        )
    )
    db.add(WatchState(source_user_id=su.id, item_external_id="jf-sh-2b", played=True))
    db.commit()
    key = next(k for k, v in local.item_keys(db, jf).items() if k == "jf-sh-2" and v)
    key = local.item_keys(db, jf)[key]

    def unwatch():
        store = local._Store(db, user1.id)
        ev = WatchEvent(
            source_user_id=su.id, item_external_id="jf-sh-2", kind="unplayed", at=utcnow()
        )
        local._apply_events(db, store, su, jf, [ev], {})
        db.commit()
        return store.states.get(key), store.changed

    state, changed = unwatch()
    assert state is not None and state.played and changed == 0  # the other copy is still played
    db.query(WatchState).filter_by(item_external_id="jf-sh-2b").delete()
    db.commit()
    state, changed = unwatch()  # now it is a real unwatch
    assert state is None and changed == 1


def test_an_unwatch_then_a_rewatch_in_one_batch_does_not_trip_the_unique_key(synced):
    db, *_ = synced
    user1 = _person(db, "user1")
    state = db.execute(select(LocalState).where(LocalState.app_user_id == user1.id)).scalars().first()
    key = (state.kind, state.canonical_id, state.season, state.episode)
    store = local._Store(db, user1.id)
    store.drop(key)  # the unwatch ...
    store.set(key, True, None, None, None)  # ... and the title is watched again before the commit
    db.commit()
    again = db.execute(
        select(LocalState).where(
            LocalState.app_user_id == user1.id,
            LocalState.kind == key[0],
            LocalState.canonical_id == key[1],
            LocalState.season == key[2],
            LocalState.episode == key[3],
        )
    ).scalars()
    assert [s.played for s in again] == [True]


def test_one_person_failing_does_not_stop_everyone_elses_local(synced, monkeypatch):
    db, *_ = synced
    called = []

    def catch_up(session, person):
        called.append(person.name)
        if person.name == "user1":
            raise RuntimeError("one person's batch is broken")
        return 0

    monkeypatch.setattr(local, "_catch_up", catch_up)
    local.refresh_local(db)  # does not raise
    assert "user1" in called and "user2" in called  # and it carried on past user1


def test_a_watch_and_an_unwatch_of_a_new_title_in_one_batch_leave_nothing(synced):
    db, *_ = synced
    user1 = _person(db, "user1")
    key = ("mv", 987_654, 0, 0)  # a title Local has no row for yet
    store = local._Store(db, user1.id)
    store.set(key, True, None, None, None)  # watched ...
    store.drop(key)  # ... and unwatched again before anything is saved
    db.commit()
    assert not db.execute(
        select(LocalState).where(
            LocalState.app_user_id == user1.id, LocalState.canonical_id == 987_654
        )
    ).first()


def test_a_play_an_app_has_and_local_lacks_is_filled_in_unless_local_is_newer(synced):
    db, *_ = synced
    user1 = _person(db, "user1")
    view, sherlock = _sherlock(db, user1)
    local_id = _local_id(view)
    assert sherlock.cells[local_id].played == 3
    states = {
        (s.season, s.episode): s
        for s in db.execute(
            select(LocalState).where(
                LocalState.app_user_id == user1.id, LocalState.canonical_id == sherlock.canonical.id
            )
        ).scalars()
    }
    gap, unwatched = states[(1, 2)], states[(1, 1)]
    db.delete(gap)  # an app has it played, Local has no record (its change was never applied)
    unwatched.played, unwatched.updated_at = False, utcnow()  # unwatched in Local since
    db.commit()
    local.refresh_local(db)
    db.expire_all()
    view, sherlock = _sherlock(db, user1)
    assert sherlock.cells[local_id].played == 2  # the gap is filled; the newer unwatch stands
    assert db.execute(
        select(LocalEvent).where(LocalEvent.change == "played", LocalEvent.reason != "seed")
    ).first()
