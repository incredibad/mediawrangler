"""Webhooks from Silo: signing, the listener, how events change what is stored, the Live tab."""

import asyncio
import json
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mediawrangler.connectors.silo import SiloConnector
from mediawrangler.db import utcnow
from mediawrangler.hooks import signing
from mediawrangler.hooks.app import create_hooks_app
from mediawrangler.models import (
    AppSetting,
    Library,
    Source,
    SourceItem,
    SourceUser,
    SyncRun,
    WatchEvent,
    WatchState,
    WebhookEvent,
)

from .conftest import post
from .test_watch import _source_user, synced  # noqa: F401  (fixture)

SECRET = "hook-secret-0123456789"
ALEX, ALI = "p-1", "p-2"


def _silo(db):
    return db.execute(select(Source).where(Source.type == "silo")).scalar_one()


@pytest.fixture
def hooks(app, synced):  # noqa: F811
    """The listener, with a secret set on the Silo source."""
    db, *_ = synced
    silo = _silo(db)
    silo.webhook_secret_enc = app.state.box.encrypt(SECRET)
    db.commit()
    with TestClient(
        create_hooks_app(app, play_delay=0.05, sync_delay=0.4, watch_delay=0.05, inline=True)
    ) as client:
        client.silo_id = silo.id
        yield client


def send(client, body, *, secret=SECRET, at=None, path=None):
    raw = json.dumps(body).encode()
    header = signing.sign(secret, int(at or time.time()), raw)
    return client.post(
        path or f"/hooks/silo/{client.silo_id}",
        content=raw,
        headers={"X-Silo-Signature": header, "Content-Type": "application/json"},
    )


def state_event(change, content_id, profile=ALEX, event_id="e1", **data):
    return {
        "event": "user_state.changed",
        "event_id": event_id,
        "profile": {"id": profile, "name": "alex" if profile == ALEX else "Jordan"},
        "data": {"change": change, "content_id": content_id, "profile_id": profile, **data},
    }


def silo_state(db, profile=ALEX):
    su = db.execute(
        select(SourceUser)
        .join(Source)
        .where(Source.type == "silo", SourceUser.external_id == profile)
    ).scalar_one()
    rows = db.execute(select(WatchState).where(WatchState.source_user_id == su.id)).scalars()
    return {w.item_external_id: w for w in rows}


# --- signing --------------------------------------------------------------------------


def test_signature_round_trip_and_rejections():
    body = b'{"x":1}'
    now = 1_800_000_000
    good = signing.sign("s3cret", now, body)
    assert good.startswith(f"t={now},v1=") and signing.verify("s3cret", good, body, now + 10)
    assert not signing.verify("other", good, body, now)  # wrong secret
    assert not signing.verify("s3cret", good, body + b" ", now)  # changed body
    assert not signing.verify("s3cret", good, body, now + 301)  # too old
    assert not signing.verify("s3cret", good, body, now - 301)  # from the future
    for bad in (None, "", "garbage", "t=abc,v1=00", "t=1"):
        assert not signing.verify("s3cret", bad, body, now)
    assert not signing.verify("", good, body, now)  # no secret set


def test_matches_the_plugins_scheme():
    # Known answer shared with plugins/silo-webhooks: HMAC-SHA256("secret", "1700000000.{}").
    assert signing.sign("secret", 1700000000, b"{}") == (
        "t=1700000000,v1="
        + __import__("hmac").new(b"secret", b"1700000000.{}", "sha256").hexdigest()
    )


# --- the listener ---------------------------------------------------------------------


def test_listener_refuses_what_it_should(app, hooks, synced):  # noqa: F811
    db, *_ = synced
    body = state_event("watched", "episode-tvdb-176941-1-1", played=False)
    assert hooks.get("/hooks/health").json() == {"status": "ok", "queued": 0}
    assert send(hooks, body, secret="wrong").status_code == 401
    assert send(hooks, body, at=time.time() - 3600).status_code == 401
    assert send(hooks, body, path="/hooks/silo/9999").status_code == 404
    assert hooks.post(f"/hooks/silo/{hooks.silo_id}", content=b"{}").status_code == 401  # unsigned
    raw = b"[1, 2]"
    signed = {"X-Silo-Signature": signing.sign(SECRET, int(time.time()), raw)}
    assert (
        hooks.post(f"/hooks/silo/{hooks.silo_id}", content=raw, headers=signed).status_code == 400
    )
    big = b"x" * (300 * 1024)
    signed = {"X-Silo-Signature": signing.sign(SECRET, int(time.time()), big)}
    assert (
        hooks.post(f"/hooks/silo/{hooks.silo_id}", content=big, headers=signed).status_code == 413
    )
    assert db.execute(select(WebhookEvent)).first() is None  # nothing refused was kept
    # An app with no secret set accepts nothing.
    silo = _silo(db)
    silo.webhook_secret_enc = None
    db.commit()
    assert send(hooks, body).status_code == 403


