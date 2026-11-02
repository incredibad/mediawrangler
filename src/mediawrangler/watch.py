"""Users, the user map, and watch-state ingest with the change log."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .connectors.base import UserRecord, WatchRecord
from .db import utcnow
from .models import AppUser, Source, SourceUser, WatchEvent, WatchState

# Position changes smaller than this aren't worth a change-log entry.
PROGRESS_EVENT_SECONDS = 60
# A played item that a full read leaves out is only an unwatch if a later read, at least
# this long after the first, leaves it out too. A read that came back short (an API
# fault, a half-finished rescan) then cannot unwatch anything, and a quick read started
# by a webhook a few seconds later cannot confirm it. Unwatches an app reports as they
# happen (apply_live) are not held.
CONFIRM_AFTER = timedelta(minutes=10)
# A webhook reports a playing client's position every few seconds. The stored position (and
# a change-log entry) only moves once it is this far from the last one noted, so a long
# viewing leaves a trail every few minutes instead of nothing or a row every tick.
PROGRESS_STEP_SECONDS = 300


def apply_users(db: Session, source: Source, records: list[UserRecord]) -> dict[str, SourceUser]:
    now = utcnow()
    existing = {
        u.external_id: u
        for u in db.execute(select(SourceUser).where(SourceUser.source_id == source.id)).scalars()
    }
    seen = set()
    for rec in records:
        seen.add(rec.external_id)
        row = existing.get(rec.external_id)
        if row is None:
            row = SourceUser(source_id=source.id, external_id=rec.external_id)
            db.add(row)
            existing[rec.external_id] = row
        row.name, row.account_id = rec.name, rec.account_id
        row.watch_readable = rec.watch_readable
        row.library_ids = rec.library_ids
        row.last_seen_at, row.removed_at = now, None
    for ext, row in existing.items():
        if ext not in seen and row.removed_at is None:
            row.removed_at = now
    db.commit()
    return {ext: row for ext, row in existing.items() if ext in seen}


def link_users(db: Session) -> None:
    """Link every source user to a person by name (case-insensitive), creating
    the person if needed. Links set by hand are left alone."""
    people = {p.name.lower(): p for p in db.execute(select(AppUser)).scalars()}
    unlinked = db.execute(
        select(SourceUser)
        .where(SourceUser.removed_at.is_(None), SourceUser.link_assigned.is_(False))
        .order_by(SourceUser.source_id, SourceUser.id)
    ).scalars()
    for su in unlinked:
        if su.app_user_id is not None:
            continue
        person = people.get(su.name.lower())
        if person is None:
            person = AppUser(name=su.name)
            db.add(person)
            db.flush()
            people[su.name.lower()] = person
        su.app_user_id = person.id
    db.commit()


def set_link(db: Session, source_user_id: int, app_user_id: int | None) -> None:
    su = db.get(SourceUser, source_user_id)
    if su is None:
        return
    su.app_user_id = app_user_id
    su.link_assigned = True
    db.commit()


@dataclass
class WatchStats:
    played: int = 0
    in_progress: int = 0
    events: int = 0
    gone: int = 0  # items that dropped off the played list because the app no longer has them
    unchecked: int = 0  # ... that could not be checked this time, so were left as they were
    held: int = 0  # played items missing from a read, waiting for a later read to agree

    def add(self, other: WatchStats) -> None:
        self.played += other.played
        self.in_progress += other.in_progress
        self.events += other.events
        self.gone += other.gone
        self.unchecked += other.unchecked
        self.held += other.held


def apply_watch(
    db: Session,
    su: SourceUser,
    records: list[WatchRecord],
    live_items: set[str] | None = None,
    exists: Callable[[list[str]], set[str] | None] | None = None,
) -> WatchStats:
    """Store one user's full watch state. After the first read, every change
    is logged: played, unplayed, and real progress moves. An item that left
    the source (`live_items`) just drops out: deleting a file isn't an unwatch.

    `live_items` is only as fresh as the last library read, so where the app
    can say (`exists`), an item that dropped off the played list is asked
    about first: gone (a replaced file, a library the user lost) drops out
    quietly, and if the app can't be asked (it returns None) the item is left
    exactly as it was and looked at again next read. Only an item the app
    confirms is still there counts as unwatched, and only once it has been
    missing from two reads at least CONFIRM_AFTER apart (`stats.held` counts
    the ones waiting for the second)."""
    now = utcnow()
    stats = WatchStats()
    log_changes = su.watch_synced_at is not None
    existing = {
        w.item_external_id: w
        for w in db.execute(select(WatchState).where(WatchState.source_user_id == su.id)).scalars()
    }

    def event(item: str, kind: str, source_time=None, position=None) -> None:
        if log_changes:
            db.add(
                WatchEvent(
                    source_user_id=su.id,
                    item_external_id=item,
                    kind=kind,
                    at=now,
                    source_time=source_time,
                    position_seconds=position,
                    via="poll",
                )
            )
            stats.events += 1

    seen = set()
    for rec in records:
        if rec.item_external_id in seen:
            continue
        seen.add(rec.item_external_id)
        stats.played += rec.played
        stats.in_progress += (not rec.played) and bool(rec.position_seconds)
        old = existing.get(rec.item_external_id)
        if old is None:
            old = WatchState(
                source_user_id=su.id, item_external_id=rec.item_external_id, played=False
            )
            db.add(old)
            was_played, old_pos = False, None
        else:
            was_played, old_pos = old.played, old.position_seconds
        if rec.played and not was_played:
            event(rec.item_external_id, "played", rec.last_played_at)
        elif was_played and not rec.played:
            event(rec.item_external_id, "unplayed", rec.last_played_at, rec.position_seconds)
        elif abs((rec.position_seconds or 0) - (old_pos or 0)) >= PROGRESS_EVENT_SECONDS:
            event(rec.item_external_id, "progress", rec.last_played_at, rec.position_seconds)
        old.missing_since = None
        old.played = rec.played
        old.play_count = rec.play_count
        old.position_seconds = rec.position_seconds
        old.duration_seconds = rec.duration_seconds
        old.last_played_at = rec.last_played_at
        old.observed_at = now

    dropped = {item: row for item, row in existing.items() if item not in seen}
    confirmed: set[str] | None = None
    if exists is not None:
        asked = [
            item
            for item, row in dropped.items()
            if (live_items is None or item in live_items) and (row.played or row.position_seconds)
        ]
        if asked:
            confirmed = exists(asked)
            if confirmed is None:
                stats.unchecked += len(asked)
            else:
                stats.gone += len(set(asked) - confirmed)
    for item, row in dropped.items():
        if live_items is not None and item not in live_items:
            db.delete(row)
            continue
        if exists is not None and (row.played or row.position_seconds):
            if confirmed is None:
                continue  # could not ask: change nothing, ask again next time
            if item not in confirmed:
                db.delete(row)  # gone, not unwatched
                continue
        if row.played:
            if row.missing_since is None or now - row.missing_since < CONFIRM_AFTER:
                if row.missing_since is None:
                    row.missing_since = now
                stats.held += 1
                continue  # not yet an unwatch: it stays played until a later read agrees
            event(item, "unplayed")
        elif row.position_seconds:
            event(item, "progress", position=0)
        db.delete(row)

    su.watch_synced_at = now
    db.commit()
    return stats


def watch_counts(db: Session) -> dict[int, tuple[int, int]]:
    """(played, in progress) per source user."""
    rows = db.execute(
        select(WatchState.source_user_id, WatchState.played, func.count()).group_by(
            WatchState.source_user_id, WatchState.played
        )
    ).all()
    out: dict[int, tuple[int, int]] = {}
    for suid, played, n in rows:
        p, i = out.get(suid, (0, 0))
        out[suid] = (p + n, i) if played else (p, i + n)
    return out


# --- live changes (0.8): webhooks tell us what changed, between full reads -------


def apply_live(
    db: Session,
    su: SourceUser,
    keys: list[str],
    played: bool,
    when: datetime | None = None,
    position: float | None = None,
    duration: float | None = None,
    via: str = "webhook",
) -> int:
    """Apply a change an app reported as it happened: items become played, or
    unplayed (their row is removed, as a full read would). Changes are logged
    like a full read's, but only once a first full read has set the baseline.
    A not-played item with a `position` is stored as in progress. Returns how
    many items changed."""
    if not keys:
        return 0
    now = utcnow()
    existing = {
        w.item_external_id: w
        for w in db.execute(
            select(WatchState).where(
                WatchState.source_user_id == su.id, WatchState.item_external_id.in_(keys)
            )
        ).scalars()
    }
    log = su.watch_synced_at is not None
    changed = 0

    def event(key: str, kind: str, pos: float | None = None) -> None:
        if log:
            db.add(
                WatchEvent(
                    source_user_id=su.id,
                    item_external_id=key,
                    kind=kind,
                    at=now,
                    source_time=when,
                    position_seconds=pos,
                    via=via,
                )
            )

    for key in keys:
        row = existing.get(key)
        if played:
            if row is None:
                db.add(
                    WatchState(
                        source_user_id=su.id,
                        item_external_id=key,
                        played=True,
                        play_count=1,
                        last_played_at=when or now,
                        observed_at=now,
                    )
                )
            elif not row.played:
                row.played, row.position_seconds = True, None
                row.last_played_at, row.observed_at = when or now, now
            else:
                continue
            event(key, "played")
            changed += 1
        elif position is not None and position >= PROGRESS_EVENT_SECONDS:
            if row is not None and row.played:
                continue  # a partial rewatch doesn't make a watched item unwatched
            if row is None:
                row = WatchState(source_user_id=su.id, item_external_id=key, played=False)
                db.add(row)
            fresh = row.position_seconds is None
            moved = fresh or abs(position - row.position_seconds) >= PROGRESS_STEP_SECONDS
            if moved:
                row.position_seconds, row.duration_seconds = position, duration
                row.last_played_at, row.observed_at = when or now, now
                event(key, "progress", position)
                changed += 1
        elif row is not None:
            was_played = row.played
            db.delete(row)
            event(key, "unplayed" if was_played else "progress")
            changed += 1
    db.commit()
    return changed
