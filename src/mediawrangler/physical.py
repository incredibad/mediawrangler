"""Physical media: which files each app picks up, per series/movie folder.

Everything is compared on canonical paths (after each source's path map). A
"folder" is the first directory under one of the group's library roots, e.g.
/media/TV/Sherlock or /media/Anime/One Pace, so Silo's per-arc roots and
Jellyfin's single One Piece folder land on the same row.

The disk scan is the ground truth; arrs say what they have on record; media
servers say what they picked up (Silo lists each file with its path, and its scanned folders).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from .connectors import connector_types
from .connectors.paths import PathMap
from .db import utcnow
from .filters import FilterSpec, apply_filter, fold
from .groups import arr_sort_key, source_order
from .models import AppSetting, LibraryGroup, MediaFile, MediaRoot, Source, SourceItem
from .presence import _episode_counts, group_libraries

SXXEXX_RE = re.compile(r"S(\d{1,4})E(\d{1,4})", re.IGNORECASE)
SAMPLES = 5
# Folders marked "not managed by an arr" on purpose (One Pace: no TVDB entry for Sonarr):
# {folder: {"reason": str, "at": iso time}}. Part of the settings export.
NOT_MANAGED_KEY = "arr_not_managed_folders"


def not_managed(db: Session) -> dict[str, dict]:
    row = db.get(AppSetting, NOT_MANAGED_KEY)
    return dict(row.value) if row and row.value else {}


def set_not_managed(db: Session, folder: str, reason: str | None) -> None:
    """Mark a folder as kept out of the arrs on purpose (reason None: remove the mark)."""
    marks = not_managed(db)
    if reason is None:
        marks.pop(folder, None)
    else:
        marks[folder] = {"reason": reason, "at": utcnow().isoformat(timespec="seconds")}
    row = db.get(AppSetting, NOT_MANAGED_KEY)
    if row is None:
        db.add(AppSetting(key=NOT_MANAGED_KEY, value=marks))
    else:
        row.value = marks


@dataclass
class FolderCell:
    files: int = 0
    size: int = 0
    # Folder-reporting sources (Silo): the scan state, skip reason and count.
    state: str | None = None
    reason: str | None = None
    reported: int | None = None
    duplicate: bool = False
    # An arr that tracks this show or film, whether or not it has files for it.
    tracked: bool = False

    @property
    def present(self) -> bool:
        return self.files > 0 or (self.state is not None and self.state != "skipped")

    @property
    def count(self) -> int | None:
        if self.files:
            return self.files
        return self.reported


@dataclass
class FolderRow:
    folder: str
    cells: dict[int, FolderCell]
    flags: set[str] = field(default_factory=set)
    search: str = ""  # what the row is searched against (see filters.fold)
    odd: list = field(default_factory=list)  # unusual files in the folder (MediaFile rows)
    # Kept out of the arrs on purpose: the reason given ("" for none); None when not marked.
    not_managed: str | None = None

    @property
    def name(self) -> str:
        return PurePosixPath(self.folder).name


@dataclass
class ColumnSummary:
    source: Source
    role: str
    folders: int = 0
    files: int = 0
    size: int = 0
    issues: dict[str, int] = field(default_factory=dict)


@dataclass
class Unmapped:
    count: int = 0
    samples: list[str] = field(default_factory=list)


@dataclass
class PhysicalView:
    group: LibraryGroup
    columns: list[Source]
    roles: dict[int, str]
    disk_id: int | None
    master_id: int | None  # the master media server, when it is in this group
    ref_id: int | None  # what numbers are compared with: the master (else the share)
    roots: list[str]
    rows: list[FolderRow]
    summaries: dict[int, ColumnSummary]
    unmapped: dict[int, Unmapped]
    outside: dict[int, Unmapped]


def _roots(libs, sources: dict[int, Source], roles: dict[int, str]) -> list[str]:
    roots = set()
    for lib in libs:
        src = sources[lib.source_id]
        for p in lib.paths or []:
            if roles[src.id] == "disk":
                roots.add(p.rstrip("/"))
            else:
                mapped = PathMap.from_pairs(src.path_map or []).apply(p)
                if mapped:
                    roots.add(mapped.rstrip("/"))
    return sorted(roots, key=len, reverse=True)


def folder_of(path: str, roots: list[str]) -> str | None:
    for root in roots:
        if path.startswith(root + "/"):
            first = path[len(root) + 1 :].split("/", 1)[0]
            return f"{root}/{first}"
    return None


def _duplicate(paths: list[str], kind: str) -> bool:
    if kind == "movie":
        return len(paths) > 1
    keys = []
    for p in paths:
        m = SXXEXX_RE.search(PurePosixPath(p).name)
        if m:
            keys.append((int(m.group(1)), int(m.group(2))))
    return len(keys) != len(set(keys))


def build_physical_view(db: Session, group: LibraryGroup) -> PhysicalView:
    libs = group_libraries(db, group.id)
    lib_ids = [lib.id for lib in libs]
    types = connector_types()
    sources = {s.id: s for s in db.execute(select(Source)).scalars()}
    roles = {
        sid: (types[s.type].role if s.type in types else "other") for sid, s in sources.items()
    }
    in_group = {lib.source_id for lib in libs}

    # Columns: media servers (master first), the share, then the arrs.
    servers = [sid for sid in source_order(db) if sid in in_group]
    disk = sorted(sid for sid in in_group if roles[sid] == "disk")
    arrs = sorted(
        (sid for sid in in_group if roles[sid] == "arr"), key=lambda i: arr_sort_key(sources[i])
    )
    column_ids = servers + disk + arrs
    columns = [sources[sid] for sid in column_ids]
    disk_id = disk[0] if disk else None
    order = source_order(db)
    master_id = order[0] if order and order[0] in in_group else None
    ref_id = master_id or disk_id
    roots = _roots(libs, sources, roles)
    marks = not_managed(db)

    cells: dict[str, dict[int, FolderCell]] = defaultdict(lambda: defaultdict(FolderCell))
    paths: dict[tuple[str, int], list[str]] = defaultdict(list)
    unmapped: dict[int, Unmapped] = defaultdict(Unmapped)
    outside: dict[int, Unmapped] = defaultdict(Unmapped)

    files = db.execute(
        select(MediaFile.source_id, MediaFile.path, MediaFile.canonical_path, MediaFile.size).where(
            MediaFile.library_id.in_(lib_ids),
            MediaFile.removed_at.is_(None),
            MediaFile.extra.is_(False),
        )
    ).all()
    for sid, raw, cpath, size in files:
        if sid not in column_ids:
            continue
        if cpath is None:
            u = unmapped[sid]
            u.count += 1
            if len(u.samples) < SAMPLES:
                u.samples.append(raw)
            continue
        folder = folder_of(cpath, roots)
        if folder is None:
            o = outside[sid]
            o.count += 1
            if len(o.samples) < SAMPLES:
                o.samples.append(cpath)
            continue
        cell = cells[folder][sid]
        cell.files += 1
        cell.size += size or 0
        paths[(folder, sid)].append(cpath)

    # Arrs also say which shows and films they track, even with no files, so a
    # folder can read "Not in Sonarr" (unknown) or "No files" (tracked, empty).
    if arrs:
        maps = {sid: PathMap.from_pairs(sources[sid].path_map or []) for sid in arrs}
        tracked = db.execute(
            select(SourceItem.source_id, SourceItem.path).where(
                SourceItem.library_id.in_(lib_ids),
                SourceItem.kind == group.kind,
                SourceItem.removed_at.is_(None),
                SourceItem.source_id.in_(arrs),
                SourceItem.path.is_not(None),
            )
        ).all()
        for sid, item_path in tracked:
            cpath = maps[sid].apply(item_path)
            folder = folder_of(cpath, roots) if cpath else None
            if folder in cells:  # only folders that already have files somewhere
                cells[folder][sid].tracked = True

    folder_sources: set[int] = set()
    roots_rows = db.execute(
        select(MediaRoot).where(MediaRoot.library_id.in_(lib_ids), MediaRoot.removed_at.is_(None))
    ).scalars()
    for r in roots_rows:
        if r.source_id not in column_ids:
            continue
        if r.canonical_path is None:
            u = unmapped[r.source_id]
            u.count += 1
            if len(u.samples) < SAMPLES:
                u.samples.append(r.path)
            continue
        folder = folder_of(r.canonical_path, roots) or (
            r.canonical_path if r.canonical_path in roots else None
        )
        if folder is None:
            continue
        cell = cells[folder][r.source_id]
        # Several Silo roots can share one folder (One Pace arcs): skipped only
        # if every root under it was skipped.
        if cell.state is None or cell.state == "skipped":
            cell.state, cell.reason = r.state, r.reason if r.state == "skipped" else None
        if r.state == "skipped":
            # Only a skipped root's count is a real file count; a scanned
            # root's observed_file_count isn't (Band of Brothers: 1 for 11).
            cell.reported = (cell.reported or 0) + (r.file_count or 0)
        folder_sources.add(r.source_id)

    # Folder-reporting sources don't list episode files, so count the episodes
    # their catalog has for each series folder instead.
    if group.kind == "series" and folder_sources:
        episodes = _episode_counts(db, lib_ids)
        series = db.execute(
            select(SourceItem.source_id, SourceItem.external_id, SourceItem.path).where(
                SourceItem.library_id.in_(lib_ids),
                SourceItem.kind == "series",
                SourceItem.removed_at.is_(None),
                SourceItem.source_id.in_(folder_sources),
                SourceItem.path.is_not(None),
            )
        ).all()
        maps = {sid: PathMap.from_pairs(sources[sid].path_map or []) for sid in folder_sources}
        for sid, ext, path in series:
            cpath = maps[sid].apply(path)
            folder = folder_of(cpath, roots) if cpath else None
            if folder is None:
                continue
            regular, specials = episodes.get((sid, ext), (0, 0))
            if regular + specials:
                cell = cells[folder][sid]
                cell.reported = (cell.reported or 0) + regular + specials

    odd: dict[str, list[MediaFile]] = defaultdict(list)
    if disk_id:
        for f in db.execute(
            select(MediaFile).where(
                MediaFile.source_id == disk_id,
                MediaFile.library_id.in_(lib_ids),
                MediaFile.removed_at.is_(None),
                MediaFile.flag.is_not(None),
            )
        ).scalars():
            where = folder_of(f.canonical_path, roots) if f.canonical_path else None
            if where:
                odd[where].append(f)

    rows: list[FolderRow] = []
    for folder, by_source in cells.items():
        row = FolderRow(
            folder,
            {sid: by_source.get(sid, FolderCell()) for sid in column_ids},
            search=fold(PurePosixPath(folder).name),
        )
        row.odd = sorted(odd.get(folder, []), key=lambda f: f.path.lower())
        if row.odd:
            row.flags.add("odd")
        for sid in column_ids:
            row.cells[sid].duplicate = _duplicate(paths.get((folder, sid), []), group.kind)
            if row.cells[sid].duplicate:
                row.flags.add("duplicate")
        disk_files = row.cells[disk_id].files if disk_id else None
        others_present = any(row.cells[sid].present for sid in column_ids if sid != disk_id)
        if disk_id and not disk_files and others_present:
            row.flags.add("not_on_disk")
        if disk_files:
            if arrs and not any(row.cells[sid].files for sid in arrs):
                if folder in marks:
                    row.not_managed = marks[folder].get("reason") or ""
                else:
                    row.flags.add("unmanaged")
            for sid in servers:
                cell = row.cells[sid]
                if cell.state == "skipped":
                    row.flags.add(f"skipped:{sid}")
                    row.flags.add("skipped")
                elif not cell.present:
                    row.flags.add(f"not_picked_up:{sid}")
                    row.flags.add("not_picked_up")
        ref = row.cells[ref_id].count if ref_id else None
        if ref is not None and row.cells[ref_id].present:
            for sid in column_ids:
                count = row.cells[sid].count
                if sid != ref_id and row.cells[sid].present and count is not None and count != ref:
                    row.flags.add("count")
                    row.flags.add(f"count:{sid}")
                    # The reference is not compared with itself, so when it is the one behind
                    # (a new episode the master has not scanned yet) flag it too: that is the
                    # cell that needs the fix.
                    if count > ref:
                        row.flags.add(f"count:{ref_id}")
        rows.append(row)
    rows.sort(key=lambda r: r.name.lower())

    summaries = {sid: ColumnSummary(sources[sid], roles[sid]) for sid in column_ids}
    for row in rows:
        for sid, cell in row.cells.items():
            summ = summaries[sid]
            if cell.present:
                summ.folders += 1
            summ.files += cell.files or cell.reported or 0
            summ.size += cell.size
            if row.not_managed is not None and summ.role == "arr" and not cell.present and disk_id:
                # Kept out of the arrs on purpose: each arr counts the share's files for it, so
                # the folder shows no difference from the master in the totals.
                summ.folders += 1
                summ.files += row.cells[disk_id].files
                summ.size += row.cells[disk_id].size
        for flag in row.flags:
            if ":" in flag:
                kind, sid = flag.split(":", 1)
                summ = summaries[int(sid)]
                summ.issues[kind] = summ.issues.get(kind, 0) + 1
    if disk_id:
        summaries[disk_id].issues["odd"] = sum(1 for r in rows if "odd" in r.flags)
    for sid in arrs:
        summaries[sid].issues["unmanaged"] = sum(1 for r in rows if "unmanaged" in r.flags)
    if disk_id:
        summaries[disk_id].issues["not_on_disk"] = sum(1 for r in rows if "not_on_disk" in r.flags)
    return PhysicalView(
        group,
        columns,
        roles,
        disk_id,
        master_id,
        ref_id,
        roots,
        rows,
        summaries,
        dict(unmapped),
        dict(outside),
    )


# Dropdown order; the default is the first. Every filter but "Everything" is
# a kind of issue, so an empty result is good news.
FILTERS: dict[str, FilterSpec] = {
    "issues": FilterSpec(
        "All issues",
        ("not_picked_up", "skipped", "unmanaged", "not_on_disk", "count", "duplicate", "odd"),
        good_when_empty=True,
    ),
    "not_picked_up": FilterSpec("On disk, not picked up by an app", ("not_picked_up",), True, True),
    "skipped": FilterSpec("Skipped by an app", ("skipped",), True, True),
    "unmanaged": FilterSpec("On disk, not in any arr", ("unmanaged",), good_when_empty=True),
    "not_on_disk": FilterSpec("Not on disk", ("not_on_disk",), good_when_empty=True),
    "count": FilterSpec("File count differs from the master", ("count",), True, True),
    "duplicate": FilterSpec("Duplicate files", ("duplicate",), good_when_empty=True),
    "odd": FilterSpec(
        "Unusual files (archives, programs, partial downloads)", ("odd",), good_when_empty=True
    ),
    "all": FilterSpec("Everything"),
}
DEFAULT_FILTER = "issues"


def filter_rows(view: PhysicalView, flt: str, q: str = "", source_id: int | None = None):
    return apply_filter(view.rows, FILTERS, flt, q, source_id)


@dataclass
class FileLine:
    path: str  # relative to the folder
    by_source: dict[int, MediaFile]
    extra: bool = False


def folder_detail(
    db: Session, view: PhysicalView, folder: str
) -> tuple[list[FileLine], list[MediaRoot]]:
    """Every file under one folder, side by side across sources."""
    lib_ids = [lib.id for lib in group_libraries(db, view.group.id)]
    prefix = folder.rstrip("/") + "/"
    rows = db.execute(
        select(MediaFile).where(
            MediaFile.library_id.in_(lib_ids),
            MediaFile.removed_at.is_(None),
            MediaFile.canonical_path.startswith(prefix, autoescape=True),
        )
    ).scalars()
    lines: dict[str, FileLine] = {}
    for f in rows:
        rel = f.canonical_path[len(prefix) :]
        line = lines.setdefault(rel, FileLine(rel, {}))
        line.by_source[f.source_id] = f
        line.extra = line.extra or f.extra
    silo_roots = [
        r
        for r in db.execute(
            select(MediaRoot).where(
                MediaRoot.library_id.in_(lib_ids), MediaRoot.removed_at.is_(None)
            )
        ).scalars()
        if r.canonical_path and (r.canonical_path == folder or r.canonical_path.startswith(prefix))
    ]
    return sorted(lines.values(), key=lambda line: (line.extra, line.path.lower())), silo_roots


def orphans(view: PhysicalView, row: FolderRow, lines: list[FileLine]) -> dict[int, list[FileLine]]:
    """Per arr that tracks this folder: the files on disk it has no record of (a duplicate
    release it did not import, a file it could not match). These are what to clean up by
    hand: MediaWrangler never changes files."""
    if view.disk_id is None:
        return {}
    out: dict[int, list[FileLine]] = {}
    for col in view.columns:
        cell = row.cells[col.id]
        if view.roles[col.id] != "arr" or not (cell.tracked or cell.files):
            continue
        missing = [
            line
            for line in lines
            if not line.extra and view.disk_id in line.by_source and col.id not in line.by_source
        ]
        if missing:
            out[col.id] = missing
    return out
