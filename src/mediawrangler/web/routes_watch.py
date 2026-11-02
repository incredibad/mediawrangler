"""Watch states: per person, played/unplayed/in progress by app."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..filters import FilterSpec, apply_filter
from ..importers import KINDS, elsewhere
from ..models import AppUser, CanonicalItem, LibraryGroup, SourceUser, WatchImport
from ..watchdiff import (
    DEFAULT_FILTER,
    FILTERS,
    IMPORT_ID_BASE,
    build_watch_view,
    filter_rows,
    readable_users,
    series_detail,
)
from .deps import get_db, require_user, verify_csrf
from .filterctx import OptionalId, filter_context, make_link
from .templating import templates

router = APIRouter(dependencies=[Depends(verify_csrf), Depends(require_user)])


def _people(db: Session) -> list[AppUser]:
    """People with watch state readable in at least one app, admins first."""
    ids = set(
        db.execute(
            select(SourceUser.app_user_id).where(
                SourceUser.watch_readable.is_(True), SourceUser.removed_at.is_(None)
            )
        ).scalars()
    )
    people = db.execute(select(AppUser).where(AppUser.id.in_(ids))).scalars().all()
    return sorted(people, key=lambda p: p.name.lower())


def _pick(db: Session, request: Request, user: int | None, group: int | None):
    people = _people(db)
    groups = list(db.execute(select(LibraryGroup).order_by(LibraryGroup.position)).scalars())
    if not people or not groups:
        return people, groups, None, None
    admin_name = request.state.user.username.lower()
    person = next((p for p in people if p.id == user), None) or next(
        (p for p in people if p.name.lower() == admin_name), people[0]
    )
    current = next((g for g in groups if g.id == group), None) or next(
        (g for g in groups if g.presence_enabled), groups[0]
    )
    return people, groups, person, current


def _view(request: Request, db: Session, person: AppUser, group: LibraryGroup):
    return request.app.state.views.get(
        ("watch", person.id, group.id),
        request.app.state.syncs.version,
        lambda: build_watch_view(db, person, group, request.app.state.animelist.entries()),
    )


@router.get("/watch")
def watch(
    request: Request,
    user: OptionalId = None,
    group: OptionalId = None,
    filter: str = DEFAULT_FILTER,
    q: str = "",
    source: OptionalId = None,
    db: Session = Depends(get_db),
):
    people, groups, person, current = _pick(db, request, user, group)
    if person is None:
        return templates.TemplateResponse(request, "watch.html", {"view": None, "people": people})
    if filter not in FILTERS:
        filter = DEFAULT_FILTER
    view = _view(request, db, person, current)
    visible_ids = {r.canonical.id for r in filter_rows(view, filter, q.strip(), source)}
    ctx = filter_context(
        action="/watch",
        table_id="watch-table",
        filters=FILTERS,
        default=DEFAULT_FILTER,
        flt=filter,
        q=q,
        source=source,
        total=len(view.rows),
        visible=len(visible_ids),
        hidden_fields={"user": person.id, "group": current.id},
        source_columns=[(c.source.id, c.source.name) for c in view.columns[1:]],
        noun_one="title",
        noun_many="titles",
        agree_text=f"No differences: every app agrees on what {person.name} has watched.",
    )
    link = make_link(
        "/watch",
        {"user": person.id, "group": current.id, "filter": filter, "q": q, "source": source},
        DEFAULT_FILTER,
    )
    return templates.TemplateResponse(
        request,
        "watch.html",
        {
            **ctx,
            "people": people,
            "groups": groups,
            "person": person,
            "current": current,
            "view": view,
            "has_imports": person.id in {p.id for p in _importers(db)},
            "visible_ids": visible_ids,
            "link": link,
        },
    )


ELSEWHERE_FILTERS = {
    "all": FilterSpec("All titles", ("any",), per_source=True),
    "movie": FilterSpec("Films", ("movie",), per_source=True),
    "series": FilterSpec("Series", ("series",), per_source=True),
    "other": FilterSpec("Can't be compared", ("other",), per_source=True),
}


def _importers(db: Session) -> list[AppUser]:
    ids = set(db.execute(select(WatchImport.app_user_id)).scalars())
    people = db.execute(select(AppUser).where(AppUser.id.in_(ids))).scalars().all()
    return sorted(people, key=lambda p: p.name.lower())


@router.get("/watch/elsewhere")
def watch_elsewhere(
    request: Request,
    user: OptionalId = None,
    filter: str = "all",
    q: str = "",
    source: OptionalId = None,
    db: Session = Depends(get_db),
):
    """Titles in a person's imported history that none of the apps has."""
    people = _importers(db)
    if not people:
        return templates.TemplateResponse(
            request, "watch_elsewhere.html", {"people": [], "groups": [], "person": None}
        )
    person = next((p for p in people if p.id == user), None) or people[0]
    groups = list(db.execute(select(LibraryGroup).order_by(LibraryGroup.position)).scalars())
    if filter not in ELSEWHERE_FILTERS:
        filter = "all"
    imports, rows = request.app.state.views.get(
        ("elsewhere", person.id),
        request.app.state.syncs.version,
        lambda: elsewhere(db, person.id, IMPORT_ID_BASE, request.app.state.animelist.entries()),
    )
    shown = apply_filter(rows, ELSEWHERE_FILTERS, filter, q.strip(), source)
    wanted = {id(r) for r in shown}
    columns = [(IMPORT_ID_BASE + i.id, KINDS.get(i.kind, i.kind)) for i in imports]
    ctx = filter_context(
        action="/watch/elsewhere",
        table_id="elsewhere-table",
        filters=ELSEWHERE_FILTERS,
        default="all",
        flt=filter,
        q=q,
        source=source,
        total=len(rows),
        visible=len(shown),
        hidden_fields={"user": person.id},
        source_columns=columns,
        noun_one="title",
        noun_many="titles",
        agree_text="Every title in these files is in one of your apps.",
    )
    return templates.TemplateResponse(
        request,
        "watch_elsewhere.html",
        {
            **ctx,
            "people": people,
            "groups": groups,
            "person": person,
            "columns": columns,
            "rows": [(r, id(r) in wanted) for r in rows],
        },
    )


@router.get("/watch/series")
def watch_series(
    request: Request, user: int, group: int, canonical: int, db: Session = Depends(get_db)
):
    person = db.get(AppUser, user)
    current = db.get(LibraryGroup, group)
    canon = db.get(CanonicalItem, canonical)
    if person is None or current is None or canon is None or current.kind != "series":
        raise HTTPException(404, "not found")
    if not readable_users(db, person):
        raise HTTPException(404, "no readable watch state for this person")
    view = _view(request, db, person, current)
    lines = series_detail(db, view, canonical)
    return templates.TemplateResponse(
        request,
        "watch_series.html",
        {"person": person, "current": current, "canon": canon, "view": view, "lines": lines},
    )
