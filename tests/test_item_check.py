"""An item that drops off the played list is only an unwatch if the app still has it."""

# ruff: noqa: F811  (the `synced` fixture is shared from test_watch)

import pytest
from sqlalchemy import select

from mediawrangler.connectors.base import WatchRecord
from mediawrangler.models import (
    LocalEvent,
    Source,
    SourceUser,
    SyncRun,
    WatchEvent,
    WatchState,
)
from mediawrangler.watch import apply_watch

from . import fakes
from .test_watch import _person, age_holds
from .test_watch import synced as synced  # re-exported: pytest finds fixtures by module


@pytest.fixture(autouse=True)
def _clean():
    fakes.JF_GONE.clear()
    yield
    fakes.JF_GONE.clear()


def _jf_user(db):
    alex = _person(db, "alex")
    jf = db.execute(select(Source).where(Source.type == "jellyfin")).scalar_one()
    return db.execute(
        select(SourceUser).where(SourceUser.source_id == jf.id, SourceUser.app_user_id == alex.id)
    ).scalar_one()


def _read_without(db, su, dropped):
    """A new read of the user's played list, minus one item."""
    return [
        WatchRecord(
            item_external_id=w.item_external_id, played=True, last_played_at=w.last_played_at
        )
        for w in db.execute(
            select(WatchState).where(WatchState.source_user_id == su.id, WatchState.played)
        ).scalars()
        if w.item_external_id != dropped
    ]


def _unplayed_events(db, su):
    return [
        e.item_external_id
        for e in db.execute(
            select(WatchEvent).where(
                WatchEvent.source_user_id == su.id, WatchEvent.kind == "unplayed"
            )
        ).scalars()
    ]


def _still_stored(db, su, item):
    return (
        db.execute(
            select(WatchState).where(
                WatchState.source_user_id == su.id, WatchState.item_external_id == item
            )
        ).first()
        is not None
    )


def test_a_confirmed_unwatch_is_logged(synced):
    db, *_ = synced
    su = _jf_user(db)
    records = _read_without(db, su, "jf-sh-2")
    live = {w.item_external_id for w in db.execute(select(WatchState)).scalars()}
    stats = apply_watch(db, su, records, live, lambda ids: set(ids))  # Jellyfin still has it
    db.commit()
    assert _unplayed_events(db, su) == [] and stats.held == 1  # waits for a second read
    age_holds(db)
    stats = apply_watch(db, su, records, live, lambda ids: set(ids))
    db.commit()
    assert _unplayed_events(db, su) == ["jf-sh-2"] and stats.gone == 0


def test_an_item_the_app_no_longer_has_is_not_an_unwatch(synced):
    db, *_ = synced
    su = _jf_user(db)
    records = _read_without(db, su, "jf-sh-2")
    live = {w.item_external_id for w in db.execute(select(WatchState)).scalars()}
    stats = apply_watch(db, su, records, live, lambda ids: set(ids) - {"jf-sh-2"})
    db.commit()
    assert _unplayed_events(db, su) == [] and stats.gone == 1
    assert not _still_stored(db, su, "jf-sh-2")  # dropped quietly, like a deleted file


def test_when_the_app_cannot_be_asked_nothing_changes(synced):
    db, *_ = synced
    su = _jf_user(db)
    records = _read_without(db, su, "jf-sh-2")
    live = {w.item_external_id for w in db.execute(select(WatchState)).scalars()}
    stats = apply_watch(db, su, records, live, lambda ids: None)  # an error talking to Jellyfin
    db.commit()
    assert (
        _unplayed_events(db, su) == [] and stats.unchecked == 2
    )  # it, and the part-watched item the fake read omits
    assert _still_stored(db, su, "jf-sh-2")  # kept as it was, so it is asked about again


def test_without_a_check_two_reads_still_confirm_it(synced):
    db, *_ = synced
    su = _jf_user(db)
    records = _read_without(db, su, "jf-sh-2")
    live = {w.item_external_id for w in db.execute(select(WatchState)).scalars()}
    apply_watch(db, su, records, live)  # an app that can't be asked (Silo)
    db.commit()
    assert _unplayed_events(db, su) == []
    age_holds(db)
    apply_watch(db, su, records, live)
    db.commit()
    assert _unplayed_events(db, su) == ["jf-sh-2"]


def test_a_replaced_file_in_a_real_sync_changes_nothing_and_queues_a_library_read(
    synced, app, monkeypatch
):
    db, sync_all, _ = synced
    su = _jf_user(db)
    before = len(db.execute(select(LocalEvent)).scalars().all())
    watched = [w for w in fakes.JF_WATCH["u-alex"] if w["Id"] != "jf-sh-2"]
    monkeypatch.setitem(fakes.JF_WATCH, "u-alex", watched)
    fakes.JF_GONE.add("jf-sh-2")  # Jellyfin gave the episode a new id
    syncs = app.state.syncs
    jf = db.execute(select(Source).where(Source.type == "jellyfin")).scalar_one()
    syncs.start(jf.id, mode="watch")  # the quick read, which doesn't refresh the library
    syncs._pool.shutdown(wait=True)
    db.expire_all()
    assert _unplayed_events(db, su) == []
    assert len(db.execute(select(LocalEvent)).scalars().all()) == before  # Local untouched
    runs = db.execute(select(SyncRun).where(SyncRun.trigger == "replaced")).scalars().all()
    assert runs and all(r.mode == "full" for r in runs)


def test_jellyfin_asks_as_the_user(synced):
    from mediawrangler.connectors.base import UserRecord
    from mediawrangler.sync import connector_for

    db, *_ = synced
    jf = db.execute(select(Source).where(Source.type == "jellyfin")).scalar_one()
    fakes.JF_GONE.add("b")
    with connector_for(jf, __import__("types").SimpleNamespace(decrypt=lambda v: "k")) as c:
        assert c.items_exist(UserRecord("u-alex", "alex"), ["a", "b", "c"]) == {"a", "c"}
