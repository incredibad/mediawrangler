"""Silo: a key per login account, so every account's watch state can be read."""

# ruff: noqa: F811  (the `synced` fixture is shared from test_watch)

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from mediawrangler.models import LocalCursor, LocalPlay, SourceAccountKey, SourceUser, WatchState

from . import fakes
from .conftest import post
from .test_watch import _person
from .test_watch import synced as synced  # re-exported: pytest finds fixtures by module


@pytest.fixture(autouse=True)
def _clean_silo_keys():
    fakes.SILO_KEYS.clear()
    yield
    fakes.SILO_KEYS.clear()


def _wait(app):
    app.state.syncs._pool.shutdown(wait=True)
    app.state.syncs._pool = ThreadPoolExecutor(1)


def _silo(db):
    from mediawrangler.models import Source

    return db.execute(select(Source).where(Source.type == "silo")).scalar_one()


def _jordan(db):
    return db.execute(
        select(SourceUser).where(SourceUser.source_id == _silo(db).id, SourceUser.name == "Jordan")
    ).scalar_one()


def test_other_accounts_are_unreadable_until_a_key_is_made(synced, logged_in):
    db, *_ = synced
    assert _jordan(db).watch_readable is False
    page = logged_in.get(f"/settings/connectors/{_silo(db).id}").text
    assert "Not readable" in page and "Make keys for 1 account" in page


def test_preview_shows_the_call_and_sends_nothing(synced, logged_in):
    db, *_ = synced
    page = post(logged_in, f"/settings/connectors/{_silo(db).id}/silo-keys/preview").text
    assert "POST /api/v2/admin/api-keys" in page and '"user_id": "2"' in page
    assert "Send these requests" in page
    assert db.execute(select(SourceAccountKey)).first() is None


def test_making_keys_stores_them_encrypted_and_reads_the_account(synced, logged_in, app):
    db, sync_all, _ = synced
    sid = _silo(db).id
    page = post(logged_in, f"/settings/connectors/{sid}/silo-keys/create").text
    assert "Made keys for Jordan" in page
    app.state.syncs._pool.shutdown(wait=True)
    app.state.syncs._pool = ThreadPoolExecutor(1)
    db.expire_all()
    key = db.execute(select(SourceAccountKey)).scalar_one()
    assert key.account_id == "2" and key.key_id == "k2" and "key-2" not in key.key_enc
    assert app.state.box.decrypt(key.key_enc) == "key-2"
    su = _jordan(db)
    assert su.watch_readable is True and su.watch_synced_at is not None
    assert db.execute(select(WatchState).where(WatchState.source_user_id == su.id)).first()
    # Local folds in what Silo says about someone it already held. (A refresh may
    # be running on a background thread; wait for it.)
    app.state.syncs.bump()
    with app.state.syncs._local_lock:
        pass
    db.expire_all()
    assert db.get(LocalCursor, su.id) is not None
    plays = db.execute(
        select(LocalPlay).where(LocalPlay.app_user_id == _person(db, "Jordan").id)
    ).scalars()
    assert any("Silo" in p.sources for p in plays)
    page = logged_in.get(f"/settings/connectors/{sid}").text
    assert "Its own key" in page and "Revoke" in page


def test_revoking_deletes_the_key(synced, logged_in, app):
    db, *_ = synced
    sid = _silo(db).id
    post(logged_in, f"/settings/connectors/{sid}/silo-keys/create")
    app.state.syncs._pool.shutdown(wait=True)
    app.state.syncs._pool = ThreadPoolExecutor(1)
    page = post(logged_in, f"/settings/connectors/{sid}/silo-keys/2/revoke").text
    assert "Key deleted in Silo" in page
    db.expire_all()
    assert db.execute(select(SourceAccountKey)).first() is None


def test_a_key_deleted_in_silo_is_noticed_and_can_be_replaced_for_that_user(synced, logged_in, app):
    db, sync_all, _ = synced
    sid = _silo(db).id
    post(logged_in, f"/settings/connectors/{sid}/silo-keys/create")
    _wait(app)
    fakes.SILO_KEYS.clear()  # someone deletes it in Silo
    sync_all()
    db.expire_all()
    assert db.execute(select(SourceAccountKey)).scalar_one().state == "gone"
    assert _jordan(db).watch_readable is False
    page = logged_in.get(f"/settings/connectors/{sid}").text
    assert "Its key is missing in Silo" in page and "Make a new key" in page
    # One user's key: the preview names just her, and the new key replaces the old row.
    preview = post(
        logged_in, f"/settings/connectors/{sid}/silo-keys/preview", {"account_id": "2"}
    ).text
    assert '"user_id": "2"' in preview
    post(logged_in, f"/settings/connectors/{sid}/silo-keys/create", {"account_id": "2"})
    _wait(app)
    db.expire_all()
    key = db.execute(select(SourceAccountKey)).scalar_one()
    assert key.state == "ok" and _jordan(db).watch_readable is True


def test_a_leftover_key_we_do_not_hold_is_deleted_before_the_new_one(synced, logged_in):
    db, *_ = synced
    sid = _silo(db).id
    fakes.SILO_KEYS.append({"id": "k9", "user_id": "2", "label": "MediaWrangler"})
    preview = post(logged_in, f"/settings/connectors/{sid}/silo-keys/preview").text
    assert "DELETE /api/v2/admin/api-keys/k9" in preview
    post(logged_in, f"/settings/connectors/{sid}/silo-keys/create")
    assert {k["id"] for k in fakes.SILO_KEYS} == {"k2"}
