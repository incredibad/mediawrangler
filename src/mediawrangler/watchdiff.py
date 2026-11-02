"""Per-user watch-state comparison across media servers.

Episodes are identified by (canonical series, season, episode) and movies by
canonical movie, so the same episode lines up across apps whatever their own
ids are. Two rules keep false differences out (the old per-episode TVDB
comparison produced ~1,200 each way):
- an episode only counts as a difference when both apps have it;
- specials (season 0) are counted with the regular episodes.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .connectors import get_connector_class
from .filters import FilterSpec, apply_filter, search_text
from .groups import source_order
from .importers import (
    KINDS,
    canonical_lookup,
    landing_groups,
    library_canonicals,
    resolve,
    title_key,
)
from .matching import normalise_title
from .models import (
    AppUser,
    CanonicalItem,
    LibraryGroup,
    LocalState,
    Source,
    SourceItem,
    SourceUser,
    WatchImport,
    WatchImportItem,
    WatchImportPlay,
    WatchState,
)
from .presence import group_libraries

Key = tuple  # ("ep", series canonical id, season, episode) or ("mv", canonical id)
_EPOCH = datetime(1970, 1, 1)


@dataclass
class Watch:
    played: bool
    position: float | None = None
    duration: float | None = None
    last_played_at: datetime | None = None
    plays: int = 0  # recorded plays (imports only: a file can list every rewatch)

    @property
    def in_progress(self) -> bool:
        return not self.played and bool(self.position)


@dataclass
class Column:
    source: Source
    user: SourceUser
    available: set = field(default_factory=set)
    state: dict = field(default_factory=dict)  # Key -> Watch
    # Series this app has in the group, episodes or not.
    series: set = field(default_factory=set)


@dataclass
class Cell:
    available: int = 0
    played: int = 0
    in_progress: int = 0
    specials: int = 0  # of `available`, how many are season 0
    plays: int = 0  # recorded plays of the played ones (imports only)
    # Compared with the reference column, on episodes both apps have:
    missing: int = 0  # played in the reference, not here
    extra: int = 0  # played here, not in the reference
    local_diff: int = 0  # on titles this app and Local both have: played in one only
    # Movies
    present: bool = False
    watch: Watch | None = None


@dataclass
class ApplyFrom:
    """The app whose watched state would be applied to the others."""

    column: Column
    played: int
    available: int
    second: Column | None = None  # the runner-up, for the explanation
    second_played: int = 0


@dataclass
class Row:
    canonical: CanonicalItem
    cells: dict[int, Cell]
    apply_from: ApplyFrom | None = None
    flags: set[str] = field(default_factory=set)
    search: str = ""  # what the row is searched against (see search_text)


@dataclass
class Summary:
    available: int = 0  # episodes (or films) the app has in this group
    played: int = 0
    in_progress: int = 0
    agree: int = 0
    missing: int = 0
    extra: int = 0


@dataclass
class WatchView:
    group: LibraryGroup
    person: AppUser
    columns: list[Column]
    ref_id: int | None
    rows: list[Row]
    summaries: dict[int, Summary]
    unreadable: list[tuple[Source, SourceUser]]


def readable_users(db: Session, person: AppUser) -> dict[int, SourceUser]:
    users = db.execute(
        select(SourceUser)
        .where(
            SourceUser.app_user_id == person.id,
            SourceUser.removed_at.is_(None),
            SourceUser.watch_readable.is_(True),
        )
        .order_by(SourceUser.id)
    ).scalars()
    out: dict[int, SourceUser] = {}
    for su in users:
        out.setdefault(su.source_id, su)  # first profile of a source wins
    return out


def _load_column(db: Session, col: Column, lib_ids: list[int], kind: str) -> dict[Key, int]:
    """Fill `available` and `state`; return watch-id -> key.

    Reads plain columns, not ORM objects: building tens of thousands of
    SourceItem instances was most of the page's load time."""
    wk = get_connector_class(col.source.type).watch_key
    items = db.execute(
        select(
            SourceItem.kind,
            SourceItem.external_id,
            SourceItem.parent_external_id,
            SourceItem.season,
            SourceItem.episode,
            SourceItem.canonical_id,
        ).where(
            SourceItem.source_id == col.source.id,
            SourceItem.library_id.in_(lib_ids),
            SourceItem.removed_at.is_(None),
            SourceItem.kind.in_(("series", "episode") if kind == "series" else ("movie",)),
        )
    ).all()
    by_watch_id: dict[str, Key] = {}
    if kind == "series":
        series_canon = {ext: canon for k, ext, _p, _s, _e, canon in items if k == "series"}
        col.series = {c for c in series_canon.values() if c is not None}
        for k, ext, parent, season, episode, _canon in items:
            if k != "episode" or season is None or episode is None:
                continue
            canon = series_canon.get(parent)
            if canon is None:
                continue
            key = ("ep", canon, season, episode)
            col.available.add(key)
            by_watch_id[wk(ext)] = key
    else:
        for _k, _ext, _p, _s, _e, canon in items:
            if canon is not None:
                col.available.add(("mv", canon))
        # A film counts as watched if any copy of it in this app was, in any
        # library: Jellyfin keeps the 1080p and 4K copies as separate items,
        # Silo merges them, and either way the person has seen the film.
        all_copies = db.execute(
            select(SourceItem.external_id, SourceItem.canonical_id).where(
                SourceItem.source_id == col.source.id,
                SourceItem.kind == "movie",
                SourceItem.removed_at.is_(None),
                SourceItem.canonical_id.is_not(None),
            )
        ).all()
        for ext, canon in all_copies:
            by_watch_id[wk(ext)] = ("mv", canon)
    for item, played, position, duration, last in db.execute(
        select(
            WatchState.item_external_id,
            WatchState.played,
            WatchState.position_seconds,
            WatchState.duration_seconds,
            WatchState.last_played_at,
        ).where(WatchState.source_user_id == col.user.id)
    ).all():
        key = by_watch_id.get(item)
        if key is None:
            continue
        watch = Watch(played, position, duration, last)
        old = col.state.get(key)
        # Several copies: played beats in progress beats nothing; latest play wins.
        if old is None or (watch.played, watch.last_played_at or _EPOCH) > (
            old.played,
            old.last_played_at or _EPOCH,
        ):
            col.state[key] = watch
    return by_watch_id


