"""Full backups of the database (0.12).

A backup is one `.tar.gz` holding `mediawrangler.db` (a consistent copy made with SQLite's
online backup, so the app keeps running), `secret.key` (so the connector keys in the copy can
be read), `settings.json` (the settings export, which also imports on its own) and
`manifest.json` (app version, the migration it was made at, when, and a few row counts).
Nothing in it is locked: whoever holds the file holds the keys, and looking after it is the
owner's job. Nightly backups run from the scheduler at a fixed local time and the newest 7
are kept; manual ones are never removed automatically. Older plain `.db` copies in the same
folder are listed but left alone.

A restore is staged, then applied at the next start (the app exits and Docker restarts it),
before the database is opened: the current database is first saved as `pre-restore-*.db`.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import sqlite3
import tarfile
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic.script import ScriptDirectory

from . import __version__
from .config import Settings
from .crypto import SecretBox, load_master_key
from .db import MIGRATIONS_DIR, Database, utcnow
from .settings_export import export_bytes

log = logging.getLogger(__name__)

PREFIX = "mw-"
SUFFIX = ".tar.gz"
NIGHTLY_AT = "02:30"  # local time, clear of the syncs that start at 04:00
KEEP_NIGHTLY = 7
COUNTED = ("source", "app_user", "canonical_item", "local_state", "local_play", "audit_log")


class BackupError(Exception):
    """A backup could not be made or read."""


def backup_dir(settings: Settings) -> Path:
    path = settings.data_dir / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def schema_head() -> str:
    cfg_dir = ScriptDirectory(str(MIGRATIONS_DIR))
    return cfg_dir.get_current_head()


def known_revisions() -> set[str]:
    return {r.revision for r in ScriptDirectory(str(MIGRATIONS_DIR)).walk_revisions()}


@dataclass
class BackupInfo:
    name: str
    kind: str  # "nightly", "manual", or "copy" for a plain .db
    created: datetime  # UTC
    size: int
    version: str = ""
    schema: str = ""
    has_key: bool = False
    counts: dict | None = None


def _stamp(now: datetime) -> str:
    return now.strftime("%Y%m%d-%H%M%S")


def create_backup(
    database: Database,
    settings: Settings,
    kind: str = "manual",
    now: datetime | None = None,
    dest: Path | None = None,
) -> Path:
    """Copy the database, the master key and the settings into a new backup file and
    return its path."""
    now = now or utcnow()
    folder = dest or backup_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / f"{PREFIX}{kind}-{_stamp(now)}-v{__version__}{SUFFIX}"
    work = Path(tempfile.mkdtemp(prefix=".making-", dir=folder))
    try:
        copy = work / "mediawrangler.db"
        raw = database.engine.raw_connection()
        try:
            target = sqlite3.connect(copy)
            try:
                raw.driver_connection.backup(target)
            finally:
                target.close()
        finally:
            raw.close()
        check = sqlite3.connect(copy)
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("the copy failed SQLite's integrity check")
            schema = check.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            counts = {t: check.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in COUNTED}  # noqa: S608
        finally:
            check.close()
        manifest = {
            "format": 1,
            "app_version": __version__,
            "schema": schema,
            "created_at": now.replace(tzinfo=UTC).isoformat(),
            "kind": kind,
            "counts": counts,
            "has_key": True,
        }
        key = load_master_key(settings)
        with database.sessionmaker() as session:
            exported = export_bytes(session, SecretBox(key))
        stamp = int(now.replace(tzinfo=UTC).timestamp())
        tmp = final.with_suffix(final.suffix + ".part")
        with tarfile.open(tmp, "w:gz") as tar:
            for arcname, blob in (
                ("manifest.json", json.dumps(manifest, indent=2).encode()),
                ("secret.key", key),
                ("settings.json", exported),
            ):
                info = tarfile.TarInfo(arcname)
                info.size, info.mtime = len(blob), stamp
                info.mode = 0o600
                tar.addfile(info, io.BytesIO(blob))
            tar.add(copy, arcname="mediawrangler.db")
        tmp.replace(final)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return final


def read_manifest(path: Path) -> dict:
    try:
        with tarfile.open(path, "r:gz") as tar:
            member = tar.extractfile("manifest.json")
            if member is None:
                raise BackupError("no manifest in this file")
            return json.loads(member.read())
    except (tarfile.TarError, OSError, KeyError, ValueError, EOFError) as exc:
        raise BackupError(f"not a MediaWrangler backup: {exc}") from exc


def list_backups(settings: Settings) -> list[BackupInfo]:
    out = []
    for path in backup_dir(settings).iterdir():
        if path.name.startswith(".") or not path.is_file():
            continue
        stat = path.stat()
        if path.name.startswith(PREFIX) and path.name.endswith(SUFFIX):
            try:
                m = read_manifest(path)
            except BackupError:
                continue
            out.append(
                BackupInfo(
                    path.name,
                    str(m.get("kind", "manual")),
                    datetime.fromisoformat(m["created_at"]).replace(tzinfo=None),
                    stat.st_size,
                    str(m.get("app_version", "")),
                    str(m.get("schema", "")),
                    bool(m.get("has_key")),
                    m.get("counts"),
                )
            )
        elif path.suffix == ".db":
            out.append(
                BackupInfo(
                    path.name,
                    "copy",
                    datetime.fromtimestamp(stat.st_mtime, UTC).replace(tzinfo=None),
                    stat.st_size,
                )
            )
    return sorted(out, key=lambda b: b.created, reverse=True)


def find_backup(settings: Settings, name: str) -> Path | None:
    """A file in the backups folder by name, or None; never a path outside it."""
    if "/" in name or "\\" in name or name.startswith("."):
        return None
    path = backup_dir(settings) / name
    return path if path.is_file() else None


def delete_backup(settings: Settings, name: str) -> bool:
    """Remove one backup (or older copy) by name. False if there is no such file."""
    path = find_backup(settings, name)
    if path is None or not (path.name.endswith(SUFFIX) or path.suffix == ".db"):
        return False
    path.unlink()
    return True


def prune(settings: Settings, keep: int = KEEP_NIGHTLY) -> list[str]:
    """Remove the oldest nightly backups beyond `keep`. Manual ones are never touched."""
    nightly = [b for b in list_backups(settings) if b.kind == "nightly"]
    removed = []
    for old in nightly[keep:]:
        (backup_dir(settings) / old.name).unlink(missing_ok=True)
        removed.append(old.name)
    return removed


def _slot_before(now: datetime, at: str, settings: Settings) -> datetime:
    """The most recent `at` (HH:MM, local time) at or before `now`, as naive UTC."""
    tz = settings.timezone
    local = now.replace(tzinfo=UTC).astimezone(tz)
    hour, minute = int(at[:2]), int(at[3:])
    slot = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot > local:
        slot -= timedelta(days=1)
    return slot.astimezone(UTC).replace(tzinfo=None)


class BackupManager:
    """Runs the nightly backup, one at a time, and remembers how the last one went."""

    def __init__(self, database: Database, settings: Settings):
        self.database, self.settings = database, settings
        self._lock = threading.Lock()
        self.last_error: str | None = None
        self.running = False

    def due(self, now: datetime) -> bool:
        slot = _slot_before(now, NIGHTLY_AT, self.settings)
        nightly = [b for b in list_backups(self.settings) if b.kind == "nightly"]
        return not nightly or nightly[0].created < slot

    def next_run(self, now: datetime | None = None) -> datetime:
        """When the next nightly backup is due, as naive UTC (now, if one is overdue)."""
        now = now or utcnow()
        if self.due(now):
            return now
        slot = _slot_before(now, NIGHTLY_AT, self.settings)
        tz = self.settings.timezone
        local = slot.replace(tzinfo=UTC).astimezone(tz) + timedelta(days=1)
        return (
            local.replace(
                hour=int(NIGHTLY_AT[:2]), minute=int(NIGHTLY_AT[3:]), second=0, microsecond=0
            )
            .astimezone(UTC)
            .replace(tzinfo=None)
        )

    def run(self, kind: str = "nightly") -> Path | None:
        """Make a backup now (and prune, for nightly). Returns None if one is running."""
        if not self._lock.acquire(blocking=False):
            return None
        self.running = True
        try:
            path = create_backup(self.database, self.settings, kind)
            if kind == "nightly":
                prune(self.settings)
            self.last_error = None
            return path
        except Exception as exc:
            log.exception("backup failed")
            self.last_error = str(exc)[:200]
            raise
        finally:
            self.running = False
            self._lock.release()

    def tick(self, now: datetime | None = None) -> bool:
        """Start the nightly backup on a thread if it is due. True if started."""
        now = now or utcnow()
        if self.running or not self.due(now):
            return False

        def work() -> None:
            try:
                self.run("nightly")
            except Exception:  # the failure is in last_error and the log
                log.debug("nightly backup failed", exc_info=True)

        threading.Thread(target=work, name="backup", daemon=True).start()
        return True


# --- restore ------------------------------------------------------------------------------

PENDING = "restore-pending"


def pending_dir(settings: Settings) -> Path:
    return settings.data_dir / PENDING


def _check_db(path: Path) -> str:
    """The migration a database file is at, after checking it is a healthy SQLite file."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("the database in this file is damaged")
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise BackupError("this is not a MediaWrangler database") from exc
    if row is None:
        raise BackupError("this is not a MediaWrangler database")
    return row[0]


