"""Asking Jellyfin to drop a show whose folder is gone from the share (0.12.86)."""

import os
import shutil

import pytest
from sqlalchemy import select

from mediawrangler import forget, writes
from mediawrangler.connectors.base import ItemGone
from mediawrangler.connectors.jellyfin import JellyfinConnector
from mediawrangler.db import utcnow
from mediawrangler.models import AuditLog, MediaFile, SourceItem

from .conftest import post
from .test_physical import _src, share, synced  # noqa: F401
from .test_trash import orphan  # noqa: F401


@pytest.fixture
def emptied(orphan):  # noqa: F811
    """Sherlock's folder is deleted from the share; Jellyfin still lists the show, no files."""
    db, group, view, folder, *_ = orphan
    jf = _src(db, "Jellyfin")
    show = db.execute(
        select(SourceItem).where(
            SourceItem.source_id == jf.id,
            SourceItem.kind == "series",
            SourceItem.title == "Sherlock",
        )
    ).scalar_one()
    assert show.id not in forget.gone(db, [jf], "series")  # it has files and a folder
    for f in db.execute(select(MediaFile).where(MediaFile.source_id == jf.id)).scalars():
        if f.item_external_id == show.external_id:
            f.removed_at = utcnow()
    db.commit()
    assert show.id not in forget.gone(db, [jf], "series")  # the folder is still there
    shutil.move(folder, folder + "-gone")
    return db, group, folder, jf, show


def test_a_show_whose_folder_is_gone_is_offered(emptied, logged_in):
    db, group, folder, jf, show = emptied
    assert show.id in forget.gone(db, [jf], "series")
    assert forget.gone(db, [jf], "movie") == set()
    assert [i.id for _s, i in forget.in_folder(db, folder, "series")] == [show.id]
    page = logged_in.get("/presence", params={"group": group.id, "filter": "all"}).text
    assert "Ask Jellyfin to drop this show" in page


def test_jellyfin_is_told_and_the_show_dropped_once_it_goes(emptied, app, monkeypatch):
    db, group, folder, jf, show = emptied
    told, reads = [], []
    monkeypatch.setattr(JellyfinConnector, "forget_folder", lambda self, p: told.append(p))
    waits = []
    monkeypatch.setattr(app.state.syncs, "_await_drop", lambda *a: waits.append(a))
    monkeypatch.setattr("threading.Thread.start", lambda self: self._target(*self._args))
    syncs = app.state.syncs
    job = 1
    syncs._titles[job] = {"steps": []}
    assert syncs._forget(job, jf.id, show.id) == ""  # the toast does not wait
    assert told == [show.path] and waits == [(jf.id, show.external_id, "series")]
    assert syncs._titles[job]["steps"][0]["state"] == "done"

    def fetch(self, ext, kind, lib):
        reads.append(ext)
        if len(reads) < 3:
            return None  # still listed for a while
        raise ItemGone(ext)

    monkeypatch.setattr(JellyfinConnector, "fetch_title", fetch)
    from mediawrangler.sync import SyncManager

    assert SyncManager._await_drop(syncs, jf.id, show.external_id, "series") is True
    assert len(reads) == 3
    db.expire_all()
    assert db.get(SourceItem, show.id).removed_at is not None


def test_the_fix_needs_writes_and_is_audited(emptied, app, logged_in, monkeypatch):
    db, group, folder, jf, show = emptied
    off = post(logged_in, "/fix/forget", {"item": show.id}).text
    assert "switched off" in off
    writes.set_flag(db, writes.ENABLED_KEY, True)
    started = []
    monkeypatch.setattr(
        app.state.syncs, "forget_job", lambda item, label: started.append((item, label)) or 3
    )
    done = post(logged_in, "/fix/forget", {"item": show.id}).text
    assert "data-close-now" in done and started == [(show.id, "Asking Jellyfin to drop Sherlock")]
    (log,) = db.execute(select(AuditLog).where(AuditLog.op == "forget")).scalars()
    assert log.source_name == "Jellyfin"
    shutil.move(folder + "-gone", folder)  # the folder is back: not offered, refused
    again = post(logged_in, "/fix/forget", {"item": show.id}).text
    assert "folder is on the share" in again and len(started) == 1
    assert os.path.isdir(folder)
