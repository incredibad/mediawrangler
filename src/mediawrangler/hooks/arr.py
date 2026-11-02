"""What to do with the notifications Sonarr, Radarr and Sportarr send.

Their webhooks say that something about the library changed (a file was
imported, renamed or deleted, a title was added or removed). We note it, and
read again the one film or show the event names (a full read of the app only when it
names none).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..models import Source
from .silo import Outcome, _store

# Event types that mean the app's library changed.
SYNC_EVENTS = {
    "Download",
    "Upgrade",
    "Rename",
    "SeriesAdd",
    "SeriesDelete",
    "EpisodeFileDelete",
    "MovieAdded",
    "MovieDelete",
    "MovieFileDelete",
    "MovieFileRename",
}
IGNORED = {"Health", "HealthRestored", "ApplicationUpdate"}


def title_of(body: dict[str, Any]) -> str:
    for key in ("series", "movie"):
        item = body.get(key)
        if isinstance(item, dict) and item.get("title"):
            title = str(item["title"])
            episodes = body.get("episodes")
            if key == "series" and isinstance(episodes, list) and episodes:
                first = episodes[0]
                try:
                    title += f" S{int(first['seasonNumber']):02d}E{int(first['episodeNumber']):02d}"
                except (KeyError, TypeError, ValueError):
                    pass
                if len(episodes) > 1:
                    title += f" (+{len(episodes) - 1} more)"
            return title
    return ""


def title_id(source: Source, body: dict[str, Any]) -> str | None:
    """The event's film or show, as MediaWrangler stores Radarr's and Sonarr's ids."""
    key, prefix = {"radarr": ("movie", "movie"), "sonarr": ("series", "series")}.get(
        source.type, (None, None)
    )
    item = body.get(key) if key else None
    if isinstance(item, dict) and isinstance(item.get("id"), int):
        return f"{prefix}-{item['id']}"
    return None


def handle_event(db: Session, source: Source, body: dict[str, Any]) -> Outcome:
    out = Outcome()
    kind = str(body.get("eventType") or "")
    if not kind or kind in IGNORED:
        return out
    if kind in SYNC_EVENTS:
        out.sync = True
        ext = title_id(source, body)
        if ext:
            out.titles.add(ext)
        else:
            out.wide = True
    title = title_of(body)
    if kind == "Test":
        summary, note = "Test notification received: the connection works", None
    else:
        summary, note = f"{kind}: {title}".rstrip(": ")[:255], None
        if kind in SYNC_EVENTS:
            note = "a read of this title follows" if out.titles else "a sync of this app follows"
    row = _store(
        db,
        source,
        event=kind[:64],
        event_id=None,
        summary=summary,
        status="logged",
        note=note,
        payload={k: body[k] for k in ("eventType", "instanceName") if k in body},
    )
    if row is not None:
        out.stored.append(row)
    return out
