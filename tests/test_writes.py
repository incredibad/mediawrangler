"""Writing Local's state out to the apps (0.9)."""

# ruff: noqa: F811  (the `synced` fixture is shared from test_watch)

import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from mediawrangler import local, writes
from mediawrangler.db import utcnow
from mediawrangler.models import (
    AuditLog,
    LocalEvent,
    LocalState,
    Source,
    SourceItem,
    SourceUser,
    WatchEvent,
    WriteJob,
)
from mediawrangler.watchdiff import readable_users

from . import fakes
from .conftest import filter_options, post, table_rows
from .test_watch import _person
from .test_watch import synced as synced  # re-exported: pytest finds fixtures by module


@pytest.fixture(autouse=True)
def _clean_fakes():
    for store in (fakes.JF_WRITES, fakes.SILO_WRITES, fakes.SILO_FAIL):
        store.clear()
    yield
    for store in (fakes.JF_WRITES, fakes.SILO_WRITES, fakes.SILO_FAIL):
        store.clear()


def _source(db, typ):
    return db.execute(select(Source).where(Source.type == typ)).scalar_one()


def _switch_on(db, person, since=None, title_filter=None):
    person.writes_filter = title_filter
    for _su, source in writes.writable(db, person):
        writes.set_history(db, person, source.id, True)
    db.flush()
    for sw in writes.history_switches(db, person).values():
        sw.since = since
    writes.set_flag(db, writes.ENABLED_KEY, True)
    db.commit()


def _cycle(app):
    writes.run_cycle(app.state.db, app.state.box)


def test_nothing_is_written_until_switched_on(synced, app):
    db, *_ = synced
    user1 = _person(db, "user1")
    assert not writes.history_switches(db, user1)
    # The plan can be looked at, but the cycle sends nothing without the switches.
    assert writes.plan_person(db, user1)
    _cycle(app)
    assert not fakes.SILO_WRITES and not fakes.JF_WRITES
    for _su, source in writes.writable(db, user1):
        writes.set_history(db, user1, source.id, True)
    db.commit()
    _cycle(app)  # the global switch is still off
    assert not fakes.SILO_WRITES and not fakes.JF_WRITES


def test_played_is_written_to_the_app_that_lacks_it_then_settles(synced, app):
    db, *_ = synced
    user1 = _person(db, "user1")
    plan = writes.plan_person(db, user1)
    assert {(p.source_name, p.op) for p in plan} == {("Silo", "played")}
    assert any("Sherlock" in p.label and p.label.endswith("S01E02") for p in plan)
    _switch_on(db, user1)
    _cycle(app)
    db.expire_all()
    assert fakes.SILO_WRITES and all(w[0] == "POST" for w in fakes.SILO_WRITES)
    assert not fakes.JF_WRITES
    jobs = db.execute(select(WriteJob)).scalars().all()
    assert jobs and all(j.status == "sent" for j in jobs)
    log = db.execute(select(AuditLog)).scalars().all()
    assert len(log) == len(jobs) and all(
        a.result == "applied" and a.before == "unplayed" for a in log
    )
    # The app's stored state followed, so the next cycle has nothing to do.
    assert writes.plan_person(db, user1) == []
    n = len(fakes.SILO_WRITES)
    _cycle(app)
    assert len(fakes.SILO_WRITES) == n


def test_writes_wait_until_local_has_taken_in_every_change(synced, app):
    db, *_ = synced
    user1 = _person(db, "user1")
    local.refresh_local(db)
    _switch_on(db, user1)
    silo = readable_users(db, user1)[_source(db, "silo").id]

    def reported(via):
        db.add(
            WatchEvent(
                source_user_id=silo.id, item_external_id="x", kind="unplayed", at=utcnow(), via=via
            )
        )
        db.commit()

    # An echo of our own write is not a change Local is missing.
    reported("write")
    assert not writes.local_behind(db, user1)
    # A change Local has not read yet: planned now, an unwatch would read as a missing play.
    reported("webhook")
    assert writes.local_behind(db, user1)
    _cycle(app)
    assert not fakes.SILO_WRITES and not fakes.JF_WRITES
    local.refresh_local(db)
    assert not writes.local_behind(db, user1)
    _cycle(app)
    assert fakes.SILO_WRITES


def test_the_title_filter_limits_what_is_written(synced, app):
    db, *_ = synced
    user1 = _person(db, "user1")
    _switch_on(db, user1, title_filter="no such show")
    _cycle(app)
    assert not fakes.SILO_WRITES
    user1.writes_filter = "sherlock"
    db.commit()
    _cycle(app)
    assert fakes.SILO_WRITES


