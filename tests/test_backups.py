"""Full backups: the file, the nightly slot, retention and the Settings page (0.12)."""

import sqlite3
import tarfile
from datetime import UTC, datetime, timedelta

import pytest

from mediawrangler import backups
from mediawrangler import settings_export as sx
from mediawrangler.config import Settings
from mediawrangler.main import create_app
from mediawrangler.models import (
    AppUser,
    Library,
    LibraryGroup,
    LibraryGroupMember,
    MatchDecision,
    Source,
    SourceUser,
    UserSync,
)

from .conftest import csrf_of, post


def _people(app, *names):
    with app.state.db.sessionmaker() as s:
        s.add_all(AppUser(name=n) for n in names)
        s.commit()


def test_a_backup_holds_a_working_copy_and_a_manifest(app, settings, tmp_path):
    _people(app, "user1", "zach")
    path = backups.create_backup(app.state.db, settings, "manual")
    assert path.name.startswith("mw-manual-") and path.name.endswith(".tar.gz")
    manifest = backups.read_manifest(path)
    assert manifest["schema"] == backups.schema_head() and manifest["counts"]["app_user"] == 2
    assert manifest["kind"] == "manual" and manifest["has_key"] is True
    out = tmp_path / "out"
    with tarfile.open(path) as tar:
        assert sorted(tar.getnames()) == [
            "manifest.json",
            "mediawrangler.db",
            "secret.key",
            "settings.json",
        ]
        tar.extract("mediawrangler.db", out, filter="data")
    copy = sqlite3.connect(out / "mediawrangler.db")
    assert copy.execute("select count(*) from app_user").fetchone()[0] == 2
    assert copy.execute("pragma integrity_check").fetchone()[0] == "ok"
    assert not list(path.parent.glob(".making-*")) and not list(path.parent.glob("*.part"))


def test_the_key_and_settings_travel_with_every_backup(app, settings):
    path = backups.create_backup(app.state.db, settings, "nightly")
    with tarfile.open(path) as tar:
        assert tar.extractfile("secret.key").read() == b"test-master-key"
        assert sx.parse(tar.extractfile("settings.json").read())["format"] == sx.FORMAT


def test_only_the_newest_nightly_backups_are_kept(app, settings):
    base = datetime(2026, 10, 1, 2, 30)
    for i in range(5):
        backups.create_backup(app.state.db, settings, "nightly", now=base + timedelta(days=i))
    manual = backups.create_backup(app.state.db, settings, "manual", now=base)
    (backups.backup_dir(settings) / "pre-old.db").write_bytes(b"x")  # an older plain copy
    removed = backups.prune(settings, keep=2)
    assert len(removed) == 3
    kinds = [(b.kind, b.created.day) for b in backups.list_backups(settings)]
    assert [k for k in kinds if k[0] == "nightly"] == [("nightly", 5), ("nightly", 4)]
    assert manual.exists() and (backups.backup_dir(settings) / "pre-old.db").exists()
    assert {b.kind for b in backups.list_backups(settings)} == {"nightly", "manual", "copy"}


def test_the_nightly_backup_is_due_after_its_slot_and_not_twice(app, settings):
    manager = app.state.backups
    tz = settings.timezone

    def utc(y, m, d, h, mi):
        return datetime(y, m, d, h, mi, tzinfo=tz).astimezone(UTC).replace(tzinfo=None)

    assert manager.due(utc(2026, 10, 30, 12, 0))  # none yet
    made = backups.create_backup(app.state.db, settings, "nightly", now=utc(2026, 10, 30, 2, 31))
    assert made.exists()
    assert not manager.due(utc(2026, 10, 30, 12, 0))  # today's is done
    assert not manager.due(utc(2026, 10, 31, 2, 0))  # tomorrow's slot has not come
    assert manager.due(utc(2026, 10, 31, 2, 31))
    assert manager.next_run(utc(2026, 10, 30, 12, 0)) == utc(2026, 10, 31, 2, 30)


def test_a_failed_backup_is_remembered(app, monkeypatch):
    manager = app.state.backups

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(backups, "create_backup", fail)
    with pytest.raises(OSError):
        manager.run("manual")
    assert manager.last_error == "disk full" and not manager.running