IMPORT_ID_BASE = 1_000_000  # an import's column id is this plus its import id
LOCAL_ID = 2_000_000  # the Local column's source id


def _local_column(db: Session, person: AppUser, columns: list[Column]) -> Column | None:
    """MediaWrangler's own record of this person (see local.py), once it has
    been filled. Like a file import it has no opinion on what exists."""
    from .local import local_states  # local.py builds on this module

    if person.local_seeded_at is None:
        return None
    states = local_states(db, person)
    every_key = set().union(*(c.available for c in columns))
    state: dict = {}
    for lkey, watch in states.items():
        key = lkey if lkey[0] == "ep" else (lkey[0], lkey[1])
        if key in every_key:
            state[key] = watch
    updated = db.execute(
        select(func.max(LocalState.updated_at)).where(LocalState.app_user_id == person.id)
    ).scalar()
    src = Source(id=LOCAL_ID, type="local", name="Local")
    user = SourceUser(id=LOCAL_ID, watch_synced_at=updated or person.local_seeded_at)
    in_group_series = set().union(*(c.series for c in columns))
    return Column(src, user, set(every_key), state, set(in_group_series))


def _import_columns(
    db: Session, person: AppUser, group: LibraryGroup, columns: list[Column], anime: dict
) -> tuple[list[Column], dict[int, CanonicalItem]]:
    """One column per file import of this person, and the titles only the
    imports have. A file only lists what was watched, so, unlike an app, it
    has no opinion on what exists: every title the apps have counts as present
    and anything not listed as unplayed. A title no app has goes in the group
    set to take that kind of title (Settings → Library groups) as a row with
    "not in app" under every real app; ones with no group are left to the "Not
    in any app" tab. A Yamtrack anime entry only says how many episodes were
    watched, so it counts as the first N episodes of the season the anime ID
    list maps it to."""
    imports = db.execute(
        select(WatchImport).where(WatchImport.app_user_id == person.id).order_by(WatchImport.kind)
    ).scalars()
    in_group_series = set().union(*(c.series for c in columns))
    in_group_movies = {k[1] for c in columns for k in c.available if k[0] == "mv"}
    every_key = set().union(*(c.available for c in columns))
    wanted = "episode" if group.kind == "series" else "movie"
    mine = {landing for landing, gid in landing_groups(db).items() if gid == group.id}
    in_library = library_canonicals(db)
    lookup = None
    extra: dict[int, CanonicalItem] = {}  # titles only the imports have
    pseudo: dict[tuple, int] = {}  # ... that no app has ever known: negative ids
    out = []
    for imp in imports:
        lookup = lookup or canonical_lookup(db)
        pseudo_src = Source(
            id=IMPORT_ID_BASE + imp.id, type="import", name=KINDS.get(imp.kind, imp.kind)
        )
        user = SourceUser(id=IMPORT_ID_BASE + imp.id, watch_synced_at=imp.imported_at)
        col = Column(pseudo_src, user, set(every_key), {}, set(in_group_series))

        def played(key, when, plays=0, col=col) -> None:
            col.available.add(key)
            old = col.state.get(key)
            if old is None or (when or _EPOCH) > (old.last_played_at or _EPOCH):
                col.state[key] = Watch(True, last_played_at=when, plays=plays)

        recorded = dict(
            db.execute(
                select(WatchImportPlay.item_id, func.count())
                .join(WatchImportItem, WatchImportItem.id == WatchImportPlay.item_id)
                .where(WatchImportItem.import_id == imp.id)
                .group_by(WatchImportPlay.item_id)
            ).all()
        )
        for item in db.execute(
            select(WatchImportItem).where(
                WatchImportItem.import_id == imp.id,
                WatchImportItem.kind.in_((wanted, "anime")),
            )
        ).scalars():
            n_plays = recorded.get(item.id, 0)
            res = resolve(item, lookup, anime)
            if res.kind != ("series" if wanted == "episode" else "movie"):
                continue
            rec, cid = res.rec, res.cid
            if cid is not None and cid in in_library:
                if cid not in (in_group_series if wanted == "episode" else in_group_movies):
                    continue  # an app has it, in another group
            elif res.landing not in mine:
                continue  # no app has it, and this isn't the list for it
            else:
                if cid is None:
                    ident = (wanted, *title_key(rec, None))
                    cid = pseudo.setdefault(ident, -(len(pseudo) + 1))
                if cid not in extra and cid < 0:
                    extra[cid] = CanonicalItem(
                        id=cid, kind=group.kind, title=rec.title, year=rec.year, key=f"import:{cid}"
                    )
                elif cid < 0 and extra[cid].year is None and rec.year:
                    extra[cid].year = rec.year
            if wanted == "episode":
                if rec.season is None:
                    continue
                if rec.episode is not None:
                    played(("ep", cid, rec.season, rec.episode), rec.watched_at, n_plays)
                else:  # anime: the first `progress` episodes of the season
                    for n in range(1, (rec.progress or 0) + 1):
                        played(("ep", cid, rec.season, n), rec.watched_at)
            else:
                played(("mv", cid), rec.watched_at, n_plays)
        out.append(col)
    return out, extra


