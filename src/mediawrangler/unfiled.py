"""Titles only an import knows (0.9.14).

Local is keyed by title, and a title needs a canonical id. Shows and films in a
Trakt or Yamtrack file that no app has used to have none, so Local left them
out. Here they get a canonical item of their own (keyed like the matcher keys
its own, e.g. "series:tvdb:123"), their plays go into Local, and the matcher
leaves them alone. When an app later gets the title, the matcher reuses that
row, or `reunite` moves Local's rows across when the two disagree on the key.
"""

from __future__ import annotations

import logging
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import utcnow
from .importers import KINDS, canonical_lookup, library_canonicals, resolve
from .local import LKey, _Store
from .matching import PRECEDENCE, TITLE_YEAR_TOLERANCE, _conflicts_any, normalise_title
from .models import (
    AppSetting,
    AppUser,
    CanonicalItem,
    LocalEvent,
    LocalPlay,
    LocalState,
    SourceItem,
    WatchImport,
    WatchImportItem,
)

log = logging.getLogger(__name__)


def canon_key(kind: str, rec) -> str:
    """The key the matcher would give this title, or a title-and-year key when
    the file has no ids for it."""
    for id_type in PRECEDENCE[kind]:
        if value := getattr(rec, id_type):
            return f"{kind}:{id_type}:{value}"
    return f"{kind}:import:{normalise_title(rec.title)}:{rec.year or ''}"[:160]


def adopt_imports(db: Session, person: AppUser, anime: dict | None) -> int:
    """Put every play in the person's imports for a title no app has into Local.
    Idempotent: what Local already has is left alone. Returns titles created."""
    anime = anime or {}
    imports = db.execute(select(WatchImport).where(WatchImport.app_user_id == person.id)).scalars()
    lookup = canonical_lookup(db)
    in_library = library_canonicals(db)
    existing = {c.key: c for c in db.execute(select(CanonicalItem)).scalars()}
    store = _Store(db, person.id)
    now, stats = utcnow(), Counter()
    for imp in imports:
        name = KINDS.get(imp.kind, imp.kind)
        items = db.execute(
            select(WatchImportItem).where(WatchImportItem.import_id == imp.id)
        ).scalars()
        for item in items:
            res = resolve(item, lookup, anime)
            if res.kind not in ("series", "movie"):
                stats["unplaced"] += 1
                continue
            rec, cid = res.rec, res.cid
            if cid is not None and cid in in_library:
                continue  # an app has it: Local already follows the apps
            if cid is None:
                key = canon_key(res.kind, rec)
                row = existing.get(key)
                if row is None:
                    row = CanonicalItem(
                        key=key,
                        kind=res.kind,
                        title=rec.title,
                        year=rec.year,
                        tvdb=rec.tvdb,
                        tmdb=rec.tmdb,
                        imdb=rec.imdb,
                    )
                    db.add(row)
                    db.flush()
                    existing[key] = row
                    stats["titles"] += 1
                cid = row.id
                for id_type in ("tvdb", "tmdb", "imdb"):
                    if value := getattr(rec, id_type):
                        lookup.setdefault((res.kind, id_type, value), cid)
            if res.kind == "movie":
                keys = [("mv", cid, 0, 0)]
            elif rec.season is None:
                continue
            elif rec.episode is not None:
                keys = [("ep", cid, rec.season, rec.episode)]
            else:  # anime: the first `progress` episodes of the season
                keys = [("ep", cid, rec.season, n) for n in range(1, (rec.progress or 0) + 1)]
            for key in keys:
                _play(store, key, rec.watched_at, name, now)
                stats["plays"] += 1
    db.commit()
    log.info(
        "Local: %s imports for %s: %d titles added, %d plays, %d not placeable",
        "folded",
        person.name,
        stats["titles"],
        stats["plays"],
        stats["unplaced"],
    )
    return stats["titles"]


def _play(store: _Store, key: LKey, when, source: str, now) -> None:
    st = store.states.get(key)
    if st is None or not st.played:
        store.set(key, True, None, None, when)
        store.log(key, "played", "import", now)
    elif when and (st.last_played_at is None or when > st.last_played_at):
        st.last_played_at = when
    store.add_play(key, when, source)


