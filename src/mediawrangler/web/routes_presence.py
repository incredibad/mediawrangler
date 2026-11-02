"""The metadata presence matrix and match decisions."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import forget
from ..fixes import can_rematch
from ..groups import get_master
from ..models import LibraryGroup, MatchDecision, SourceItem
from ..presence import DEFAULT_FILTER, FILTERS, build_group_view, filter_rows
from .deps import get_db, require_user, verify_csrf
from .filterctx import OptionalId, filter_context, make_link
from .templating import safe_next, templates

router = APIRouter(dependencies=[Depends(verify_csrf), Depends(require_user)])


@router.get("/presence")
def presence(
    request: Request,
    group: OptionalId = None,
    filter: str = DEFAULT_FILTER,
    q: str = "",
    source: OptionalId = None,
    db: Session = Depends(get_db),
):
    groups = (
        db.execute(
            select(LibraryGroup)
            .where(LibraryGroup.presence_enabled.is_(True))
            .order_by(LibraryGroup.position)
        )
        .scalars()
        .all()
    )
    if not groups:
        return templates.TemplateResponse(request, "presence.html", {"groups": [], "view": None})
    current = next((g for g in groups if g.id == group), groups[0])
    if filter not in FILTERS:
        filter = DEFAULT_FILTER
    view = request.app.state.views.get(
        ("presence", current.id),
        request.app.state.syncs.version,
        lambda: build_group_view(db, current),
    )
    # Every row goes to the page and the browser filters instantly; the rows
    # the starting filter excludes are marked hidden so nothing flashes.
    visible_ids = {r.canonical.id for r in filter_rows(view, filter, q.strip(), source)}
    ctx = filter_context(
        action="/presence",
        table_id="presence-table",
        filters=FILTERS,
        default=DEFAULT_FILTER,
        flt=filter,
        q=q,
        source=source,
        total=len(view.rows),
        visible=len(visible_ids),
        hidden_fields={"group": current.id},
        source_columns=[(c.id, c.name) for c in view.columns if c.id != view.master_id],
        noun_one="title",
        noun_many="titles",
        agree_text="Nothing here: every title agrees across apps.",
    )
    link = make_link(
        "/presence",
        {"group": current.id, "filter": filter, "q": q, "source": source},
        DEFAULT_FILTER,
    )
    return templates.TemplateResponse(
        request,
        "presence.html",
        {
            **ctx,
            "groups": groups,
            "current": current,
            "view": view,
            "visible_ids": visible_ids,
            "link": link,
            "master": get_master(db),
            "fixable_sources": {s.id for s in view.columns if can_rematch(s)},
            "forgettable": forget.gone(db, view.columns, current.kind),
            "here": str(request.url.path) + ("?" + request.url.query if request.url.query else ""),
        },
    )


@router.post("/presence/decide")
def decide(
    request: Request,
    item_id: int = Form(...),
    decision: str = Form(...),
    back: str = Form("/presence"),
    db: Session = Depends(get_db),
):
    """Confirm or reject an unconfirmed (title) match. The pair is stored as
    (the match's partner, this item) so it survives re-ingest and re-matching."""
    if decision not in ("confirm", "reject"):
        raise HTTPException(400, "unknown decision")
    item = db.get(SourceItem, item_id)
    if item is None or item.canonical_id is None:
        raise HTTPException(404, "item not found")
    master = get_master(db)
    partners = (
        db.execute(
            select(SourceItem).where(
                SourceItem.canonical_id == item.canonical_id,
                SourceItem.id != item.id,
                SourceItem.source_id != item.source_id,
                SourceItem.removed_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    partners.sort(
        key=lambda p: (p.source_id != (master.id if master else None), p.match_confidence == "low")
    )
    if not partners:
        raise HTTPException(400, "this item has no match to decide on")
    partner = partners[0]
    exists = db.execute(
        select(MatchDecision).where(
            MatchDecision.source_a_id == partner.source_id,
            MatchDecision.external_a == partner.external_id,
            MatchDecision.source_b_id == item.source_id,
            MatchDecision.external_b == item.external_id,
        )
    ).scalar()
    if exists is None:
        exists = MatchDecision(
            kind=item.kind,
            source_a_id=partner.source_id,
            external_a=partner.external_id,
            source_b_id=item.source_id,
            external_b=item.external_id,
        )
        db.add(exists)
    exists.decision = decision
    db.commit()
    request.app.state.syncs.rematch()
    return RedirectResponse(safe_next(back), status_code=303)