def _apply_from(columns: list[Column], row: Row) -> ApplyFrom | None:
    """Which source (an app, an imported file, or Local) has the most progress
    on a title. Local wins a tie (it is the record of truth), then the master, then
    the order of the columns. Named even when everything agrees, so every
    watched title says where its state comes from; nothing if nothing was
    played."""
    here = [(c, row.cells[c.source.id]) for c in columns if row.cells[c.source.id].present]
    # stable: Local first on ties, then the master
    ranked = sorted(here, key=lambda pair: (-pair[1].played, pair[0].source.type != "local"))
    if not ranked or ranked[0][1].played == 0:
        return None
    best, runner = ranked[0], (ranked[1] if len(ranked) > 1 else None)
    return ApplyFrom(
        best[0],
        best[1].played,
        best[1].available,
        runner[0] if runner else None,
        runner[1].played if runner else 0,
    )


def build_watch_view(
    db: Session, person: AppUser, group: LibraryGroup, anime: dict | None = None
) -> WatchView:
    libs = group_libraries(db, group.id)
    lib_ids = [lib.id for lib in libs]
    in_group = {lib.source_id for lib in libs}
    readable = readable_users(db, person)
    sources = {s.id: s for s in db.execute(select(Source)).scalars()}
    order = [sid for sid in source_order(db) if sid in in_group]
    columns = [Column(sources[sid], readable[sid]) for sid in order if sid in readable]
    unreadable = []
    for su in db.execute(
        select(SourceUser).where(
            SourceUser.app_user_id == person.id,
            SourceUser.removed_at.is_(None),
            SourceUser.watch_readable.is_(False),
        )
    ).scalars():
        if su.source_id in in_group and su.source_id not in readable:
            unreadable.append((sources[su.source_id], su))
    for col in columns:
        _load_column(db, col, lib_ids, group.kind)
    extra: dict[int, CanonicalItem] = {}
    if columns:
        imported, extra = _import_columns(db, person, group, columns, anime or {})
        local = _local_column(db, person, columns + imported)  # it holds the files' titles too
        columns += imported + ([local] if local else [])  # Local last, by Apply from
    ref_id = columns[0].source.id if columns else None
    ref = columns[0] if columns else None

    # Every title some app has in this group, watched or not. (Watch state for
    # a film can come from a copy in another group, so rows follow `available`.)
    by_canon: dict[int, dict[int, set]] = defaultdict(lambda: defaultdict(set))
    for col in columns:
        for key in col.available:
            by_canon[key[1]][col.source.id].add(key)
        for cid in col.series:  # a series with no episodes is still a title
            by_canon[cid].setdefault(col.source.id, set())
    canon = {
        c.id: c
        for c in db.execute(
            select(CanonicalItem).where(CanonicalItem.id.in_([c for c in by_canon if c > 0]))
        ).scalars()
    }
    canon.update(extra)

    local_col = next((c for c in columns if c.source.type == "local"), None)
    rows: list[Row] = []
    summaries = {col.source.id: Summary() for col in columns}
    for cid, keys_by_source in by_canon.items():
        if cid not in canon:
            continue
        row = Row(canon[cid], {}, search=search_text(canon[cid].title))
        for col in columns:
            cell = Cell()
            if group.kind == "series":
                mine = keys_by_source.get(col.source.id, set())
                cell.available = len(mine)
                cell.present = col.source.id in keys_by_source
                for k in mine:
                    cell.specials += k[2] == 0
                    w = col.state.get(k)
                    if w is None:
                        continue
                    if w.played:
                        cell.played += 1
                        cell.plays += w.plays
                    elif w.in_progress:
                        cell.in_progress += 1
                if ref is not None and col is not ref:
                    both = mine & keys_by_source.get(ref.source.id, set())
                    for k in both:
                        here = col.state.get(k)
                        there = ref.state.get(k)
                        a, b = bool(here and here.played), bool(there and there.played)
                        if a == b:
                            continue
                        if b and not a:
                            cell.missing += 1
                        else:
                            cell.extra += 1
                    if cell.missing or cell.extra:
                        row.flags.add("differs")
                        row.flags.add(f"differs:{col.source.id}")
                if local_col is not None and col.source.type not in ("import", "local"):
                    for k in mine:
                        here, there = col.state.get(k), local_col.state.get(k)
                        cell.local_diff += bool(here and here.played) != bool(
                            there and there.played
                        )
            else:
                key = ("mv", cid)
                cell.present = key in col.available
                cell.watch = col.state.get(key)
                cell.played = int(bool(cell.watch and cell.watch.played))
                cell.plays = cell.watch.plays if cell.watch and cell.watch.played else 0
                cell.in_progress = int(bool(cell.watch and cell.watch.in_progress))
                if ref is not None and col is not ref and cell.present and key in ref.available:
                    there = ref.state.get(key)
                    a, b = bool(cell.played), bool(there and there.played)
                    if a != b:
                        if b:
                            cell.missing = 1
                        else:
                            cell.extra = 1
                        row.flags.add("differs")
                        row.flags.add(f"differs:{col.source.id}")
                if (
                    local_col is not None
                    and col.source.type not in ("import", "local")
                    and cell.present
                ):
                    there = local_col.state.get(key)
                    cell.local_diff = int(bool(cell.played) != bool(there and there.played))
            if cell.local_diff:
                row.flags.add("differs_local")
                row.flags.add(f"differs_local:{col.source.id}")
            if cell.in_progress:
                row.flags.add("in_progress")
            row.cells[col.source.id] = cell
        if any(c.played or c.in_progress for c in row.cells.values()):
            row.flags.add("watched")
        if not any(
            row.cells[c.source.id].present
            for c in columns
            if c.source.type not in ("import", "local")
        ):
            row.flags.add("elsewhere")  # only an imported history has this title
        row.apply_from = _apply_from(columns, row)
        rows.append(row)
        for col in columns:
            cell, summ = row.cells[col.source.id], summaries[col.source.id]
            summ.played += cell.played
            summ.in_progress += cell.in_progress
            summ.available += cell.available if group.kind == "series" else int(cell.present)
            summ.missing += cell.missing
            summ.extra += cell.extra
    for col in columns:
        if col is ref:
            continue
        summ = summaries[col.source.id]
        both = col.available & ref.available
        summ.agree = sum(
            1
            for k in both
            if bool(col.state.get(k) and col.state[k].played)
            and bool(ref.state.get(k) and ref.state[k].played)
        )
    rows.sort(key=lambda r: (normalise_title(r.canonical.title), r.canonical.year or 0))
    return WatchView(group, person, columns, ref_id, rows, summaries, unreadable)


