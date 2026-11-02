"""Moving orphaned files to a trash folder, and putting them back (0.12.18)."""

import os
from datetime import timedelta

import pytest
from sqlalchemy import select

from mediawrangler import trash
from mediawrangler.connectors.arr import SonarrConnector
from mediawrangler.connectors.base import FileRecord, TitleSnapshot
from mediawrangler.db import utcnow
from mediawrangler.fixes import FixRefused
from mediawrangler.models import AuditLog, MediaFile, SourceItem, TrashItem
from mediawrangler.physical import build_physical_view

from .conftest import post
from .test_physical import _src, _tv, share, synced  # noqa: F401


@pytest.fixture
def orphan(synced, app, monkeypatch):  # noqa: F811
    """Sherlock on the share, with one episode file Sonarr (asked afresh) does not have."""
    db = synced
    group = _tv(db)
    sonarr = _src(db, "Sonarr")
    view = build_physical_view(db, group)
    folder = next(r.folder for r in view.rows if r.name == "Sherlock")
    files = (
        db.execute(
            select(MediaFile).where(
                MediaFile.source_id == sonarr.id,
                MediaFile.canonical_path.like(folder + "/%"),
                MediaFile.removed_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    lost = next(f for f in files if "S01E01" in f.path)
    lost.removed_at = utcnow()  # Sonarr's last read does not have it either
    db.commit()
    view = build_physical_view(db, group)
    held = [FileRecord(path=f.path, item_external_id="series-1") for f in files if f is not lost]
    state = {"files": held}
    monkeypatch.setattr(
        SonarrConnector,
        "fetch_title",
        lambda self, ext, kind, lib: TitleSnapshot(ext, [], list(state["files"])),
    )
    # The same file, on the disk side: the one Sonarr does not have.
    disk_file = lost.canonical_path
    assert os.path.isfile(disk_file)
    return db, group, view, folder, sonarr, disk_file, state, lost, files


def _check(db, app, o, paths):
    db_, group, view, folder, sonarr, *_ = o
    return trash.check(db_, app.state.box, group, view, folder, sonarr, paths)


def test_the_move_needs_the_switch_and_every_check(orphan, app):
    db, group, view, folder, sonarr, disk_file, state, lost, files = orphan
    box = app.state.box
    with pytest.raises(FixRefused, match="switched off"):
        trash.move_to_trash(db, box, group, view, folder, sonarr, [disk_file])
    assert os.path.isfile(disk_file)
    trash.set_enabled(db, True)

    # Each guard on its own: nothing moves when any file fails.
    for path, why in (
        (folder + "/nope.mkv", "not on disk"),
        (os.path.join(os.path.dirname(folder), "elsewhere.mkv"), "not inside this folder"),
    ):
        with pytest.raises(FixRefused, match=why):
            trash.move_to_trash(db, box, group, view, folder, sonarr, [disk_file, path])
    assert os.path.isfile(disk_file)
    sub = os.path.join(folder, "subs.srt")
    open(sub, "w").write("x")
    with pytest.raises(FixRefused, match="not a video"):
        trash.move_to_trash(db, box, group, view, folder, sonarr, [sub])
    link = os.path.join(folder, "link.mkv")
    os.symlink(disk_file, link)
    with pytest.raises(FixRefused, match="link"):
        trash.move_to_trash(db, box, group, view, folder, sonarr, [link])
    with pytest.raises(FixRefused, match="At most"):
        trash.move_to_trash(
            db, box, group, view, folder, sonarr, [disk_file] * (trash.MAX_FILES + 1)
        )
    # The app, asked afresh, now has the file: not an orphan, not moved.
    state["files"].append(FileRecord(path=lost.path, item_external_id="series-1"))
    with pytest.raises(FixRefused, match="has this file now"):
        trash.move_to_trash(db, box, group, view, folder, sonarr, [disk_file])
    assert os.path.isfile(disk_file) and not db.execute(select(TrashItem)).scalars().all()


def test_a_file_is_moved_recorded_and_put_back(orphan, app):
    db, group, view, folder, sonarr, disk_file, state, lost, files = orphan
    trash.set_enabled(db, True)
    (item,) = trash.move_to_trash(db, app.state.box, group, view, folder, sonarr, [disk_file])
    assert not os.path.exists(disk_file) and os.path.isfile(item.trash_path)
    assert "/.mediawrangler-trash/" in item.trash_path and item.trash_path.endswith("S01E01.mkv")
    disk_row = db.execute(
        select(MediaFile).where(
            MediaFile.canonical_path == disk_file, MediaFile.source_id == view.disk_id
        )
    ).scalar_one()
    assert disk_row.removed_at is not None
    assert [a.op for a in db.execute(select(AuditLog)).scalars() if a.op == "trash"] == ["trash"]

    assert "back" in trash.restore(db, item)
    assert os.path.isfile(disk_file) and not os.path.exists(item.trash_path)
    assert item.status == "restored"
    assert not os.path.isdir(os.path.dirname(item.trash_path))  # the empty trash folders go

    with pytest.raises(FixRefused, match="not in the trash"):
        trash.restore(db, item)


def test_restore_never_overwrites(orphan, app):
    db, group, view, folder, sonarr, disk_file, *_ = orphan
    trash.set_enabled(db, True)
    (item,) = trash.move_to_trash(db, app.state.box, group, view, folder, sonarr, [disk_file])
    open(disk_file, "w").write("a new file")
    with pytest.raises(FixRefused, match="already exists"):
        trash.restore(db, item)
    assert os.path.isfile(item.trash_path) and open(disk_file).read() == "a new file"


def test_deleting_for_good_only_inside_the_trash(orphan, app):
    db, group, view, folder, sonarr, disk_file, *_ = orphan
    trash.set_enabled(db, True)
    (item,) = trash.move_to_trash(db, app.state.box, group, view, folder, sonarr, [disk_file])
    keep = os.path.join(folder, "Season 01", "keep.mkv")
    open(keep, "w").write("x")
    bad = TrashItem(
        original_path=keep, trash_path=keep, label="keep.mkv", folder=folder, trashed_at=utcnow()
    )
    db.add(bad)
    db.commit()
    with pytest.raises(FixRefused, match="not in a trash folder"):
        trash.purge(db, bad)
    assert os.path.isfile(keep)  # a tampered record deletes nothing
    trash.purge(db, item)
    assert not os.path.exists(item.trash_path) and item.status == "deleted"
    assert any(a.op == "purge" for a in db.execute(select(AuditLog)).scalars())


def test_expired_trash_is_emptied_only_with_the_switch_on(orphan, app):
    db, group, view, folder, sonarr, disk_file, *_ = orphan
    trash.set_enabled(db, True)
    (item,) = trash.move_to_trash(db, app.state.box, group, view, folder, sonarr, [disk_file])
    assert trash.purge_expired(db) == 0 and os.path.isfile(item.trash_path)  # not 30 days yet
    later = utcnow() + trash.RETENTION + timedelta(hours=1)
    trash.set_enabled(db, False)
    assert trash.purge_expired(db, later) == 0 and os.path.isfile(item.trash_path)
    trash.set_enabled(db, True)
    assert trash.days_left(item) in (29, 30)
    assert trash.purge_expired(db, later) == 1 and not os.path.exists(item.trash_path)


def test_the_dialog_and_the_trash_page(orphan, app, logged_in, monkeypatch):

    db, group, view, folder, sonarr, disk_file, *_ = orphan
    form = {"group": group.id, "source": sonarr.id, "folder": folder, "path": disk_file}
    page = post(logged_in, "/fix/trash/preview", form)
    assert "Move to the trash?" in page.text and "File changes are switched off" in page.text
    assert "/.mediawrangler-trash/" in page.text and "S01E01" in page.text
    refused = post(logged_in, "/fix/trash", form)
    assert "switched off" in refused.text and os.path.isfile(disk_file)

    trash.set_enabled(db, True)
    done = post(logged_in, "/fix/trash", form)
    assert "data-close-now" in done.text
    app.state.syncs.wait_idle()
    assert not os.path.exists(disk_file)
    listing = logged_in.get("/settings/trash").text
    assert "S01E01" in listing and "Put back" in listing and "30 days" in listing
    (item,) = db.execute(select(TrashItem)).scalars()
    back = post(logged_in, f"/settings/trash/{item.id}/restore", {})
    assert back.status_code in (200, 303)
    app.state.syncs.wait_idle()  # put back in the background, with a toast
    assert os.path.isfile(disk_file)
    (job,) = [j for j in app.state.syncs.jobs() if j["label"].startswith("Putting")]
    assert job["state"] == "done" and job["url"] == "/settings/trash"
    again = post(logged_in, f"/settings/trash/{item.id}/restore", {})
    app.state.syncs.wait_idle()
    assert again.status_code in (200, 303) and os.path.isfile(disk_file)
    assert "File changes" in logged_in.get("/settings/writes").text
    assert "Moved to trash" in logged_in.get("/activity?tab=audit").text


def test_the_folder_page_offers_the_checkboxes(orphan, logged_in):
    db, group, view, folder, sonarr, disk_file, state, *_ = orphan
    page = logged_in.get("/physical/folder", params={"group": group.id, "folder": folder}).text
    assert 'name="path"' in page and "Delete…" in page and "data-needs-checked" in page
    assert "orphan-actions" in page
    assert "checked" not in page.split("Move the ticked")[0].split('name="path"')[-1][:80]


def test_a_named_trash_folder_is_used_and_checked(orphan, app, tmp_path):
    db, group, view, folder, sonarr, disk_file, state, lost, files = orphan
    trash.set_enabled(db, True)
    box = app.state.box
    for bad, why in (
        ("relative/path", "full path"),
        (str(tmp_path / "missing"), "not a folder"),
        (os.path.dirname(folder), "overlaps"),  # a library folder itself
        (folder, "overlaps"),  # inside one
    ):
        with pytest.raises(FixRefused, match=why):
            trash.set_dir(db, bad)
    elsewhere = tmp_path / "bin" / ".trash"
    elsewhere.mkdir(parents=True)
    trash.set_dir(db, str(elsewhere))
    (item,) = trash.move_to_trash(db, box, group, view, folder, sonarr, [disk_file])
    assert item.trash_path.startswith(str(elsewhere) + "/") and "/TV/Sherlock/" in item.trash_path
    assert item.trash_root == str(elsewhere) and not os.path.exists(disk_file)
    assert not os.path.isdir(os.path.dirname(disk_file) + "/.mediawrangler-trash")
    trash.restore(db, item)
    assert os.path.isfile(disk_file) and elsewhere.is_dir()  # the named folder is never removed
    assert not any(elsewhere.iterdir())  # but what we made inside it is
    trash.set_dir(db, "")  # back to inside each library
    assert trash.configured_dir(db) is None


def test_a_move_to_another_disk_is_copied_checked_then_removed(orphan, app, tmp_path, monkeypatch):
    import errno

    db, group, view, folder, sonarr, disk_file, *_ = orphan
    trash.set_enabled(db, True)
    elsewhere = tmp_path / "other-disk"
    elsewhere.mkdir()
    trash.set_dir(db, str(elsewhere))
    real = os.rename

    def across(src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "rename", across)
    size = os.path.getsize(disk_file)
    (item,) = trash.move_to_trash(db, app.state.box, group, view, folder, sonarr, [disk_file])
    assert not os.path.exists(disk_file) and os.path.getsize(item.trash_path) == size
    monkeypatch.setattr(os, "rename", real)

    # A copy that comes out the wrong size leaves the original alone and writes nothing.
    other = os.path.join(os.path.dirname(disk_file), "Sherlock - S01E09.mkv")
    open(other, "w").write("data")
    monkeypatch.setattr(os, "rename", across)
    monkeypatch.setattr(
        os, "copy_file_range", lambda *a: (_ for _ in ()).throw(OSError(errno.ENOSYS, "no"))
    )  # the fast copy is not available here, so the ordinary one runs
    monkeypatch.setattr(
        "mediawrangler.trash.shutil.copy2", lambda s, d: open(d, "w").write("x") and None
    )
    with pytest.raises(OSError):
        trash._move(other, str(elsewhere / "x" / "copy.mkv"))
    assert open(other).read() == "data" and not (elsewhere / "x" / "copy.mkv").exists()


def test_unwritable_folders_are_named(orphan, app, tmp_path, monkeypatch):
    db, group, view, folder, sonarr, *_ = orphan
    assert trash.unwritable(db) == []
    root = trash.library_roots(db)[0]
    real = os.access
    monkeypatch.setattr(os, "access", lambda p, mode: False if str(p) == root else real(p, mode))
    assert trash.unwritable(db) == [root]


def test_after_a_removal_the_folder_is_rescanned_and_each_app_read(orphan, app, monkeypatch):
    from mediawrangler import writes
    from mediawrangler.connectors.base import Connector, ItemRecord
    from mediawrangler.connectors.jellyfin import JellyfinConnector
    from mediawrangler.connectors.silo import SiloConnector

    db, group, view, folder, sonarr, disk_file, state, lost, files = orphan
    trash.set_enabled(db, True)
    writes.set_flag(db, writes.ENABLED_KEY, True)
    asked = []
    monkeypatch.setattr(Connector, "run_task", lambda self, task: asked.append(self.type))
    reads = []

    def snapshot(self, ext, kind, lib):
        reads.append(self.type)
        return TitleSnapshot(ext, [ItemRecord(ext, kind, "Sherlock")], [])

    for cls in (JellyfinConnector, SiloConnector):
        monkeypatch.setattr(cls, "fetch_title", snapshot)
    (moved,) = trash.move_to_trash(db, app.state.box, group, view, folder, sonarr, [disk_file])

    syncs = app.state.syncs
    syncs._titles[5] = {"state": "running", "error": ""}
    syncs._run_removal(5, group.id, folder, [moved.original_path])
    assert syncs.title_job(5)["state"] == "done", syncs.title_job(5)
    assert {"jellyfin", "silo", "sonarr"} <= set(asked)  # every app that has the show was asked
    assert {"jellyfin", "silo"} <= set(reads)
    disk_row = db.execute(
        select(MediaFile).where(
            MediaFile.canonical_path == disk_file, MediaFile.source_id == view.disk_id
        )
    ).scalar_one()
    assert disk_row.removed_at is not None  # the share was read again and the file is gone


def test_a_delete_runs_as_a_background_job_with_a_toast(orphan, app, logged_in, monkeypatch):
    db, group, view, folder, sonarr, disk_file, *_ = orphan
    trash.set_enabled(db, True)
    started = []
    monkeypatch.setattr(
        app.state.syncs,
        "trash_job",
        lambda g, f, a, paths, label, leftover=False: started.append((f, a, paths, label)) or 9,
    )
    form = {"group": group.id, "source": sonarr.id, "folder": folder, "path": disk_file}
    done = post(logged_in, "/fix/trash", form)
    assert "data-close-now" in done.text  # the dialog closes at once
    assert started == [(folder, sonarr.id, [disk_file], "Deleting 1 file from Sherlock")]
    assert os.path.exists(disk_file)  # nothing is moved by the request itself
    off = post(logged_in, "/fix/trash", {**form, "path": os.path.join(folder, "nope.mkv")})
    assert "Nothing was moved" in off.text and len(started) == 1


def test_toasts_persist_and_finish(app, logged_in):
    from mediawrangler.fixes import FixRefused

    syncs = app.state.syncs
    gate = __import__("threading").Event()

    def slow(job):
        syncs._text(job, "Halfway there")
        gate.wait(5)
        return {"state": "done", "error": "", "note": "Silo still lists 1 file."}

    running = syncs.run_job("Deleting 2 files from Sherlock", slow)
    syncs.set_job_url(running, "/physical/folder?group=1")
    page = logged_in.get("/fix/toasts").text
    assert "Deleting 2 files from Sherlock" in page and "Halfway there" in page
    assert "Check the page" not in page  # not finished: nothing to check yet
    assert 'hx-get="/fix/toasts"' in logged_in.get("/").text  # every page polls for toasts

    gate.set()
    syncs.wait_idle()
    page = logged_in.get("/fix/toasts").text
    assert "Done, with one thing to know" in page and "Silo still lists 1 file." in page
    assert "data-reload-job" not in page  # a page is never reloaded for you
    assert 'href="/physical/folder?group=1"' in page  # the page to check, once it is done

    def refuse(job):
        raise FixRefused("Writes to your apps are switched off.")

    failed = syncs.run_job("Rescanning Sherlock", refuse)
    syncs.wait_idle()
    page = logged_in.get("/fix/toasts").text
    assert "Writes to your apps are switched off." in page

    clean = syncs.run_job("Scanning Sherlock", lambda job: None)
    syncs.wait_idle()
    assert "data-reload-job" not in logged_in.get("/fix/toasts").text and clean

    gone = post(logged_in, f"/fix/toasts/{running}/dismiss", {})
    assert "Deleting 2 files from Sherlock" not in gone.text
    assert "Rescanning Sherlock" in gone.text and failed


def test_an_app_that_keeps_listing_removed_files_is_named(orphan, app, monkeypatch):
    from mediawrangler import writes
    from mediawrangler.connectors.base import Connector, FileRecord, ItemRecord
    from mediawrangler.connectors.jellyfin import JellyfinConnector
    from mediawrangler.connectors.silo import SiloConnector

    db, group, view, folder, sonarr, disk_file, state, lost, files = orphan
    writes.set_flag(db, writes.ENABLED_KEY, True)
    monkeypatch.setattr(
        JellyfinConnector,
        "fetch_title",
        lambda self, ext, kind, lib: TitleSnapshot(ext, [ItemRecord(ext, kind, "Sherlock")], []),
    )
    monkeypatch.setattr(Connector, "run_task", lambda self, task: None)
    silo_map = [["/mnt/media/TV", os.path.dirname(folder)]]
    silo = _src(db, "Silo")
    silo.path_map = silo_map
    db.commit()
    raw = "/mnt/media/TV/" + disk_file[len(os.path.dirname(folder)) + 1 :]
    monkeypatch.setattr(
        SiloConnector,
        "fetch_title",
        lambda self, ext, kind, lib: TitleSnapshot(
            ext, [ItemRecord(ext, kind, "Sherlock")], [FileRecord(path=raw)]
        ),
    )
    syncs = app.state.syncs
    syncs._titles[6] = {"state": "running", "error": ""}
    syncs._run_removal(6, group.id, folder, [disk_file])
    job = syncs.title_job(6)
    assert job["state"] == "done" and "Silo still lists Sherlock - S01E01.mkv" in job.get(
        "note", ""
    )


def test_a_failed_read_while_waiting_is_retried_not_fatal(orphan, app, monkeypatch):
    import mediawrangler.folder_sync as folder_sync
    from mediawrangler import writes
    from mediawrangler.connectors import ConnectorError
    from mediawrangler.connectors.base import Connector, FileRecord, ItemRecord
    from mediawrangler.connectors.jellyfin import JellyfinConnector
    from mediawrangler.connectors.silo import SiloConnector

    db, group, view, folder, sonarr, disk_file, state, lost, files = orphan
    writes.set_flag(db, writes.ENABLED_KEY, True)
    monkeypatch.setattr(folder_sync, "WAIT", 5)
    monkeypatch.setattr(
        JellyfinConnector,
        "fetch_title",
        lambda self, ext, kind, lib: TitleSnapshot(ext, [ItemRecord(ext, kind, "Sherlock")], []),
    )
    monkeypatch.setattr(Connector, "run_task", lambda self, task: None)
    silo = _src(db, "Silo")
    silo.path_map = [["/mnt/media/TV", os.path.dirname(folder)]]
    db.commit()
    raw = "/mnt/media/TV/" + disk_file[len(os.path.dirname(folder)) + 1 :]
    reads = []

    def silo_read(self, ext, kind, lib):
        reads.append(ext)
        if len(reads) == 1:
            raise ConnectorError("GET /api/v2/catalog/items: still rate limited after 5 attempts")
        files = [FileRecord(path=raw)] if len(reads) == 2 else []
        return TitleSnapshot(ext, [ItemRecord(ext, kind, "Sherlock")], files)

    monkeypatch.setattr(SiloConnector, "fetch_title", silo_read)
    syncs = app.state.syncs
    syncs._titles[8] = {"state": "running", "error": ""}
    syncs._run_removal(8, group.id, folder, [disk_file])
    job = syncs.title_job(8)
    assert job["state"] == "done" and not job.get("note"), job
    assert len(reads) >= 3


def test_rescan_this_show_in_every_app(orphan, app, logged_in, monkeypatch):
    from mediawrangler import writes
    from mediawrangler.connectors.base import Connector, FileRecord, ItemRecord
    from mediawrangler.connectors.jellyfin import JellyfinConnector
    from mediawrangler.connectors.silo import SiloConnector

    db, group, view, folder, sonarr, disk_file, state, lost, files = orphan
    jf, silo = _src(db, "Jellyfin"), _src(db, "Silo")
    silo.path_map = [["/mnt/media/TV", os.path.dirname(folder)]]
    jf.path_map = [["/data/TV", os.path.dirname(folder)]]
    db.commit()
    from mediawrangler.connectors.arr import SonarrConnector
    from mediawrangler.connectors.paths import is_extra, is_video

    on_disk = []
    for d, dirs, fs in os.walk(folder):
        dirs[:] = [x for x in dirs if not x.startswith((".", "@", "#"))]  # as the scan does
        on_disk += [
            os.path.join(d, f)
            for f in fs
            if is_video(os.path.join(d, f)) and not is_extra(os.path.join(d, f))
        ]
    from mediawrangler.models import SourceItem

    paths = {i.external_id: i.path for i in db.execute(select(SourceItem)).scalars()}  # kept
    asked = []
    monkeypatch.setattr(
        Connector,
        "run_task",
        lambda self, task: asked.append(self.type) or ("cmd-1" if self.type == "sonarr" else None),
    )
    monkeypatch.setattr(SonarrConnector, "task_done", lambda self, token: True)

    def all_files(prefix_from, prefix_to):
        def read(self, ext, kind, lib):
            fs = [FileRecord(path=p.replace(prefix_from, prefix_to, 1)) for p in on_disk]
            return TitleSnapshot(
                ext,
                [ItemRecord(ext, kind, "Sherlock", library_external_id=lib, path=paths.get(ext))],
                fs,
            )

        return read

    base = os.path.dirname(folder)
    monkeypatch.setattr(JellyfinConnector, "fetch_title", all_files(base, "/data/TV"))
    monkeypatch.setattr(SiloConnector, "fetch_title", all_files(base, "/mnt/media/TV"))

    # The dialog lists what will be sent; writes are off, so it says so.
    page = logged_in.get("/fix/rescan-all", params={"group": group.id, "folder": folder}).text
    assert "Rescan this show in every app?" in page and "/Library/Media/Updated" in page
    assert "Writes to your apps are off" in page
    refused = post(logged_in, "/fix/rescan-all", {"group": group.id, "folder": folder})
    assert "switched off" in refused.text and not asked

    writes.set_flag(db, writes.ENABLED_KEY, True)
    syncs = app.state.syncs
    syncs._titles[11] = {"state": "running", "error": ""}
    syncs._run_removal(11, group.id, folder, [], True)
    job = syncs.title_job(11)
    assert job["state"] == "done" and "note" not in job, job
    assert {"jellyfin", "silo", "sonarr"} <= set(asked)

    # An app whose count stays different from the share's is named, not trusted.
    monkeypatch.setattr(
        JellyfinConnector,
        "fetch_title",
        lambda self, ext, kind, lib: TitleSnapshot(
            ext,
            [ItemRecord(ext, kind, "Sherlock", library_external_id=lib, path=paths.get(ext))],
            [],
        ),
    )
    syncs._titles[12] = {"state": "running", "error": ""}
    syncs._run_removal(12, group.id, folder, [], True)
    assert "Jellyfin does not list" in syncs.title_job(12).get("note", ""), syncs.title_job(12)


def test_unusual_files_are_recognised():
    from mediawrangler.connectors.paths import odd_kind

    assert odd_kind("/m/TV/X/Season 40/X - S40E05 [WEBDL-1080p]-MeGusta.zipx") == "archive"
    assert odd_kind("/m/TV/X/setup.EXE") == "executable"
    assert odd_kind("/m/TV/X/ep.mkv.part") == "incomplete"
    assert odd_kind("/m/TV/X/._X - S01E02.mkv") == "macos"
    assert odd_kind("/m/TV/X/._.DS_Store") is None and odd_kind("/m/TV/X/._poster.jpg") is None
    assert odd_kind("/m/TV/X/X - S01E02.mkv") is None and odd_kind("/m/TV/X/poster.jpg") is None


def test_the_share_scan_keeps_unusual_files_flagged_and_uncounted(tmp_path):
    from mediawrangler.connectors.base import ConnectorConfig
    from mediawrangler.connectors.disk import DiskConnector

    show = tmp_path / "TV" / "X (2020)" / "Season 01"
    show.mkdir(parents=True)
    for name in ("X - S01E01.mkv", "X - S01E05.zipx", "._X - S01E02.mkv", "setup.exe", "p.jpg"):
        (show / name).write_bytes(b"x")
    found = DiskConnector(ConnectorConfig(str(tmp_path), "")).scan_folder(
        str(tmp_path / "TV" / "X (2020)"), "TV"
    )
    by_name = {f.path.rsplit("/", 1)[-1]: f for f in found}
    assert set(by_name) == {"X - S01E01.mkv", "X - S01E05.zipx", "._X - S01E02.mkv", "setup.exe"}
    assert by_name["X - S01E01.mkv"].flag is None and by_name["X - S01E01.mkv"].extra is False
    assert (by_name["X - S01E05.zipx"].flag, by_name["X - S01E05.zipx"].extra) == ("archive", True)
    assert by_name["._X - S01E02.mkv"].flag == "macos" and by_name["setup.exe"].flag == "executable"


def _plant_unusual(db, view, folder, share_name="odd.zipx"):
    from mediawrangler.connectors.base import ConnectorConfig
    from mediawrangler.connectors.disk import DiskConnector
    from mediawrangler.ingest import apply_folder

    path = os.path.join(folder, share_name)
    with open(path, "wb") as f:
        f.write(b"not a video")
    disk = db.get(type(_src(db, "Media share")), view.disk_id)
    root = next(r for r in view.roots if folder.startswith(r + "/"))
    found = DiskConnector(ConnectorConfig(root, "")).scan_folder(folder, root.rsplit("/", 1)[-1])
    apply_folder(db, disk, folder, found)
    return path


def test_unusual_files_show_on_the_physical_page_and_in_issues(orphan, logged_in):
    db, group, view, folder, sonarr, disk_file, state, lost, files = orphan
    path = _plant_unusual(db, view, folder)
    from mediawrangler.physical import build_physical_view, filter_rows

    view = build_physical_view(db, group)
    row = next(r for r in view.rows if r.folder == folder)
    assert "odd" in row.flags and [f.canonical_path for f in row.odd] == [path]
    assert row.cells[view.disk_id].files == 4  # the .zipx is not counted as a file
    assert row in filter_rows(view, "odd") and row in filter_rows(view, "issues")
    page = logged_in.get(f"/physical?group={group.id}&filter=issues").text
    assert "unusual files" in page
    folder_page = logged_in.get(
        "/physical/folder", params={"group": group.id, "folder": folder}
    ).text
    assert (
        "unusual file in this folder" in folder_page and "do not open or extract it" in folder_page
    )
    assert 'name="source" value="0"' in folder_page


def test_an_unusual_file_can_be_trashed_without_an_app_vouching_for_it(
    orphan, app, logged_in, monkeypatch
):

    db, group, view, folder, sonarr, disk_file, state, lost, files = orphan
    path = _plant_unusual(db, view, folder)
    trash.set_enabled(db, True)
    box = app.state.box
    # A video no app has recorded still needs an app to vouch for it.
    problems = trash.check(db, box, group, view, folder, None, [disk_file])[1]
    assert problems == ["No app was named to check these files against"]
    files_ok, problems = trash.check(db, box, group, view, folder, None, [path])
    assert problems == [] and files_ok[0].flag == "archive"
    form = {"group": group.id, "source": 0, "folder": folder, "path": path}
    page = post(logged_in, "/fix/trash/preview", form).text
    assert "not part of any release" in page and "Move 1 file to the trash" in page
    done = post(logged_in, "/fix/trash", form).text
    assert "data-close-now" in done
    app.state.syncs.wait_idle()
    assert not os.path.exists(path)
    (item,) = db.execute(select(TrashItem)).scalars()
    assert item.why.startswith("Unusual file (Archive)")


def test_the_share_cell_has_a_read_only_reread_entry(orphan, app, logged_in, monkeypatch):
    db, group, view, folder, sonarr, *_ = orphan
    page = logged_in.get(f"/physical?group={group.id}&filter=all").text
    assert "Re-read this folder on the share" in page and "/fix/reread-folder" in page
    started = []
    monkeypatch.setattr(
        app.state.syncs,
        "refresh_after_removal",
        lambda g, f, removed, apps=True, label="": started.append((f, removed, apps, label)) or 4,
    )
    resp = post(logged_in, "/fix/reread-folder", {"group": group.id, "folder": folder})
    assert resp.headers["HX-Trigger"] == "mw-toast"  # the toast appears at once
    assert started == [
        (folder, [], False, "Re-reading Sherlock on the share")
    ]  # share only, no apps


def test_the_master_is_flagged_when_it_is_the_one_behind(orphan):
    from mediawrangler.db import utcnow

    db, group, view, folder, sonarr, disk_file, state, lost, files = orphan
    jf = _src(db, "Jellyfin")
    assert f"count:{jf.id}" not in next(r for r in view.rows if r.folder == folder).flags
    # Jellyfin has not scanned the newest episode yet: the others (the share, Silo) have more.
    gone = (
        db.execute(
            select(MediaFile).where(
                MediaFile.source_id == jf.id,
                MediaFile.canonical_path.like(folder + "/%"),
                MediaFile.removed_at.is_(None),
            )
        )
        .scalars()
        .first()
    )
    gone.removed_at = utcnow()
    db.commit()
    row = next(r for r in build_physical_view(db, group).rows if r.folder == folder)
    assert f"count:{jf.id}" in row.flags and "count" in row.flags  # the master's cell gets the fix


def test_each_row_has_one_fix_menu_in_its_title_cell(orphan, logged_in):
    db, group, view, folder, *_ = orphan
    for path in (
        f"/physical?group={group.id}&filter=all",
        f"/presence?group={group.id}&filter=all",
    ):
        page = logged_in.get(path).text
        rows = page.split("<tr data-flags=")[1:]
        assert rows
        for row in rows:
            assert row.count("<summary>Fix ▾</summary>") <= 1  # never one per cell
            if "<summary>Fix" in row:
                title_cell = row.split("</td>", 1)[0]
                assert "<summary>Fix ▾</summary>" in title_cell  # and it is in the title cell
    physical = logged_in.get(f"/physical?group={group.id}&filter=all").text
    assert 'class="fix-group">Jellyfin</div>' in physical  # actions are grouped by app


def test_files_already_being_deleted_are_refused_again(app):
    from mediawrangler.fixes import FixRefused

    syncs = app.state.syncs
    syncs._claim(["/media/TV/Show/a.mkv"])
    assert syncs.busy_paths() == {"/media/TV/Show/a.mkv"}
    with pytest.raises(FixRefused, match="Already being deleted"):
        both = ["/media/TV/Show/a.mkv", "/media/TV/Show/b.mkv"]
        syncs.trash_job(1, "/media/TV/Show", 0, both, "x")
    # the refused request claimed nothing of its own
    assert syncs.busy_paths() == {"/media/TV/Show/a.mkv"}
    syncs._release(["/media/TV/Show/a.mkv"])
    assert syncs.busy_paths() == set()


def test_after_deleting_an_unusual_file_only_the_app_that_lists_it_is_asked(
    orphan, app, monkeypatch
):
    """Silo counts a `._` stub the share no longer has; it is asked to rescan, no other app."""
    from mediawrangler import folder_sync
    from mediawrangler.models import MediaFile

    db, group, view, folder, sonarr, *_ = orphan
    path = _plant_unusual(db, view, folder, "._stub.mkv")
    silo = _src(db, "Silo")
    db.add(MediaFile(source_id=silo.id, path=path, canonical_path=path, extra=False))
    db.commit()
    syncs = app.state.syncs
    assert folder_sync.lists_any(db, silo, {path}) and not folder_sync.lists_any(db, sonarr, {path})
    asked = []
    monkeypatch.setattr(
        folder_sync,
        "sync_app",
        lambda m, job, key, g, sid, f, roots, gone, present: (
            asked.append(sid) or folder_sync.Outcome(sid)
        ),
    )
    syncs._titles[6] = {"state": "running", "error": ""}
    os.remove(path)
    syncs._removal_state(6, group.id, folder, [path], False, True, listed_only=True)
    assert asked == [silo.id]


def test_a_refused_rescan_does_not_make_the_job_wait(orphan, app, monkeypatch):
    from mediawrangler import folder_sync, tasks
    from mediawrangler.connectors.base import ItemRecord, TitleSnapshot
    from mediawrangler.connectors.silo import SiloConnector

    db, group, view, folder, sonarr, *_ = orphan
    silo = _src(db, "Silo")
    monkeypatch.setattr(tasks, "run", lambda *a, **k: (False, "POST /api/v2/scan: HTTP 400", None))
    reads = []

    def snapshot(self, ext, kind, lib):
        reads.append(ext)
        return TitleSnapshot(ext, [ItemRecord(ext, kind, "Sherlock")], [])

    monkeypatch.setattr(SiloConnector, "fetch_title", snapshot)
    syncs = app.state.syncs
    syncs._titles[7] = {"state": "running", "error": ""}
    monkeypatch.setattr(folder_sync, "WAIT", 30)  # a refusal must not wait it out
    out = folder_sync.sync_app(
        syncs, 7, "app", group.id, silo.id, folder, list(view.roots), set(), set()
    )
    assert "did not accept the scan request" in out.note and "HTTP 400" in out.note
    assert len(reads) == 1  # read once, no polling


def test_an_app_that_still_has_a_folder_gone_from_the_share_can_be_asked_to_rescan(
    orphan, app, logged_in
):
    from mediawrangler.db import utcnow

    db, group, view, folder, *_ = orphan
    for f in db.execute(
        select(MediaFile).where(
            MediaFile.source_id == view.disk_id, MediaFile.canonical_path.like(folder + "/%")
        )
    ).scalars():
        f.removed_at = utcnow()  # the folder was deleted from the share
    db.commit()
    app.state.syncs.bump()
    page = logged_in.get(f"/physical?group={group.id}&filter=not_on_disk").text
    name = os.path.basename(folder).lower()
    row = next(r for r in page.split("<tr data-flags=")[1:] if f'data-search="{name}' in r)
    assert "not_on_disk" in row.split(">", 1)[0]
    assert "Ask Silo to look for new files" in row and "Ask Jellyfin to look for new files" in row


def test_dropping_a_title_also_drops_the_folder_record_the_app_kept_for_it(orphan):
    from mediawrangler.ingest import remove_title
    from mediawrangler.models import MediaRoot, SourceItem

    db, group, view, folder, *_ = orphan
    silo = _src(db, "Silo")
    item = (
        db.execute(
            select(SourceItem).where(
                SourceItem.source_id == silo.id, SourceItem.removed_at.is_(None)
            )
        )
        .scalars()
        .first()
    )
    root = MediaRoot(
        source_id=silo.id, path=folder, state="resolved", item_external_id=item.external_id
    )
    db.add(root)
    db.commit()
    remove_title(db, silo, item.external_id)
    db.refresh(root)
    assert root.removed_at is not None


def test_a_folder_no_arr_tracks_can_have_its_leftovers_trashed(orphan, app, logged_in):
    db, group, view, folder, sonarr, *_ = orphan
    box = app.state.box
    trash.set_enabled(db, True)
    os.makedirs(os.path.join(folder, "Extras"), exist_ok=True)
    open(os.path.join(folder, "Extras", "poster.jpg"), "w").write("x")
    paths = trash.leftover_paths(folder)
    assert any(p.endswith("poster.jpg") for p in paths) and len(paths) > 1
    with pytest.raises(FixRefused, match="tracks this folder"):  # Sonarr still has it
        trash.move_to_trash(db, box, group, view, folder, sonarr, paths, leftover=True)

    for item in db.execute(select(SourceItem).where(SourceItem.source_id == sonarr.id)).scalars():
        item.removed_at = utcnow()  # gone from Sonarr, files left on the share
    db.commit()
    menu = logged_in.get("/physical", params={"group": group.id}).text
    assert menu.index("Re-read this folder on the share") < menu.index("Delete the leftover files")
    page = logged_in.get(
        "/fix/leftovers", params={"group": group.id, "source": sonarr.id, "folder": folder}
    )
    assert "does not track this folder" in page.text and "poster.jpg" in page.text
    assert 'name="leftover" value="true"' in page.text

    moved = trash.move_to_trash(db, box, group, view, folder, sonarr, paths, leftover=True)
    assert len(moved) == len(paths) and all(not os.path.exists(p) for p in paths)
    assert all("does not track" in m.why for m in moved)
    trash.remove_empty_dirs(folder)
    assert not os.path.exists(folder)
    assert "back" in trash.restore(db, next(m for m in moved if "poster" in m.original_path))
    assert os.path.isfile(os.path.join(folder, "Extras", "poster.jpg"))
