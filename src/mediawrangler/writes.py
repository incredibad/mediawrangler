"""Writing Local's state out to the apps (0.9).

The rules:
- Off until switched on, globally and then per person and app (0.10); a pause stops sending
  without losing anything queued.
- Played goes everywhere: whatever Local says is played is marked played in each
  app that has the title and does not show it played. Safe to infer from a
  difference, so it also catches up when a title appears in an app later.
- Unplayed is written only when MediaWrangler actually saw the unwatch happen
  (a Local change after the person was switched on), never from a mere
  difference. An unwatch an app reports as it happens is sent at once; one seen
  only by comparing reads counts once two reads at least ten minutes apart agree
  (watch.CONFIRM_AFTER).
- Nothing is held for approval. Changes are queued (the outbox) and sent at
  once at the app's pace; every attempt is recorded in the audit log.
- Our own writes come back as ordinary events; they change nothing in Local
  (already played, or already not), so they cannot loop.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .connectors import get_connector_class
from .connectors.base import Capability, ConnectorError, ItemGone, UserRecord
from .crypto import SecretBox
from .db import Database, utcnow
from .local import LKey, item_keys, item_keys_for
from .models import (
    AppSetting,
    AppUser,
    AuditLog,
    CanonicalItem,
    Library,
    LocalCursor,
    LocalEvent,
    LocalPlay,
    LocalState,
    Source,
    SourceAccountKey,
    SourceItem,
    SourceUser,
    UserSync,
    WatchEvent,
    WatchState,
    WriteJob,
)
from .watch import apply_live
from .watchdiff import readable_users

log = logging.getLogger("mediawrangler.writes")

ENABLED_KEY, PAUSED_KEY = "writes_enabled", "writes_paused"
RETRIES = 3
WORKERS = 4  # writes to one app in flight at once
CHUNK = 50
FAILED_BACKOFF = timedelta(hours=1)  # a write that failed is not re-queued for this long
PREVIEW_ROWS = 100


def flag(db: Session, key: str) -> bool:
    row = db.get(AppSetting, key)
    return bool(row.value) if row is not None else False


def set_flag(db: Session, key: str, value: bool) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    db.commit()


@dataclass
class Planned:
    source_id: int
    source_name: str
    source_user_id: int
    item_id: str
    key: LKey
    op: str
    played_at: datetime | None
    label: str = ""
    origin: str = ""  # the apps or imports Local learned this from

    @property
    def before(self) -> str:
        return "unplayed" if self.op == "played" else "played"


def filter_lines(text: str | None) -> list[str]:
    """The shows a filter names: one per line, matched anywhere in the title."""
    return [line.strip().lower() for line in (text or "").splitlines() if line.strip()]


def history_switches(db: Session, person: AppUser) -> dict[int, UserSync]:
    """The apps this person's watch history is written to, by source id."""
    return {
        u.source_id: u
        for u in db.execute(
            select(UserSync).where(
                UserSync.app_user_id == person.id, UserSync.sync_history.is_(True)
            )
        ).scalars()
    }


def set_history(db: Session, person: AppUser, source_id: int, on: bool) -> None:
    """Switch writing watch history for one person to one app on or off. Switching
    off drops what was queued for that app (Pause keeps it)."""
    row = db.execute(
        select(UserSync).where(UserSync.app_user_id == person.id, UserSync.source_id == source_id)
    ).scalar_one_or_none()
    if row is None:
        row = UserSync(app_user_id=person.id, source_id=source_id)
        db.add(row)
    if on and not row.sync_history:
        row.since = utcnow()  # only unwatches from here on are written
    if not on:
        db.execute(
            delete(WriteJob).where(
                WriteJob.app_user_id == person.id,
                WriteJob.source_id == source_id,
                WriteJob.status == "pending",
            )
        )
    row.sync_history = on


