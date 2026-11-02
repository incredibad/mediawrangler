"""The physical media view: files per folder across disk, arrs and media servers."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import episode_import, identify
from ..connectors import ConnectorError
from ..connectors.paths import is_video
from ..crypto import SecretBox
from ..models import LibraryGroup
from ..physical import (
    DEFAULT_FILTER,
    FILTERS,
    build_physical_view,
    filter_rows,
    folder_detail,
    orphans,
)
from ..sync import connector_for
from ..tasks import capable, find_item
from .deps import get_box, get_db, require_user, verify_csrf
from .filterctx import OptionalId, filter_context, make_link
from .templating import templates

router = APIRouter(dependencies=[Depends(verify_csrf), Depends(require_user)])


def _groups(db: Session) -> list[LibraryGroup]:
    return list(db.execute(select(LibraryGroup).order_by(LibraryGroup.position)).scalars())


def _view(request: Request, db: Session, group: LibraryGroup):
    return request.app.state.views.get(
        ("physical", group.id),
        request.app.state.syncs.version,
        lambda: build_physical_view(db, group),
    )


@router.get("/physical")
def physical(
    request: Request,
    group: OptionalId = None,
    filter: str = DEFAULT_FILTER,
    q: str = "",
    source: OptionalId = None,
    db: Session = Depends(get_db),
):
    groups = _groups(db)
    if not groups:
        return templates.TemplateResponse(request, "physical.html", {"groups": [], "view": None})
    current = next((g for g in groups if g.id == group), groups[0])
    if filter not in FILTERS:
        filter = DEFAULT_FILTER
    view = _view(request, db, current)
    # Every folder goes to the page and the browser filters instantly; the rows
    # the starting filter excludes are marked hidden so nothing flashes.
    visible = {r.folder for r in filter_rows(view, filter, q.strip(), source)}
    ctx = filter_context(
        action="/physical",
        table_id="physical-table",
        filters=FILTERS,
        default=DEFAULT_FILTER,
        flt=filter,
        q=q,
        source=source,
        total=len(view.rows),
        visible=len(visible),
        hidden_fields={"group": current.id},
        source_columns=[(c.id, c.name) for c in view.columns if c.id != view.disk_id],
        noun_one="folder",
        noun_many="folders",
        agree_text="Nothing here: every folder agrees across apps.",
    )
    link = make_link(
        "/physical",
        {"group": current.id, "filter": filter, "q": q, "source": source},
        DEFAULT_FILTER,
    )
    return templates.TemplateResponse(
        request,
        "physical.html",
        {
            **ctx,
            "groups": groups,
            "current": current,
            "view": view,
            "visible": visible,
            "link": link,
            "scannable": {c.id for c in view.columns if capable(c, "scan")},
            "refreshable": {c.id for c in view.columns if capable(c, "refresh")},
            "addable": {c.id for c in view.columns if identify.can_do(c, "add", current)},
            "overridable": {c.id for c in view.columns if identify.can_do(c, "override", current)},
        },
    )


@router.get("/physical/folder/links")
def physical_folder_links(
    request: Request,
    group: int,
    folder: str,
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
):
    """The "View in <app>" buttons for one folder, loaded after the page: an arr's page is
    named by a slug only the arr knows, so each is asked."""
    current = db.get(LibraryGroup, group)
    if current is None:
        raise HTTPException(404, "group not found")
    view = _view(request, db, current)
    links = []
    for col in view.columns:
        if view.roles[col.id] == "disk" or not col.base_url.startswith("http"):
            continue
        item = find_item(db, current, col, folder, view.roots)
        if item is None:
            continue
        try:
            with connector_for(col, box) as connector:
                url = connector.web_link(item.external_id)
        except ConnectorError:
            continue  # the app is unreachable: no button rather than a broken page
        if url:
            links.append((col.name, url))
    return templates.TemplateResponse(request, "_app_links.html", {"links": links})


@router.get("/physical/folder")
def physical_folder(request: Request, group: int, folder: str, db: Session = Depends(get_db)):
    current = db.get(LibraryGroup, group)
    if current is None:
        raise HTTPException(404, "group not found")
    view = _view(request, db, current)
    row = next((r for r in view.rows if r.folder == folder), None)
    if row is None:
        raise HTTPException(404, "folder not found in this group")
    lines, roots = folder_detail(db, view, folder)
    found = orphans(view, row, lines)
    importable = {  # orphaned videos Sonarr could be told the episode of
        sid: [line for line in found_lines if is_video(line.path)]
        for sid, found_lines in found.items()
        if episode_import.can_do(next(c for c in view.columns if c.id == sid), current)
    }
    return templates.TemplateResponse(
        request,
        "physical_folder.html",
        {
            "current": current,
            "view": view,
            "row": row,
            "lines": lines,
            "roots": roots,
            "orphans": found,
            "importable": {sid: lines for sid, lines in importable.items() if lines},
            "odd": row.odd,
            "busy": request.app.state.syncs.busy_paths(),
        },
    )
