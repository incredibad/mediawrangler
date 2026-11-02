"""Starts syncs on each connector's schedule.

Nothing is stored about "next run": it is worked out from the last finished
run of the source, so schedules survive a restart. Each connector has up to
two intervals: a full sync, and a quick one that reads only users and watch
state (full syncs read those too, so they also reset the quick timer).
"""

import logging
import random
import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from . import animelist, trash
from .config import get_settings
from .connectors import Capability, get_connector_class
from .db import Database, utcnow
from .models import Source, SyncRun
from .sync import SyncManager, can_sync

log = logging.getLogger(__name__)

FULL, QUICK = "full", "watch"
JITTER = 0.10  # each interval is stretched by up to this much, so sources drift apart
MAX_BACKOFF = 8  # a failing source waits 2x, 4x, 8x its interval, then stays at 8x
TICK_SECONDS = 30
NEVER = datetime.min  # "due as soon as possible"


def type_can_quick(source_type: str) -> bool:
    try:
        return Capability.READ_WATCH_STATE in get_connector_class(source_type).capabilities
    except Exception:
        return False


def _last_runs(db, source_id: int, modes: tuple[str, ...]) -> list[SyncRun]:
    return list(
        db.execute(
            select(SyncRun)
            .where(
                SyncRun.source_id == source_id,
                SyncRun.mode.in_(modes),
                SyncRun.status != "running",
            )
            .order_by(SyncRun.id.desc())
            .limit(MAX_BACKOFF.bit_length())
        ).scalars()
    )


def _slot_after(moment: datetime, at: str) -> datetime:
    """The first HH:MM (local time) after `moment`, as naive UTC like the database."""
    tz = get_settings().timezone
    local = moment.replace(tzinfo=UTC).astimezone(tz)
    hour, minute = int(at[:2]), int(at[3:])
    slot = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot <= local:
        slot = (local.replace(tzinfo=None) + timedelta(days=1)).replace(
            hour=hour, minute=minute, second=0, microsecond=0, tzinfo=tz
        )
    return slot.astimezone(UTC).replace(tzinfo=None)


def _due(db, source: Source, mode: str, now: datetime) -> datetime | None:
    minutes, at = (
        (source.schedule_minutes, source.schedule_at)
        if mode == FULL
        else (source.quick_minutes, source.quick_at)
    )
    if not minutes and not at:
        return None
    runs = _last_runs(db, source.id, (FULL,) if mode == FULL else (FULL, QUICK))
    if not runs:
        # A daily time waits for its hour even on a source that never ran.
        return _slot_after(now, at) if at else NEVER
    failures = 0
    for run in runs:
        if run.status != "error":
            break
        failures += 1
    last = runs[0]
    finished = last.finished_at or last.started_at
    if at:
        if not failures:
            return _slot_after(finished, at)
        # A failed daily run retries after 30 min, doubling to 4 h, not waiting a day.
        return finished + timedelta(minutes=30 * min(2 ** (failures - 1), MAX_BACKOFF))
    rng = random.Random(f"{source.id}:{mode}:{last.id}")  # noqa: S311  (spreads load, not security)
    jitter = rng.uniform(0, JITTER)
    return finished + timedelta(minutes=minutes * (1 + jitter) * min(2**failures, MAX_BACKOFF))


def next_run(db, source: Source, now: datetime | None = None) -> tuple[str, datetime] | None:
    """What the scheduler will start next for a source, and when (UTC). A full
    sync that is due wins over a quick one, since it reads watch state too."""
    now = now or utcnow()
    if not source.enabled or not can_sync(source):
        return None
    full = _due(db, source, FULL, now)
    quick = _due(db, source, QUICK, now) if type_can_quick(source.type) else None
    if quick is not None and not _last_runs(db, source.id, (FULL,)):
        # Quick reads need the items a full sync stores first.
        full, quick = (min(full, quick) if full else quick), None
    if full is not None and (quick is None or full <= quick or full <= now):
        return FULL, full
    if quick is not None:
        return QUICK, quick
    return None


class Scheduler:
    def __init__(
        self, db: Database, syncs: SyncManager, clock=utcnow, animelist=None, backups=None
    ):
        self.db, self.syncs, self.clock, self.animelist = db, syncs, clock, animelist
        self.backups = backups
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> list[tuple[int, str]]:
        """Start whatever is due; returns (source id, mode) for each."""
        started = []
        now = self.clock()
        if self.animelist is not None and animelist.refresh_due(self.db, self.animelist, now):
            self.syncs.bump()  # anime matches in cached views may have changed
        if self.backups is not None:
            self.backups.tick(now)
        try:
            with self.db.sessionmaker() as ts:
                trash.purge_expired(ts, now)
        except Exception:  # never let the trash stop the sync schedule
            log.exception("could not empty the expired trash")
        with self.db.sessionmaker() as s:
            for source in s.execute(select(Source).where(Source.enabled.is_(True))).scalars():
                nxt = next_run(s, source, now)
                if nxt is None or nxt[1] > now:
                    continue
                if self.syncs.running(s, source.id) is not None:
                    continue
                if self.syncs.start(source.id, "schedule", nxt[0]) is not None:
                    started.append((source.id, nxt[0]))
        return started

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
