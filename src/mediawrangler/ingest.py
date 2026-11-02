"""Store a connector snapshot: upsert libraries and items, mark what vanished."""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .connectors.base import FileRecord, ItemRecord, RootRecord, Snapshot, TitleSnapshot
from .connectors.paths import PathMap
from .db import utcnow
from .models import Library, MediaFile, MediaRoot, Source, SourceItem

ITEM_FIELDS = (
    "kind",
    "title",
    "year",
    "tvdb",
    "tmdb",
    "imdb",
    "path",
    "parent_external_id",
    "season",
    "episode",
    "episode_end",
    "source_status",
)


@dataclass
class IngestStats:
    added: int = 0
    changed: int = 0
    removed: int = 0
    unchanged: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    files: int = 0
    files_unmapped: int = 0
    roots: int = 0

    def as_dict(self) -> dict:
        return {
            "added": self.added,
            "changed": self.changed,
            "removed": self.removed,
            "unchanged": self.unchanged,
            "by_kind": self.by_kind,
            "files": self.files,
            "files_unmapped": self.files_unmapped,
            "roots": self.roots,
        }


def previous_records(db: Session, source: Source) -> dict[str, ItemRecord]:
    """What the last run stored, for connectors that reuse expensive details."""
    rows = db.execute(
        select(SourceItem).where(SourceItem.source_id == source.id, SourceItem.removed_at.is_(None))
    ).scalars()
    files: dict[str, list[str]] = {}
    for item_ext, path in db.execute(
        select(MediaFile.item_external_id, MediaFile.path).where(
            MediaFile.source_id == source.id, MediaFile.removed_at.is_(None)
        )
    ).all():
        if item_ext:
            files.setdefault(item_ext, []).append(path)
    return {
        r.external_id: ItemRecord(
            external_id=r.external_id,
            kind=r.kind,
            title=r.title,
            tvdb=r.tvdb,
            tmdb=r.tmdb,
            imdb=r.imdb,
            path=r.path,
            details_fetched=r.details_fetched_at is not None,
            file_paths=files.get(r.external_id, []),
        )
        for r in rows
    }


def apply_snapshot(
    db: Session, source: Source, snapshot: Snapshot, read_from: datetime | None = None
) -> IngestStats:
    """Store a full read. `read_from` is when the read began: an item or file marked removed
    after that (a delete confirmed while the read was running) stays removed, as the read may
    have listed it before the app dropped it. The next read corrects it if the app has it."""
    now = utcnow()
    stats = IngestStats()

    libs = {
        lib.external_id: lib
        for lib in db.execute(select(Library).where(Library.source_id == source.id)).scalars()
    }
    seen_libs = set()
    for rec in snapshot.libraries:
        lib = libs.get(rec.external_id)
        if lib is None:
            lib = Library(source_id=source.id, external_id=rec.external_id)
            db.add(lib)
            libs[rec.external_id] = lib
        lib.name, lib.kind, lib.paths = rec.name, rec.kind, rec.paths
        lib.last_seen_at, lib.removed_at = now, None
        seen_libs.add(rec.external_id)
    for ext, lib in libs.items():
        if ext not in seen_libs and lib.removed_at is None:
            lib.removed_at = now
    db.flush()
    lib_ids = {ext: lib.id for ext, lib in libs.items()}

    existing = {
        row.external_id: row
        for row in db.execute(select(SourceItem).where(SourceItem.source_id == source.id)).scalars()
    }
    seen: set[str] = set()
    for rec in snapshot.items:
        if rec.external_id in seen:
            continue  # an item listed twice (e.g. in two libraries): keep the first
        seen.add(rec.external_id)
        stats.by_kind[rec.kind] = stats.by_kind.get(rec.kind, 0) + 1
        library_id = lib_ids.get(rec.library_external_id) if rec.library_external_id else None
        row = existing.get(rec.external_id)
        if row is None:
            row = SourceItem(source_id=source.id, external_id=rec.external_id, first_seen_at=now)
            for name in ITEM_FIELDS:
                setattr(row, name, getattr(rec, name))
            row.library_id = library_id
            row.last_seen_at = now
            if rec.details_fetched:
                row.details_fetched_at = now
            db.add(row)
            stats.added += 1
            continue
        if _removed_since(row, read_from):
            continue
        changed = row.removed_at is not None or row.library_id != library_id
        for name in ITEM_FIELDS:
            value = getattr(rec, name)
            if getattr(row, name) != value:
                setattr(row, name, value)
                changed = True
        row.library_id = library_id
        row.last_seen_at = now
        row.removed_at = None
        if rec.details_fetched:
            row.details_fetched_at = now
        if changed:
            stats.changed += 1
        else:
            stats.unchanged += 1

    for ext, row in existing.items():
        if ext not in seen and row.removed_at is None:
            row.removed_at = now
            stats.removed += 1

    canon = canonicaliser(source)
    _apply_files(db, source, snapshot.files, lib_ids, canon, now, stats, read_from)
    _apply_roots(db, source, snapshot.roots, lib_ids, canon, now, stats)
    db.commit()
    return stats