def adopt_new_imports(db: Session, person: AppUser, anime: dict | None) -> int:
    """Run `adopt_imports` when the person's files (or the anime list their
    anime rows need) changed since last time."""
    ids = [
        [i.id, i.imported_at.isoformat()]
        for i in db.execute(
            select(WatchImport).where(WatchImport.app_user_id == person.id)
        ).scalars()
    ]
    if not ids:
        return 0
    marker = f"local_imports_{person.id}"
    signature = {"imports": ids, "anime": bool(anime)}
    row = db.get(AppSetting, marker)
    if row is not None and row.value == signature:
        return 0
    n = adopt_imports(db, person, anime)
    db.merge(AppSetting(key=marker, value=signature))
    db.commit()
    return n


# --- when an app gets the title ---------------------------------------------------


def referenced_ids(db: Session) -> set[int]:
    """Canonical items Local holds anything for."""
    out: set[int] = set()
    for model in (LocalState, LocalPlay, LocalEvent):
        out.update(db.execute(select(model.canonical_id).distinct()).scalars())
    return out


def reunite(db: Session) -> int:
    """Move Local's rows from a title only an import knew onto the canonical item
    an app's copy of it now has, when the matcher gave the two different keys
    (it shares an id, or the same title and year). Returns titles merged."""
    held = referenced_ids(db)
    with_items = set(
        db.execute(
            select(SourceItem.canonical_id).where(SourceItem.canonical_id.is_not(None)).distinct()
        ).scalars()
    )
    rows = {c.id: c for c in db.execute(select(CanonicalItem)).scalars()}
    orphans = [rows[i] for i in held if i in rows and i not in with_items]
    if not orphans:
        return 0
    live = [rows[i] for i in with_items if i in rows]
    id_types = ("tvdb", "tmdb", "imdb")
    by_id = {(c.kind, t, v): c for c in live for t in id_types if (v := getattr(c, t))}
    by_title: dict[tuple[str, str], list[CanonicalItem]] = {}
    for c in live:
        by_title.setdefault((c.kind, normalise_title(c.title)), []).append(c)
    merged = 0
    for orphan in orphans:
        target = None
        for t in id_types:
            if (v := getattr(orphan, t)) and (orphan.kind, t, v) in by_id:
                target = by_id[(orphan.kind, t, v)]
                break
        if target is None and ":import:" in orphan.key:
            for c in by_title.get((orphan.kind, normalise_title(orphan.title)), []):
                gap = None if orphan.year is None or c.year is None else abs(orphan.year - c.year)
                if gap is None or gap <= TITLE_YEAR_TOLERANCE:
                    target = c
                    break
        mine = {t: v for t in ("tvdb", "tmdb", "imdb") if (v := getattr(orphan, t))}
        if target is not None:
            theirs = {t: v for t in ("tvdb", "tmdb", "imdb") if (v := getattr(target, t))}
            if _conflicts_any(mine, theirs):
                target = None  # they disagree on an id: not the same title
        if target is None or target.id == orphan.id:
            continue
        _merge(db, orphan, target)
        merged += 1
    db.flush()
    return merged


def _merge(db: Session, orphan: CanonicalItem, target: CanonicalItem) -> None:
    """Re-point everything Local has for `orphan` at `target`, keeping the better
    of two states, then drop the orphan."""
    for st in db.execute(select(LocalState).where(LocalState.canonical_id == orphan.id)).scalars():
        keep = db.execute(
            select(LocalState).where(
                LocalState.app_user_id == st.app_user_id,
                LocalState.canonical_id == target.id,
                LocalState.kind == st.kind,
                LocalState.season == st.season,
                LocalState.episode == st.episode,
            )
        ).scalar_one_or_none()
        if keep is None:
            st.canonical_id = target.id
        else:
            if st.played and not keep.played:
                keep.played, keep.last_played_at = True, st.last_played_at
            db.delete(st)
    for play in db.execute(select(LocalPlay).where(LocalPlay.canonical_id == orphan.id)).scalars():
        clash = db.execute(
            select(LocalPlay).where(
                LocalPlay.app_user_id == play.app_user_id,
                LocalPlay.canonical_id == target.id,
                LocalPlay.kind == play.kind,
                LocalPlay.season == play.season,
                LocalPlay.episode == play.episode,
                LocalPlay.day == play.day,
            )
        ).scalar_one_or_none()
        if clash is None:
            play.canonical_id = target.id
        else:
            names = set((clash.sources or "").split(", ")) | set((play.sources or "").split(", "))
            clash.sources = ", ".join(sorted(n for n in names if n))[:255]
            db.delete(play)
    for ev in db.execute(select(LocalEvent).where(LocalEvent.canonical_id == orphan.id)).scalars():
        ev.canonical_id = target.id
    db.flush()
    db.delete(orphan)