def writable(db: Session, person: AppUser) -> list[tuple[SourceUser, Source]]:
    out = []
    for su in readable_users(db, person).values():
        source = db.get(Source, su.source_id)
        if source is None or not source.enabled:
            continue
        if Capability.WRITE_WATCH_STATE in get_connector_class(source.type).capabilities:
            out.append((su, source))
    return out


def _closed_ids(db: Session, su: SourceUser, source: Source) -> set[str]:
    """Watch ids of items in libraries this user can't open; the app 404s on them."""
    if su.library_ids is None:
        return set()
    wk = get_connector_class(source.type).watch_key
    rows = db.execute(
        select(SourceItem.external_id)
        .join(Library, Library.id == SourceItem.library_id)
        .where(
            SourceItem.source_id == source.id,
            Library.external_id.not_in(su.library_ids),
        )
    ).scalars()
    return {wk(ext) for ext in rows}


def _labels(db: Session, keys: set[LKey]) -> dict[LKey, str]:
    ids = {k[1] for k in keys}
    titles = {}
    for i in range(0, len(ids), 500):
        chunk = list(ids)[i : i + 500]
        for cid, title, year in db.execute(
            select(CanonicalItem.id, CanonicalItem.title, CanonicalItem.year).where(
                CanonicalItem.id.in_(chunk)
            )
        ):
            titles[cid] = f"{title} ({year})" if year else title
    out = {}
    for key in keys:
        base = titles.get(key[1], f"#{key[1]}")
        out[key] = f"{base} S{key[2]:02d}E{key[3]:02d}" if key[0] == "ep" else base
    return out


def _play_sources(db: Session, person: AppUser, keys: set[LKey]) -> dict[LKey, str]:
    """For each title, the apps and imports whose plays Local holds for it."""
    ids = {k[1] for k in keys}
    where = [LocalPlay.app_user_id == person.id]
    if len(ids) <= 500:  # a few titles (a webhook's change): read only theirs
        where.append(LocalPlay.canonical_id.in_(ids))
    found: dict[LKey, list[str]] = {}
    for play in db.execute(select(LocalPlay).where(*where)).scalars():
        if play.canonical_id not in ids:
            continue
        key = (play.kind, play.canonical_id, play.season, play.episode)
        if key in keys:
            names = found.setdefault(key, [])
            names.extend(n for n in (play.sources or "").split(", ") if n and n not in names)
    return {k: ", ".join(v) for k, v in found.items()}


def local_behind(db: Session, person: AppUser) -> bool:
    """Whether an app has reported a change that Local has not taken in yet. Writes wait
    until it has: planned against a Local that lags an app's own state, a fresh unwatch
    there reads as a play the app is missing and is undone. The writer is kicked again
    whenever Local catches up."""
    for su in readable_users(db, person).values():
        cursor = db.get(LocalCursor, su.id)
        if cursor is None:
            continue  # not folded into Local yet; Local's catch-up adds it whole
        newer = db.execute(
            select(WatchEvent.id)
            .where(
                WatchEvent.source_user_id == su.id,
                WatchEvent.id > cursor.last_event_id,
                WatchEvent.via != "write",  # our own writes, checked against Local as sent
            )
            .limit(1)
        ).first()
        if newer is not None:
            return True
    return False


def plan_titles(db: Session, touched: dict[int, set[LKey]]) -> int:
    """Queue the writes for only these titles of these people (a webhook's change),
    under the same rules as a full cycle. Returns how many were queued."""
    if not touched or not flag(db, ENABLED_KEY):
        return 0
    queued = 0
    for person in db.execute(select(AppUser).where(AppUser.id.in_(touched))).scalars():
        if person.local_seeded_at is None or local_behind(db, person):
            continue
        keys = touched[person.id]
        keys_of = {
            source.id: item_keys_for(db, source, keys) for _su, source in writable(db, person)
        }
        planned = plan_person(db, person, only_on=True, keys_of=keys_of, shows={k[1] for k in keys})
        enqueue(db, person, planned)
        queued += len(planned)
    return queued


