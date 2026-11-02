"""Fixing what the comparison pages show, from the page (0.11).

The first fix is "change which show an app thinks an item is": search the app's own
metadata, pick the right show, send it. Nothing is sent until the person has seen the
exact call and pressed the button; the write needs the global write switch on, and every
attempt is recorded in the audit log.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .connectors import Capability, ConnectorError, get_connector_class
from .connectors.base import ItemGone, MatchCandidate
from .connectors.paths import PathMap, is_video
from .crypto import SecretBox
from .db import utcnow
from .matching import normalise_title
from .models import AuditLog, Source, SourceItem
from .sync import connector_for
from .writes import ENABLED_KEY, PAUSED_KEY, flag

ID_TYPES = ("tvdb", "tmdb", "imdb")


class FixRefused(Exception):
    """The fix cannot be made now; the message says why and what to do."""


def can_rematch(source: Source) -> bool:
    try:
        return Capability.REMATCH in get_connector_class(source.type).capabilities
    except Exception:
        return False


@dataclass
class Reference:
    """What another app calls the same title, to show which candidate agrees with it."""

    source: str
    title: str
    year: int | None
    ids: dict[str, str]


def _folder(maps: dict[int, PathMap], source: Source, item: SourceItem) -> str | None:
    """The item's folder on the share: a film's file is read as the folder holding it."""
    if source.id not in maps:
        maps[source.id] = PathMap.from_pairs(source.path_map or [])
    path = maps[source.id].apply(item.path) if item.path else None
    return path.rsplit("/", 1)[0] if path and is_video(path) else path


def references(db: Session, item: SourceItem) -> list[Reference]:
    """Items other apps hold under this item's title, or in its folder on the share, that are
    paired with a different show (the classic wrong match): their IDs are what a correct
    candidate would carry."""
    norm = normalise_title(item.title)
    mine = db.get(Source, item.source_id)
    maps: dict[int, PathMap] = {}
    folder = _folder(maps, mine, item) if mine else None
    out = []
    others = db.execute(
        select(SourceItem, Source)
        .join(Source, Source.id == SourceItem.source_id)
        .where(
            SourceItem.kind == item.kind,
            SourceItem.source_id != item.source_id,
            SourceItem.removed_at.is_(None),
            SourceItem.parent_external_id.is_(None),
        )
    )
    for other, src in others:
        if other.canonical_id == item.canonical_id:
            continue
        same_folder = folder is not None and _folder(maps, src, other) == folder
        if normalise_title(other.title) != norm and not same_folder:
            continue
        ids = {t: getattr(other, t) for t in ID_TYPES if getattr(other, t)}
        if ids:
            out.append(Reference(src.name, other.title, other.year, ids))
    return out


def current_ids(item: SourceItem) -> dict[str, str]:
    return {t: getattr(item, t) for t in ID_TYPES if getattr(item, t)}


def _short_ids(ids: dict[str, str]) -> str:
    """The first ID there is, for the audit log's narrow columns."""
    return next((f"{t} {ids[t]}" for t in ID_TYPES if t in ids), "no IDs")


def _describe_ids(ids: dict[str, str]) -> str:
    return ", ".join(f"{k} {v}" for k, v in ids.items() if k in ID_TYPES) or "no IDs"


@dataclass
class Candidate:
    data: dict[str, Any]  # the MatchCandidate, as the page round-trips it
    agrees_with: list[str]  # names of apps whose IDs this one carries

    @property
    def ids_text(self) -> str:
        return _describe_ids(self.data["ids"])


def to_candidate(data: dict[str, Any]) -> MatchCandidate:
    return MatchCandidate(
        title=str(data.get("title", "")),
        year=data.get("year"),
        ids={str(k): str(v) for k, v in (data.get("ids") or {}).items()},
        overview=str(data.get("overview", "")),
        image_url=str(data.get("image_url", "")),
        note=str(data.get("note", "")),
        payload=dict(data.get("payload") or {}),
    )


def parse_candidate(raw: str) -> MatchCandidate:
    try:
        return to_candidate(json.loads(raw))
    except (ValueError, TypeError, AttributeError) as exc:
        raise FixRefused("that choice could not be read; search again and pick it") from exc


def search(
    db: Session, box: SecretBox, item: SourceItem, query: str, year: int | None
) -> list[Candidate]:
    """The app's own search for `query`, with candidates that carry another app's IDs for
    this title first."""
    source = db.get(Source, item.source_id)
    if source is None or not can_rematch(source):
        raise FixRefused(f"{source.name if source else 'that app'} cannot change a match from here")
    refs = references(db, item)
    try:
        with connector_for(source, box) as connector:
            found = connector.search_matches(item.external_id, item.kind, query, year)
    except ConnectorError as exc:
        raise FixRefused(f"{source.name} could not search: {exc}") from exc
    out = []
    for cand in found:
        agrees = sorted(
            {r.source for r in refs if any(cand.ids.get(t) == v for t, v in r.ids.items())}
        )
        out.append(Candidate(asdict(cand), agrees))
    out.sort(key=lambda c: (not c.agrees_with,))  # stable: the app's own order otherwise
    return out


def preview_call(
    db: Session, box: SecretBox, item: SourceItem, cand: MatchCandidate
) -> tuple[str, str, str]:
    """(method, full address, JSON body) of the call apply will make. The key is sent in a
    header and is not shown."""
    source = db.get(Source, item.source_id)
    with connector_for(source, box) as connector:
        method, path, body = connector.match_call(item.external_id, cand)
    return method, f"{source.base_url.rstrip('/')}{path}", json.dumps(body, indent=2)


def apply(
    db: Session, box: SecretBox, item: SourceItem, cand: MatchCandidate
) -> tuple[bool, str, str]:
    """Send the change. Returns (ok, message for the person, the app's id for the item
    afterwards); always writes the audit log."""
    source = db.get(Source, item.source_id)
    if not flag(db, ENABLED_KEY):
        raise FixRefused(
            "Writes to your apps are switched off. Turn them on under Settings → Writes first."
        )
    if flag(db, PAUSED_KEY):
        raise FixRefused("Writes are paused. Resume them under Settings → Writes first.")
    after = _describe_ids(cand.ids)
    new_id, error = item.external_id, None
    try:
        with connector_for(source, box) as connector:
            new_id = connector.apply_match(item.external_id, cand)
    except ItemGone as exc:
        error = str(exc)
    except ConnectorError as exc:
        error = str(exc)[:240]
    moved = error is None and new_id != item.external_id
    what = f"“{item.title}” ({item.year})" if item.year else f"“{item.title}”"
    now_is = f"“{cand.title}”" + (f" ({cand.year})" if cand.year else "")
    detail = error or (
        f"{source.name} gave it a new id ({new_id}); the library is being read again."
        if moved
        else None
    )
    db.add(
        AuditLog(
            at=utcnow(),
            app_user_id=0,
            source_id=source.id,
            source_name=source.name,
            user_name="—",
            label=item.title[:255],
            item_id=item.external_id,
            op="match",
            before=_short_ids(current_ids(item))[:16],
            after=(_short_ids(cand.ids) if error is None else _short_ids(current_ids(item)))[:16],
            result="error" if error else "applied",
            detail=detail[:255] if detail else None,
            why=f"Changed which show {source.name} thinks {what} is, to {now_is} ({after})"[:200],
        )
    )
    db.commit()
    if error:
        return False, error, item.external_id
    return (
        True,
        (
            f"{source.name} now has {what} as {now_is}. "
            + (f"It gave the show a new id ({new_id}). " if moved else "")
            + "MediaWrangler now reads it again."
        ),
        new_id,
    )
