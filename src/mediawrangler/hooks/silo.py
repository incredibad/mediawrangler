"""What to do with the events Silo's Event Webhooks plugin sends.

Silo tells us that something changed; we update what MediaWrangler has stored
(never anything in Silo) and note it for the Activity page.

- ``user_state.changed``: watched, unwatched, favourite, watchlist. A season or
  series event names the season or series, so it is expanded to the episodes
  we know. Applied to people whose watch state we can read (their baseline is
  the last full read); for everyone else it is logged only.
- ``playback_sessions_changed``: someone started or stopped playing. We read
  Silo's list of finished plays and apply new ones: a completed play is
  watched, an unfinished one is a resume point.
- ``scan.completed`` and the library events: Silo has new items, so a read. A scan
  of one folder (Silo scans the folder an arr imported into, and the folder a fix
  asked about) reads only the titles in that folder; a whole-library scan, or a
  library event with nothing narrower, reads the whole app.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..connectors.silo import content_id_of, external_id
from ..db import utcnow
from ..models import (
    AppSetting,
    Library,
    Source,
    SourceItem,
    SourceUser,
    SyncRun,
    WebhookEvent,
)
from ..watch import apply_live

log = logging.getLogger(__name__)

# Frequent and of no use to MediaWrangler: dropped without being stored.
IGNORED = {
    "scan.progress",
    "job.progress",
    "operational_log_appended",
    "audit_log_appended",
    "sessions.replaced",
    "playback_sessions_changed",  # acts as a nudge to read plays, below
}
PLAY_NUDGES = {"playback_sessions_changed"}
LIBRARY_NUDGES = {
    "scan.completed",
    "library.item_added",
    "catalog.library.changed",
    "catalog.item.changed",
    "library.changed",
}
MIN_FULL_SYNC_GAP = timedelta(minutes=10)
MAX_NARROW_READS = 20  # a scan touching more titles than this reads the whole app
KEEP_EVENTS = 5000


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def expand_content_id(db: Session, source: Source, content_id: str) -> list[str]:
    """The episodes (or the one film or episode) a content id stands for."""
    for prefix in ("season-", "series-"):
        if content_id.startswith(prefix):
            like = _like("episode-" + content_id[len(prefix) :] + "-")
            rows = db.execute(
                select(SourceItem.external_id).where(
                    SourceItem.source_id == source.id,
                    SourceItem.kind == "episode",
                    SourceItem.removed_at.is_(None),
                    SourceItem.external_id.like(f"%/{like}%", escape="\\"),
                )
            ).scalars()
            return sorted({content_id_of(e) for e in rows})
    return [content_id]


def title_of(db: Session, source: Source, content_id: str) -> str:
    """A readable name for an item, for the Activity page."""
    first = expand_content_id(db, source, content_id)[:1]
    key = first[0] if first else content_id
    row = db.execute(
        select(SourceItem)
        .where(
            SourceItem.source_id == source.id,
            SourceItem.external_id.like(f"%/{_like(key)}", escape="\\"),
        )
        .limit(1)
    ).scalar()
    if row is None:
        return content_id
    if row.kind == "episode" and row.parent_external_id:
        series = db.execute(
            select(SourceItem.title).where(
                SourceItem.source_id == source.id, SourceItem.external_id == row.parent_external_id
            )
        ).scalar()
        if content_id.startswith(("season-", "series-")) and series:
            return series if content_id.startswith("series-") else f"{series} S{row.season}"
        return f"{series or '?'} S{row.season or 0:02d}E{row.episode or 0:02d} {row.title}".strip()
    return row.title


def _profile(db: Session, source: Source, profile_id: str | None) -> SourceUser | None:
    if not profile_id:
        return None
    return db.execute(
        select(SourceUser).where(
            SourceUser.source_id == source.id, SourceUser.external_id == profile_id
        )
    ).scalar()


def _store(db: Session, source: Source, **fields: Any) -> WebhookEvent | None:
    """Keep an event, unless one with the same id from this source is already kept."""
    event_id = fields.get("event_id")
    if (
        event_id
        and db.execute(
            select(WebhookEvent.id).where(
                WebhookEvent.source_id == source.id, WebhookEvent.event_id == event_id
            )
        ).first()
    ):
        return None
    row = WebhookEvent(source_id=source.id, received_at=utcnow(), **fields)
    db.add(row)
    db.commit()
    newest = db.execute(select(func.max(WebhookEvent.id))).scalar() or 0
    if newest > KEEP_EVENTS and newest % 50 == 0:
        db.execute(delete(WebhookEvent).where(WebhookEvent.id <= newest - KEEP_EVENTS))
        db.commit()
    return row


def feed_item(row: WebhookEvent, app: str = "") -> dict[str, Any]:
    return {
        "id": row.id,
        "app": app,
        "at": row.received_at.isoformat() + "Z",
        "event": row.event,
        "profile": row.profile_name or row.profile_external_id or "",
        "summary": row.summary,
        "status": row.status,
        "note": row.note or "",
    }


class Outcome:
    """What the receiver should do next, besides storing the event."""

    def __init__(self) -> None:
        self.stored: list[WebhookEvent] = []
        self.read_plays = False
        self.sync = False
        self.reconcile = False
        self.duplicate = False
        # What a library event narrows the read to: (library, folder) scanned, titles changed.
        self.folders: set[tuple[str, str]] = set()
        self.titles: set[str] = set()
        self.wide = False  # a whole-library scan


def handle_event(db: Session, source: Source, body: dict[str, Any]) -> Outcome:
    out = Outcome()
    name = str(body.get("event") or "")[:64]
    if not name:
        return out
    if name in PLAY_NUDGES:
        out.read_plays = True
    if name in LIBRARY_NUDGES:
        out.sync = True
    if name in IGNORED:
        return out
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    profile = body.get("profile") if isinstance(body.get("profile"), dict) else {}
    profile_id = profile.get("id") or data.get("profile_id")
    library = str(data.get("library_id") or "")
    if name == "scan.completed":
        if data.get("mode") == "subtree" and data.get("path") and library:
            out.folders.add((library, str(data["path"]).rstrip("/")))
        else:
            out.wide = True
    elif name == "catalog.item.changed" and data.get("content_id") and library:
        out.titles.add(external_id(library, str(data["content_id"])))
    fields: dict[str, Any] = {
        "event": name,
        "event_id": (str(body.get("event_id")) or None) if body.get("event_id") else None,
        "profile_external_id": profile_id,
        "profile_name": profile.get("name"),
        "payload": body,
    }
    if name == "user_state.changed":
        fields.update(_user_state(db, source, data, profile_id, fields))
    elif name == "plugin.warning":
        fields.update(status="error", summary=str(data.get("message") or "warning")[:255])
    else:
        fields.update(status="logged", summary=_describe(name, data))
    row = _store(db, source, **fields)
    if row is None:
        out.duplicate = True
        out.read_plays = out.sync = out.wide = False
        out.folders.clear()
        out.titles.clear()
    else:
        out.stored.append(row)
    return out


def _describe(name: str, data: dict[str, Any]) -> str:
    bits = [
        f"{k}={data[k]}"
        for k in ("library_id", "status", "mode", "path", "content_id", "reason")
        if k in data
    ]
    return (name + (": " + ", ".join(bits) if bits else ""))[:255]


def _user_state(
    db: Session,
    source: Source,
    data: dict[str, Any],
    profile_id: str | None,
    fields: dict[str, Any],
) -> dict[str, Any]:
    change = str(data.get("change") or "")
    content_id = str(data.get("content_id") or "")
    who = fields.get("profile_name") or profile_id or "?"
    su = _profile(db, source, profile_id)
    if su is not None and not fields.get("profile_name"):
        fields["profile_name"] = su.name
        who = su.name
    title = title_of(db, source, content_id) if content_id else ""
    if change == "watched" and content_id:
        played = bool(data.get("played"))
        verb = "watched" if played else "unwatched"
        summary = f"{who} {verb} {title}"
        if su is None:
            return {
                "status": "logged",
                "summary": summary,
                "note": "profile not known to MediaWrangler yet",
            }
        if not su.watch_readable or su.watch_synced_at is None:
            return {
                "status": "logged",
                "summary": summary,
                "note": "this profile's watch state isn't read by MediaWrangler yet",
            }
        keys = expand_content_id(db, source, content_id)
        changed = apply_live(db, su, keys, played)
        note = f"{len(keys)} item(s), {changed} changed" if len(keys) != 1 else None
        if not keys:
            return {"status": "logged", "summary": summary, "note": "no matching episodes known"}
        return {"status": "applied", "summary": summary, "note": note}
    if change in ("favorite", "watchlist", "not_interested"):
        flag = {
            "favorite": "is_favorite",
            "watchlist": "in_watchlist",
            "not_interested": "not_interested",
        }[change]
        on = data.get(flag)
        words = {
            "favorite": "favourite",
            "watchlist": "watchlist",
            "not_interested": "not interested",
        }[change]
        return {
            "status": "logged",
            "summary": f"{who} {'added' if on else 'removed'} {title} ({words})",
        }
    if change == "progress":
        return {
            "status": "logged",
            "summary": f"{who}: progress changed on {title}",
            "note": "see plays below",
        }
    return {"status": "logged", "summary": f"{who}: {change or 'change'} {title}".strip()}


# --- finished plays -------------------------------------------------------------------


def _cursor_key(source: Source) -> str:
    return f"silo_plays:{source.id}"


def apply_plays(db: Session, source: Source, plays: list[dict[str, Any]]) -> list[WebhookEvent]:
    """Handle Silo's finished plays (newest first) that we have not seen.

    The first time, nothing is applied: the newest play is only noted, so a
    long history is not replayed as if it had just happened."""
    setting = db.get(AppSetting, _cursor_key(source))
    if setting is None:
        newest = max((p.get("ended_at") or "" for p in plays), default="")
        db.add(AppSetting(key=_cursor_key(source), value={"ended_at": newest}))
        db.commit()
        return []
    cursor = setting.value.get("ended_at") or ""
    fresh = [p for p in plays if (p.get("ended_at") or "") >= cursor and p.get("session_id")]
    stored: list[WebhookEvent] = []
    for play in sorted(fresh, key=lambda p: p.get("ended_at") or ""):
        row = _apply_play(db, source, play)
        if row is not None:
            stored.append(row)
    newest = max([cursor, *(p.get("ended_at") or "" for p in fresh)])
    setting.value = {"ended_at": newest}
    db.commit()
    return stored


def _apply_play(db: Session, source: Source, play: dict[str, Any]) -> WebhookEvent | None:
    item = str(play.get("media_item_id") or "")
    profile_id = play.get("profile_id")
    name = play.get("profile_name") or play.get("username") or profile_id
    duration = play.get("duration_seconds") or 0
    watched = float(play.get("watched_seconds") or 0)
    pct = round(100 * watched / duration) if duration else 0
    completed = bool(play.get("completed"))
    title = title_of(db, source, item) if item else str(play.get("media_title") or "?")
    ended = _parse_time(play.get("ended_at"))
    status, note = "logged", None
    su = _profile(db, source, profile_id)
    if not item:
        note = "no item id"
    elif su is None or not su.watch_readable or su.watch_synced_at is None:
        note = "this profile's watch state isn't read by MediaWrangler yet"
    else:
        changed = apply_live(
            db,
            su,
            [item],
            completed,
            when=ended,
            position=None if completed else watched,
            duration=duration,
        )
        status = "applied" if changed else "logged"
        if not changed:
            note = "already known"
    summary = f"{name} {'finished' if completed else 'stopped'} {title} at {pct}%"
    return _store(
        db,
        source,
        event="play.finished",
        event_id=f"play:{play['session_id']}",
        profile_external_id=profile_id,
        profile_name=name,
        summary=summary[:255],
        status=status,
        note=note,
        payload={
            k: play.get(k)
            for k in (
                "session_id",
                "media_item_id",
                "completed",
                "watched_seconds",
                "duration_seconds",
                "ended_at",
            )
        },
    )


def _parse_time(value: Any) -> datetime | None:
    """An ISO time as naive UTC, like the database stores them."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is None else parsed.astimezone(UTC).replace(tzinfo=None)


