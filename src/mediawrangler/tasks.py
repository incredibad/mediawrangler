"""Asking an app to look again, from the Physical page (0.11).

Two kinds of request: "scan" (a media server reads a folder for new or changed files; an arr
re-reads one show or film) and "refresh" (a media server downloads an item's details again).
Like every fix, nothing is sent until the person has seen the exact call and pressed the
button; the write needs the global write switch on, and each attempt goes in the audit log.
Neither request touches files or watched history.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from .connectors import Capability, ConnectorError, get_connector_class
from .connectors.base import Task
from .connectors.paths import PathMap
from .crypto import SecretBox
from .db import utcnow
from .fixes import FixRefused
from .models import AuditLog, LibraryGroup, MediaFile, Source, SourceItem
from .physical import folder_of
from .presence import group_libraries
from .sync import connector_for
from .writes import ENABLED_KEY, PAUSED_KEY, flag

CAPABILITY = {"scan": Capability.SCAN, "refresh": Capability.REFRESH}


def capable(source: Source, kind: str) -> bool:
    try:
        return CAPABILITY[kind] in get_connector_class(source.type).capabilities
    except Exception:
        return False


@dataclass
class Plan:
    source: Source
    task: Task
    kind: str
    folder: str
    item: SourceItem | None
    title: str  # the dialog's heading, also the button
    explanation: str
    sentence: str  # what the audit log says
    # How to find the app's entry afterwards when it has none yet (a scan that adds one).
    locate: dict | None = None

    @property
    def name(self) -> str:
        return PurePosixPath(self.folder).name


def find_item(
    db: Session, group: LibraryGroup, source: Source, folder: str, roots: list[str]
) -> SourceItem | None:
    """The app's own entry for the show or film that lives in this folder."""
    libs = [lib.id for lib in group_libraries(db, group.id) if lib.source_id == source.id]
    if not libs:
        return None
    path_map = PathMap.from_pairs(source.path_map or [])
    items = db.execute(
        select(SourceItem).where(
            SourceItem.library_id.in_(libs),
            SourceItem.kind == group.kind,
            SourceItem.removed_at.is_(None),
            SourceItem.path.is_not(None),
        )
    ).scalars()
    for item in items:
        cpath = path_map.apply(item.path)
        if cpath and folder_of(cpath, roots) == folder:
            return item
    if source.type == "sportarr":
        return next(iter(held_items(db, source, libs, folder, live=True)), None)
    return None


def held_items(db: Session, source: Source, libs: list[int], folder: str, live: bool = False):
    """Sportarr's leagues carry no folder: the ones that hold files in it, newest first."""
    held = select(MediaFile.item_external_id).where(
        MediaFile.source_id == source.id, MediaFile.canonical_path.startswith(folder + "/")
    )
    q = select(SourceItem).where(
        SourceItem.source_id == source.id,
        SourceItem.library_id.in_(libs),
        SourceItem.external_id.in_(held),
    )
    if live:
        q = q.where(SourceItem.removed_at.is_(None))
    return list(
        db.execute(
            q.order_by(SourceItem.removed_at.is_not(None), SourceItem.last_seen_at.desc())
        ).scalars()
    )


