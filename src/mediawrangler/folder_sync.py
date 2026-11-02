"""Bring every app up to date with one title's folder on the share, after a change to it (a
delete, a put back, a rescan).

Each app that has the folder is asked to look at just that folder, then MediaWrangler reads
what the app now lists there, not the entries it remembers, until the app agrees with the
share or its time is up. All apps run at once; each gives one answer. What was read is
stored by folder, so a copy the app re-added, put back or gave a new id cannot linger.

- Jellyfin: the folder's own entry is refreshed directly (`POST /Items/{folder}/Refresh`),
  which skips its file watcher's 60-second delay; everything under that entry is listed.
- Silo: a scan of the folder (done in about a second), then the title is read.
- Sonarr, Radarr, Sportarr: a rescan of the one title, waited on through its command id.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath

from sqlalchemy import select

from .connectors import ConnectorError
from .connectors.base import ItemGone, TitleSnapshot
from .connectors.paths import PathMap
from .ingest import apply_title, remove_title
from .models import Library, LibraryGroup, MediaFile, Source, SourceItem

log = logging.getLogger(__name__)

POLL = 1.0  # seconds between reads while an app catches up
WAIT = 60  # how long Jellyfin and Silo get to agree with the share
ARR_WAIT = 120  # how long an arr's rescan command may run


@dataclass
class Outcome:
    """One app's answer: what it now lists in the folder, to store; a note if it disagrees."""

    source_id: int
    snaps: list[TitleSnapshot] = field(default_factory=list)
    gone_titles: set[str] = field(default_factory=set)  # titles the app no longer has
    note: str | None = None
    detail: str = "Matches the share"


def _names(paths: set[str]) -> str:
    names = sorted(p.rsplit("/", 1)[-1] for p in paths)
    return ", ".join(names[:2]) + (f" and {len(names) - 2} more" if len(names) > 2 else "")


def known_titles(s, source: Source, group: LibraryGroup, folder: str, roots: list[str]):
    """The app's films or shows in this folder, as MediaWrangler last stored them (also those
    it has marked gone, so a copy put back is found under its old id)."""
    from .physical import folder_of
    from .tasks import group_libraries, held_items

    libs = [lib.id for lib in group_libraries(s, group.id) if lib.source_id == source.id]
    if not libs:
        return []
    path_map = PathMap.from_pairs(source.path_map or [])
    rows = s.execute(
        select(SourceItem)
        .where(
            SourceItem.library_id.in_(libs),
            SourceItem.kind == group.kind,
            SourceItem.path.is_not(None),
        )
        .order_by(SourceItem.removed_at.is_not(None), SourceItem.last_seen_at.desc())
    ).scalars()
    found = [r for r in rows if folder_of(path_map.apply(r.path) or "", roots) == folder]
    if found or source.type != "sportarr":
        return found
    return held_items(s, source, libs, folder)


def lists_any(s, source: Source, paths: set[str]) -> bool:
    return (
        s.execute(
            select(MediaFile.id)
            .where(
                MediaFile.source_id == source.id,
                MediaFile.canonical_path.in_(paths),
                MediaFile.removed_at.is_(None),
            )
            .limit(1)
        ).first()
        is not None
    )


