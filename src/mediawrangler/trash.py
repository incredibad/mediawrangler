"""Removing orphaned files without losing them (0.11).

An orphan is a file on the share that Sonarr or Radarr tracks the folder for but has no
record of. MediaWrangler never deletes one outright: it renames the file into a trash folder
into a trash folder, and keeps a record so it can be put back. By default the folder is inside
the file's own library folder (`.mediawrangler-trash/<stamp>/<path>`), where the rename is instant
and stays on the same disk; the person can name one folder to use instead (Settings → Writes),
and a file moved to another disk is copied, checked and only then removed. The trash is emptied only
by hand, or automatically once a file has been there for RETENTION.

Every move is checked just before it happens: the switch is on, the file is a plain video file
inside a library folder, it is still there, and the arr, asked afresh, still has no record of
it. If any file in a request fails a check, nothing is moved.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from .connectors import ConnectorError, get_connector_class
from .connectors.paths import ODD_LABELS, PathMap, is_video
from .crypto import SecretBox
from .db import utcnow
from .fixes import FixRefused
from .models import (
    AppSetting,
    AuditLog,
    Library,
    LibraryGroup,
    LibraryGroupMember,
    MediaFile,
    Source,
    TrashItem,
)
from .physical import PhysicalView
from .presence import group_libraries
from .sync import connector_for
from .tasks import find_item
from .writes import flag, set_flag

log = logging.getLogger("mediawrangler.trash")

FILES_KEY = "file_changes_enabled"
DIR_KEY = "trash_dir"
TRASH_DIR = ".mediawrangler-trash"
RETENTION = timedelta(days=30)
MAX_FILES = 500  # a whole show's folder: every file is still checked before it moves


def enabled(db: Session) -> bool:
    return flag(db, FILES_KEY)


def set_enabled(db: Session, on: bool) -> None:
    set_flag(db, FILES_KEY, on)


def library_root(path: str, roots: list[str]) -> str | None:
    return next((r for r in roots if path.startswith(r.rstrip("/") + "/")), None)


def unwritable(db: Session) -> list[str]:
    """The library folders (and the named trash folder) MediaWrangler cannot write to."""
    folders = library_roots(db)
    configured = configured_dir(db)
    if configured:
        folders.append(configured)
    return [f for f in folders if not os.access(f, os.W_OK)]


@dataclass
class Checked:
    path: str
    size: int
    destination: str
    flag: str | None = None  # why it is unusual (paths.odd_kind), when it is


def configured_dir(db: Session) -> str | None:
    row = db.get(AppSetting, DIR_KEY)
    return row.value if row is not None and row.value else None


def library_roots(db: Session) -> list[str]:
    """Every library folder of the share that belongs to a group."""
    rows = db.execute(
        select(Library.paths)
        .join(LibraryGroupMember, LibraryGroupMember.library_id == Library.id)
        .join(Source, Source.id == Library.source_id)
        .where(Source.type == "disk", Library.removed_at.is_(None))
    ).scalars()
    return [p.rstrip("/") for paths in rows for p in paths or []]


def set_dir(db: Session, path: str) -> None:
    """Name the folder trashed files go to, or "" for inside each library folder. Raises
    FixRefused when it is not a usable folder: it must exist and be writable, and must not
    be, hold or sit inside any library folder (it would then be scanned as media)."""
    path = path.strip()
    if path:
        if not path.startswith("/"):
            raise FixRefused("Give the folder as a full path, for example /media/.trash")
        path = os.path.normpath(path)
        if not os.path.isdir(path):
            raise FixRefused(f"{path} is not a folder MediaWrangler can see (create it first)")
        if not os.access(path, os.W_OK):
            raise FixRefused(f"{path} is read-only for MediaWrangler; mount it read-write")
        for root in library_roots(db):
            if path == root or path.startswith(root + "/") or root.startswith(path + "/"):
                raise FixRefused(
                    f"{path} overlaps the library folder {root}; pick one outside every library"
                )
    row = db.get(AppSetting, DIR_KEY)
    if row is None:
        db.add(AppSetting(key=DIR_KEY, value=path))
    else:
        row.value = path
    db.commit()


def _base(db: Session, root: str) -> str:
    """The trash folder for files from this library folder (without the date)."""
    configured = configured_dir(db)
    if configured:
        return f"{configured.rstrip('/')}"
    return f"{root.rstrip('/')}/{TRASH_DIR}"


def _destination(db: Session, root: str, stamp: str, rel: str) -> str:
    base = _base(db, root)
    if configured_dir(db):  # keep libraries apart: <trash>/<date>/<library>/<path>
        return f"{base}/{stamp}/{os.path.basename(root.rstrip('/'))}/{rel}"
    return f"{base}/{stamp}/{rel}"


COPY_SPEED = 150 * 1024 * 1024  # bytes a second, for the estimate shown beforehand


def _copy(src: str, dst: str) -> None:
    """Copy a file with the kernel's copy call (on a network share the server can do the
    copy itself, about twice as fast as reading it all through here); where that is not
    available, an ordinary copy."""
    try:
        with open(src, "rb") as s, open(dst, "wb") as d:
            left = os.fstat(s.fileno()).st_size
            while left > 0:
                n = os.copy_file_range(s.fileno(), d.fileno(), min(left, 1 << 30))
                if n == 0:
                    break
                left -= n
        shutil.copystat(src, dst)
        return
    except (OSError, AttributeError):
        if os.path.exists(dst):
            os.remove(dst)
    shutil.copy2(src, dst)


def same_disk(path: str, folder: str) -> bool:
    """Whether a rename from `path` into `folder` would work (the same mount)."""
    try:
        return os.stat(path).st_dev == os.stat(folder).st_dev
    except OSError:
        return True


def _move(src: str, dst: str) -> None:
    """Rename, or when the folders are on different disks copy, check the size, then remove
    the original. A failed copy removes what it wrote and leaves the original alone."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.rename(src, dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    try:
        _copy(src, dst)
        if os.path.getsize(dst) != os.path.getsize(src):
            raise OSError(errno.EIO, "the copy is not the same size as the original")
    except OSError:
        if os.path.exists(dst):
            os.remove(dst)
        raise
    os.remove(src)