def test_an_unwatch_is_written_only_if_seen_after_switching_on(synced, app):
    db, *_ = synced
    user1 = _person(db, "user1")
    st = (
        db.execute(
            select(LocalState).where(LocalState.app_user_id == user1.id, LocalState.kind == "mv")
        )
        .scalars()
        .first()
    )
    key = (st.kind, st.canonical_id, st.season, st.episode)
    # Local now says the film is unwatched, while Jellyfin and Silo still show it played.
    db.delete(st)
    now = utcnow()

    def unplayed(at):
        db.add(
            LocalEvent(
                app_user_id=user1.id,
                kind=key[0],
                canonical_id=key[1],
                season=0,
                episode=0,
                change="unplayed",
                reason="Jellyfin",
                at=at,
            )
        )
        db.commit()

    unplayed(now - timedelta(hours=2))
    _switch_on(db, user1, since=now - timedelta(hours=1))
    assert not [p for p in writes.plan_person(db, user1) if p.op == "unplayed"]  # before switch-on
    unplayed(now - timedelta(minutes=5))
    ops = [(p.source_name, p.op) for p in writes.plan_person(db, user1) if p.op == "unplayed"]
    assert ("Silo", "unplayed") in ops
    # A brand-new unwatch is sent at once.
    db.query(LocalEvent).filter(LocalEvent.at < now - timedelta(minutes=1)).delete()
    db.commit()
    unplayed(utcnow())
    assert ("Silo", "unplayed") in [
        (p.source_name, p.op) for p in writes.plan_person(db, user1) if p.op == "unplayed"
    ]


def test_pause_keeps_the_queue_and_resume_sends_it(synced, app):
    db, *_ = synced
    user1 = _person(db, "user1")
    syncs = app.state.syncs
    with syncs._local_lock, syncs.writes._lock:  # let the start-up refresh and write cycle end
        pass
    _switch_on(db, user1)
    writes.set_flag(db, writes.PAUSED_KEY, True)
    with app.state.db.sessionmaker() as s:
        writes.enqueue(s, s.get(type(user1), user1.id), writes.plan_person(s, s.get(type(user1), user1.id)))
    _cycle(app)
    assert not fakes.SILO_WRITES
    assert writes.status(db)["pending"] > 0
    writes.set_flag(db, writes.PAUSED_KEY, False)
    writes.drain(app.state.db, app.state.box)
    assert fakes.SILO_WRITES and writes.status(db)["pending"] == 0


def test_a_failed_write_is_recorded_and_not_retried_at_once(synced, app, monkeypatch):
    db, *_ = synced
    monkeypatch.setattr(writes.time, "sleep", lambda _s: None)
    user1 = _person(db, "user1")
    fakes.SILO_FAIL.update({p.item_id for p in writes.plan_person(db, user1)})
    _switch_on(db, user1)
    _cycle(app)
    db.expire_all()
    jobs = db.execute(select(WriteJob)).scalars().all()
    assert jobs and all(j.status == "failed" and j.attempts == writes.RETRIES for j in jobs)
    assert all(a.result == "error" for a in db.execute(select(AuditLog)).scalars())
    assert writes.plan_person(db, user1) == []  # backed off, not queued again straight away


def test_the_filter_takes_one_show_per_line(synced, app):
    db, *_ = synced
    user1 = _person(db, "user1")
    _switch_on(db, user1, title_filter="no such show\n  \nSHERLOCK")
    labels = {p.label for p in writes.plan_person(db, user1)}
    assert labels and all("sherlock" in label.lower() for label in labels)
    user1.writes_filter = "no such show\nanother"
    db.commit()
    assert writes.plan_person(db, user1) == []