def plan_person(
    db: Session,
    person: AppUser,
    now: datetime | None = None,
    only_on: bool = False,
    keys_of: dict[int, dict[str, LKey]] | None = None,
    shows: set[int] | None = None,
) -> list[Planned]:
    """What would be written for this person right now. Reads only. A preview looks
    at every app that can be written to; only_on keeps to the apps switched on.
    `keys_of` shares each app's item keys across a cycle's people; with `shows`
    (canonical ids) it holds only those titles, and only they are read from Local."""
    keys_of = {} if keys_of is None else keys_of
    now = now or utcnow()
    switches = history_switches(db, person)
    local_where = [LocalState.app_user_id == person.id]
    if shows is not None:
        local_where.append(LocalState.canonical_id.in_(shows))
    local = {
        (st.kind, st.canonical_id, st.season, st.episode): st
        for st in db.execute(select(LocalState).where(*local_where)).scalars()
    }
    unwatched: dict[LKey, tuple[str, datetime]] = {}
    starts = [sw.since for sw in switches.values() if sw.since is not None]
    if starts:
        for ev in db.execute(
            select(LocalEvent).where(
                LocalEvent.app_user_id == person.id,
                LocalEvent.change == "unplayed",
                LocalEvent.at >= min(starts),
            )
        ).scalars():
            unwatched[(ev.kind, ev.canonical_id, ev.season, ev.episode)] = (ev.reason, ev.at)
    busy = {
        (j.source_user_id, j.item_id, j.op)
        for j in db.execute(
            select(WriteJob).where(
                (WriteJob.app_user_id == person.id)
                & (
                    (WriteJob.status == "pending")
                    | ((WriteJob.status == "failed") & (WriteJob.done_at >= now - FAILED_BACKOFF))
                )
            )
        ).scalars()
    }
    planned: list[Planned] = []
    for su, source in writable(db, person):
        switch = switches.get(source.id)
        if only_on and switch is None:
            continue
        since = switch.since if switch is not None else None
        app_played = set(
            db.execute(
                select(WatchState.item_external_id).where(
                    WatchState.source_user_id == su.id, WatchState.played.is_(True)
                )
            ).scalars()
        )
        closed = _closed_ids(db, su, source)
        if source.id not in keys_of:
            keys_of[source.id] = item_keys(db, source)
        for wid, key in keys_of[source.id].items():
            if wid in closed:
                continue
            st = local.get(key)
            if st is not None and st.played:
                op = None if wid in app_played else "played"
            elif (
                key in unwatched
                and wid in app_played
                and since is not None
                and unwatched[key][1] >= since
            ):
                op = "unplayed"
            else:
                op = None
            if op is None or (su.id, wid, op) in busy:
                continue
            planned.append(
                Planned(
                    source.id,
                    source.name,
                    su.id,
                    wid,
                    key,
                    op,
                    st.last_played_at if st is not None and op == "played" else None,
                )
            )
    if planned:
        labels = _labels(db, {p.key for p in planned})
        origins = _play_sources(db, person, {p.key for p in planned if p.op == "played"})
        for p in planned:
            p.label = labels[p.key]
            p.origin = origins.get(p.key, "") if p.op == "played" else unwatched[p.key][0]
        needles = filter_lines(person.writes_filter)
        if needles:
            planned = [p for p in planned if any(n in p.label.lower() for n in needles)]
    planned.sort(key=lambda p: (p.source_name, p.label.lower(), p.op))
    return planned


def why(p: Planned) -> str:
    """The reason for a write, in words, for the audit log."""
    seen = f" in {p.origin}" if p.origin else ""
    if p.op == "played":
        when = f" on {p.played_at:%d %b %Y}" if p.played_at else ""
        return f"Local has it watched{when} (seen{seen or ' elsewhere'}); {p.source_name} did not"
    return f"Unwatched{seen}; {p.source_name} still had it watched"


