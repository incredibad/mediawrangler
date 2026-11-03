"""Settings → Trash: what MediaWrangler moved off the share, and putting it back or deleting
it for good."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import trash
from ..fixes import FixRefused
from ..models import TrashItem
from .deps import get_db, require_user, verify_csrf
from .templating import templates

router = APIRouter(
    prefix="/settings/trash", dependencies=[Depends(verify_csrf), Depends(require_user)]
)


def _back(problem: str = "", ok: str = "") -> RedirectResponse:
    from urllib.parse import urlencode

    q = urlencode({k: v for k, v in (("problem", problem), ("ok", ok)) if v})
    return RedirectResponse("/settings/trash" + (f"?{q}" if q else ""), status_code=303)


def _item(db: Session, item_id: int) -> TrashItem:
    item = db.get(TrashItem, item_id)
    if item is None:
        raise HTTPException(404, "not in the trash")
    return item


@router.get("")
def trash_page(request: Request, problem: str = "", ok: str = "", db: Session = Depends(get_db)):
    items = trash.listing(db)
    return templates.TemplateResponse(
        request,
        "settings/trash.html",
        {
            "trashed": items,
            "days": {i.id: trash.days_left(i) for i in items},
            "total": sum(i.size or 0 for i in items),
            "on": trash.enabled(db),
            "writable": not trash.unwritable(db),
            "readonly": trash.unwritable(db),
            "problem": problem,
            "ok": ok,
            "retention": trash.RETENTION.days,
            "trash_dir": trash.configured_dir(db) or "",
        },
    )


@router.post("/dir")
def set_dir(trash_dir: str = Form(""), db: Session = Depends(get_db)):
    try:
        trash.set_dir(db, trash_dir)
    except FixRefused as exc:
        return _back(problem=str(exc))
    return _back(ok="Trash folder saved.")


@router.post("/{item_id}/restore")
def restore(item_id: int, request: Request, db: Session = Depends(get_db)):
    """Runs in the background: a move back across disks can take minutes. A toast tracks it."""
    item = _item(db, item_id)
    syncs = request.app.state.syncs
    try:
        job = syncs.restore_job(item.id, f"Putting {item.label} back")
    except FixRefused as exc:
        return _back(problem=str(exc))
    syncs.set_job_url(job, "/settings/trash")
    return _back(ok=f"Putting {item.label} back. The toast shows when it is done.")


@router.post("/{item_id}/delete")
def delete(item_id: int, db: Session = Depends(get_db)):
    if not trash.enabled(db):
        return _back(problem="File changes are switched off (Settings → Writes).")
    try:
        trash.purge(db, _item(db, item_id))
        return _back(ok="Deleted for good.")
    except FixRefused as exc:
        return _back(problem=str(exc))


@router.post("/empty")
def empty(db: Session = Depends(get_db)):
    if not trash.enabled(db):
        return _back(problem="File changes are switched off (Settings → Writes).")
    n = 0
    for item in trash.listing(db):
        try:
            trash.purge(db, item)
            n += 1
        except FixRefused as exc:
            return _back(problem=f"Stopped after {n}: {exc}")
    return _back(ok=f"Deleted {n} {'file' if n == 1 else 'files'} for good.")
