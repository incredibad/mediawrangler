"""The webhook listener: its own small web app on its own port.

It is kept apart from the login page on purpose. It answers only
``/hooks/...``, has no sessions or cookies, trusts nothing but a signature
made with a per-app secret, and is meant for the LAN (publish its port to the
LAN, never through a public proxy).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool

from ..connectors import ConnectorError
from ..models import Source
from ..sync import connector_for
from . import arr, jellyfin, signing, silo
from .silo import feed_item

log = logging.getLogger(__name__)

MAX_BODY = 256 * 1024
PLAY_READ_DELAY = 3.0  # nudges arrive every few seconds while someone watches; coalesce them
SYNC_DELAY = 10.0  # a scan or import sends a burst of events; read once it has ended
WATCH_DELAY = 20.0  # after watch changes, a quick re-read heals any webhook that was lost
QUEUE_SIZE = 5000


def hook_kind(source_type: str) -> str | None:
    """Which receiver an app's webhooks go to, or None if it sends none."""
    return {
        "silo": "silo",
        "jellyfin": "jellyfin",
        "sonarr": "arr",
        "radarr": "arr",
        "sportarr": "arr",
    }.get(source_type)


class Coalescer:
    """Run a job shortly after the first request for it, however many more
    arrive in the meantime."""

    def __init__(self) -> None:
        self._pending: set[tuple] = set()

    def schedule(self, key: tuple, delay: float, job: Callable[[], None]) -> None:
        if key in self._pending:
            return
        self._pending.add(key)
        loop = asyncio.get_running_loop()

        async def run() -> None:
            await asyncio.sleep(delay)
            self._pending.discard(key)
            try:
                await run_in_threadpool(job)
            except Exception:
                log.exception("webhook job %s failed", key)

        loop.create_task(run())