def test_writes_page_toggles_save_as_they_change(synced, logged_in, app):
    db, *_ = synced
    user1, al = _person(db, "user1"), _person(db, "user2")
    hx = {"headers": {"HX-Request": "true"}}
    page = logged_in.get(f"/settings/writes?preview={user1.id}").text
    assert "Preview for user1" in page and "Mark played" in page and "Silo" in page
    assert "20 Oct 2026" in page  # the date Local has for the watch
    assert (
        "Known from" in page and "Writes to" in page and "Jellyfin" in page
    )  # which app Local learned it from
    on = {"on": "on"}
    resp = post(logged_in, "/settings/writes/switch/enabled", on, **hx)
    assert 'id="writes-controls"' in resp.text and writes.flag(db, writes.ENABLED_KEY)
    silo = _source(db, "silo")
    post(logged_in, f"/settings/writes/user/{user1.id}/app/{silo.id}", on, **hx)
    resp = post(
        logged_in,
        f"/settings/writes/user/{user1.id}/filter",
        {"title_filter": "sherlock\nheat"},
        **hx,
    )
    assert resp.status_code == 204
    app.state.syncs.writes._lock.acquire()  # wait for the cycle the change started
    app.state.syncs.writes._lock.release()
    db.expire_all()
    assert set(writes.history_switches(db, user1)) == {silo.id}
    assert writes.history_switches(db, user1)[silo.id].since is not None
    assert user1.writes_filter == "sherlock\nheat"
    # "All users" turns everyone who can be written to on, and off again.
    post(logged_in, "/settings/writes/all", on, **hx)
    db.expire_all()
    assert len(writes.history_switches(db, user1)) == len(writes.writable(db, user1))
    assert len(writes.history_switches(db, al)) == len(writes.writable(db, al))
    post(logged_in, "/settings/writes/all", {}, **hx)
    db.expire_all()
    assert not writes.history_switches(db, user1) and not writes.history_switches(db, al)
    post(logged_in, "/settings/writes/switch/paused", on, **hx)
    assert writes.flag(db, writes.PAUSED_KEY)


def test_switching_a_user_off_drops_their_queue(synced, logged_in, app):
    db, *_ = synced
    user1 = _person(db, "user1")
    silo = _source(db, "silo")
    writes.set_history(db, user1, silo.id, True)  # writing stays off, so no cycle runs
    db.commit()
    writes.set_flag(db, writes.PAUSED_KEY, True)
    writes.enqueue(db, user1, writes.plan_person(db, user1))
    queued = writes.status(db)["pending"]
    assert queued > 0
    assert writes.status(db)["pending"] == queued  # paused: kept
    post(
        logged_in,
        f"/settings/writes/user/{user1.id}/app/{silo.id}",
        {},
        headers={"HX-Request": "true"},
    )
    db.expire_all()
    assert writes.status(db)["pending"] == 0 and not writes.history_switches(db, user1)


def test_preview_shows_the_first_100_and_can_show_all(synced, logged_in, monkeypatch):
    db, *_ = synced
    user1 = _person(db, "user1")
    monkeypatch.setattr("mediawrangler.web.routes_settings.PREVIEW_ROWS", 0)
    page = logged_in.get(f"/settings/writes?preview={user1.id}").text
    assert "Showing the first 0 of" in page and "Show all" in page
    page = logged_in.get(f"/settings/writes?preview={user1.id}&all=1").text
    assert "Showing all" in page and "Show the first" in page


def test_libraries_a_user_cannot_open_are_not_written_to(synced, app):
    db, *_ = synced
    user1 = _person(db, "user1")
    st = (
        db.execute(
            select(LocalState).where(LocalState.app_user_id == user1.id, LocalState.kind == "mv")
        )
        .scalars()
        .first()
    )
    db.add(
        LocalEvent(
            app_user_id=user1.id,
            kind="mv",
            canonical_id=st.canonical_id,
            season=0,
            episode=0,
            change="unplayed",
            reason="Silo",
            at=utcnow() - timedelta(minutes=5),
        )
    )
    db.delete(st)
    _switch_on(db, user1, since=utcnow() - timedelta(hours=1))

    def apps():
        return {p.source_name for p in writes.plan_person(db, user1) if p.op == "unplayed"}

    assert "Jellyfin" in apps()
    jf = db.execute(select(Source).where(Source.type == "jellyfin")).scalar_one()
    row = db.execute(
        select(SourceUser).where(SourceUser.source_id == jf.id, SourceUser.app_user_id == user1.id)
    ).scalar_one()
    row.library_ids = ["jf-lib-tv"]  # not the Movies library
    db.commit()
    assert "Jellyfin" not in apps()


def _remove_silo_items(db):
    silo = _source(db, "silo")
    for item in db.execute(select(SourceItem).where(SourceItem.source_id == silo.id)).scalars():
        item.removed_at = utcnow()
    db.commit()