def enqueue(db: Session, person: AppUser, planned: list[Planned]) -> int:
    for p in planned:
        db.add(
            WriteJob(
                app_user_id=person.id,
                source_id=p.source_id,
                source_user_id=p.source_user_id,
                item_id=p.item_id,
                kind=p.key[0],
                canonical_id=p.key[1],
                season=p.key[2],
                episode=p.key[3],
                label=p.label[:255],
                op=p.op,
                played_at=p.played_at,
                before=p.before,
                why=why(p)[:200],
            )
        )
    db.commit()
    return len(planned)


GONE = "gone"  # _send's answer when the app no longer knows the item


def _send(connector, user: UserRecord, job: WriteJob) -> tuple[int, str | None]:
    """One write, retried a couple of times. Returns (attempts, error). An item the
    app no longer knows is not retried: the error is GONE."""
    error = None
    for attempt in range(1, RETRIES + 1):
        try:
            connector.set_played(user, job.item_id, job.op == "played", job.played_at)
            return attempt, None
        except ItemGone:
            return attempt, GONE
        except ConnectorError as exc:
            error = str(exc)[:255]
            time.sleep(attempt)
    return RETRIES, error


def _stale_reason(
    db: Session, job: WriteJob, ids: dict, closed: set[str], su: SourceUser | None
) -> str | None:
    """Why a queued write no longer makes sense, or None if it still does."""
    if su is None or su.removed_at is not None:
        return "The user is no longer in the app"
    if job.item_id not in ids:
        return "The app no longer lists this item (replaced or removed)"
    if job.item_id in closed:
        return "The user cannot open this item's library"
    st = db.execute(
        select(LocalState.played).where(
            LocalState.app_user_id == job.app_user_id,
            LocalState.kind == job.kind,
            LocalState.canonical_id == job.canonical_id,
            LocalState.season == job.season,
            LocalState.episode == job.episode,
        )
    ).scalar()
    if bool(st) != (job.op == "played"):
        return "Local no longer says this, so nothing to write"
    return None


def drop_stale(db: Session, source: Source, jobs: list[WriteJob]) -> list[WriteJob]:
    """Cancel queued or failed writes that no longer make sense (the item was
    replaced, the library closed, Local changed its mind) instead of sending or
    retrying them. Returns the jobs that still stand."""
    if not jobs:
        return []
    ids = item_keys(db, source)
    users = {
        su.id: su
        for su in db.execute(
            select(SourceUser).where(SourceUser.id.in_({j.source_user_id for j in jobs}))
        ).scalars()
    }
    names = {
        p.id: p.name
        for p in db.execute(
            select(AppUser).where(AppUser.id.in_({j.app_user_id for j in jobs}))
        ).scalars()
    }
    closed = {sid: _closed_ids(db, su, source) for sid, su in users.items()}
    now, keep = utcnow(), []
    for job in jobs:
        su = users.get(job.source_user_id)
        reason = _stale_reason(db, job, ids, closed.get(job.source_user_id, set()), su)
        if reason is None:
            keep.append(job)
            continue
        job.status, job.done_at, job.error = "skipped", now, reason
        db.add(
            AuditLog(
                at=now,
                app_user_id=job.app_user_id,
                source_id=source.id,
                source_name=source.name,
                user_name=names.get(job.app_user_id, "?"),
                label=job.label,
                item_id=job.item_id,
                op=job.op,
                before=job.before,
                after=job.before,
                result="skipped",
                detail=reason,
                why=job.why,
                job_id=job.id,
            )
        )
    db.commit()
    return keep


def sweep_failed(db: Session) -> None:
    """Failed writes whose target has gone stop being failures."""
    failed = list(db.execute(select(WriteJob).where(WriteJob.status == "failed")).scalars())
    by_source: dict[int, list[WriteJob]] = {}
    for job in failed:
        by_source.setdefault(job.source_id, []).append(job)
    for sid, group in by_source.items():
        source = db.get(Source, sid)
        if source is not None:
            drop_stale(db, source, group)