def test_the_backups_page_makes_lists_and_downloads(logged_in, app, settings):
    page = logged_in.get("/settings/backups").text
    assert "No backups yet" in page and "Next nightly backup" in page
    resp = post(logged_in, "/settings/backups/run")
    assert resp.status_code == 303 and "msg=backup_made" in resp.headers["location"]
    page = logged_in.get("/settings/backups").text
    assert "By hand" in page and "Download</a>" in page
    (name,) = [b.name for b in backups.list_backups(settings)]
    got = logged_in.get(f"/settings/backups/download/{name}")
    assert got.status_code == 200 and got.content[:2] == b"\x1f\x8b"  # gzip
    assert logged_in.get("/settings/backups/download/..%2Fmediawrangler.db").status_code == 404
    assert logged_in.get("/settings/backups/download/nope.tar.gz").status_code == 404


def test_a_backup_can_be_deleted_from_the_page(logged_in, app, settings):
    post(logged_in, "/settings/backups/run")
    (name,) = [b.name for b in backups.list_backups(settings)]
    assert post(logged_in, "/settings/backups/delete/nope.tar.gz").status_code == 404
    assert post(logged_in, "/settings/backups/delete/..%2Fmediawrangler.db").status_code == 404
    resp = post(logged_in, f"/settings/backups/delete/{name}")
    assert resp.status_code == 303 and "msg=backup_deleted" in resp.headers["location"]
    assert backups.list_backups(settings) == []


def _fill(app, box):
    """A small but complete setup: two sources, a group, a decision, a person and links."""
    with app.state.db.sessionmaker() as s:
        jf = Source(
            type="jellyfin",
            name="Jellyfin",
            base_url="http://jf",
            secret_enc=box.encrypt("jf-api-key"),
            webhook_secret_enc=box.encrypt("hook"),
            path_map=[["/media", "/data"]],
            schedule_at="04:00",
        )
        silo = Source(type="silo", name="Silo", base_url="http://silo")
        s.add_all([jf, silo])
        s.flush()
        lib = Library(
            source_id=jf.id, external_id="L1", name="TV", kind="series", group_assigned=True
        )
        s.add(lib)
        s.flush()
        group = LibraryGroup(name="TV", kind="series", position=1)
        s.add(group)
        s.flush()
        s.add(LibraryGroupMember(group_id=group.id, library_id=lib.id))
        s.add(
            MatchDecision(
                kind="series",
                source_a_id=jf.id,
                external_a="a1",
                source_b_id=silo.id,
                external_b="b1",
                decision="confirm",
            )
        )
        user1 = AppUser(name="user1")
        s.add(user1)
        s.flush()
        s.add(
            UserSync(app_user_id=user1.id, source_id=silo.id, sync_account=True, sync_history=True)
        )
        s.add(
            SourceUser(
                source_id=jf.id,
                external_id="u1",
                name="User1 JF",
                app_user_id=user1.id,
                link_assigned=True,
            )
        )
        s.commit()


def _other_install(tmp_path, key="a-different-key"):
    other = Settings(
        data_dir=tmp_path / "other", public_url="http://testserver", secret_key=key, webhook_port=0
    )
    return other, create_app(other)


def test_settings_export_imports_into_a_fresh_install(app, settings, tmp_path):
    _fill(app, app.state.box)
    with app.state.db.sessionmaker() as s:
        raw = sx.export_bytes(s, app.state.box)
    assert b"jf-api-key" in raw and b"mediawrangler.db" not in raw
    _other, fresh = _other_install(tmp_path)
    data = sx.parse(raw)
    with fresh.state.db.sessionmaker() as s:
        done = sx.import_settings(s, fresh.state.box, data)
        assert done["sources"] == 2 and done["groups"] == 1 and done["decisions"] == 1
        jf = s.query(Source).filter_by(name="Jellyfin").one()
        assert fresh.state.box.decrypt(jf.secret_enc) == "jf-api-key"  # under the new key
        assert fresh.state.box.decrypt(jf.webhook_secret_enc) == "hook"
        assert jf.path_map == [["/media", "/data"]] and jf.schedule_at == "04:00"
        (member,) = s.query(LibraryGroupMember).all()
        assert s.get(Library, member.library_id).external_id == "L1"
        assert s.query(LibraryGroup).one().position == 1
        (decision,) = s.query(MatchDecision).all()
        assert (decision.external_a, decision.external_b) == ("a1", "b1")
        (link,) = s.query(SourceUser).all()
        assert s.get(AppUser, link.app_user_id).name == "user1" and link.link_assigned
        sync = s.query(UserSync).one()
        assert sync.sync_history and sync.since is not None  # history is counted from the import
        # importing again changes nothing
        sx.import_settings(s, fresh.state.box, data)
        assert s.query(Source).count() == 2 and s.query(MatchDecision).count() == 1
        assert s.query(LibraryGroupMember).count() == 1 and s.query(UserSync).count() == 1