def unusual_flags(db: Session, view: PhysicalView, paths: list[str]) -> dict[str, str]:
    """Which of these paths the last scan flagged as unusual files, and why."""
    if view.disk_id is None:
        return {}
    rows = db.execute(
        select(MediaFile.canonical_path, MediaFile.flag).where(
            MediaFile.source_id == view.disk_id,
            MediaFile.canonical_path.in_(paths),
            MediaFile.flag.is_not(None),
            MediaFile.removed_at.is_(None),
        )
    ).all()
    return {p: f for p, f in rows}


def check(
    db: Session,
    box: SecretBox,
    group: LibraryGroup,
    view: PhysicalView,
    folder: str,
    arr: Source | None,
    paths: list[str],
    stamp: str = "preview",
    leftover: bool = False,
) -> tuple[list[Checked], list[str]]:
    """What would be moved, and what stops it. (files, problems): an empty problem list means
    every file passed. With leftover, the files are everything in a folder the arr does not
    track at all (a title removed from the arr whose files stayed), of any type."""
    problems: list[str] = []
    if not paths:
        return [], ["No files were chosen"]
    if len(paths) > MAX_FILES:
        return [], [f"At most {MAX_FILES} files can be moved at once"]
    if len(set(paths)) != len(paths):
        return [], ["The same file was chosen twice"]
    flags = unusual_flags(db, view, paths)
    tracked: set[str | None] = set()
    if leftover:
        if arr is None or get_connector_class(arr.type).role != "arr":
            return [], ["No app that tracks files was named to check this folder against"]
        if find_item(db, group, arr, folder, view.roots) is not None:
            return [], [f"{arr.name} tracks this folder, so its files are not leftovers"]
    elif any(p not in flags for p in paths):  # orphaned videos need their app to vouch for them
        if arr is None:
            return [], ["No app was named to check these files against"]
        if get_connector_class(arr.type).role != "arr":
            return [], [f"{arr.name} is not an app that tracks files"]
        item = find_item(db, group, arr, folder, view.roots)
        if item is None:
            return [], [f"{arr.name} does not track this folder, so these are not orphans of it"]
        library = next(
            (lib for lib in group_libraries(db, group.id) if lib.id == item.library_id), None
        )
        try:
            with connector_for(arr, box) as connector:
                fresh = connector.fetch_title(
                    item.external_id, item.kind, library.external_id if library else None
                )
        except ConnectorError as exc:
            return [], [f"{arr.name} could not be asked whether it has these files ({exc})"]
        pm = PathMap.from_pairs(arr.path_map or [])
        tracked = {pm.apply(f.path) for f in fresh.files}
    out: list[Checked] = []
    for path in paths:
        name = PurePosixPath(path).name
        root = library_root(path, view.roots)
        if root is None or not path.startswith(folder.rstrip("/") + "/"):
            problems.append(f"{name}: not inside this folder")
            continue
        if f"/{TRASH_DIR}/" in path or (
            configured_dir(db) and path.startswith(configured_dir(db).rstrip("/") + "/")
        ):
            problems.append(f"{name}: already in the trash")
            continue
        if os.path.islink(path) or os.path.realpath(path) != os.path.normpath(path):
            problems.append(f"{name}: is a link, which is never moved")
            continue
        if not os.path.isfile(path):
            problems.append(f"{name}: is not on disk any more")
            continue
        if not leftover and path not in flags and not is_video(path):
            problems.append(f"{name}: is not a video file")
            continue
        if path not in flags and path in tracked:
            problems.append(f"{name}: {arr.name if arr else 'an app'} has this file now")
            continue
        rel = path[len(root.rstrip("/")) + 1 :]
        out.append(
            Checked(
                path, os.path.getsize(path), _destination(db, root, stamp, rel), flags.get(path)
            )
        )
    return out, problems