def apply_title(db: Session, source: Source, snap: TitleSnapshot) -> None:
    """Store one show or film read on its own: its item, a show's episodes and files are
    added or updated, and any of them the app no longer has are marked gone. Nothing else
    in the source is touched."""
    now = utcnow()
    lib_ids = {
        lib.external_id: lib.id
        for lib in db.execute(select(Library).where(Library.source_id == source.id)).scalars()
    }
    wanted = {rec.external_id for rec in snap.items}
    rows = {
        row.external_id: row
        for row in db.execute(
            select(SourceItem).where(
                SourceItem.source_id == source.id,
                or_(
                    SourceItem.external_id.in_(wanted),
                    SourceItem.external_id == snap.root_id,
                    SourceItem.parent_external_id == snap.root_id,
                ),
            )
        ).scalars()
    }
    for rec in snap.items:
        row = rows.get(rec.external_id)
        if row is None:
            row = SourceItem(source_id=source.id, external_id=rec.external_id, first_seen_at=now)
            db.add(row)
        for name in ITEM_FIELDS:
            setattr(row, name, getattr(rec, name))
        row.library_id = lib_ids.get(rec.library_external_id) if rec.library_external_id else None
        row.last_seen_at, row.removed_at = now, None
        if rec.details_fetched:
            row.details_fetched_at = now
    for ext, row in rows.items():
        if ext not in wanted and row.removed_at is None:
            row.removed_at = now

    canon = canonicaliser(source)
    have = {
        row.path: row
        for row in db.execute(
            select(MediaFile).where(
                MediaFile.source_id == source.id,
                or_(
                    MediaFile.item_external_id == snap.root_id,
                    MediaFile.path.in_([f.path for f in snap.files]),
                ),
            )
        ).scalars()
    }
    seen = set()
    for rec in snap.files:
        seen.add(rec.path)
        row = have.get(rec.path)
        if row is None:
            row = MediaFile(source_id=source.id, path=rec.path)
            db.add(row)
        row.library_id = lib_ids.get(rec.library_external_id) if rec.library_external_id else None
        row.canonical_path = canon(rec.path)
        row.size, row.quality = rec.size, rec.quality
        row.item_external_id, row.season, row.episode = (
            rec.item_external_id,
            rec.season,
            rec.episode,
        )
        row.extra, row.flag = rec.extra, rec.flag
        row.last_seen_at, row.removed_at = now, None
    for path, row in have.items():
        if path not in seen and row.removed_at is None and row.item_external_id == snap.root_id:
            row.removed_at = now
    db.commit()


def apply_folder(db: Session, source: Source, folder: str, records: list[FileRecord]) -> None:
    """Store one folder of the share read on its own: its files are added or updated, and any
    the share no longer has under it are marked gone. Nothing else is touched."""
    now = utcnow()
    lib_ids = {
        lib.external_id: lib.id
        for lib in db.execute(select(Library).where(Library.source_id == source.id)).scalars()
    }
    prefix = folder.rstrip("/") + "/"
    have = {
        row.path: row
        for row in db.execute(
            select(MediaFile).where(
                MediaFile.source_id == source.id, MediaFile.path.startswith(prefix, autoescape=True)
            )
        ).scalars()
    }
    seen = set()
    for rec in records:
        seen.add(rec.path)
        row = have.get(rec.path)
        if row is None:
            row = MediaFile(source_id=source.id, path=rec.path)
            db.add(row)
        row.library_id = lib_ids.get(rec.library_external_id) if rec.library_external_id else None
        row.canonical_path = rec.path
        row.size, row.extra, row.flag = rec.size, rec.extra, rec.flag
        row.last_seen_at, row.removed_at = now, None
    for path, row in have.items():
        if path not in seen and row.removed_at is None:
            row.removed_at = now
    db.commit()