def test_a_queued_write_for_an_item_that_has_gone_is_skipped_not_sent(synced, app):
    db, *_ = synced
    user1 = _person(db, "user1")
    plan = writes.plan_person(db, user1)
    _switch_on(db, user1)
    writes.enqueue(db, user1, plan)
    _remove_silo_items(db)  # e.g. the app replaced the file and gave the item a new id
    writes.drain(app.state.db, app.state.box)
    db.expire_all()
    assert not fakes.SILO_WRITES
    jobs = db.execute(select(WriteJob)).scalars().all()
    assert jobs and all(
        j.status == "skipped" and "no longer lists this item" in j.error for j in jobs
    )
    assert {a.result for a in db.execute(select(AuditLog)).scalars()} == {"skipped"}


def test_a_failed_write_whose_item_has_gone_stops_being_a_failure(synced, app, monkeypatch):
    db, *_ = synced
    monkeypatch.setattr(writes.time, "sleep", lambda _s: None)
    user1 = _person(db, "user1")
    fakes.SILO_FAIL.update({p.item_id for p in writes.plan_person(db, user1)})
    _switch_on(db, user1)
    _cycle(app)
    db.expire_all()
    assert {j.status for j in db.execute(select(WriteJob)).scalars()} == {"failed"}
    _remove_silo_items(db)
    _cycle(app)
    db.expire_all()
    assert {j.status for j in db.execute(select(WriteJob)).scalars()} == {"skipped"}


def test_the_queue_panel_shows_live_progress_and_refreshes_itself(synced, logged_in, app):
    db, *_ = synced
    idle = logged_in.get("/settings/writes/status").text
    assert "Off" in idle and 'hx-trigger="every 2s"' in idle
    user1 = _person(db, "user1")
    plan = writes.plan_person(db, user1)
    _switch_on(db, user1)
    writes.enqueue(db, user1, plan)
    live = logged_in.get("/settings/writes/status").text
    assert f"0 / {len(plan):,}" in live and "Running" in live and "every 1s" in live
    assert '<progress class="queue-bar"' in live
    writes.drain(app.state.db, app.state.box)
    done = logged_in.get("/settings/writes/status").text
    assert "Done" in done and "every 2s" in done
    page = logged_in.get("/settings/writes").text
    assert 'id="writes-queue"' in page and "hx-swap-oob" not in page


def test_the_run_total_counts_jobs_that_have_waited_a_while(synced, app):
    db, *_ = synced
    user1 = _person(db, "user1")
    plan = writes.plan_person(db, user1)
    _switch_on(db, user1)
    writes.enqueue(db, user1, plan)
    for job in db.execute(select(WriteJob)).scalars():  # queued long ago, e.g. before a pause
        job.created_at = utcnow() - timedelta(hours=2)
    db.commit()
    run = writes.status(db)["run"]
    assert run["total"] == len(plan) and run["done"] == 0 and run["left"] == len(plan)


def test_preview_says_so_when_writes_are_already_queued(synced, logged_in, app):
    db, *_ = synced
    user1 = _person(db, "user1")
    plan = writes.plan_person(db, user1)
    _switch_on(db, user1)
    writes.enqueue(db, user1, plan)  # queued, not yet sent
    page = logged_in.get(f"/settings/writes?preview={user1.id}").text
    assert f"{len(plan):,}</strong> write" in page and "already queued" in page
    assert "every app agrees with Local" not in page


def test_a_restart_carries_on_with_the_queue(synced, app):
    from fastapi.testclient import TestClient

    db, *_ = synced
    user1 = _person(db, "user1")
    plan = writes.plan_person(db, user1)
    _switch_on(db, user1)
    writes.enqueue(db, user1, plan)  # queued, then the app goes down before sending
    assert writes.status(db)["pending"] > 0 and not fakes.SILO_WRITES
    with TestClient(app):  # starting up again
        for _ in range(100):
            db.expire_all()
            if writes.status(db)["pending"] == 0:
                break
            time.sleep(0.1)
    assert writes.status(db)["pending"] == 0 and fakes.SILO_WRITES


def test_a_name_and_year_match_is_not_written_to(synced):
    db, *_ = synced
    user1 = _person(db, "user1")
    silo = _source(db, "silo")
    assert any("Sherlock" in p.label for p in writes.plan_person(db, user1))
    for item in db.execute(
        select(SourceItem).where(
            SourceItem.source_id == silo.id,
            SourceItem.kind == "series",
            SourceItem.title.like("Sherlock%"),
        )
    ).scalars():
        item.match_confidence = "low"  # paired with Jellyfin's only by title and year
    db.commit()
    assert not any("Sherlock" in p.label for p in writes.plan_person(db, user1))