# Dropdown order. "All titles" has no flags, so it matches every row.
FILTERS: dict[str, FilterSpec] = {
    "diffs": FilterSpec("All differences", ("differs",), True, True),
    "differs_local": FilterSpec("Differs from Local", ("differs_local",), True, True),
    "in_progress": FilterSpec("In progress", ("in_progress",)),
    "watched": FilterSpec("Watched in any app", ("watched",)),
    "elsewhere": FilterSpec("Not in any app", ("elsewhere",)),
    "all": FilterSpec("All titles"),
}
DEFAULT_FILTER = "diffs"


def filter_rows(view: WatchView, flt: str, q: str = "", source_id: int | None = None) -> list[Row]:
    return apply_filter(view.rows, FILTERS, flt, q, source_id)


@dataclass
class EpisodeLine:
    season: int
    episode: int
    title: str | None
    cells: dict[int, tuple[bool, Watch | None]]  # source -> (available, watch)
    differs: bool = False


def series_detail(db: Session, view: WatchView, canonical_id: int) -> list[EpisodeLine]:
    lines: dict[tuple[int, int], EpisodeLine] = {}
    for col in view.columns:
        for key in col.available:
            if key[0] == "ep" and key[1] == canonical_id:
                lines.setdefault((key[2], key[3]), EpisodeLine(key[2], key[3], None, {}))
    # Episode titles from the reference app where possible.
    if view.columns:
        ref = view.columns[0]
        parents = list(
            db.execute(
                select(SourceItem.external_id).where(
                    SourceItem.source_id == ref.source.id, SourceItem.canonical_id == canonical_id
                )
            ).scalars()
        )
        for season, episode, title in db.execute(
            select(SourceItem.season, SourceItem.episode, SourceItem.title).where(
                SourceItem.source_id == ref.source.id,
                SourceItem.kind == "episode",
                SourceItem.removed_at.is_(None),
                SourceItem.parent_external_id.in_(parents),
            )
        ).all():
            if (season, episode) in lines and lines[(season, episode)].title is None:
                lines[(season, episode)].title = title
    for (season, episode), line in lines.items():
        key = ("ep", canonical_id, season, episode)
        for col in view.columns:
            line.cells[col.source.id] = (key in col.available, col.state.get(key))
        played = {bool(w and w.played) for avail, w in line.cells.values() if avail}
        line.differs = len(played) > 1
    return sorted(lines.values(), key=lambda line: (line.season == 0, line.season, line.episode))