def remove_title(db: Session, source: Source, root_id: str) -> None:
    """The app no longer has this show or film: mark it, its episodes and files gone."""
    now = utcnow()
    for row in db.execute(
        select(SourceItem).where(
            SourceItem.source_id == source.id,
            or_(SourceItem.external_id == root_id, SourceItem.parent_external_id == root_id),
        )
    ).scalars():
        if row.removed_at is None:
            row.removed_at = now
    for f in db.execute(
        select(MediaFile).where(
            MediaFile.source_id == source.id, MediaFile.item_external_id == root_id
        )
    ).scalars():
        if f.removed_at is None:
            f.removed_at = now
    # A source that reports folders (Silo) keeps a record of the folder it scanned for the
    # title; with the title gone, so is that folder.
    for root in db.execute(
        select(MediaRoot).where(
            MediaRoot.source_id == source.id, MediaRoot.item_external_id == root_id
        )
    ).scalars():
        if root.removed_at is None:
            root.removed_at = now
    db.commit()


def canonicaliser(source: Source):
    """Paths from the disk scan are canonical already; others go through the
    source's path map (None = no rule matched, a path-map failure)."""
    if source.type == "disk":
        return lambda path: path
    return PathMap.from_pairs(source.path_map or []).apply


def _removed_since(row, read_from: datetime | None) -> bool:
    return bool(row is not None and read_from and row.removed_at and row.removed_at >= read_from)


def _apply_files(
    db, source, records: list[FileRecord], lib_ids, canon, now, stats, read_from=None
) -> None:
    existing = {
        row.path: row
        for row in db.execute(select(MediaFile).where(MediaFile.source_id == source.id)).scalars()
    }
    seen = set()
    for rec in records:
        if rec.path in seen:
            continue
        seen.add(rec.path)
        row = existing.get(rec.path)
        if _removed_since(row, read_from):
            continue
        if row is None:
            row = MediaFile(source_id=source.id, path=rec.path)
            db.add(row)
        row.library_id = lib_ids.get(rec.library_external_id) if rec.library_external_id else None
        row.canonical_path = canon(rec.path)
        row.size, row.quality = rec.size, rec.quality
        row.item_external_id, row.season, row.episode = (
            rec.item_external_id,
            rec.season,
            rec.episode,
        )
        row.extra, row.flag = rec.extra, rec.flag
        row.last_seen_at, row.removed_at = now, None
        stats.files += 1
        if row.canonical_path is None:
            stats.files_unmapped += 1
    for path, row in existing.items():
        if path not in seen and row.removed_at is None:
            row.removed_at = now


def _apply_roots(db, source, records: list[RootRecord], lib_ids, canon, now, stats) -> None:
    existing = {
        row.path: row
        for row in db.execute(select(MediaRoot).where(MediaRoot.source_id == source.id)).scalars()
    }
    seen = set()
    for rec in records:
        if rec.path in seen:
            continue
        seen.add(rec.path)
        row = existing.get(rec.path)
        if row is None:
            row = MediaRoot(source_id=source.id, path=rec.path)
            db.add(row)
        row.library_id = lib_ids.get(rec.library_external_id) if rec.library_external_id else None
        row.canonical_path = canon(rec.path)
        row.state, row.reason, row.file_count = rec.state, rec.reason, rec.file_count
        row.item_external_id = rec.item_external_id
        row.last_seen_at, row.removed_at = now, None
        stats.roots += 1
    for path, row in existing.items():
        if path not in seen and row.removed_at is None:
            row.removed_at = now


def recanonicalise(db: Session, source: Source) -> None:
    """Re-apply the source's path map to everything it reported (after an edit)."""
    canon = canonicaliser(source)
    for model in (MediaFile, MediaRoot):
        for row in db.execute(select(model).where(model.source_id == source.id)).scalars():
            row.canonical_path = canon(row.path)
    db.commit()


def apply_libraries(db: Session, source: Source, libraries) -> None:
    """Store just the library list (the disk scan lists folders before walking)."""
    now = utcnow()
    libs = {
        lib.external_id: lib
        for lib in db.execute(select(Library).where(Library.source_id == source.id)).scalars()
    }
    for rec in libraries:
        lib = libs.get(rec.external_id)
        if lib is None:
            lib = Library(source_id=source.id, external_id=rec.external_id)
            db.add(lib)
        lib.name, lib.kind, lib.paths = rec.name, rec.kind, rec.paths
        lib.last_seen_at, lib.removed_at = now, None
    db.commit()