def test_an_item_jellyfin_has_replaced_is_a_replaced_file_not_an_error(synced, app):
    db, *_ = synced
    user1 = _person(db, "user1")
    jf = _source(db, "jellyfin")
    key = local.item_keys(db, jf)["jf-sh-3"]
    db.merge(
        LocalState(
            app_user_id=user1.id,
            kind=key[0],
            canonical_id=key[1],
            season=key[2],
            episode=key[3],
            played=True,
            last_played_at=datetime(2015, 1, 13),
        )
    )
    db.commit()
    _switch_on(db, user1)
    mine = [p for p in writes.plan_person(db, user1) if p.source_name == jf.name]
    assert [p.item_id for p in mine] == ["jf-sh-3"]
    assert mine[0].origin is not None
    writes.enqueue(db, user1, mine)
    fakes.JF_GONE.add("jf-sh-3")  # Radarr/Sonarr upgraded the file; Jellyfin gave it a new id
    asked = []
    writes.run_cycle(app.state.db, app.state.box, asked.append)
    db.expire_all()
    (job,) = db.execute(select(WriteJob).where(WriteJob.item_id == "jf-sh-3")).scalars().all()
    assert job.status == "skipped" and job.attempts == 1  # not retried
    (log,) = db.execute(select(AuditLog).where(AuditLog.item_id == "jf-sh-3")).scalars().all()
    assert log.result == "replaced" and "replaced" in log.detail
    assert log.why.startswith("Local has it watched on 13 Jan 2015")
    assert asked == [jf.id]  # the library is read again to learn the new id


def test_the_audit_log_tab_holds_what_was_received_and_what_was_sent(synced, logged_in, app):
    db, *_ = synced
    user1 = _person(db, "user1")
    _switch_on(db, user1)
    _cycle(app)
    page = logged_in.get("/activity?tab=audit").text
    rows = table_rows(page)
    assert any(" out " in f" {r[0]} " for r in rows) and "Marked watched" in page
    assert any(" in " in f" {r[0]} " for r in rows)  # and what the apps reported
    assert "Last 24 hours" in page and "Watched" in page  # the summary and the why column
    options = [v for v, *_ in filter_options(page)]
    assert {"all", "in", "out", "ignored", "problems", "replaced"} <= set(options)
    # The change MediaWrangler noted as it wrote is marked as ignored, with the reason.
    assert "Ignored" in page and "reported it back after MediaWrangler" in page
    assert 'class="trunc why"' in page and "data-tip-truncated" in page  # one line, with a tip
    assert logged_in.get("/activity?tab=changes").text.count("Audit log") >= 1  # the old name
    assert "/activity?tab=audit" in logged_in.get("/settings/writes").text


def test_skipped_and_failed_writes_are_described_in_words():
    from mediawrangler.activity import sent_why

    def entry(result, detail, why="a reason"):
        return AuditLog(source_name="Jellyfin", result=result, detail=detail, why=why)

    assert sent_why(entry("applied", None)) == "a reason"
    gone = sent_why(entry("skipped", "The app no longer lists this item (replaced or removed)"))
    assert "Skipped before sending" in gone and "replaced or removed" in gone
    assert "cannot open" in sent_why(entry("skipped", "The user cannot open this item's library"))
    assert "changed its mind" in sent_why(entry("skipped", "Local no longer says this"))
    assert "no longer in Jellyfin" in sent_why(entry("skipped", "The user is no longer in the app"))
    assert "file was most likely replaced" in sent_why(entry("replaced", "x"))
    old = sent_why(entry("error", "POST /UserPlayedItems/ab: unexpected HTTP 404"))
    assert "file was most likely replaced" in old  # rows from before 0.10.3
    failed = sent_why(entry("error", "POST /x: unexpected HTTP 500"))
    assert "Could not write to Jellyfin after 3 tries" in failed and "HTTP 500" in failed


def test_every_audit_tag_has_a_colour_of_its_own():
    import re

    template = open("src/mediawrangler/templates/activity.html").read()
    audit = template[template.index("{% else %}\n    <thead><tr><th></th>") :]
    tags = re.findall(r'<span class="status (c-[a-z]+)">([^<{]+)</span>', audit)
    colours = [c for c, _ in tags if _ != "{{ a.op }}"]
    assert len(colours) == len(set(colours)) >= 12  # no two tags share a colour
