"""The metadata presence matrix: which series/movies each app has, per library group."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .connectors import connector_types
from .connectors.paths import PathMap
from .filters import FilterSpec, apply_filter, search_text
from .groups import arr_sort_key, source_order
from .matching import normalise_title
from .models import CanonicalItem, Library, LibraryGroup, LibraryGroupMember, Source, SourceItem


@dataclass
class Cell:
    state: str  # "present", "low" (unconfirmed title match), "missing"
    items: list[SourceItem] = field(default_factory=list)
    episodes: int | None = None  # regular episodes (season != 0)
    specials: int | None = None
    delta: int | None = None  # episodes minus the master's
    specials_delta: int | None = None  # compared separately: layouts differ a lot
    # Sonarr/Radarr cells that are "untracked": its items with this title that are paired
    # with a different show (same name, different IDs), so the match is probably wrong.
    look_alike: list[SourceItem] = field(default_factory=list)


@dataclass
class Row:
    canonical: CanonicalItem
    cells: dict[int, Cell]
    flags: set[str] = field(default_factory=set)
    search: str = ""  # what the row is searched against (see filters.search_text)


@dataclass
class SourceSummary:
    source: Source
    total: int = 0
    present: int = 0
    low: int = 0
    missing: int = 0
    extra: int = 0
    episode_mismatch: int = 0
    specials_mismatch: int = 0
    duplicates: int = 0
    untracked: int = 0  # arrs: titles a media server has that no arr tracks


@dataclass
class GroupView:
    group: LibraryGroup
    columns: list[Source]  # the media servers, master first
    master_id: int | None
    rows: list[Row]
    summaries: dict[int, SourceSummary]
    # (row in master only, row not in master) pairs with the same title.
    possible_mismatches: list[tuple[Row, Row]]
    # Sonarr and Radarr: they say which titles they track, but take no part in
    # the comparison (they track titles they have no files for). Their cells
    # are "tracked" or "untracked", and only titles a media server has get a row.
    # A title no arr tracks is flagged "untracked", unless its folder is marked
    # "not managed by an arr" on the Physical page.
    arrs: list[Source] = field(default_factory=list)


def group_libraries(db: Session, group_id: int) -> list[Library]:
    return list(
        db.execute(
            select(Library)
            .join(LibraryGroupMember, LibraryGroupMember.library_id == Library.id)
            .where(LibraryGroupMember.group_id == group_id, Library.removed_at.is_(None))
        ).scalars()
    )


def _episode_counts(db: Session, library_ids: list[int]) -> dict[tuple[int, str], tuple[int, int]]:
    is_special = case((SourceItem.season == 0, 1), else_=0)
    rows = db.execute(
        select(
            SourceItem.source_id,
            SourceItem.parent_external_id,
            func.count() - func.sum(is_special),
            func.sum(is_special),
        )
        .where(
            SourceItem.kind == "episode",
            SourceItem.removed_at.is_(None),
            SourceItem.library_id.in_(library_ids),
        )
        .group_by(SourceItem.source_id, SourceItem.parent_external_id)
    ).all()
    return {(sid, parent): (int(reg or 0), int(spec or 0)) for sid, parent, reg, spec in rows}


def build_group_view(db: Session, group: LibraryGroup) -> GroupView:
    libs = group_libraries(db, group.id)
    lib_ids = [lib.id for lib in libs]
    order = source_order(db)
    present_sources = {lib.source_id for lib in libs}
    columns = [db.get(Source, sid) for sid in order if sid in present_sources]
    master_id = order[0] if order and order[0] in present_sources else None
    types = connector_types()
    arrs = sorted(
        (
            s
            for s in db.execute(
                select(Source).where(Source.id.in_(present_sources)).order_by(Source.id)
            ).scalars()
            if s.type in types and types[s.type].role == "arr"
        ),
        key=arr_sort_key,
    )

    items = db.execute(
        select(SourceItem, CanonicalItem)
        .join(CanonicalItem, CanonicalItem.id == SourceItem.canonical_id)
        .where(
            SourceItem.library_id.in_(lib_ids),
            SourceItem.kind == group.kind,
            SourceItem.removed_at.is_(None),
            # Media servers only; arr and disk sources are in the physical view.
            SourceItem.source_id.in_([c.id for c in columns]),
        )
    ).all()
    episodes = _episode_counts(db, lib_ids) if group.kind == "series" else {}

    by_canon: dict[int, tuple[CanonicalItem, dict[int, list[SourceItem]]]] = {}
    for item, canon in items:
        entry = by_canon.setdefault(canon.id, (canon, defaultdict(list)))
        entry[1][item.source_id].append(item)

    if arrs:
        for item in db.execute(
            select(SourceItem).where(
                SourceItem.library_id.in_(lib_ids),
                SourceItem.kind == group.kind,
                SourceItem.removed_at.is_(None),
                SourceItem.source_id.in_([a.id for a in arrs]),
                SourceItem.canonical_id.in_(list(by_canon)),
            )
        ).scalars():
            by_canon[item.canonical_id][1][item.source_id].append(item)

    # What each arr tracks that no media server's title was paired with, by name: a media
    # server title with the same name that the arr tracks under other IDs does not match, so
    # it is flagged (no allowance for years: if the metadata differs, it is worth knowing).
    unpaired: dict[int, dict[str, list[SourceItem]]] = {a.id: defaultdict(list) for a in arrs}
    if arrs:
        for item in db.execute(
            select(SourceItem).where(
                SourceItem.library_id.in_(lib_ids),
                SourceItem.kind == group.kind,
                SourceItem.removed_at.is_(None),
                SourceItem.source_id.in_([a.id for a in arrs]),
            )
        ).scalars():
            if item.canonical_id not in by_canon:
                unpaired[item.source_id][normalise_title(item.title)].append(item)

    from .physical import not_managed  # physical imports this module

    maps = {c.id: PathMap.from_pairs(c.path_map or []) for c in columns}
    marks = not_managed(db) if arrs else {}

    rows: list[Row] = []
    for canon, members in by_canon.values():
        cells: dict[int, Cell] = {}
        for arr in arrs:
            its = members.get(arr.id, [])
            if its:
                cells[arr.id] = Cell("tracked", its)
                continue
            cells[arr.id] = cell = Cell("untracked")
            cell.look_alike = [i for i in unpaired[arr.id].get(normalise_title(canon.title), [])]
        for col in columns:
            its = members.get(col.id, [])
            if not its:
                cells[col.id] = Cell("missing")
                continue
            confident = [i for i in its if i.match_confidence != "low"]
            cell = Cell("present" if confident else "low", its)
            if group.kind == "series":
                reg = spec = 0
                for i in its:
                    r, s = episodes.get((col.id, i.external_id), (0, 0))
                    reg, spec = reg + r, spec + s
                cell.episodes, cell.specials = reg, spec
            cells[col.id] = cell
        row = Row(canon, cells, search=search_text(canon.title))
        for arr in arrs:
            if cells[arr.id].look_alike:
                row.flags.update({"mismatch", f"arr_mismatch:{arr.id}"})
        if arrs and not any(cells[a.id].state == "tracked" for a in arrs):
            folders = {
                maps[c.id].apply(i.path) for c in columns for i in cells[c.id].items if i.path
            } - {None}
            if not any(f == m or f.startswith(m + "/") for f in folders for m in marks):
                row.flags.add("untracked")
        master_cell = cells.get(master_id) if master_id else None
        if master_cell is not None and master_cell.state == "missing":
            row.flags.add("not_in_master")
        for col in columns:
            cell = cells[col.id]
            if len(cell.items) > 1:
                row.flags.add("duplicate")
            if col.id == master_id:
                continue
            if cell.state == "low":
                row.flags.add("low")
            if master_cell is not None and master_cell.state != "missing":
                if cell.state == "missing":
                    row.flags.add("missing")
                    row.flags.add(f"missing:{col.id}")
                elif cell.episodes is not None and master_cell.episodes is not None:
                    cell.delta = cell.episodes - master_cell.episodes
                    if cell.delta:
                        row.flags.add("episodes")
                    cell.specials_delta = (cell.specials or 0) - (master_cell.specials or 0)
                    if cell.specials_delta:
                        row.flags.add("specials")
        rows.append(row)

    # An arr that tracks a title under other IDs than the media servers' gets its own row, so
    # the difference reads like any other: tracked there, not found in the media servers.
    by_title: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        by_title[normalise_title(row.canonical.title)].append(row)
    arr_only: dict[int, Row] = {}
    arr_pairs: list[tuple[Row, Row]] = []
    for arr in arrs:
        for title, twins in by_title.items():
            for item in unpaired[arr.id].get(title, []):
                if item.canonical_id is None:
                    continue
                # An arr that also tracks the version the media servers have, and has no files
                # for this one, is only keeping a second title (a remake, a list sync): there is
                # nothing in the apps to compare it with, so it is no mismatch.
                if item.source_status != "has_files" and any(
                    t.cells[arr.id].state == "tracked" for t in twins
                ):
                    continue
                row = arr_only.get(item.canonical_id)
                if row is None:
                    canon = db.get(CanonicalItem, item.canonical_id)
                    if canon is None:
                        continue
                    cells = {a.id: Cell("untracked") for a in arrs}
                    cells.update({c.id: Cell("missing") for c in columns})
                    row = Row(canon, cells, {"not_in_master", "mismatch"}, search_text(canon.title))
                    arr_only[item.canonical_id] = row
                    rows.append(row)
                    arr_pairs.extend((twin, row) for twin in twins)
                cell = row.cells[arr.id]
                cell.state = "tracked"
                cell.items.append(item)

    rows.sort(key=lambda r: (normalise_title(r.canonical.title), r.canonical.year or 0))

    summaries = {col.id: SourceSummary(col) for col in columns}
    for row in rows:
        master_cell = row.cells.get(master_id) if master_id else None
        in_master = master_cell is not None and master_cell.state != "missing"
        for col in columns:
            cell, summ = row.cells[col.id], summaries[col.id]
            if cell.state != "missing":
                summ.total += 1
            if len(cell.items) > 1:
                summ.duplicates += 1
            if col.id == master_id:
                continue
            if cell.state == "present":
                summ.present += 1 if in_master else 0
                summ.extra += 0 if in_master else 1
            elif cell.state == "low":
                summ.low += 1
            elif in_master:
                summ.missing += 1
            if cell.delta:
                summ.episode_mismatch += 1
            if cell.specials_delta:
                summ.specials_mismatch += 1

    # Same title, different identity: one side only in the master, the other
    # only elsewhere. The classic case is a show matched to the wrong ID.
    mismatches: list[tuple[Row, Row]] = list(arr_pairs)
    if master_id:
        master_only = defaultdict(list)
        for row in rows:
            if "not_in_master" not in row.flags and any(
                f.startswith("missing:") for f in row.flags
            ):
                master_only[normalise_title(row.canonical.title)].append(row)
        for row in rows:
            if "not_in_master" in row.flags:
                for other in master_only.get(normalise_title(row.canonical.title), []):
                    mismatches.append((other, row))
                    other.flags.add("mismatch")
                    row.flags.add("mismatch")

    # Same file, different identity: two apps play one film from the share but matched it to
    # different titles, so the rows share no name to pair them by.
    if group.kind == "movie":
        by_file: dict[str, list[Row]] = defaultdict(list)
        for row in rows:
            files = {
                maps[c.id].apply(i.path)
                for c in columns
                for i in row.cells[c.id].items
                if i.path and row.cells[c.id].state != "missing"
            }
            for f in files - {None}:
                by_file[f].append(row)
        for twins in by_file.values():
            for other in twins[1:]:
                if (twins[0], other) not in mismatches and (other, twins[0]) not in mismatches:
                    mismatches.append((twins[0], other))
                twins[0].flags.add("mismatch")
                other.flags.add("mismatch")

    for arr in arrs:
        summaries[arr.id] = SourceSummary(arr)
        summaries[arr.id].total = sum(r.cells[arr.id].state == "tracked" for r in rows)
        summaries[arr.id].untracked = sum("untracked" in r.flags for r in rows)
    return GroupView(group, columns, master_id, rows, summaries, mismatches, arrs)


# Dropdown order; the default is the first. Every filter but "Everything" is
# a kind of issue, so an empty result is good news.
FILTERS: dict[str, FilterSpec] = {
    "issues": FilterSpec(
        "All issues",
        (
            "missing",
            "not_in_master",
            "low",
            "mismatch",
            "episodes",
            "specials",
            "duplicate",
            "untracked",
        ),
        good_when_empty=True,
    ),
    "missing": FilterSpec("Missing somewhere", ("missing",), True, True),
    "not_in_master": FilterSpec("Not in master", ("not_in_master",), good_when_empty=True),
    "low": FilterSpec("Unconfirmed matches", ("low",), good_when_empty=True),
    "mismatch": FilterSpec("Possible mismatches", ("mismatch",), good_when_empty=True),
    "episodes": FilterSpec("Episode count differs", ("episodes",), good_when_empty=True),
    "specials": FilterSpec("Specials differ", ("specials",), good_when_empty=True),
    "duplicate": FilterSpec("Duplicates", ("duplicate",), good_when_empty=True),
    "untracked": FilterSpec("Not in any arr", ("untracked",), good_when_empty=True),
    "all": FilterSpec("Everything"),
}
DEFAULT_FILTER = "issues"


def filter_rows(view: GroupView, flt: str, q: str = "", source_id: int | None = None) -> list[Row]:
    return apply_filter(view.rows, FILTERS, flt, q, source_id)
