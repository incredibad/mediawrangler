"""Watch-history file imports: upload, list, and what no app has."""

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import animelist
from ..connectors import connector_types
from ..importers import (
    KINDS,
    MAX_UPLOAD,
    PARSERS,
    ImportFileError,
    apply_import,
)
from ..models import AppUser, Source, WatchImport
from .deps import get_db, require_user, verify_csrf
from .templating import templates

router = APIRouter(dependencies=[Depends(verify_csrf), Depends(require_user)])


def import_context(request: Request, db: Session, error: str | None = None) -> dict:
    """What the Imports section of Settings → Connectors shows."""
    people = db.execute(select(AppUser).order_by(AppUser.name)).scalars().all()
    names = {p.id: p.name for p in people}
    imports = db.execute(
        select(WatchImport).order_by(WatchImport.app_user_id, WatchImport.kind)
    ).scalars()
    return {
        "people": people,
        "imports": [(i, names.get(i.app_user_id, "?")) for i in imports],
        "kinds": KINDS,
        "import_error": error,
        "anime_list": animelist.status(request.app.state.db),
        "anime_count": len(request.app.state.animelist.entries()),
    }


def _page(request: Request, db: Session, error: str, status: int = 400):
    sources = db.execute(select(Source).order_by(Source.type, Source.name)).scalars().all()
    return templates.TemplateResponse(
        request,
        "settings/connectors.html",
        {"sources": sources, "types": connector_types(), **import_context(request, db, error)},
        status_code=status,
    )


@router.post("/settings/imports")
async def imports_upload(
    request: Request,
    person: str = Form(""),
    kind: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = db.get(AppUser, int(person)) if person.isdigit() else None
    if user is None:
        return _page(request, db, "Choose which person this history belongs to.")
    if kind not in PARSERS:
        return _page(request, db, "Choose what kind of file this is.")
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        return _page(request, db, "That file is too large.")
    try:
        recs = PARSERS[kind](data)
    except ImportFileError as exc:
        return _page(request, db, str(exc))
    apply_import(db, user.id, kind, file.filename or "upload", recs)
    return RedirectResponse("/settings/connectors#imports", status_code=303)


@router.post("/settings/imports/{import_id}/delete")
def imports_delete(import_id: int, db: Session = Depends(get_db)):
    imp = db.get(WatchImport, import_id)
    if imp is None:
        raise HTTPException(404, "import not found")
    db.delete(imp)
    db.commit()
    return RedirectResponse("/settings/connectors#imports", status_code=303)


@router.post("/settings/imports/anime-list/refresh")
def anime_list_refresh(request: Request):
    state = request.app.state
    animelist.fetch(state.db, state.animelist)
    return RedirectResponse("/settings/connectors#imports", status_code=303)