def test_unwatching_a_season_clears_its_episodes_and_logs_it(app, hooks, synced):  # noqa: F811
    db, *_ = synced
    before = silo_state(db)
    assert before["episode-tvdb-176941-1-1"].played
    version = app.state.syncs.version
    resp = send(hooks, state_event("watched", "season-tvdb-176941-1", played=False))
    assert resp.status_code == 204
    db.expire_all()
    assert "episode-tvdb-176941-1-1" not in silo_state(db)
    assert app.state.syncs.version > version  # cached comparison pages are rebuilt
    (event,) = db.execute(select(WebhookEvent)).scalars()
    assert event.status == "applied" and event.event == "user_state.changed"
    assert "alex unwatched" in event.summary and event.profile_name == "alex"
    kinds = {(e.item_external_id, e.kind) for e in db.execute(select(WatchEvent)).scalars()}
    assert ("episode-tvdb-176941-1-1", "unplayed") in kinds

    # ...and marking the season watched again brings them back.
    send(hooks, state_event("watched", "season-tvdb-176941-1", event_id="e2", played=True))
    db.expire_all()
    assert silo_state(db)["episode-tvdb-176941-1-1"].played


def test_a_single_episode_and_a_series_expand_correctly(hooks, synced):  # noqa: F811
    db, *_ = synced
    send(hooks, state_event("watched", "episode-tvdb-176941-1-1", played=False))
    db.expire_all()
    assert "episode-tvdb-176941-1-1" not in silo_state(db)
    send(hooks, state_event("watched", "series-tvdb-176941", event_id="e2", played=True))
    db.expire_all()
    keys = [k for k in silo_state(db) if k.startswith("episode-tvdb-176941-")]
    assert len(keys) >= 2 and all(silo_state(db)[k].played for k in keys)
    # A series that shares a prefix is not caught by accident.
    from mediawrangler.hooks.silo import expand_content_id

    assert expand_content_id(db, _silo(db), "series-tvdb-17694") == []


def test_other_profiles_and_other_changes_are_logged_not_applied(hooks, synced):  # noqa: F811
    db, *_ = synced
    before = {k: v.played for k, v in silo_state(db).items()}
    send(
        hooks,
        state_event("watched", "episode-tvdb-176941-1-1", profile=ALI, event_id="a", played=True),
    )
    send(hooks, state_event("favorite", "series-tvdb-176941", event_id="b", is_favorite=True))
    send(hooks, state_event("watchlist", "series-tvdb-176941", event_id="c", in_watchlist=False))
    db.expire_all()
    assert {k: v.played for k, v in silo_state(db).items()} == before
    rows = {e.event_id: e for e in db.execute(select(WebhookEvent)).scalars()}
    assert rows["a"].status == "logged" and "isn't read" in rows["a"].note
    assert "favourite" in rows["b"].summary and "removed" in rows["c"].summary
    assert {r.status for r in rows.values()} == {"logged"}


def test_a_repeat_is_ignored_and_noise_is_not_kept(hooks, synced):  # noqa: F811
    db, *_ = synced
    body = state_event("favorite", "series-tvdb-176941", is_favorite=True)
    assert send(hooks, body).status_code == 204 and send(hooks, body).status_code == 204
    for noisy in ("scan.progress", "sessions.replaced", "job.progress", "operational_log_appended"):
        send(hooks, {"event": noisy, "event_id": noisy, "data": {}})
    assert len(db.execute(select(WebhookEvent)).scalars().all()) == 1


def test_a_plugin_warning_is_kept_as_an_error(hooks, synced):  # noqa: F811
    db, *_ = synced
    send(
        hooks,
        {"event": "plugin.warning", "event_id": "w", "data": {"message": "lookup failed: 403"}},
    )
    (row,) = db.execute(select(WebhookEvent)).scalars()
    assert row.status == "error" and "403" in row.summary