def sync_app(
    manager,
    job: int,
    key: str,
    group_id: int,
    source_id: int,
    folder: str,
    roots: list[str],
    gone: set[str],
    present: set[str],
    ask: bool = True,
) -> Outcome:
    """Ask one app to look at the folder, then read it until no `gone` file is listed and
    every `present` file is (an arr is only held to `gone`: it tracks one copy per title)."""
    from . import tasks
    from .fixes import FixRefused
    from .sync import connector_for

    with manager.db.sessionmaker() as s:
        source = s.get(Source, source_id)
        group = s.get(LibraryGroup, group_id)
        out = Outcome(source_id)
        arr = source.type in ("sonarr", "radarr", "sportarr")
        titles = known_titles(s, source, group, folder, roots)
        live = [t for t in titles if t.removed_at is None]
        if not titles and not present:
            out.detail = "Has nothing in this folder"
            return out
        path_map = PathMap.from_pairs(source.path_map or [])
        kind = group.kind
        library = s.get(Library, titles[0].library_id) if titles and titles[0].library_id else None
        lib_ext = library.external_id if library else None
        token, plan, locate = None, None, None
        with connector_for(source, manager.box) as connector:
            folder_id, scan_id = None, None
            app_folder = path_map.reverse(folder) if not arr else None
            if source.type == "jellyfin" and app_folder:
                # A show whose folder is gone cannot drop itself: only a scan of the folder
                # above it does, and the read then checks the titles it held one by one.
                gone_folder = not os.path.isdir(folder)
                look = str(PurePosixPath(app_folder).parent) if gone_folder else app_folder
                try:
                    scan_id = connector.folder_entry(look, [t.external_id for t in titles])
                except ConnectorError as exc:  # the older scan request still works without it
                    log.info("could not find %s's entry for %s: %s", source.name, folder, exc)
                folder_id = None if gone_folder else scan_id
            if ask:
                try:
                    plan = tasks.plan(s, group, roots, source, folder, "scan")
                    locate = plan.locate
                    if scan_id:
                        plan = replace(plan, task=replace(plan.task, item_id=scan_id))
                    manager._step(job, key, "running", "Asking it to look at the folder…")
                    ok, message, token = tasks.run(s, manager.box, plan)
                    if not ok:  # refused: nothing is coming, so read once and say so
                        ask = False
                        out.note = (
                            f"{source.name} did not accept the scan request ({message}). The "
                            "page shows what it reports; scan from the app itself."
                        )
                except FixRefused as exc:  # writes off, or no path rule: read it anyway
                    log.info("did not ask %s to scan: %s", source.name, exc)
                    ask = False
            started = time.monotonic()
            deadline = started + (ARR_WAIT if arr else WAIT)
            while True:
                waited = int(time.monotonic() - started)
                if ask and token and not _done(connector, token) and time.monotonic() < deadline:
                    manager._step(job, key, "running", f"Scanning… {waited}s")
                    time.sleep(POLL)
                    continue
                try:
                    snaps, gone_titles = _read(
                        connector,
                        source,
                        kind,
                        lib_ext,
                        titles,
                        live,
                        folder_id,
                        locate,
                        app_folder,
                    )
                except ConnectorError as exc:  # busy or slow: try again until the time is up
                    log.info("could not read %s for %s: %s", source.name, folder, exc)
                    if time.monotonic() >= deadline:
                        out.note = f"{source.name} could not be read ({exc})."
                        return out
                    time.sleep(POLL)
                    continue
                listed = {
                    c
                    for snap in snaps
                    for f in snap.files
                    if (c := path_map.apply(f.path)) and c.startswith(folder + "/")
                }
                still = listed & gone
                missing = set() if arr else present - listed
                if (not still and not missing) or not ask or arr or time.monotonic() >= deadline:
                    break
                manager._step(job, key, "running", f"Waiting for it to catch up… {waited}s")
                time.sleep(POLL)
        out.snaps, out.gone_titles = snaps, gone_titles
        if still:
            out.note = f"{source.name} still lists {_names(still)}."
        elif missing:
            out.note = f"{source.name} does not list {_names(missing)} yet."
        if not out.note:
            n = len(listed)
            out.detail = (
                f"Matches the share ({n} {'file' if n == 1 else 'files'})"
                if not arr
                else "Confirmed"
            )
        return out


def _done(connector, token: str) -> bool:
    try:
        return connector.task_done(token)
    except ConnectorError:
        return False


def _read(connector, source, kind, lib_ext, titles, live, folder_id, locate, app_folder):
    """What the app lists for the folder now: its titles' snapshots, and the titles (by id)
    it no longer has."""
    if folder_id:  # Jellyfin: everything under the folder's own entry
        ids = connector.titles_under(folder_id) or set()
    else:
        ids = {t.external_id for t in live}
        if not ids and locate:
            try:
                found = connector.find_title(
                    locate["name"], kind, locate["library"], locate["path"]
                )
            except NotImplementedError:
                found = None
            ids = {found} if found else set()
        elif not ids and titles:
            ids = {titles[0].external_id}  # a title marked gone: see whether it is back
    snaps, gone = [], set()
    for ext in sorted(ids):
        try:
            snaps.append(connector.fetch_title(ext, kind, lib_ext))
        except ItemGone:
            gone.add(ext)
    gone |= {t.external_id for t in live if t.external_id not in ids}
    return snaps, gone


def store(s, source: Source, out: Outcome) -> None:
    for snap in out.snaps:
        apply_title(s, source, snap)
    for ext in out.gone_titles:
        remove_title(s, source, ext)
