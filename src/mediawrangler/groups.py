"""The master source and library groups.

Groups start from the master's libraries. Other sources' libraries are mapped
onto them automatically once (same folder name or same library name, same
kind); after that the mapping is the user's, edited in Settings.
"""

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .connectors import connector_types
from .connectors.paths import PathMap
from .models import AppSetting, Library, LibraryGroup, LibraryGroupMember, Source

MASTER_KEY = "master_source_id"


def media_server_sources(db: Session, enabled_only: bool = True) -> list[Source]:
    types = {t for t, cls in connector_types().items() if cls.role == "media_server"}
    q = select(Source).where(Source.type.in_(types)).order_by(Source.id)
    if enabled_only:
        q = q.where(Source.enabled.is_(True))
    return list(db.execute(q).scalars())


def get_master(db: Session) -> Source | None:
    """The configured master, else the first Jellyfin, else the first media server."""
    servers = media_server_sources(db)
    setting = db.get(AppSetting, MASTER_KEY)
    if setting is not None:
        chosen = next((s for s in servers if s.id == setting.value), None)
        if chosen is not None:
            return chosen
    return next((s for s in servers if s.type == "jellyfin"), servers[0] if servers else None)


def set_master(db: Session, source_id: int) -> None:
    row = db.get(AppSetting, MASTER_KEY)
    if row is None:
        db.add(AppSetting(key=MASTER_KEY, value=source_id))
    else:
        row.value = source_id
    db.commit()


def source_order(db: Session) -> list[int]:
    """Master first, then the other media servers by id."""
    master = get_master(db)
    ids = [s.id for s in media_server_sources(db)]
    if master is not None:
        ids.remove(master.id)
        ids.insert(0, master.id)
    return ids


def _folder_names(lib: Library) -> set[str]:
    return {PurePosixPath(p).name.lower() for p in lib.paths or [] if p}


def _unique_name(db: Session, name: str) -> str:
    candidate, n = name, 2
    while db.execute(select(LibraryGroup.id).where(LibraryGroup.name == candidate)).first():
        candidate, n = f"{name} ({n})", n + 1
    return candidate


def ensure_groups(db: Session) -> None:
    master = get_master(db)
    if master is None:
        return
    live = select(Library).where(Library.removed_at.is_(None), Library.kind != "other")
    master_libs = db.execute(live.where(Library.source_id == master.id)).scalars().all()
    position = db.execute(select(func.max(LibraryGroup.position))).scalar() or 0

    for lib in master_libs:
        if lib.group_assigned:
            continue
        position += 1
        group = LibraryGroup(
            name=_unique_name(db, lib.name),
            kind=lib.kind,
            presence_enabled="sport" not in lib.name.lower(),
            position=position,
        )
        db.add(group)
        db.flush()
        db.add(LibraryGroupMember(group_id=group.id, library_id=lib.id))
        lib.group_assigned = True
    db.flush()

    # Suggest a group for other sources' new libraries: same folder name first
    # (Silo "Shows" at .../TV matches Jellyfin "TV" at .../TV), then same name.
    anchors = []
    for group in db.execute(select(LibraryGroup)).scalars():
        member_libs = (
            db.execute(
                select(Library)
                .join(LibraryGroupMember)
                .where(LibraryGroupMember.group_id == group.id)
            )
            .scalars()
            .all()
        )
        folders = (
            set().union(*(_folder_names(lib) for lib in member_libs)) if member_libs else set()
        )
        names = {lib.name.lower() for lib in member_libs} | {group.name.lower()}
        anchors.append((group, folders, names, {lib.source_id for lib in member_libs}))

    sources = {s.id: s for s in db.execute(select(Source)).scalars()}
    types = connector_types()
    others = db.execute(live.where(Library.source_id != master.id)).scalars().all()
    for lib in others:
        if lib.group_assigned:
            continue
        mine = _folder_names(lib)
        cls = types.get(sources[lib.source_id].type)
        hint = cls.group_hint if cls else None

        def fits(group, srcs, lib=lib):
            # Disk folders have no kind of their own; they take the group's.
            kind_ok = group.kind == lib.kind or lib.kind == "folder"
            return kind_ok and lib.source_id not in srcs

        match = (
            next((g for g, folders, _n, srcs in anchors if fits(g, srcs) and mine & folders), None)
            or next(
                (
                    g
                    for g, _f, names, srcs in anchors
                    if fits(g, srcs) and lib.name.lower() in names
                ),
                None,
            )
            or next(
                (
                    g
                    for g, _f, _n, srcs in anchors
                    if hint and fits(g, srcs) and hint in g.name.lower()
                ),
                None,
            )
        )
        if match is None:
            continue  # try again on later syncs, e.g. once a matching group exists
        lib.group_assigned = True
        db.add(LibraryGroupMember(group_id=match.id, library_id=lib.id))
        for anchor in anchors:
            if anchor[0] is match:
                anchor[3].add(lib.source_id)
    db.commit()


