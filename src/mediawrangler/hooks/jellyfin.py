"""What to do with the events Jellyfin's Webhook plugin sends.

The plugin posts one JSON object per event (we ask it to send all properties).
Jellyfin says when a user's played state or position is saved, so we apply it
to what MediaWrangler has stored (never to Jellyfin) the moment it happens.
Jellyfin reports each episode or film on its own, including for a season or
series marked at once, so no expansion is needed. An item added or deleted reads
again just its film or show.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Source, SourceItem, SourceUser
from ..watch import apply_live
from .silo import Outcome, _store

TICKS_PER_SECOND = 10_000_000
ITEM_NUDGES = {"ItemAdded", "ItemDeleted"}
PLAYBACK = {"PlaybackStart", "PlaybackStop"}
APPLY_TYPES = {"Episode", "Movie"}
TITLE_TYPES = {"Episode": "SeriesId", "Season": "SeriesId", "Movie": "ItemId", "Series": "ItemId"}
IGNORED = {"PlaybackProgress", "SessionStart", "AuthenticationSuccess", "AuthenticationFailure"}


def norm(value: Any) -> str:
    """Jellyfin ids with or without dashes, in any case."""
    return str(value or "").replace("-", "").lower()


def truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() == "true"


def _candidates(value: Any) -> list[str]:
    """The ways Jellyfin may write an id: as given, lower case, and without dashes."""
    raw = str(value or "").strip()
    return list(dict.fromkeys(c for c in (raw, raw.lower(), norm(raw)) if c))


def _user(db: Session, source: Source, user_id: str) -> SourceUser | None:
    ids = _candidates(user_id)
    if not ids:
        return None
    return db.execute(
        select(SourceUser).where(SourceUser.source_id == source.id, SourceUser.external_id.in_(ids))
    ).scalar()


def _item_key(db: Session, source: Source, item_id: str) -> str | None:
    """The id this item has in the stored catalogue (Jellyfin's own form)."""
    ids = _candidates(item_id)
    if not ids:
        return None
    return db.execute(
        select(SourceItem.external_id).where(
            SourceItem.source_id == source.id,
            SourceItem.external_id.in_(ids),
            SourceItem.removed_at.is_(None),
            SourceItem.kind.in_(("episode", "movie")),
        )
    ).scalar()


def describe(body: dict[str, Any]) -> str:
    name = body.get("Name") or "?"
    if body.get("ItemType") == "Episode" and body.get("SeriesName"):
        season, episode = body.get("SeasonNumber"), body.get("EpisodeNumber")
        try:
            name = f"{body['SeriesName']} S{int(season):02d}E{int(episode):02d} {name}"
        except (TypeError, ValueError):
            name = f"{body['SeriesName']} {name}"
    return str(name)


def handle_event(db: Session, source: Source, body: dict[str, Any]) -> Outcome:
    out = Outcome()
    kind = str(body.get("NotificationType") or "")
    if not kind or kind in IGNORED:
        return out
    item_type = str(body.get("ItemType") or "")
    if kind in ITEM_NUDGES and item_type in TITLE_TYPES:  # not folders, music, people
        out.sync = True
        title = norm(body.get(TITLE_TYPES[item_type]))
        if title:
            out.titles.add(title)
        else:
            out.wide = True
    who = str(body.get("Username") or body.get("NotificationUsername") or "")
    user_id = str(body.get("UserId") or "")
    fields: dict[str, Any] = {
        "event": kind[:64],
        "event_id": None,  # Jellyfin sends no event id; repeats are harmless
        "profile_external_id": user_id or None,
        "profile_name": who or None,
        "payload": {
            k: body[k]
            for k in ("NotificationType", "ItemId", "ItemType", "UserId", "Played")
            if k in body
        },
    }
    title = describe(body)
    if kind == "UserDataSaved":
        fields.update(_user_data(db, source, body, who, title))
        if fields.get("note") == "already known" and not truthy(body.get("Played")):
            return out  # a client's position tick (about every 3 s) that changes nothing
    elif kind in PLAYBACK:
        seconds = round(int(body.get("PlaybackPositionTicks") or 0) / TICKS_PER_SECOND)
        done = " to the end" if truthy(body.get("PlayedToCompletion")) else ""
        verb = "started" if kind == "PlaybackStart" else f"stopped at {seconds // 60} min{done}"
        fields.update(status="logged", summary=f"{who or '?'} {verb}: {title}"[:255])
    else:
        fields.update(status="logged", summary=f"{kind}: {title}"[:255])
    row = _store(db, source, **fields)
    if row is not None:
        out.stored.append(row)
    return out


def _user_data(
    db: Session, source: Source, body: dict[str, Any], who: str, title: str
) -> dict[str, Any]:
    su = _user(db, source, str(body.get("UserId") or ""))
    if su is not None and not who:
        who = su.name
    item_type = str(body.get("ItemType") or "")
    played = truthy(body.get("Played"))
    ticks = int(body.get("PlaybackPositionTicks") or 0)
    position = ticks / TICKS_PER_SECOND
    runtime = int(body.get("RunTimeTicks") or 0) / TICKS_PER_SECOND or None
    if played:
        what = "watched"
    elif position >= 60:
        what = f"stopped at {round(position / 60)} min on"
    else:
        what = "unwatched"
    summary = f"{who or '?'} {what} {title}"[:255]
    if item_type not in APPLY_TYPES:
        return {
            "status": "logged",
            "summary": summary,
            "note": f"{item_type or 'item'} events aren't applied",
        }
    if su is None:
        return {
            "status": "logged",
            "summary": summary,
            "note": "user not known to MediaWrangler yet",
        }
    if su.watch_synced_at is None:
        return {"status": "logged", "summary": summary, "note": "no first read of this user yet"}
    key = _item_key(db, source, str(body.get("ItemId") or ""))
    if key is None:
        return {"status": "logged", "summary": summary, "note": "item not in the catalogue yet"}
    changed = apply_live(
        db,
        su,
        [key],
        played,
        position=None if played or position < 60 else position,
        duration=runtime,
    )
    return {
        "status": "applied" if changed else "logged",
        "summary": summary,
        "note": None if changed else "already known",
    }