def move_to_trash(
    db: Session,
    box: SecretBox,
    group: LibraryGroup,
    view: PhysicalView,
    folder: str,
    arr: Source | None,
    paths: list[str],
    leftover: bool = False,
) -> list[TrashItem]:
    """Move the files into the trash. Raises FixRefused (moving nothing) when any check fails."""
    if not enabled(db):
        raise FixRefused(
            "File changes are switched off. Turn them on under Settings → Writes first."
        )
    now = utcnow()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    files, problems = check(db, box, group, view, folder, arr, paths, stamp, leftover)
    if problems:
        raise FixRefused("Nothing was moved. " + "; ".join(problems))
    done: list[tuple[str, str]] = []
    items: list[TrashItem] = []
    try:
        for f in files:
            _move(f.path, f.destination)
            done.append((f.path, f.destination))
            items.append(
                TrashItem(
                    original_path=f.path,
                    trash_path=f.destination,
                    size=f.size,
                    label=PurePosixPath(f.path).name[:255],
                    folder=folder,
                    why=(
                        f"Left over: {arr.name} does not track this folder"
                        if leftover
                        else f"Unusual file ({ODD_LABELS[f.flag]})"
                        if f.flag
                        else f"{arr.name} tracks this folder but has no record of the file"
                    )[:200],
                    trash_root=_base(db, library_root(f.path, view.roots) or ""),
                    trashed_at=now,
                )
            )
    except OSError as exc:
        for original, moved in reversed(done):  # put back what already moved
            try:
                _move(moved, original)
            except OSError:
                log.exception("could not put %s back", original)
        raise FixRefused(
            f"Nothing was moved: {exc.strerror or exc}. Is the share mounted read-write? "
            "See Settings → Writes."
        ) from exc
    db.add_all(items)
    for f in files:
        for row in db.execute(
            select(MediaFile).where(
                MediaFile.source_id == view.disk_id,
                MediaFile.canonical_path == f.path,
                MediaFile.removed_at.is_(None),
            )
        ).scalars():
            row.removed_at = now
        _audit(
            db,
            "trash",
            f.path,
            f"Moved to the trash: left over, {arr.name} does not track this folder"
            if leftover
            else f"Moved to the trash: an unusual file ({ODD_LABELS[f.flag]})"
            if f.flag
            else f"Moved to the trash: {arr.name} has no record of it",
        )
    db.commit()
    return items


def leftover_paths(folder: str) -> list[str]:
    """Every plain file under a folder, for removing what a title left behind. Links and the
    trash folder are left alone."""
    out = []
    for top, dirs, names in os.walk(folder):
        dirs[:] = sorted(
            d for d in dirs if d != TRASH_DIR and not os.path.islink(os.path.join(top, d))
        )
        for n in sorted(names):
            path = os.path.join(top, n)
            if os.path.isfile(path) and not os.path.islink(path):
                out.append(path)
    return out


