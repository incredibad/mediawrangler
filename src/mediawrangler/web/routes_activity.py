"""Activity: sync runs, the audit log and the live feed."""

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..activity import AUDIT_FILTERS, RUN_FILTERS, build_audit, build_runs
from ..filters import apply_filter
from ..hooks.silo import feed_item
from ..models import Source, WebhookEvent
from .deps import get_db, require_user, verify_csrf
from .filterctx import OptionalId, filter_context, make_link
from .templating import templates

router = APIRouter(dependencies=[Depends(verify_csrf), Depends(require_user)])

TABS = {
    "runs": ("Sync runs", RUN_FILTERS, build_runs, "run", "runs"),
    "audit": ("Audit log", AUDIT_FILTERS, build_audit, "entry", "entries"),
}


LIVE_ROWS = 200
TAB_LABELS = {"runs": "Sync runs", "audit": "Audit log", "live": "Live events"}


def _live_page(request: Request, db: Session):
    rows = db.execute(
        select(WebhookEvent, Source.name)
        .join(Source, Source.id == WebhookEvent.source_id)
        .order_by(WebhookEvent.id.desc())
        .limit(LIVE_ROWS)
    ).all()
    items = [feed_item(ev, name) for ev, name in rows]
    return templates.TemplateResponse(
        request, "activity_live.html", {"tab": "live", "tabs": TAB_LABELS, "events": items}
    )


@router.get("/activity/stream")
async def activity_stream(request: Request):
    """Server-sent events: each webhook as it is handled."""
    live = request.app.state.live
    queue = live.subscribe()

    async def stream():
        try:
            yield ": connected\n\n"
            while not await request.is_disconnected():
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(item)}\n\n"
        finally:
            live.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/activity")
def activity(
    request: Request,
    tab: str = "runs",
    filter: str = "all",
    q: str = "",
    source: OptionalId = None,
    db: Session = Depends(get_db),
):
    if tab == "changes":  # the old name of the watch changes tab, now part of the audit log
        tab = "audit"
    if tab == "live":
        return _live_page(request, db)
    if tab not in TABS:
        tab = "runs"
    _, filters, build, one, many = TABS[tab]
    if filter not in filters:
        filter = "all"
    view = request.app.state.views.get(
        ("activity", tab), request.app.state.syncs.version, lambda: build(db)
    )
    shown = apply_filter(view.rows, filters, filter, q.strip(), source)
    wanted = {id(r) for r in shown}
    visible = {i for i, r in enumerate(view.rows) if id(r) in wanted}
    ctx = filter_context(
        action="/activity",
        table_id="activity-table",
        filters=filters,
        default="all",
        flt=filter,
        q=q,
        source=source,
        total=len(view.rows),
        visible=len(shown),
        hidden_fields={"tab": tab},
        source_columns=view.sources,
        noun_one=one,
        noun_many=many,
        agree_text="Nothing to show.",
    )
    return templates.TemplateResponse(
        request,
        "activity.html",
        {
            **ctx,
            "tab": tab,
            "tabs": TAB_LABELS,
            "view": view,
            "visible": visible,
            "link": make_link("/activity", {"tab": tab}, "all"),
        },
    )