def full_sync_due(db: Session, source: Source, now: datetime | None = None) -> bool:
    """False while a sync of this source is running, or finished moments ago."""
    now = now or utcnow()
    last = db.execute(
        select(SyncRun)
        .where(SyncRun.source_id == source.id, SyncRun.mode == "full")
        .order_by(SyncRun.id.desc())
        .limit(1)
    ).scalar()
    if last is None:
        return True
    if last.status == "running":
        return False
    return (now - (last.finished_at or last.started_at)) >= MIN_FULL_SYNC_GAP


_SEASON = re.compile(r"^(season \d+|specials)$", re.IGNORECASE)
_YEAR = re.compile(r"\s*[\[(]\d{4}[\])].*$")


def narrow_reads(
    db: Session, source: Source, folders: set[tuple[str, str]], titles: set[str]
) -> list[dict] | None:
    """The titles to read again after a scan of these folders and changes to these titles,
    as refresh specs ({"external_id"} for a title MediaWrangler has, {"locate"} for a new
    folder). None when only a read of the whole app will do: nothing narrower is known, or
    too many titles are touched."""
    if not folders and not titles:
        return None
    reads: dict[str, dict] = {}
    for library, folder in sorted(folders):
        like = _like(folder)
        rows = db.execute(
            select(SourceItem.external_id)
            .where(
                SourceItem.source_id == source.id,
                SourceItem.kind.in_(("movie", "series")),
                SourceItem.removed_at.is_(None),
            )
            # The folder holds the title (a film's path is its file), or is inside it (a season).
            .where(
                (SourceItem.path == folder)
                | SourceItem.path.like(f"{like}/%", escape="\\")
                | (
                    func.substr(folder, 1, func.length(SourceItem.path) + 1)
                    == SourceItem.path + "/"
                )
            )
        ).scalars()
        found = sorted(set(rows))
        for ext in found:
            reads[ext] = {"external_id": ext}
        if not found:
            lib = db.execute(
                select(Library).where(
                    Library.source_id == source.id, Library.external_id == library
                )
            ).scalar()
            if lib is None:
                return None
            path = folder
            if _SEASON.match(path.rsplit("/", 1)[-1]):
                path = path.rsplit("/", 1)[0]
            name = _YEAR.sub("", path.rsplit("/", 1)[-1]).strip()
            if not name:
                return None
            reads[f"locate:{path}"] = {
                "locate": {"name": name, "kind": lib.kind, "library": library, "path": path}
            }
    for ext in sorted(titles):
        known = db.execute(
            select(SourceItem.id).where(
                SourceItem.source_id == source.id,
                SourceItem.external_id == ext,
                SourceItem.removed_at.is_(None),
            )
        ).first()
        if known:
            reads.setdefault(ext, {"external_id": ext})
        elif not folders:
            return None  # a title MediaWrangler has not seen, and no folder to find it by
    if len(reads) > MAX_NARROW_READS:
        return None
    return list(reads.values())


WATCH_SYNC_GAP = timedelta(minutes=2)


def watch_sync_due(db: Session, source: Source, now: datetime | None = None) -> bool:
    """A quick read of who watched what is worth starting unless one is running
    or finished a moment ago. It heals anything a webhook missed."""
    now = now or utcnow()
    last = db.execute(
        select(SyncRun).where(SyncRun.source_id == source.id).order_by(SyncRun.id.desc()).limit(1)
    ).scalar()
    if last is None:
        return False  # nothing has been read yet, so there is nothing to heal
    if last.status == "running":
        return False
    return (now - (last.finished_at or last.started_at)) >= WATCH_SYNC_GAP