def stage_restore(settings: Settings, source: Path) -> str:
    """Check a backup (a `.tar.gz` made here, or a plain `.db`) and put it where the next
    start will pick it up. Returns a line saying what will be restored. Nothing live changes."""
    folder = pending_dir(settings)
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    try:
        if tarfile.is_tarfile(source):
            manifest = read_manifest(source)
            with tarfile.open(source, "r:gz") as tar:
                for name in ("mediawrangler.db", "secret.key"):
                    try:
                        member = tar.extractfile(name)
                    except KeyError:
                        member = None
                    if member is None:
                        if name == "mediawrangler.db":
                            raise BackupError("there is no database in this backup")
                        continue
                    with (folder / name).open("wb") as out:
                        shutil.copyfileobj(member, out)
            version = manifest.get("app_version", "?")
        else:
            shutil.copyfile(source, folder / "mediawrangler.db")
            version = "?"
        schema = _check_db(folder / "mediawrangler.db")
        if schema not in known_revisions():
            raise BackupError(
                f"this backup is from a newer MediaWrangler (version {version}); "
                "update this one before restoring it"
            )
    except (BackupError, OSError, tarfile.TarError, EOFError) as exc:
        shutil.rmtree(folder, ignore_errors=True)
        if isinstance(exc, BackupError):
            raise
        raise BackupError(f"could not read this file: {exc}") from exc
    return f"version {version}, schema {schema}"


