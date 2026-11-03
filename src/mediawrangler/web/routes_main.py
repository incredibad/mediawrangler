"""Overview, syncing, and the sections still to come."""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..connectors import connector_types
from ..db import utcnow
from ..groups import app_rank, arr_sort_key, get_master
from ..models import (
    AppUser,
    LibraryGroup,
    LocalState,
    MediaFile,
    Source,
    SourceItem,
    SourceUser,
    SyncRun,
    WatchEvent,
    WatchState,
)
from ..physical import build_physical_view
from ..presence import build_group_view
from ..scheduler import next_run
from ..sync import can_sync
from .deps import get_db, require_user, verify_csrf
from .templating import templates

router = APIRouter(dependencies=[Depends(verify_csrf), Depends(require_user)])


def _source_rows(db: Session) -> list[dict]:
    sources = db.execute(select(Source).order_by(Source.type, Source.name)).scalars().all()
    counts: dict[tuple[int, str], int] = {
        (sid, kind): n
        for sid, kind, n in db.execute(
            select(SourceItem.source_id, SourceItem.kind, func.count())
            .where(SourceItem.removed_at.is_(None))
            .group_by(SourceItem.source_id, SourceItem.kind)
        ).all()
    }
    files = dict(
        db.execute(
            select(MediaFile.source_id, func.count())
            .where(MediaFile.removed_at.is_(None), MediaFile.extra.is_(False))
            .group_by(MediaFile.source_id)
        ).all()
    )
    rows = []
    for s in sources:
        last = db.execute(
            select(SyncRun).where(SyncRun.source_id == s.id).order_by(SyncRun.id.desc()).limit(1)
        ).scalar()
        # Sonarr totals episodes with a file, not files; its last full read keeps that figure.
        full = db.execute(
            select(SyncRun.stats)
            .where(SyncRun.source_id == s.id, SyncRun.mode == "full", SyncRun.status == "ok")
            .order_by(SyncRun.id.desc())
            .limit(1)
        ).scalar()
        rows.append(
            {
                "source": s,
                "syncable": can_sync(s),
                "series": counts.get((s.id, "series"), 0),
                "movies": counts.get((s.id, "movie"), 0),
                "episodes": counts.get((s.id, "episode"), 0),
                "files": files.get(s.id, 0),
                "episodes_with_files": (full or {}).get("episodes_with_files"),
                "multi_episode_files": (full or {}).get("multi_episode_files"),
                "episodes_in_multi_files": (full or {}).get("episodes_in_multi_files"),
                "last_run": last,
                "next": next_run(db, s),
            }
        )
    return rows


def _sync_context(db: Session) -> dict:
    rows = _source_rows(db)
    return {
        "source_rows": rows,
        "any_running": any(r["last_run"] and r["last_run"].status == "running" for r in rows),
        "types": connector_types(),
        "master": get_master(db),
    }


@router.get("/")
def overview(request: Request, db: Session = Depends(get_db)):
    groups = db.execute(select(LibraryGroup).order_by(LibraryGroup.position)).scalars().all()
    views = [
        request.app.state.views.get(
            ("presence", g.id),
            request.app.state.syncs.version,
            lambda g=g: build_group_view(db, g),
        )
        for g in groups
        if g.presence_enabled
    ]
    physical = [
        request.app.state.views.get(
            ("physical", g.id),
            request.app.state.syncs.version,
            lambda g=g: build_physical_view(db, g),
        )
        for g in groups
    ]

    def apps(vs, arrs=False):
        seen = {}
        for v in vs:
            for c in [*v.columns, *(v.arrs if arrs else [])]:
                seen.setdefault(c.id, c)
        types = connector_types()
        role = lambda c: types[c.type].role if c.type in types else "other"  # noqa: E731
        # Standard order: media servers (master first), the share, then the arrs.
        return sorted(
            seen.values(),
            key=lambda c: (app_rank(role(c)), *(arr_sort_key(c) if role(c) == "arr" else ())),
        )

    per_app = db.execute(
        select(SourceUser.app_user_id, SourceUser.source_id, func.count())
        .join(WatchState, WatchState.source_user_id == SourceUser.id)
        .where(WatchState.played.is_(True), SourceUser.app_user_id.is_not(None))
        .group_by(SourceUser.app_user_id, SourceUser.source_id)
    ).all()
    watched_by = {(uid, sid): n for uid, sid, n in per_app}
    watched_apps = {
        s.id: s
        for s in db.execute(select(Source).where(Source.id.in_({sid for _, sid, _ in per_app})))
        .scalars()
        .all()
    }

    now = utcnow()
    played = dict(
        db.execute(
            select(LocalState.app_user_id, func.count())
            .where(LocalState.played.is_(True))
            .group_by(LocalState.app_user_id)
        ).all()
    )
    people = [
        (u, played.get(u.id, 0))
        for u in db.execute(select(AppUser).order_by(AppUser.name)).scalars()
    ]
    events = {
        label: db.execute(
            select(func.count()).select_from(WatchEvent).where(WatchEvent.at >= now - delta)
        ).scalar()
        for label, delta in (
            ("Last hour", timedelta(hours=1)),
            ("Last 24 hours", timedelta(days=1)),
            ("Last 7 days", timedelta(days=7)),
        )
    }
    return templates.TemplateResponse(
        request,
        "overview.html",
        {
            **_sync_context(db),
            "views": views,
            "meta_cols": apps(views, arrs=True),
            "watched_by": watched_by,
            "watched_apps": sorted(watched_apps.values(), key=lambda a: a.name),
            "physical_cols": apps([v for v in physical if v is not None]),
            "physical": [v for v in physical if v is not None],
            "people": [(u, n) for u, n in people if u.local_seeded_at],
            "events": events,
        },
    )


@router.get("/sync/status")
def sync_status(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "_sync_status.html", _sync_context(db))


def _after_sync(request: Request, db: Session):
    if request.headers.get("hx-request"):
        return templates.TemplateResponse(request, "_sync_status.html", _sync_context(db))
    return RedirectResponse("/", status_code=303)


@router.post("/sync/all")
def sync_all(request: Request, db: Session = Depends(get_db)):
    for s in db.execute(select(Source).where(Source.enabled.is_(True))).scalars():
        if can_sync(s):
            request.app.state.syncs.start(s.id)
    return _after_sync(request, db)


@router.post("/sync/{source_id}")
def sync_one(request: Request, source_id: int, db: Session = Depends(get_db)):
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(404, "connector not found")
    request.app.state.syncs.start(source_id)
    return _after_sync(request, db)