def ensure_path_maps(db: Session) -> list[Source]:
    """Derive each source's path map from its groups: a library's folder maps to
    the disk folder of the same group (e.g. Sonarr /data/TV => /media/TV).
    Only for sources whose map hasn't been edited by hand. Returns the sources
    whose map changed (their stored paths need re-canonicalising)."""
    disk_types = {t for t, cls in connector_types().items() if cls.role == "disk"}
    members = db.execute(
        select(Library, LibraryGroupMember.group_id, Source)
        .join(LibraryGroupMember, LibraryGroupMember.library_id == Library.id)
        .join(Source, Source.id == Library.source_id)
        .where(Library.removed_at.is_(None))
    ).all()
    disk_root = {}
    for lib, gid, src in members:
        if src.type in disk_types and lib.paths:
            disk_root.setdefault(gid, lib.paths[0])
    rules: dict[int, list[tuple[str, str]]] = {}
    for lib, gid, src in members:
        if src.type in disk_types or gid not in disk_root:
            continue
        for p in lib.paths or []:
            rules.setdefault(src.id, []).append((p, disk_root[gid]))
    changed = []
    for src in db.execute(select(Source)).scalars():
        if src.type in disk_types or not (src.options or {}).get("path_map_auto", True):
            continue
        new = PathMap.from_pairs(rules.get(src.id, [])).to_pairs()
        if new and new != (src.path_map or []):
            src.path_map = new
            changed.append(src)
    db.commit()
    return changed


def fits_group(group: LibraryGroup, lib: Library) -> bool:
    """A library can join a group of its own kind; a disk folder has none, so it fits any."""
    return lib.kind == group.kind or lib.kind == "folder"


@dataclass
class LibraryRow:
    library: Library
    # (the app's folder, what it is treated as; None when no path rule covers it)
    paths: list[tuple[str, str | None]]


@dataclass
class AppRow:
    source: Source
    role: str
    libraries: list[LibraryRow] = field(default_factory=list)
    choices: list[Library] = field(default_factory=list)  # unplaced libraries that fit here
    status: str = "ok"  # "ok", "no_reference", "unmapped", "not_set" or "none"


@dataclass
class Section:
    group: LibraryGroup
    rows: list[AppRow]
    has_reference: bool


def library_sections(db: Session) -> tuple[list[Section], list[tuple[Library, Source]]]:
    """Everything the Libraries page shows: one section per group with a row per app, and
    the libraries that are in no group."""
    types = connector_types()
    master = get_master(db)
    sources = [s for s in db.execute(select(Source).order_by(Source.id)).scalars() if s.enabled]

    def role(src: Source) -> str:
        cls = types.get(src.type)
        return cls.role if cls else "media_server"

    rank = {"media_server": 1, "disk": 2, "arr": 3}
    sources.sort(
        key=lambda s: (
            0 if master and s.id == master.id else rank.get(role(s), 4),
            s.name.lower() if role(s) == "arr" else "",
            s.id,
        )
    )
    maps = {s.id: PathMap.from_pairs(s.path_map or []) for s in sources}
    live = (
        db.execute(select(Library).where(Library.removed_at.is_(None), Library.kind != "other"))
        .scalars()
        .all()
    )
    member_of = {m.library_id: m.group_id for m in db.execute(select(LibraryGroupMember)).scalars()}
    by_id = {s.id: s for s in sources}

    sections = []
    for group in db.execute(
        select(LibraryGroup).order_by(LibraryGroup.position, LibraryGroup.id)
    ).scalars():
        rows = []
        for src in sources:
            mine = [lib for lib in live if lib.source_id == src.id]
            row = AppRow(src, role(src))
            for lib in mine:
                if member_of.get(lib.id) == group.id:
                    paths = [
                        (p, p if row.role == "disk" else maps[src.id].apply(p))
                        for p in lib.paths or []
                    ]
                    row.libraries.append(LibraryRow(lib, paths))
            row.choices = [
                lib for lib in mine if lib.id not in member_of and fits_group(group, lib)
            ]
            if row.libraries or row.choices:
                rows.append(row)
        has_reference = any(r.role == "disk" and r.libraries for r in rows)
        for row in rows:
            if not row.libraries:
                row.status = "not_set"
            elif row.role == "disk":
                row.status = "ok"
            elif not has_reference:
                row.status = "no_reference"
            elif any(mapped is None for lr in row.libraries for _p, mapped in lr.paths):
                row.status = "unmapped"
        sections.append(Section(group, rows, has_reference))

    left_out = [
        (lib, by_id[lib.source_id])
        for lib in live
        if lib.id not in member_of and lib.source_id in by_id
    ]
    left_out.sort(key=lambda t: (sources.index(t[1]), t[0].name.lower()))
    return sections, left_out


def set_library_group(db: Session, library_id: int, group_id: int | None) -> None:
    lib = db.get(Library, library_id)
    if lib is None:
        return
    member = db.execute(
        select(LibraryGroupMember).where(LibraryGroupMember.library_id == library_id)
    ).scalar()
    if group_id is None:
        if member is not None:
            db.delete(member)
    elif member is None:
        db.add(LibraryGroupMember(group_id=group_id, library_id=library_id))
    else:
        member.group_id = group_id
    lib.group_assigned = True
    db.commit()


# The standard order of apps wherever they are listed: media servers (master first),
# then the media share, then the arrs (Sonarr, Radarrs, Sportarr).
ARR_ORDER = ("sonarr", "radarr", "sportarr")


def arr_sort_key(source: Source) -> tuple:
    rank = ARR_ORDER.index(source.type) if source.type in ARR_ORDER else len(ARR_ORDER)
    return (rank, source.name.lower(), source.id)


def app_rank(role: str) -> int:
    return {"media_server": 0, "disk": 1, "arr": 2}.get(role, 3)