def _drain_source(
    db: Session,
    box: SecretBox,
    source: Source,
    jobs: list[WriteJob],
    on_replaced: Callable[[int], None] | None = None,
) -> int:
    from .sync import connector_for

    sent = 0
    jobs = drop_stale(db, source, jobs)
    if not jobs:
        return 0
    users = {
        su.id: su
        for su in db.execute(
            select(SourceUser).where(SourceUser.id.in_({j.source_user_id for j in jobs}))
        ).scalars()
    }
    people = {
        p.id: p.name
        for p in db.execute(
            select(AppUser).where(AppUser.id.in_({j.app_user_id for j in jobs}))
        ).scalars()
    }
    with connector_for(source, box) as connector:
        if hasattr(connector, "account_keys"):
            connector.account_keys = {
                k.account_id: box.decrypt(k.key_enc)
                for k in db.execute(
                    select(SourceAccountKey).where(
                        SourceAccountKey.source_id == source.id, SourceAccountKey.state == "ok"
                    )
                ).scalars()
            }
        records = {
            sid: UserRecord(su.external_id, su.name, su.account_id) for sid, su in users.items()
        }
        with ThreadPoolExecutor(WORKERS) as pool:
            results = list(pool.map(lambda j: _send(connector, records[j.source_user_id], j), jobs))
    now = utcnow()
    replaced = False
    for job, (attempts, error) in zip(jobs, results, strict=True):
        job.attempts += attempts
        job.done_at = now
        gone = error == GONE
        if gone:
            # The app swapped the item for a new one (an upgraded file): not a failure.
            # The library is read again, which learns the new id, and the next cycle
            # writes to that if it still needs it.
            replaced = True
            error = f"{source.name} no longer has this item"
        job.status = "skipped" if gone else "failed" if error else "sent"
        job.error = error
        if not error:
            sent += 1
            apply_live(
                db,
                users[job.source_user_id],
                [job.item_id],
                job.op == "played",
                when=job.played_at,
                via="write",
            )
        db.add(
            AuditLog(
                at=now,
                app_user_id=job.app_user_id,
                source_id=source.id,
                source_name=source.name,
                user_name=people.get(job.app_user_id, "?"),
                label=job.label,
                item_id=job.item_id,
                op=job.op,
                before=job.before,
                after=job.op if not error else job.before,
                result="replaced" if gone else "error" if error else "applied",
                detail=(
                    f"{source.name} no longer has this item: its file was most likely replaced "
                    "(an upgrade). The library is being read again; nothing is wrong."
                    if gone
                    else error
                ),
                why=job.why,
                job_id=job.id,
            )
        )
    db.commit()
    if replaced and on_replaced is not None:
        on_replaced(source.id)
    return sent


def drain(
    database: Database, box: SecretBox, on_replaced: Callable[[int], None] | None = None
) -> int:
    """Send what is queued, in order, a chunk at a time, until nothing is left or
    writes are switched off or paused. Returns how many were sent."""
    sent = 0
    while True:
        with database.sessionmaker() as db:
            if not flag(db, ENABLED_KEY) or flag(db, PAUSED_KEY):
                return sent
            waiting = {
                pid
                for pid in db.execute(
                    select(WriteJob.app_user_id).where(WriteJob.status == "pending").distinct()
                ).scalars()
                if local_behind(db, db.get(AppUser, pid))
            }
            jobs = list(
                db.execute(
                    select(WriteJob)
                    .join(
                        UserSync,
                        (UserSync.app_user_id == WriteJob.app_user_id)
                        & (UserSync.source_id == WriteJob.source_id),
                    )
                    .where(
                        WriteJob.status == "pending",
                        UserSync.sync_history.is_(True),
                        WriteJob.app_user_id.not_in(waiting),
                    )
                    .order_by(WriteJob.id)
                    .limit(CHUNK)
                ).scalars()
            )
            if not jobs:
                return sent
            by_source: dict[int, list[WriteJob]] = {}
            for j in jobs:
                by_source.setdefault(j.source_id, []).append(j)
            for sid, group in by_source.items():
                source = db.get(Source, sid)
                try:
                    sent += _drain_source(db, box, source, group, on_replaced)
                except Exception:  # an app that cannot be reached: leave its jobs queued
                    log.exception("could not write to %s", source.name if source else sid)
                    db.rollback()
                    return sent


