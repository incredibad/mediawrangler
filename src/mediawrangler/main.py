"""Application factory."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from . import __version__
from .animelist import AnimeList
from .auth import csrf
from .backups import BackupManager, apply_pending_restore
from .config import Settings, get_settings, override_settings
from .crypto import SecretBox, load_master_key
from .db import Database, run_migrations
from .hooks.app import HooksServer
from .hooks.bus import LiveBus
from .scheduler import Scheduler
from .sync import SyncManager, ensure_disk_source
from .viewcache import ViewCache
from .web import (
    api,
    routes_activity,
    routes_auth,
    routes_backups,
    routes_fix,
    routes_imports,
    routes_main,
    routes_physical,
    routes_presence,
    routes_settings,
    routes_trash,
    routes_watch,
)
from .web.deps import LoginRequired, SetupRequired
from .web.templating import templates

log = logging.getLogger("mediawrangler")

STATIC_DIR = Path(__file__).parent / "static"

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "object-src 'none'"
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    override_settings(settings)
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    apply_pending_restore(settings)
    db = Database(settings.db_path)
    run_migrations(db.engine)
    box = SecretBox(load_master_key(settings))
    syncs = SyncManager(db, box)
    syncs.recover()
    ensure_disk_source(db, settings)
    animelist = AnimeList(settings.data_dir)
    syncs.anime_entries = animelist.entries
    live = LiveBus()
    backups = BackupManager(db, settings)
    scheduler = Scheduler(db, syncs, animelist=animelist, backups=backups)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        scheduler.start()
        syncs.bump(background=True)  # fills Local for anyone not yet in it
        syncs.writes.kick()  # a restart carries on with whatever was queued
        live.bind(asyncio.get_running_loop())
        hooks = None
        if settings.webhook_port:
            hooks = HooksServer(_app, "0.0.0.0", settings.webhook_port, settings.log_level.lower())  # noqa: S104
            await hooks.start()
        yield
        if hooks is not None:
            await hooks.stop()
        scheduler.stop()
        syncs.shutdown()

    app = FastAPI(
        title="MediaWrangler",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.box = box
    app.state.syncs = syncs
    app.state.scheduler = scheduler
    app.state.backups = backups
    app.state.animelist = animelist
    app.state.live = live
    app.state.views = ViewCache()

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(api.router)
    app.include_router(routes_auth.router)
    app.include_router(routes_settings.router)
    app.include_router(routes_backups.router)
    app.include_router(routes_trash.router)
    app.include_router(routes_fix.router)
    app.include_router(routes_presence.router)
    app.include_router(routes_physical.router)
    app.include_router(routes_imports.router)
    app.include_router(routes_watch.router)
    app.include_router(routes_activity.router)
    app.include_router(routes_main.router)

    @app.middleware("http")
    async def security_middleware(request: Request, call_next) -> Response:
        token, is_new = csrf.get_or_create_token(request, settings)
        request.state.csrf_token = token
        response = await call_next(request)
        if request.method not in csrf.SAFE_METHODS and response.status_code < 400:
            # Any change made through the UI (links, groups, connectors...) may
            # alter what comparison views are built from.
            syncs.bump(background=True)
        if is_new and not request.url.path.startswith(("/static/", "/api/")):
            csrf.set_cookie(response, token, settings)
        headers = response.headers
        headers.setdefault("Content-Security-Policy", CSP)
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "same-origin")
        headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if settings.secure_cookies:
            headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if not request.url.path.startswith("/static/"):
            headers.setdefault("Cache-Control", "no-store")
        return response

    def _redirect_or_hx(request: Request, url: str) -> Response:
        if request.headers.get("hx-request"):
            return Response(status_code=200, headers={"HX-Redirect": url})
        return RedirectResponse(url, status_code=303)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _exc: LoginRequired) -> Response:
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        url = "/login" if target == "/" else "/login?" + urlencode({"next": target})
        return _redirect_or_hx(request, url)

    @app.exception_handler(SetupRequired)
    async def _setup_required(request: Request, _exc: SetupRequired) -> Response:
        return _redirect_or_hx(request, "/setup")

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return templates.TemplateResponse(
            request,
            "error.html",
            {"status": exc.status_code, "detail": exc.detail},
            status_code=exc.status_code,
        )

    log.info(
        "MediaWrangler %s starting; data in %s; %s mode",
        __version__,
        settings.data_dir,
        "internet-facing (secure cookies)" if settings.secure_cookies else "LAN-only",
    )
    return app