def apply_pending_restore(settings: Settings) -> bool:
    """At start, before the database is opened: swap in a staged restore. The database it
    replaces is first saved as `pre-restore-*.db`. True if a restore was applied."""
    folder = pending_dir(settings)
    staged = folder / "mediawrangler.db"
    if not staged.is_file():
        shutil.rmtree(folder, ignore_errors=True)
        return False
    live = settings.db_path
    stamp = _stamp(utcnow())
    try:
        if live.is_file():
            safe = backup_dir(settings) / f"pre-restore-{stamp}.db"
            src = sqlite3.connect(live)
            try:
                dst = sqlite3.connect(safe)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(live) + suffix).unlink(missing_ok=True)
        shutil.move(staged, live)
        key = folder / "secret.key"
        if key.is_file():
            if settings.secret_key_file or settings.secret_key:
                log.warning(
                    "The restored backup holds a secret.key, but this install takes its key "
                    "from the environment; connector keys will only read if they match"
                )
            else:
                target = settings.data_dir / "secret.key"
                if target.exists():
                    shutil.copyfile(target, settings.data_dir / f"secret.key.pre-restore-{stamp}")
                shutil.copyfile(key, target)
                target.chmod(0o600)
        log.warning(
            "Restored the database from a backup; the previous one is pre-restore-%s.db", stamp
        )
        return True
    finally:
        shutil.rmtree(folder, ignore_errors=True)