# --- plays and scans ------------------------------------------------------------------


def play(
    session, item, completed, watched=3500, duration=3600, profile=ALEX, ended="2026-10-27T01:00:00Z"
):
    return {
        "session_id": session,
        "media_item_id": item,
        "profile_id": profile,
        "profile_name": "alex" if profile == ALEX else "Jordan",
        "completed": completed,
        "watched_seconds": watched,
        "duration_seconds": duration,
        "ended_at": ended,
    }


def wait_for(check, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if check():
            return True
        time.sleep(0.05)
    return False


def test_finished_plays_become_watched_or_resume_points(app, hooks, synced, monkeypatch):  # noqa: F811
    db, *_ = synced
    plays = [play("s0", "episode-tvdb-176941-1-1", True, ended="2026-10-27T00:00:00Z")]
    monkeypatch.setattr(SiloConnector, "recent_plays", lambda self, limit=50: list(plays))
    nudge = {"event": "playback_sessions_changed", "event_id": "n", "data": {"raw": "abc"}}

    send(hooks, nudge)  # the first read only sets the baseline
    assert wait_for(lambda: db.get(AppSetting, f"silo_plays:{_silo(db).id}") is not None)
    db.expire_all()
    assert db.execute(select(WebhookEvent)).first() is None

    # A finished episode Silo says is completed, and one stopped halfway.
    send(hooks, state_event("watched", "episode-tvdb-176941-1-2", event_id="x", played=False))
    plays[:0] = [
        play("s1", "episode-tvdb-176941-1-2", True, 3550, ended="2026-10-27T01:00:00Z"),
        play("s2", "episode-tvdb-176941-1-3", False, 1800, ended="2026-10-27T01:05:00Z"),
        play("s3", "episode-tvdb-176941-1-1", True, ended="2026-10-27T01:06:00Z", profile=ALI),
    ]
    send(hooks, {**nudge, "event_id": "n2"})
    assert wait_for(lambda: len(db.execute(select(WebhookEvent)).scalars().all()) >= 4)
    db.expire_all()
    state = silo_state(db)
    assert state["episode-tvdb-176941-1-2"].played  # the finished one
    half = state["episode-tvdb-176941-1-3"]
    assert not half.played and half.position_seconds == 1800  # the unfinished one
    rows = {e.event_id: e for e in db.execute(select(WebhookEvent)).scalars()}
    assert rows["play:s1"].status == "applied" and "finished" in rows["play:s1"].summary
    assert "stopped" in rows["play:s2"].summary and "50%" in rows["play:s2"].summary
    assert rows["play:s3"].status == "logged"  # Jordan isn't read yet

    # The same plays arriving again change nothing.
    count = len(rows)
    send(hooks, {**nudge, "event_id": "n3"})
    time.sleep(0.4)
    db.expire_all()
    assert len(db.execute(select(WebhookEvent)).scalars().all()) == count


def test_a_scan_asks_for_a_sync_once_per_burst(app, hooks, synced, monkeypatch):  # noqa: F811
    db, *_ = synced
    started = []
    monkeypatch.setattr(
        app.state.syncs,
        "start",
        lambda sid, trigger="manual", mode="full": started.append((sid, trigger, mode)),
    )
    # The fixture synced a moment ago; make that sync old so a new one is due.
    for run in db.execute(select(SyncRun).where(SyncRun.source_id == hooks.silo_id)).scalars():
        run.finished_at = datetime(2026, 1, 1)
    db.commit()
    for i in range(5):
        send(hooks, {"event": "scan.completed", "event_id": f"sc{i}", "data": {"library_id": 2}})
    assert wait_for(lambda: started)
    time.sleep(0.3)
    assert started == [(hooks.silo_id, "webhook", "full")]  # five scans, one sync

    # Not again right after a sync has finished.
    db.add(
        SyncRun(
            source_id=hooks.silo_id,
            mode="full",
            trigger="webhook",
            status="ok",
            finished_at=utcnow(),
        )
    )
    db.commit()
    started.clear()
    send(hooks, {"event": "scan.completed", "event_id": "late", "data": {}})
    time.sleep(1.0)
    assert started == []


def test_a_one_folder_scan_reads_only_that_title(app, hooks, synced, monkeypatch):  # noqa: F811
    db, *_ = synced
    started, read = [], []
    monkeypatch.setattr(
        app.state.syncs,
        "start",
        lambda sid, trigger="manual", mode="full": started.append((sid, trigger, mode)),
    )
    answer = {"state": "done"}
    monkeypatch.setattr(
        app.state.syncs,
        "read_title",
        lambda sid, ext=None, locate=None, kind=None: read.append((ext, locate)) or answer,
    )
    for run in db.execute(select(SyncRun).where(SyncRun.source_id == hooks.silo_id)).scalars():
        run.finished_at = datetime(2026, 1, 1)
    film, show = (
        db.execute(
            select(SourceItem).where(SourceItem.source_id == hooks.silo_id, SourceItem.kind == k)
        )
        .scalars()
        .first()
        for k in ("movie", "series")
    )
    film.path = "/mnt/media/Movies/A Film [1999]/A Film [1999] [Bluray-1080p].mkv"
    show.path = "/mnt/media/TV/A Show (2011)"
    db.commit()

    def scan(n, path, library="1"):
        burst = [
            {
                "event": "scan.completed",
                "data": {"library_id": library, "mode": "subtree", "path": path},
            },
            {"event": "library.changed", "data": {"library_id": library, "reason": "scan"}},
            {"event": "library.item_added", "data": {"library_id": library}},
        ]
        for i, body in enumerate(burst):
            send(hooks, {**body, "event_id": f"{n}-{i}"})

    scan("a", "/mnt/media/Movies/A Film [1999]")
    scan("b", "/mnt/media/TV/A Show (2011)/Season 04", library="2")
    assert wait_for(lambda: len(read) == 2)
    time.sleep(0.3)
    assert sorted(ext for ext, _ in read) == sorted([film.external_id, show.external_id])
    assert started == []  # no read of the whole app

    # A folder MediaWrangler has not seen is looked up by name.
    read.clear()
    scan("c", "/mnt/media/TV/New Show (2025)/Season 01", library="2")
    assert wait_for(lambda: read)
    lib = db.execute(
        select(Library).where(Library.source_id == hooks.silo_id, Library.external_id == "2")
    ).scalar()
    assert read == [
        (
            None,
            {
                "name": "New Show",
                "kind": lib.kind,
                "library": "2",
                "path": "/mnt/media/TV/New Show (2025)",
            },
        )
    ]
    assert started == []

    # If that read fails, the whole app is read after all.
    read.clear()
    answer = {"state": "error", "error": "not picked up yet"}
    scan("d", "/mnt/media/TV/Newer Show (2026)", library="2")
    assert wait_for(lambda: started)
    assert started == [(hooks.silo_id, "webhook", "full")]

    # A library event with nothing narrower still reads the whole app.
    for run in db.execute(select(SyncRun).where(SyncRun.source_id == hooks.silo_id)).scalars():
        run.finished_at = datetime(2026, 1, 1)
    db.commit()
    started.clear()
    read.clear()
    send(hooks, {"event": "library.changed", "event_id": "bare", "data": {"library_id": "1"}})
    assert wait_for(lambda: started)
    assert read == []


# --- the Live tab ---------------------------------------------------------------------


def test_live_tab_lists_events_and_streams_new_ones(app, hooks, synced, logged_in):  # noqa: F811
    send(hooks, state_event("favorite", "series-tvdb-176941", is_favorite=True))
    page = logged_in.get("/activity?tab=live")
    assert page.status_code == 200 and "Live events" in page.text
    assert "alex added" in page.text and "user_state.changed" in page.text
    assert 'data-live-stream="/activity/stream"' in page.text and "live-feed.js" in page.text

    async def run():
        queue = app.state.live.subscribe()
        app.state.live.publish({"id": 1, "event": "x"})
        assert (await asyncio.wait_for(queue.get(), 1)) == {"id": 1, "event": "x"}
        app.state.live.unsubscribe(queue)

    asyncio.run(run())
    with logged_in.stream("GET", "/activity/stream") as stream:
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert next(stream.iter_lines()) == ": connected"


# --- the connector's webhook card -------------------------------------------------------


def test_connector_page_makes_a_secret_shown_once(app, synced, logged_in):  # noqa: F811
    db, *_ = synced
    silo = _silo(db)
    app.state.settings.webhook_port = 8791  # the test settings switch the listener off
    page = logged_in.get(f"/settings/connectors/{silo.id}")
    assert f"/hooks/silo/{silo.id}" in page.text and "No secret yet" in page.text
    made = post(logged_in, f"/settings/connectors/{silo.id}/webhook-secret")
    assert made.status_code == 200 and "shown once" in made.text
    import re

    secret = re.search(r"<code>([0-9a-f]{48})</code>", made.text).group(1)
    db.expire_all()
    assert secret not in silo.webhook_secret_enc  # encrypted at rest
    assert app.state.box.decrypt(silo.webhook_secret_enc) == secret
    again = logged_in.get(f"/settings/connectors/{silo.id}")
    assert secret not in again.text and "Secret set" in again.text
    # Jellyfin has the card too, with its own instructions; the media share sends nothing.
    jf = db.execute(select(Source).where(Source.type == "jellyfin")).scalar_one()
    card = logged_in.get(f"/settings/connectors/{jf.id}").text
    assert f"/hooks/jellyfin/{jf.id}" in card and "X-MW-Token" in card
    disk = Source(type="disk", name="Share", base_url="/media")
    db.add(disk)
    db.commit()
    assert "Webhooks from" not in logged_in.get(f"/settings/connectors/{disk.id}").text
    assert post(logged_in, f"/settings/connectors/{disk.id}/webhook-secret").status_code == 400
    assert _source_user(db, "silo", "alex")


# --- Jellyfin and the arrs: a header token instead of a signature ---------------------------


def jf_event(kind, item="jf-sh-3", user="u-alex", **fields):
    return {
        "NotificationType": kind,
        "ItemId": item,
        "ItemType": "Episode",
        "UserId": user,
        "Username": "alex",
        "Name": "Ep",
        "SeriesName": "Sherlock",
        "SeasonNumber": 1,
        "EpisodeNumber": 3,
        **fields,
    }


def send_token(client, path, body, token="tok-0123456789"):  # noqa: S107
    headers = {"X-MW-Token": token} if token else {}
    return client.post(path, json=body, headers=headers)


@pytest.fixture
def jf_hooks(app, synced):  # noqa: F811
    db, *_ = synced
    jf = db.execute(select(Source).where(Source.type == "jellyfin")).scalar_one()
    jf.webhook_secret_enc = app.state.box.encrypt("tok-0123456789")
    db.commit()
    with TestClient(
        create_hooks_app(app, play_delay=0.05, sync_delay=0.4, watch_delay=0.4, inline=True)
    ) as client:
        client.path = f"/hooks/jellyfin/{jf.id}"
        client.jf_id = jf.id
        yield client


def jf_state(db):
    su = _source_user(db, "jellyfin", "alex")
    return {
        w.item_external_id: w
        for w in db.execute(select(WatchState).where(WatchState.source_user_id == su.id)).scalars()
    }


def test_jellyfin_token_is_required_and_the_path_must_match(app, jf_hooks, synced):  # noqa: F811
    db, *_ = synced
    body = jf_event("UserDataSaved", Played=True)
    assert send_token(jf_hooks, jf_hooks.path, body, token=None).status_code == 401
    assert send_token(jf_hooks, jf_hooks.path, body, token="wrong").status_code == 401
    # Silo's path refuses a Jellyfin source, and the Jellyfin path refuses Silo.
    assert send_token(jf_hooks, f"/hooks/silo/{jf_hooks.jf_id}", body).status_code == 404
    assert send_token(jf_hooks, f"/hooks/jellyfin/{_silo(db).id}", body).status_code == 404
    assert db.execute(select(WebhookEvent)).first() is None
    assert send_token(jf_hooks, jf_hooks.path, body).status_code == 204


def test_jellyfin_played_unplayed_and_progress_are_applied(jf_hooks, synced):  # noqa: F811
    db, *_ = synced
    assert "jf-sh-3" not in jf_state(db)
    send_token(jf_hooks, jf_hooks.path, jf_event("UserDataSaved", Played=True))
    db.expire_all()
    assert jf_state(db)["jf-sh-3"].played

    # Dashes and capitals in Jellyfin's ids don't matter; unwatched removes the row.
    send_token(
        jf_hooks,
        jf_hooks.path,
        jf_event("UserDataSaved", item="JF-SH-3", user="U-ALEX", Played="False"),
    )
    db.expire_all()
    assert "jf-sh-3" not in jf_state(db)

    # Part-watched: ten minutes in.
    ticks = 600 * 10_000_000
    send_token(
        jf_hooks,
        jf_hooks.path,
        jf_event(
            "UserDataSaved",
            Played=False,
            PlaybackPositionTicks=ticks,
            RunTimeTicks=3600 * 10_000_000,
        ),
    )
    db.expire_all()
    row = jf_state(db)["jf-sh-3"]
    assert not row.played and row.position_seconds == 600 and row.duration_seconds == 3600
    kinds = [e.kind for e in db.execute(select(WatchEvent).order_by(WatchEvent.id)).scalars()]
    assert kinds == ["played", "unplayed", "progress"]
    rows = list(db.execute(select(WebhookEvent).order_by(WebhookEvent.id)).scalars())
    assert [r.status for r in rows] == ["applied"] * 3
    assert (
        "alex watched Sherlock S01E03" in rows[0].summary and "stopped at 10 min" in rows[2].summary
    )


def test_jellyfin_other_item_types_users_and_noise(app, jf_hooks, synced, monkeypatch):  # noqa: F811
    db, *_ = synced
    before = {k: v.played for k, v in jf_state(db).items()}
    send_token(jf_hooks, jf_hooks.path, jf_event("UserDataSaved", ItemType="Series", Played=True))
    send_token(jf_hooks, jf_hooks.path, jf_event("UserDataSaved", user="nobody", Played=True))
    send_token(
        jf_hooks, jf_hooks.path, jf_event("UserDataSaved", item="not-in-catalogue", Played=True)
    )
    send_token(jf_hooks, jf_hooks.path, jf_event("PlaybackProgress"))
    send_token(jf_hooks, jf_hooks.path, jf_event("PlaybackStart"))
    send_token(
        jf_hooks,
        jf_hooks.path,
        jf_event("PlaybackStop", PlaybackPositionTicks=900 * 10_000_000, PlayedToCompletion="True"),
    )
    db.expire_all()
    assert {k: v.played for k, v in jf_state(db).items()} == before
    notes = [
        r.note or r.summary
        for r in db.execute(select(WebhookEvent).order_by(WebhookEvent.id)).scalars()
    ]
    assert any("aren't applied" in n for n in notes) and any("not known" in n for n in notes)
    assert any("not in the catalogue" in n for n in notes)
    assert any("started" in n for n in notes) and any(
        "stopped at 15 min to the end" in n for n in notes
    )
    assert not any("PlaybackProgress" in n for n in notes)  # progress pings are dropped


def test_jellyfin_new_items_ask_for_a_sync(app, jf_hooks, synced, monkeypatch):  # noqa: F811
    db, *_ = synced
    started = []
    monkeypatch.setattr(
        app.state.syncs,
        "start",
        lambda sid, trigger="manual", mode="full": started.append((sid, trigger)),
    )
    for run in db.execute(select(SyncRun).where(SyncRun.source_id == jf_hooks.jf_id)).scalars():
        run.finished_at = datetime(2026, 1, 1)
    db.commit()
    for _ in range(3):
        send_token(jf_hooks, jf_hooks.path, jf_event("ItemAdded", ItemType="Episode"))
    assert wait_for(lambda: started)
    time.sleep(0.3)
    assert started == [(jf_hooks.jf_id, "webhook")]


def test_arr_notifications_are_logged_and_ask_for_a_sync(app, synced, monkeypatch):  # noqa: F811
    db, *_ = synced
    sonarr = Source(
        type="sonarr",
        name="Sonarr",
        base_url="http://sonarr",
        secret_enc=app.state.box.encrypt("k"),
    )
    sonarr.webhook_secret_enc = app.state.box.encrypt("arr-token")
    db.add(sonarr)
    db.commit()
    started = []
    monkeypatch.setattr(
        app.state.syncs,
        "start",
        lambda sid, trigger="manual", mode="full": started.append((sid, trigger)),
    )
    path = f"/hooks/arr/{sonarr.id}"
    download = {
        "eventType": "Download",
        "series": {"title": "Sherlock"},
        "episodes": [
            {"seasonNumber": 1, "episodeNumber": 3},
            {"seasonNumber": 1, "episodeNumber": 4},
        ],
    }
    with TestClient(
        create_hooks_app(app, play_delay=0.05, sync_delay=0.4, watch_delay=0.05, inline=True)
    ) as client:
        assert send_token(client, path, download, token="nope").status_code == 401
        assert send_token(client, path, download, token="arr-token").status_code == 204
        send_token(client, path, {"eventType": "Test"}, token="arr-token")
        send_token(client, path, {"eventType": "Health"}, token="arr-token")  # noise: not kept
        assert wait_for(lambda: started)
    assert started == [(sonarr.id, "webhook")]
    rows = list(
        db.execute(
            select(WebhookEvent)
            .where(WebhookEvent.source_id == sonarr.id)
            .order_by(WebhookEvent.id)
        ).scalars()
    )
    assert [r.event for r in rows] == ["Download", "Test"]
    assert rows[0].summary == "Download: Sherlock S01E03 (+1 more)" and "sync" in rows[0].note
    assert "connection works" in rows[1].summary


def test_arr_and_jellyfin_events_read_only_their_title(app, jf_hooks, synced, monkeypatch):  # noqa: F811
    db, *_ = synced
    radarr = Source(
        type="radarr",
        name="Radarr",
        base_url="http://radarr",
        secret_enc=app.state.box.encrypt("k"),
    )
    radarr.webhook_secret_enc = app.state.box.encrypt("arr-token")
    db.add(radarr)
    db.commit()
    started, read = [], []
    monkeypatch.setattr(
        app.state.syncs,
        "start",
        lambda sid, trigger="manual", mode="full": started.append(sid),
    )
    monkeypatch.setattr(
        app.state.syncs,
        "read_title",
        lambda sid, ext=None, locate=None, kind=None: (
            read.append((sid, ext, kind)) or {"state": "done"}
        ),
    )
    with TestClient(
        create_hooks_app(app, play_delay=0.05, sync_delay=0.2, watch_delay=0.05, inline=True)
    ) as client:
        film = {"eventType": "Download", "movie": {"id": 12, "title": "The Core"}}
        for _ in range(2):
            send_token(client, f"/hooks/arr/{radarr.id}", film, token="arr-token")
        assert wait_for(lambda: read)
    send_token(
        jf_hooks,
        jf_hooks.path,
        jf_event("ItemDeleted", ItemType="Episode", SeriesId="AB-CD"),
    )
    send_token(jf_hooks, jf_hooks.path, jf_event("ItemAdded", ItemType="Folder"))  # ignored
    assert wait_for(lambda: len(read) == 2)
    time.sleep(0.3)
    assert read == [(radarr.id, "movie-12", "movie"), (jf_hooks.jf_id, "abcd", None)]
    assert started == []


# --- the queue: bursts, order, overflow, healing ----------------------------------------------


def queued_client(app, jf_id, **kwargs):
    return TestClient(
        create_hooks_app(app, play_delay=0.05, sync_delay=0.4, watch_delay=0.05, **kwargs)
    )


def test_a_burst_is_answered_at_once_and_applied_in_order(app, synced):  # noqa: F811
    db, *_ = synced
    jf = db.execute(select(Source).where(Source.type == "jellyfin")).scalar_one()
    jf.webhook_secret_enc = app.state.box.encrypt("tok-0123456789")
    db.commit()
    path = f"/hooks/jellyfin/{jf.id}"
    with queued_client(app, jf.id) as client:
        # Watched, unwatched, watched... 41 times: only the order decides where it ends.
        codes = [
            send_token(client, path, jf_event("UserDataSaved", Played=(i % 2 == 0))).status_code
            for i in range(41)
        ]
        assert set(codes) == {204}
        assert wait_for(
            lambda: len(db.execute(select(WatchEvent)).scalars().all()) >= 41, timeout=10
        )
    db.expire_all()
    assert jf_state(db)["jf-sh-3"].played  # the last of 41 events (an even number) was "played"
    kinds = [e.kind for e in db.execute(select(WatchEvent).order_by(WatchEvent.id)).scalars()]
    assert kinds == ["played", "unplayed"] * 20 + ["played"]


def test_a_full_queue_says_try_again(app, synced, monkeypatch):  # noqa: F811
    db, *_ = synced
    jf = db.execute(select(Source).where(Source.type == "jellyfin")).scalar_one()
    jf.webhook_secret_enc = app.state.box.encrypt("tok-0123456789")
    db.commit()
    from mediawrangler.hooks import jellyfin as jf_hooks_module

    original = jf_hooks_module.handle_event
    monkeypatch.setattr(
        jf_hooks_module,
        "handle_event",
        lambda *a, **k: (time.sleep(0.4), original(*a, **k))[1],
    )
    with queued_client(app, jf.id, queue_size=2) as client:
        responses = [
            send_token(client, f"/hooks/jellyfin/{jf.id}", jf_event("PlaybackStart"))
            for _ in range(8)
        ]
    refused = [r for r in responses if r.status_code == 503]
    assert refused and refused[0].headers["retry-after"] == "5"
    assert {r.status_code for r in responses} <= {204, 503}


def test_changes_trigger_a_healing_read(app, jf_hooks, synced, monkeypatch):  # noqa: F811
    db, *_ = synced
    started = []
    monkeypatch.setattr(
        app.state.syncs,
        "start",
        lambda sid, trigger="manual", mode="full": started.append((sid, trigger, mode)),
    )
    # A read that finished long ago, then a burst of three changes: one quick read follows.
    for run in db.execute(select(SyncRun).where(SyncRun.source_id == jf_hooks.jf_id)).scalars():
        run.finished_at = datetime(2026, 1, 1)
    db.commit()
    for played in (True, False, True):
        send_token(jf_hooks, jf_hooks.path, jf_event("UserDataSaved", Played=played))
    assert wait_for(lambda: started)
    time.sleep(0.3)
    assert started == [(jf_hooks.jf_id, "webhook", "watch")]


def test_live_events_carry_the_app_name(hooks, synced, app):  # noqa: F811
    db, *_ = synced
    queue = app.state.live.subscribe()
    send(hooks, state_event("watched", "episode-tvdb-176941-1-1", played=False))
    item = queue.get_nowait()
    assert item["app"] == _silo(db).name  # the Live events table fills its App column from this


def test_a_long_viewing_leaves_a_trail_every_few_minutes_and_no_idle_rows(jf_hooks, synced):  # noqa: F811
    db, *_ = synced

    def tick(seconds):
        send_token(
            jf_hooks,
            jf_hooks.path,
            jf_event(
                "UserDataSaved",
                Played=False,
                PlaybackPositionTicks=seconds * 10_000_000,
                RunTimeTicks=3600 * 10_000_000,
            ),
        )

    # A client reports every few seconds; only a move of five minutes is a change.
    for seconds in (120, 123, 126, 129, 200, 300, 419, 415):
        tick(seconds)
    db.expire_all()
    kinds = [(e.kind, e.position_seconds) for e in db.execute(select(WatchEvent)).scalars()]
    assert kinds == [("progress", 120)]
    tick(500)  # eight minutes on: noted again
    db.expire_all()
    positions = [e.position_seconds for e in db.execute(select(WatchEvent)).scalars()]
    assert positions == [120, 500]
    # The ticks that changed nothing are not kept in the live feed.
    rows = list(db.execute(select(WebhookEvent).order_by(WebhookEvent.id)).scalars())
    assert [r.status for r in rows] == ["applied", "applied"]


def test_a_jellyfin_change_goes_straight_to_the_other_app(app, jf_hooks, synced, monkeypatch):  # noqa: F811
    from mediawrangler import local, writes

    from . import fakes
    from .test_watch import _person
    from .test_writes import _switch_on

    db, *_ = synced
    alex = _person(db, "alex")
    local.refresh_local(db)
    _switch_on(db, alex, since=utcnow())  # unwatches count only from switch-on
    fakes.SILO_WRITES.clear()
    syncs = app.state.syncs
    # Only the direct path may run: a full cycle would also send Local's other differences.
    monkeypatch.setattr(syncs, "bump", lambda background=False: pytest.fail("full refresh"))
    monkeypatch.setattr(
        syncs.writes,
        "kick",
        lambda plan=True: (
            plan and pytest.fail("full cycle") or writes.drain(syncs.db, app.state.box)
        ),
    )
    # Both apps have S01E01 watched; S01E02 differs already, and only a full cycle sends that.
    unwatch = jf_event("UserDataSaved", item="jf-sh-1", Played=False, EpisodeNumber=1)
    send_token(jf_hooks, jf_hooks.path, unwatch)
    assert [(m, cid) for m, cid, *_ in fakes.SILO_WRITES] == [("DELETE", "episode-tvdb-176941-1-1")]
    fakes.SILO_WRITES.clear()