def create_hooks_app(
    main_app: FastAPI,
    play_delay: float = PLAY_READ_DELAY,
    sync_delay: float = SYNC_DELAY,
    watch_delay: float = WATCH_DELAY,
    inline: bool = False,
    queue_size: int = QUEUE_SIZE,
) -> FastAPI:
    """The listener. A request is checked at once and answered 204; the work it
    asks for is then done one event at a time, in the order events arrived, so a
    burst (a whole season marked) can neither pile up nor be applied out of
    order. `inline` does the work before answering, for tests."""
    coalescer = Coalescer()
    jobs: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
    state = main_app.state

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        worker = asyncio.create_task(work())
        yield
        worker.cancel()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.get("/hooks/health")
    async def health() -> dict:
        return {"status": "ok", "queued": jobs.qsize()}

    def check(
        source_id: int, signature: str | None, token: str | None, body: bytes, kind: str
    ) -> tuple[int, dict | None]:
        """Is this request genuine and well formed? (A quick read; nothing is applied.)"""
        with state.db.sessionmaker() as db:
            source = db.get(Source, source_id)
            if source is None or hook_kind(source.type) != kind:
                return 404, None
            if not source.webhook_secret_enc:
                return 403, None
            secret = state.box.decrypt(source.webhook_secret_enc)
            if kind == "silo":
                authentic = signing.verify(secret, signature, body, time.time())
            else:  # Jellyfin and the arrs can send a header but cannot sign
                authentic = signing.token_matches(secret, token)
            if not authentic:
                return 401, None
            try:
                parsed = json.loads(body)
            except ValueError:
                return 400, None
            return (204, parsed) if isinstance(parsed, dict) else (400, None)

    def apply(source_id: int, kind: str, parsed: dict) -> silo.Outcome | None:
        with state.db.sessionmaker() as db:
            source = db.get(Source, source_id)
            if source is None:
                return None
            handler = {"silo": silo, "jellyfin": jellyfin, "arr": arr}[kind]
            outcome = handler.handle_event(db, source, parsed)
            for row in outcome.stored:
                state.live.publish(feed_item(row, source.name))
            if any(row.status == "applied" for row in outcome.stored):
                state.syncs.apply_now()
                outcome.reconcile = kind in ("silo", "jellyfin")
            return outcome

    def read_plays(source_id: int) -> None:
        with state.db.sessionmaker() as db:
            source = db.get(Source, source_id)
            if source is None or not source.enabled:
                return
            try:
                with connector_for(source, state.box) as connector:
                    plays = connector.recent_plays()
            except ConnectorError as exc:
                log.warning("could not read Silo's finished plays: %s", exc)
                return
            rows = silo.apply_plays(db, source, plays)
            for row in rows:
                state.live.publish(feed_item(row, source.name))
            if any(row.status == "applied" for row in rows):
                state.syncs.bump()

    narrow: dict[int, dict] = {}  # per source, what the pending read is narrowed to

    def maybe_sync(source_id: int) -> None:
        pending = narrow.pop(source_id, None) or {"folders": set(), "titles": set(), "wide": True}
        with state.db.sessionmaker() as db:
            source = db.get(Source, source_id)
            if source is None or not source.enabled:
                return
            reads = None
            if pending["wide"]:
                pass
            elif source.type == "silo":
                reads = silo.narrow_reads(db, source, pending["folders"], pending["titles"])
            elif pending["titles"] and len(pending["titles"]) <= silo.MAX_NARROW_READS:
                # An arr's id says what it is ("movie-12"); a Jellyfin title must be known.
                kinds = {"movie": "movie", "series": "series"}
                reads = [
                    {"external_id": t, "kind": kinds.get(t.split("-", 1)[0])}
                    for t in sorted(pending["titles"])
                ]
            if reads is not None:
                failed = [
                    r
                    for r in reads
                    if state.syncs.read_title(
                        source_id, r.get("external_id"), r.get("locate"), r.get("kind")
                    )["state"]
                    != "done"
                ]
                log.info(
                    "%s scanned %s: read %d title(s) again, %d failed",
                    source.name,
                    ", ".join(sorted(f for _lib, f in pending["folders"])) or "titles",
                    len(reads),
                    len(failed),
                )
                if not failed:
                    return
            if silo.full_sync_due(db, source):
                state.syncs.start(source_id, "webhook", "full")

    def maybe_watch_sync(source_id: int) -> None:
        with state.db.sessionmaker() as db:
            source = db.get(Source, source_id)
            if source is not None and source.enabled and silo.watch_sync_due(db, source):
                state.syncs.start(source_id, "webhook", "watch")

    async def process(source_id: int, kind: str, parsed: dict) -> None:
        outcome = await run_in_threadpool(apply, source_id, kind, parsed)
        if outcome is None:
            return
        if outcome.read_plays:
            coalescer.schedule(("plays", source_id), play_delay, lambda: read_plays(source_id))
        if outcome.sync:
            pending = narrow.setdefault(
                source_id, {"folders": set(), "titles": set(), "wide": False}
            )
            pending["folders"] |= outcome.folders
            pending["titles"] |= outcome.titles
            pending["wide"] = pending["wide"] or outcome.wide
            coalescer.schedule(("sync", source_id), sync_delay, lambda: maybe_sync(source_id))
        if outcome.reconcile:
            # A webhook can be lost; a quick read soon after a burst makes sure.
            coalescer.schedule(
                ("watch", source_id), watch_delay, lambda: maybe_watch_sync(source_id)
            )

    async def work() -> None:
        while True:
            source_id, kind, parsed = await jobs.get()
            try:
                await process(source_id, kind, parsed)
            except Exception:
                log.exception("could not handle a %s webhook", kind)
            finally:
                jobs.task_done()

    async def accept(source_id: int, request: Request, kind: str) -> Response:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY:
            return Response(status_code=413)
        body = await request.body()
        if len(body) > MAX_BODY:
            return Response(status_code=413)
        status, parsed = await run_in_threadpool(
            check,
            source_id,
            request.headers.get("x-silo-signature"),
            request.headers.get("x-mw-token"),
            body,
            kind,
        )
        if parsed is None:
            return Response(status_code=status)
        if inline:
            await process(source_id, kind, parsed)
            return Response(status_code=204)
        try:
            jobs.put_nowait((source_id, kind, parsed))
        except asyncio.QueueFull:
            return Response(status_code=503, headers={"Retry-After": "5"})
        return Response(status_code=204)

    @app.post("/hooks/silo/{source_id}")
    async def silo_hook(source_id: int, request: Request) -> Response:
        return await accept(source_id, request, "silo")

    @app.post("/hooks/jellyfin/{source_id}")
    async def jellyfin_hook(source_id: int, request: Request) -> Response:
        return await accept(source_id, request, "jellyfin")

    @app.post("/hooks/arr/{source_id}")
    async def arr_hook(source_id: int, request: Request) -> Response:
        return await accept(source_id, request, "arr")

    return app


class HooksServer:
    """Runs the listener inside the main process, on the main event loop."""

    def __init__(self, main_app: FastAPI, host: str, port: int, log_level: str) -> None:
        config = uvicorn.Config(
            create_hooks_app(main_app),
            host=host,
            port=port,
            log_level=log_level,
            server_header=False,
            access_log=False,
        )
        self.server = uvicorn.Server(config)
        self.server.install_signal_handlers = lambda: None  # the main server owns signals
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self.server.serve())

    async def stop(self) -> None:
        self.server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()


__all__ = ["HooksServer", "create_hooks_app"]