def test_a_bad_settings_file_is_refused(app):
    with pytest.raises(sx.SettingsFileError):
        sx.parse(b"not json")
    with pytest.raises(sx.SettingsFileError):
        sx.parse(b'{"hello": 1}')
    with pytest.raises(sx.SettingsFileError, match="newer"):
        sx.parse(b'{"format": 99, "sources": []}')


def test_the_settings_page_exports_and_imports(logged_in, app, tmp_path):
    _fill(app, app.state.box)
    got = logged_in.get("/settings/export")
    assert got.status_code == 200 and "attachment" in got.headers["content-disposition"]
    assert sx.parse(got.content)["sources"][0]["name"] == "Jellyfin"
    token = csrf_of(logged_in)
    bad = logged_in.post(
        "/settings/import",
        data={"csrf_token": token},
        files={"file": ("x.json", b"nope")},
        follow_redirects=False,
    )
    assert bad.status_code == 303 and "error=import" in bad.headers["location"]
    good = logged_in.post(
        "/settings/import",
        data={"csrf_token": token},
        files={"file": ("x.json", got.content)},
        follow_redirects=False,
    )
    assert "msg=settings_imported" in good.headers["location"]


def test_a_restored_backup_boots_with_its_data_and_key(app, settings, tmp_path):
    _people(app, "user1", "zach")
    made = backups.create_backup(app.state.db, settings, "manual")
    target = Settings(data_dir=tmp_path / "fresh", public_url="http://testserver", webhook_port=0)
    backups.backup_dir(target)
    assert "version" in backups.stage_restore(target, made)
    assert backups.apply_pending_restore(target) is True
    assert not backups.pending_dir(target).exists()
    restored = create_app(target)
    with restored.state.db.sessionmaker() as s:
        assert s.query(AppUser).count() == 2
    assert (target.data_dir / "secret.key").read_bytes() == b"test-master-key"
    assert backups.apply_pending_restore(target) is False  # nothing staged now


def test_a_restore_keeps_the_database_it_replaces(app, settings, tmp_path):
    made = backups.create_backup(app.state.db, settings, "manual")
    _people(app, "new-person")  # after the backup
    backups.stage_restore(settings, made)
    backups.apply_pending_restore(settings)
    kept = [b for b in backups.list_backups(settings) if b.name.startswith("pre-restore-")]
    assert len(kept) == 1
    copy = sqlite3.connect(backups.backup_dir(settings) / kept[0].name)
    assert copy.execute("select count(*) from app_user").fetchone()[0] == 1
    with create_app(settings).state.db.sessionmaker() as s:
        assert s.query(AppUser).count() == 0


def test_a_backup_from_a_newer_version_is_refused(app, settings, tmp_path):
    newer = tmp_path / "newer.db"
    backups.create_backup(app.state.db, settings, "manual")
    shutil_copy = sqlite3.connect(newer)
    app.state.db.engine.raw_connection().driver_connection.backup(shutil_copy)
    shutil_copy.execute("update alembic_version set version_num = '9999'")
    shutil_copy.commit()
    shutil_copy.close()
    with pytest.raises(backups.BackupError, match="newer"):
        backups.stage_restore(settings, newer)
    assert not backups.pending_dir(settings).exists()
    junk = tmp_path / "junk"
    junk.write_bytes(b"this is not a backup")
    with pytest.raises(backups.BackupError):
        backups.stage_restore(settings, junk)


def test_first_run_can_restore_a_backup(settings, tmp_path):
    source_settings = Settings(
        data_dir=tmp_path / "old", public_url="http://testserver", secret_key="k", webhook_port=0
    )
    old = create_app(source_settings)
    made = backups.create_backup(old.state.db, source_settings, "manual")
    app = create_app(settings)
    asked = []
    app.state.restart = lambda: asked.append(True)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        client.get("/setup")
        token = csrf_of(client)
        bad = client.post(
            "/setup/restore",
            data={"csrf_token": token},
            files={"file": ("x", b"junk")},
            follow_redirects=False,
        )
        assert bad.status_code == 400 and not asked
        ok = client.post(
            "/setup/restore",
            data={"csrf_token": token},
            files={"file": (made.name, made.read_bytes())},
            follow_redirects=False,
        )
        assert ok.status_code == 200 and "Restoring" in ok.text and asked
    assert (backups.pending_dir(settings) / "mediawrangler.db").is_file()
