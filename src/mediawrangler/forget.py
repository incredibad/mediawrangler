"""Shows a media server keeps after their folder was deleted from the share (0.12.86).

Shows only: a film's entry goes with its file, which the usual update after a delete covers.
Jellyfin drops a show whose folder is gone only when it next looks at the folder's parent, so
after a folder is removed (a leftover delete, or by hand) the show can stay listed with no
episodes. Telling it the folder was deleted, as its own file watcher would, makes it drop the
show after its watcher's delay. Offered only when the folder is really gone from the share.
"""

from __future__ import annotations

import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from .connectors import get_connector_class
from .connectors.paths import PathMap
from .models import MediaFile, Source, SourceItem


def can_forget(source: Source) -> bool:
    cls = get_connector_class(source.type)
    return bool(cls and cls.can_forget)


def share_path(source: Source, item: SourceItem) -> str | None:
    """The item's folder on the share, as MediaWrangler sees it."""
    return PathMap.from_pairs(source.path_map or []).apply(item.path) if item.path else None


def _items(db: Session, sources: list[Source], kind: str):
    by_id = {s.id: s for s in sources if can_forget(s)}
    if not by_id or kind != "series":  # a film's path is its file, not a folder
        return []
    rows = db.execute(
        select(SourceItem).where(
            SourceItem.source_id.in_(by_id),
            SourceItem.kind == kind,
            SourceItem.removed_at.is_(None),
            SourceItem.parent_external_id.is_(None),
            SourceItem.path.is_not(None),
        )
    ).scalars()
    return [(by_id[i.source_id], i) for i in rows]


def gone(db: Session, sources: list[Source], kind: str) -> set[int]:
    """Ids of the shows or films a media server lists with no files whose folder is gone from
    the share: the ones to offer "drop it" for."""
    items = _items(db, sources, kind)
    with_files = set(
        db.execute(
            select(MediaFile.source_id, MediaFile.item_external_id)
            .where(
                MediaFile.source_id.in_({s.id for s, _ in items}),
                MediaFile.removed_at.is_(None),
            )
            .distinct()
        ).all()
    )
    out = set()
    for source, item in items:
        if (source.id, item.external_id) in with_files:
            continue
        folder = share_path(source, item)
        if folder and not os.path.isdir(folder):
            out.add(item.id)
    return out


def in_folder(db: Session, folder: str, kind: str) -> list[tuple[Source, SourceItem]]:
    """The media servers' entries for the show or film that lived in this share folder."""
    sources = list(db.execute(select(Source).where(Source.enabled.is_(True))).scalars())
    return [(s, i) for s, i in _items(db, sources, kind) if share_path(s, i) == folder]