def plan(
    db: Session, group: LibraryGroup, roots: list[str], source: Source, folder: str, kind: str
) -> Plan:
    if kind not in CAPABILITY or not capable(source, kind):
        raise FixRefused(f"{source.name} cannot do that from here")
    role = get_connector_class(source.type).role
    item = find_item(db, group, source, folder, roots)
    if kind == "refresh":
        if item is None:
            raise FixRefused(f"{source.name} has no entry for this folder to refresh")
        return Plan(
            source,
            Task("refresh", item_id=item.external_id),
            kind,
            folder,
            item,
            f"Re-download {source.name}'s details for this show",
            f"{source.name} will fetch its details for “{item.title}” again: names, episode "
            "lists and images. It keeps its current match (to change that, use the Metadata "
            "page). Files and watched history are not touched.",
            f"Asked {source.name} to download its details for “{item.title}” again",
        )
    if source.type == "sportarr":  # its leagues carry no folder, so any folder can ask
        return Plan(
            source,
            Task("scan"),
            kind,
            folder,
            item,
            f"Ask {source.name} to rescan its library",
            f"{source.name} cannot rescan one league, so it will run its Disk Scan: it checks "
            "every file it tracks is still there and looks for files it does not track yet. "
            "It runs in the background and can take a few minutes; MediaWrangler reads "
            f"{source.name} again straight away, so the page may only catch up on its next sync. "
            "No files are changed.",
            f"Asked {source.name} to run its Disk Scan (for {PurePosixPath(folder).name})",
        )
    if role == "arr":
        if item is None:
            raise FixRefused(
                f"{source.name} does not track this folder, so it has nothing to rescan"
            )
        return Plan(
            source,
            Task("scan", item_id=item.external_id),
            kind,
            folder,
            item,
            f"Ask {source.name} to rescan this show's folder",
            f"{source.name} will re-read the files in the folder for “{item.title}” and update "
            "which episodes it has on disk. No files are changed.",
            f"Asked {source.name} to rescan the folder of “{item.title}”",
        )
    path_map = PathMap.from_pairs(source.path_map or [])
    own = [lib for lib in group_libraries(db, group.id) if lib.source_id == source.id]
    lib = next(
        (
            lib
            for lib in own
            if any(
                (mapped := path_map.apply(p)) and folder.startswith(mapped.rstrip("/") + "/")
                for p in lib.paths or []
            )
        ),
        own[0] if own else None,
    )
    if lib is None:
        raise FixRefused(f"{source.name} has no library in this group to scan")
    app_path = path_map.reverse(folder)
    if app_path is None:
        raise FixRefused(
            f"No path rule translates this folder into {source.name}'s paths; "
            "set the library up on the Libraries page first"
        )
    if source.type == "silo" and not os.path.isdir(folder):
        # Silo refuses (HTTP 400) to scan a path that no longer exists, so a folder deleted from
        # the share cannot be scanned by path. Scanning the library is what lets it notice.
        return Plan(
            source,
            Task("scan", library_id=lib.external_id),
            kind,
            folder,
            item,
            f"Ask {source.name} to scan the whole {lib.name} library",
            f"This folder is no longer on the share, and {source.name} will not scan a path that "
            f"does not exist, so it will scan its whole {lib.name} library instead. That reads "
            "every folder in the library, so it takes longer than a one-folder scan, and it "
            "should notice what is gone. It only reads; no files are changed. MediaWrangler "
            f"then reads {source.name} again and updates the page.",
            f"Asked {source.name} to scan its {lib.name} library (the folder is gone)",
            locate=None,
        )
    return Plan(
        source,
        Task("scan", path=app_path, library_id=lib.external_id),
        kind,
        folder,
        item,
        f"Ask {source.name} to look for new files",
        f"{source.name} will read its folder {app_path} for new or changed files. It only "
        "reads the folder; no files are changed. MediaWrangler then reads "
        f"{source.name} again and updates the page.",
        f"Asked {source.name} to look for new files in {app_path}",
        locate=(
            None
            if item is not None
            else {
                "name": re.sub(r"\s*\(\d{4}\)\s*$", "", PurePosixPath(folder).name),
                "kind": group.kind,
                "library": lib.external_id,
                "path": app_path,
            }
        ),
    )


def preview_call(box: SecretBox, p: Plan) -> tuple[str, str, object]:
    with connector_for(p.source, box) as connector:
        method, path, body = connector.task_call(p.task)
    return method, f"{p.source.base_url.rstrip('/')}{path}", body


def run(db: Session, box: SecretBox, p: Plan) -> tuple[bool, str, str | None]:
    """Send the request. Returns (ok, message, the app's id for the task if it gave one);
    always writes the audit log."""
    if not flag(db, ENABLED_KEY):
        raise FixRefused(
            "Writes to your apps are switched off. Turn them on under Settings → Writes first."
        )
    if flag(db, PAUSED_KEY):
        raise FixRefused("Writes are paused. Resume them under Settings → Writes first.")
    error, token = None, None
    try:
        with connector_for(p.source, box) as connector:
            token = connector.run_task(p.task)
    except ConnectorError as exc:
        error = str(exc)[:240]
    db.add(
        AuditLog(
            at=utcnow(),
            app_user_id=0,
            source_id=p.source.id,
            source_name=p.source.name,
            user_name="—",
            label=(p.item.title if p.item else p.name)[:255],
            item_id=(p.task.item_id or p.task.path or p.folder)[:160],
            op=p.kind,
            result="error" if error else "applied",
            detail=error,
            why=p.sentence[:200],
        )
    )
    db.commit()
    if error:
        return False, error, None
    return True, f"{p.sentence}.", token