def run_cycle(
    database: Database, box: SecretBox, on_replaced: Callable[[int], None] | None = None
) -> None:
    """Queue what is needed for everyone switched on, then send it."""
    with database.sessionmaker() as db:
        if not flag(db, ENABLED_KEY):
            return
        people = (
            select(AppUser)
            .join(UserSync, UserSync.app_user_id == AppUser.id)
            .where(UserSync.sync_history.is_(True))
            .distinct()
        )
        keys_of: dict[int, dict[str, LKey]] = {}
        for person in db.execute(people).scalars():
            if person.local_seeded_at is None or local_behind(db, person):
                continue
            enqueue(db, person, plan_person(db, person, only_on=True, keys_of=keys_of))
        sweep_failed(db)
    drain(database, box, on_replaced)


class WriteRunner:
    """Runs cycles on a background thread; asks arriving while one runs make it
    go round once more."""

    def __init__(self, database: Database, box: SecretBox):
        self.database, self.box = database, box
        # Asked to read an app's library again when a write finds its item replaced.
        self.on_replaced: Callable[[int], None] | None = None
        self._lock = threading.Lock()
        self._again = False
        self._plan = False

    def kick(self, plan: bool = True) -> None:
        """`plan`: work out everyone's writes first (a full cycle); without it, only
        send what is queued (the direct path has queued its own)."""
        if plan:
            self._plan = True
        self._again = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        if not self._lock.acquire(blocking=False):
            return
        try:
            while self._again:
                self._again = False
                plan, self._plan = self._plan, False
                try:
                    if plan:
                        run_cycle(self.database, self.box, self.on_replaced)
                    else:
                        drain(self.database, self.box, self.on_replaced)
                except Exception:
                    log.exception("write cycle failed")
        finally:
            self._lock.release()


RECENT = timedelta(minutes=5)  # a run stays on show this long after its last write
RATE_WINDOW = timedelta(seconds=10)


def status(db: Session, now: datetime | None = None) -> dict:
    """Queue counts, plus the current run (everything queued or finished lately)
    with progress per app, speed and time left, for the live panel."""
    from sqlalchemy import func, or_

    now = now or utcnow()
    counts = dict(db.execute(select(WriteJob.status, func.count()).group_by(WriteJob.status)).all())
    out = {
        "pending": counts.get("pending", 0),
        "sent": counts.get("sent", 0),
        "failed": counts.get("failed", 0),
        "skipped": counts.get("skipped", 0),
        "run": None,
    }
    since = now - RECENT
    rows = db.execute(
        select(WriteJob.source_id, WriteJob.status, func.count())
        .where(
            or_(
                WriteJob.status == "pending",  # however long it has waited (a pause, say)
                WriteJob.created_at >= since,
                WriteJob.done_at >= since,
            )
        )
        .group_by(WriteJob.source_id, WriteJob.status)
    ).all()
    if rows:
        names = dict(db.execute(select(Source.id, Source.name)).all())
        per: dict[str, dict[str, int]] = {}
        for sid, st, n in rows:
            app = per.setdefault(names.get(sid, "?"), {"total": 0, "done": 0, "failed": 0})
            app["total"] += n
            if st != "pending":
                app["done"] += n
            if st == "failed":
                app["failed"] += n
        total = sum(a["total"] for a in per.values())
        done = sum(a["done"] for a in per.values())
        recent = db.execute(
            select(func.count()).where(
                WriteJob.status == "sent", WriteJob.done_at >= now - RATE_WINDOW
            )
        ).scalar_one()
        rate = recent / RATE_WINDOW.total_seconds()
        left = total - done
        out["run"] = {
            "total": total,
            "done": done,
            "left": left,
            "failed": sum(a["failed"] for a in per.values()),
            "apps": sorted(per.items()),
            "rate": rate,
            "eta": int(left / rate) if left and rate else None,
        }
    return out
