"""Quick (watch-state) syncs, the scheduler, and the Activity page."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import select

from mediawrangler.db import utcnow
from mediawrangler.models import Source, SyncRun, WatchEvent
from mediawrangler.scheduler import Scheduler, next_run

from . import fakes
from .conftest import filter_options, post, table_rows
from .test_watch import age_holds, synced  # noqa: F401  (fixture)


def _source(db, name):
    return db.execute(select(Source).where(Source.name == name)).scalar_one()


def _wait(app):
    app.state.syncs._pool.shutdown(wait=True)
    app.state.syncs._pool = ThreadPoolExecutor(1)


def test_watch_sync_reads_only_watch_state(synced, app, monkeypatch):  # noqa: F811
    db, _sync_all, _ = synced
    tim_watch = list(fakes.JF_WATCH["u-alex"]) + [fakes._jf_watch("jf-sh-3")]
    monkeypatch.setitem(fakes.JF_WATCH, "u-alex", tim_watch)
    run_id = app.state.syncs.start(_source(db, "Jellyfin").id, "schedule", "watch")
    _wait(app)
    db.expire_all()
    run = db.get(SyncRun, run_id)
    assert run.status == "ok" and run.mode == "watch" and run.trigger == "schedule"
    assert "matching" not in run.stats and run.stats["watch"]["events"] >= 1
    assert ("jf-sh-3", "played") in {
        (e.item_external_id, e.kind) for e in db.execute(select(WatchEvent)).scalars()
    }


def test_a_read_that_fails_to_save_still_ends(synced, app, monkeypatch):  # noqa: F811
    db, *_ = synced

    def broken(run_id):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(app.state.syncs, "_run_once", broken)
    jf = _source(db, "Jellyfin").id
    run_id = app.state.syncs.start(jf, "schedule", "full")
    _wait(app)
    db.expire_all()
    run = db.get(SyncRun, run_id)
    assert run.status == "error" and "locked" in run.error and run.finished_at
    assert app.state.syncs.start(jf, "schedule", "full") != run_id  # the next read can start


def test_scheduler_due_jitter_and_overlap(synced, app):  # noqa: F811
    db, _sync_all, _ = synced
    jf = _source(db, "Jellyfin")
    jf.quick_minutes, jf.schedule_minutes = 15, 1440
    db.commit()
    now = utcnow()
    # Silo has no schedule: never started.
    assert next_run(db, _source(db, "Silo")) is None
    mode, when = next_run(db, jf)
    assert mode == "watch"
    wait = (
        when - db.execute(select(SyncRun).order_by(SyncRun.id.desc())).scalars().first().finished_at
    )
    assert timedelta(minutes=15) <= wait <= timedelta(minutes=16.6)  # 15 min + up to 10%

    sched = Scheduler(app.state.db, app.state.syncs, clock=lambda: now)
    assert sched.tick() == []
    sched.clock = lambda: now + timedelta(minutes=17)
    assert sched.tick() == [(jf.id, "watch")]
    assert sched.tick() == []  # that run is still queued or running
    _wait(app)
    # A day later the full sync is due and wins over the quick one.
    sched.clock = lambda: now + timedelta(days=2)
    assert sched.tick() == [(jf.id, "full")]
    _wait(app)


def test_scheduler_backs_off_after_failures(synced, app):  # noqa: F811
    db, _sync_all, _ = synced
    jf = _source(db, "Jellyfin")
    jf.quick_minutes = 10
    db.commit()
    _, first = next_run(db, jf)
    for _ in range(2):
        db.add(
            SyncRun(
                source_id=jf.id,
                mode="watch",
                trigger="schedule",
                status="error",
                finished_at=utcnow(),
            )
        )
    db.commit()
    _, later = next_run(db, jf)
    assert later - utcnow() > timedelta(minutes=39)  # 10 min x 4 after two failures
    assert first - utcnow() < timedelta(minutes=12)


def test_never_synced_source_gets_a_full_sync_first(synced, app):  # noqa: F811
    db, _sync_all, _ = synced
    other = Source(type="jellyfin", name="New", base_url="http://x", quick_minutes=10)
    db.add(other)
    db.commit()
    assert next_run(db, other)[0] == "full"
    other.enabled = False
    db.commit()
    assert next_run(db, other) is None


def test_schedule_fields_validate_and_save(synced, logged_in, app):  # noqa: F811
    db, _sync_all, _ = synced
    jf = _source(db, "Jellyfin")
    form = {"name": "Jellyfin", "base_url": "http://jellyfin", "enabled": "on"}
    url = f"/settings/connectors/{jf.id}"
    bad = post(logged_in, url, {**form, "quick_mode": "every", "quick_every": "2"})
    assert bad.status_code == 400 and "Watch-state sync" in bad.text
    bad = post(logged_in, url, {**form, "schedule_mode": "at", "schedule_at": "25:00"})
    assert bad.status_code == 400 and "HH:MM" in bad.text
    ok = post(
        logged_in,
        url,
        {
            **form,
            "quick_mode": "every",
            "quick_every": "15",
            "schedule_mode": "at",
            "schedule_at": "04:00",
        },
    )
    assert ok.status_code == 303
    db.expire_all()
    assert (jf.quick_minutes, jf.quick_at, jf.schedule_minutes, jf.schedule_at) == (
        15,
        None,
        None,
        "04:00",
    )
    page = logged_in.get(url).text
    assert 'name="quick_every" value="15"' in page and 'name="schedule_at" value="04:00"' in page
    assert "Watch-state sync" in logged_in.get("/").text
    hours = post(
        logged_in,
        url,
        {**form, "schedule_mode": "every", "schedule_every": "6", "schedule_unit": "hours"},
    )
    assert hours.status_code == 303
    db.expire_all()
    assert (jf.schedule_minutes, jf.schedule_at) == (360, None)
    assert 'name="schedule_every" value="6"' in logged_in.get(url).text
    assert post(logged_in, url, form).status_code == 303  # manual clears both
    db.expire_all()
    assert jf.schedule_minutes is None and jf.schedule_at is None


def test_activity_tabs(synced, logged_in, app, monkeypatch):  # noqa: F811
    db, _sync_all, _ = synced
    tim_watch = [w for w in fakes.JF_WATCH["u-alex"] if w["Id"] != "jf-sh-2"]
    monkeypatch.setitem(fakes.JF_WATCH, "u-alex", tim_watch)
    app.state.syncs.start(_source(db, "Jellyfin").id, "schedule", "watch")
    _wait(app)
    runs = logged_in.get("/activity").text
    rows = table_rows(runs)
    assert len(rows) == 3 and not any(hidden for *_, hidden in rows)
    assert "Watch state" in runs and "schedule" in runs
    assert [v for v, *_ in filter_options(runs)][:2] == ["all", "failed"]
    failed = table_rows(logged_in.get("/activity?filter=failed").text)
    assert all(hidden for *_, hidden in failed)

    assert not table_rows(logged_in.get("/activity?tab=audit").text)  # held for a second read
    age_holds(db)
    app.state.syncs.start(_source(db, "Jellyfin").id, "schedule", "watch")
    _wait(app)
    changes = logged_in.get("/activity?tab=audit").text
    (row,) = table_rows(changes)
    assert not row[2] and "unplayed" in row[0]
    assert "Found by a read of Jellyfin: alex unwatched it" in changes and "Recorded" in changes
    assert "Unplayed" in changes and "alex" in changes


def test_daily_time_waits_for_its_hour_and_skips_peak(synced, app):  # noqa: F811
    from datetime import UTC, datetime

    from mediawrangler.config import get_settings

    db, _sync_all, _ = synced
    jf = _source(db, "Jellyfin")
    tz = get_settings().timezone
    jf.schedule_at = "04:00"
    db.commit()
    last = db.execute(select(SyncRun).order_by(SyncRun.id.desc())).scalars().first()
    mode, when = next_run(db, jf)
    local = when.replace(tzinfo=UTC).astimezone(tz)
    assert mode == "full" and (local.hour, local.minute) == (4, 0)
    assert when > last.finished_at and when - last.finished_at <= timedelta(days=1)
    # Never synced: waits for 04:00 instead of running as soon as it is enabled.
    new = Source(type="jellyfin", name="New", base_url="http://x", schedule_at="04:00")
    db.add(new)
    db.commit()
    _, first = next_run(db, new)
    assert first > utcnow() and first - utcnow() <= timedelta(days=1)
    assert isinstance(datetime.now(UTC), datetime)


def test_a_full_sync_asks_the_app_to_scan_first(synced, app, monkeypatch):  # noqa: F811
    from mediawrangler.connectors.jellyfin import JellyfinConnector
    from mediawrangler.models import AuditLog
    from mediawrangler.writes import ENABLED_KEY, set_flag

    db, *_ = synced
    jf = _source(db, "Jellyfin").id
    asked, checks = [], iter([False, True])
    monkeypatch.setattr(JellyfinConnector, "scan_all", lambda self, libs: asked.append(1) or ["t"])
    monkeypatch.setattr(JellyfinConnector, "scan_all_done", lambda self, token: next(checks))

    run_id = app.state.syncs.start(jf, "manual", "full")  # writes off: read without asking
    _wait(app)
    db.expire_all()
    assert not asked and "writes are off" in db.get(SyncRun, run_id).stats["scan"]["note"]

    set_flag(db, ENABLED_KEY, True)
    db.commit()
    run_id = app.state.syncs.start(jf, "manual", "full")
    _wait(app)
    db.expire_all()
    run = db.get(SyncRun, run_id)
    assert run.status == "ok" and asked == [1] and "note" not in run.stats["scan"]
    assert db.execute(select(AuditLog).where(AuditLog.op == "scan")).scalar_one().source_id == jf

    run_id = app.state.syncs.start(jf, "webhook", "full")  # a webhook's read never scans
    _wait(app)
    db.expire_all()
    assert asked == [1] and "scan" not in db.get(SyncRun, run_id).stats