def remove_empty_dirs(folder: str) -> None:
    """Remove the folders a leftover delete emptied, deepest first, the folder itself last.
    A folder that still holds anything stays."""
    for top, _dirs, _names in sorted(os.walk(folder), key=lambda t: -t[0].count("/")):
        try:
            os.rmdir(top)
        except OSError:
            pass


def _audit(db: Session, op: str, path: str, why: str, error: str | None = None) -> None:
    name = PurePosixPath(path).name
    db.add(
        AuditLog(
            at=utcnow(),
            app_user_id=0,
            source_id=0,
            source_name="Media share",
            user_name="—",
            label=name[:255],
            item_id=path[:160],
            op=op,
            result="error" if error else "applied",
            detail=error,
            why=why[:200],
        )
    )


def _in_trash(item: TrashItem) -> bool:
    """Whether the file is inside the trash folder it was moved to (no links, no escapes)."""
    root = item.trash_root or ""
    if not root:  # moved before the trash folder was recorded: the per-library marker
        inside = f"/{TRASH_DIR}/" in item.trash_path
    else:
        inside = item.trash_path.startswith(root.rstrip("/") + "/")
    return inside and os.path.realpath(item.trash_path) == os.path.normpath(item.trash_path)


def restore(db: Session, item: TrashItem) -> str:
    """Put a trashed file back where it was. Returns a message; raises FixRefused."""
    if item.status != "trashed":
        raise FixRefused("That file is not in the trash")
    if not _in_trash(item) or not os.path.isfile(item.trash_path):
        raise FixRefused("The file is not in the trash folder any more")
    if os.path.exists(item.original_path):
        raise FixRefused("A file already exists at the original path; move or rename it first")
    try:
        os.makedirs(os.path.dirname(item.original_path), exist_ok=True)
        _move(item.trash_path, item.original_path)
    except OSError as exc:
        raise FixRefused(f"Could not put it back: {exc.strerror or exc}") from exc
    item.status, item.finished_at = "restored", utcnow()
    _audit(db, "restore", item.original_path, "Put back from the trash")
    db.commit()
    _prune(item)
    return f"Put {item.label} back."


def purge(db: Session, item: TrashItem, reason: str = "Deleted from the trash by hand") -> None:
    """Delete a trashed file for good. Raises FixRefused when it is not safely in the trash."""
    if item.status != "trashed":
        return
    if not _in_trash(item):
        raise FixRefused("That path is not in a trash folder, so it is not deleted")
    try:
        if os.path.isfile(item.trash_path):
            os.remove(item.trash_path)
    except OSError as exc:
        raise FixRefused(f"Could not delete it: {exc.strerror or exc}") from exc
    item.status, item.finished_at = "deleted", utcnow()
    _audit(db, "purge", item.original_path, reason)
    db.commit()
    _prune(item)


def _prune(item: TrashItem) -> None:
    """Remove the empty folders a trashed file left behind, never the trash folder you named."""
    folder = os.path.dirname(item.trash_path)
    root = (item.trash_root or "").rstrip("/")
    while True:
        legacy_top = os.path.basename(folder) == TRASH_DIR
        if root and (folder == root or not folder.startswith(root + "/")):
            return
        if not root and f"/{TRASH_DIR}/" not in folder + "/":
            return
        try:
            os.rmdir(folder)
        except OSError:
            return
        if legacy_top:
            return
        folder = os.path.dirname(folder)


def listing(db: Session) -> list[TrashItem]:
    return list(
        db.execute(
            select(TrashItem).where(TrashItem.status == "trashed").order_by(TrashItem.trashed_at)
        ).scalars()
    )


def days_left(item: TrashItem, now=None) -> int:
    return max(0, (item.trashed_at + RETENTION - (now or utcnow())).days)


def purge_expired(db: Session, now=None) -> int:
    """Delete what has been in the trash for the retention period. Does nothing while file
    changes are switched off."""
    if not enabled(db):
        return 0
    now = now or utcnow()
    n = 0
    for item in db.execute(
        select(TrashItem).where(
            TrashItem.status == "trashed", TrashItem.trashed_at <= now - RETENTION
        )
    ).scalars():
        try:
            purge(db, item, "Deleted from the trash after 30 days")
            n += 1
        except FixRefused:
            log.warning("could not delete %s from the trash", item.trash_path)
    return n
