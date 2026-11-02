"""Fixes started from the comparison pages: the search, preview and apply steps of
"change which show an app thinks this is". Each answers with a fragment for the modal."""

import json
import os
from dataclasses import asdict
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import episode_import, fixes, forget, identify, physical, tasks, trash
from ..connectors.base import ConnectorError, MatchCandidate
from ..crypto import SecretBox
from ..db import utcnow
from ..models import AuditLog, LibraryGroup, LibraryGroupMember, Source, SourceItem
from ..sync import connector_for
from ..writes import ENABLED_KEY, PAUSED_KEY, flag
from .deps import get_box, get_db, require_user, verify_csrf
from .routes_physical import _view
from .templating import templates

router = APIRouter(prefix="/fix", dependencies=[Depends(verify_csrf), Depends(require_user)])


def _plan(request, db, group_id: int, source_id: int, folder: str, kind: str):
    group = db.get(LibraryGroup, group_id)
    source = db.get(Source, source_id)
    if group is None or source is None:
        raise HTTPException(404, "not found")
    view = _view(request, db, group)
    return tasks.plan(db, group, view.roots, source, folder, kind)


def _writes_ready(db):
    """None when writes to the apps are on, else why not."""
    if not flag(db, ENABLED_KEY):
        return "Writes to your apps are switched off. Turn them on under Settings → Writes first."
    if flag(db, PAUSED_KEY):
        return "Writes are paused. Resume them under Settings → Writes first."
    return None


def _folder_page(group: int, folder: str) -> str:
    """Where to check a fix on a folder: the Physical page, filtered to that show, showing every
    row (not only the issues) so it is still there once the issue is gone."""
    return f"/physical?group={group}&filter=all&q={quote(folder.rsplit('/', 1)[-1])}"


def _started(request, label):
    """The dialog closes and the work carries on in the background; its progress is a toast."""
    return templates.TemplateResponse(request, "fix/started.html", {"label": label})


def _refused(request, template, message, **extra):
    return templates.TemplateResponse(request, template, {"ok": False, "message": message, **extra})


@router.get("/toasts")
def toasts(request: Request):
    """The background jobs to show as toasts: polled by every page, so they persist across
    page loads and navigation."""
    return templates.TemplateResponse(
        request, "fix/toasts.html", {"jobs": request.app.state.syncs.jobs()}
    )


@router.post("/toasts/{job}/dismiss")
def toast_dismiss(request: Request, job: int):
    request.app.state.syncs.dismiss_job(job)
    return templates.TemplateResponse(
        request, "fix/toasts.html", {"jobs": request.app.state.syncs.jobs()}
    )


@router.get("/folder-task")
def task_preview(
    request: Request,
    group: int,
    source: int,
    folder: str,
    kind: str,
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
):
    """What asking the app would send, before anything is sent."""
    try:
        p = _plan(request, db, group, source, folder, kind)
    except fixes.FixRefused as exc:
        return templates.TemplateResponse(
            request, "fix/task_result.html", {"p": None, "ok": False, "message": str(exc)}
        )
    method, address, body = tasks.preview_call(box, p)
    return templates.TemplateResponse(
        request,
        "fix/task.html",
        {
            "p": p,
            "group_id": group,
            "method": method,
            "address": address,
            "body": json.dumps(body, indent=2) if body is not None else "",
            "writes_on": flag(db, ENABLED_KEY) and not flag(db, PAUSED_KEY),
        },
    )


@router.post("/reread-folder")
def reread_folder(
    request: Request,
    group: int = Form(...),
    folder: str = Form(...),
    db: Session = Depends(get_db),
):
    """Read one folder on the share again, in the background. Read-only: nothing is sent to
    any app, so it needs no write switch."""
    g = db.get(LibraryGroup, group)
    if g is None:
        raise HTTPException(404, "not found")
    syncs = request.app.state.syncs
    job = syncs.refresh_after_removal(
        g.id, folder, [], apps=False, label=f"Re-reading {folder.rsplit('/', 1)[-1]} on the share"
    )
    syncs.set_job_url(job, _folder_page(g.id, folder))
    return Response(status_code=200, headers={"HX-Trigger": "mw-toast"})


def _not_managed_row(request, db, group: int, folder: str):
    g = db.get(LibraryGroup, group)
    if g is None:
        raise HTTPException(404, "not found")
    row = next((r for r in _view(request, db, g).rows if r.folder == folder), None)
    if row is None:
        raise HTTPException(404, "folder not found in this group")
    return g, row


@router.get("/not-managed")
def not_managed_dialog(request: Request, group: int, folder: str, db: Session = Depends(get_db)):
    """Mark a folder as kept out of the arrs on purpose: the arr cells turn grey and it stops
    counting as "On disk, not in any arr". Nothing is sent to any app."""
    g, row = _not_managed_row(request, db, group, folder)
    return templates.TemplateResponse(
        request, "fix/not_managed.html", {"group": g, "folder": folder, "name": row.name}
    )


@router.post("/not-managed")
def not_managed_set(
    request: Request,
    group: int = Form(...),
    folder: str = Form(...),
    reason: str = Form(""),
    clear: bool = Form(False),
    db: Session = Depends(get_db),
):
    g, row = _not_managed_row(request, db, group, folder)
    reason = reason.strip()[:200]
    physical.set_not_managed(db, folder, None if clear else reason)
    sentence = (
        f"{row.name} is managed by an arr again"
        if clear
        else f"Marked {row.name} as not managed by an arr" + (f": {reason}" if reason else "")
    )
    db.add(
        AuditLog(
            at=utcnow(),
            app_user_id=0,
            source_id=0,
            source_name="MediaWrangler",
            user_name="—",
            label=row.name[:255],
            item_id=folder[:160],
            op="manage" if clear else "unmanage",
            result="applied",
            why=sentence[:200],
        )
    )
    db.commit()
    request.app.state.syncs.bump(background=True)
    ctx = {"message": sentence + ".", "group": g, "folder": folder}
    return templates.TemplateResponse(request, "fix/not_managed_result.html", ctx)


@router.post("/forget")
def forget_show(request: Request, item: int = Form(...), db: Session = Depends(get_db)):
    """Tell a media server that a show's folder was deleted, so it drops the empty show."""
    it = db.get(SourceItem, item)
    if it is None or it.removed_at is not None:
        raise HTTPException(404, "not found")
    src = db.get(Source, it.source_id)
    if (why := _writes_ready(db)) is not None:
        return _refused(request, "fix/identify_result.html", why, p=None)
    if it.id not in forget.gone(db, [src], it.kind):
        message = f"{src.name} still has files for this show, or its folder is on the share"
        return _refused(request, "fix/identify_result.html", message, p=None)
    db.add(
        AuditLog(
            at=utcnow(),
            app_user_id=0,
            source_id=src.id,
            source_name=src.name,
            user_name="—",
            label=it.title[:255],
            item_id=(it.path or "")[:160],
            op="forget",
            result="applied",
            why=f"Told {src.name} the folder of “{it.title}” was deleted"[:200],
        )
    )
    db.commit()
    label = f"Asking {src.name} to drop {it.title}"
    request.app.state.syncs.forget_job(it.id, label)
    return _started(request, label)


@router.get("/rescan-all")
def rescan_all_preview(
    request: Request,
    group: int,
    folder: str,
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
):
    """What "rescan this show everywhere" would do, with the exact request for each app."""
    g = db.get(LibraryGroup, group)
    if g is None:
        raise HTTPException(404, "not found")
    view = _view(request, db, g)
    steps = []
    for source in view.columns:
        if source.id == view.disk_id:
            continue
        item = tasks.find_item(db, g, source, folder, view.roots)
        if item is None:
            steps.append({"app": source.name, "skip": "does not have this title"})
            continue
        try:
            p = tasks.plan(db, g, view.roots, source, folder, "scan")
            method, address, body = tasks.preview_call(box, p)
            steps.append(
                {
                    "app": source.name,
                    "method": method,
                    "address": address,
                    "body": json.dumps(body, indent=2) if body is not None else "",
                }
            )
        except fixes.FixRefused as exc:
            steps.append({"app": source.name, "skip": str(exc)})
    return templates.TemplateResponse(
        request,
        "fix/rescan_all.html",
        {
            "group": g,
            "folder": folder,
            "name": folder.rsplit("/", 1)[-1],
            "steps": steps,
            "writes_on": flag(db, ENABLED_KEY) and not flag(db, PAUSED_KEY),
        },
    )


@router.post("/rescan-all")
def rescan_all_apply(
    request: Request,
    group: int = Form(...),
    folder: str = Form(...),
    db: Session = Depends(get_db),
):
    g = db.get(LibraryGroup, group)
    if g is None:
        raise HTTPException(404, "not found")
    if not flag(db, ENABLED_KEY) or flag(db, PAUSED_KEY):
        return templates.TemplateResponse(
            request,
            "fix/rescan_result.html",
            {
                "ok": False,
                "message": "Writes to your apps are switched off or paused. Turn them on under "
                "Settings → Writes first. (Nothing was sent.)",
            },
        )
    label = f"Rescanning {folder.rsplit('/', 1)[-1]} in every app"
    syncs = request.app.state.syncs
    syncs.set_job_url(syncs.rescan_folder(g.id, folder, label), _folder_page(g.id, folder))
    return _started(request, label)


@router.post("/folder-task")
def task_apply(
    request: Request,
    group: int = Form(...),
    source: int = Form(...),
    folder: str = Form(...),
    kind: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        p = _plan(request, db, group, source, folder, kind)
    except fixes.FixRefused as exc:
        return _refused(request, "fix/task_result.html", str(exc), p=None)
    if (why := _writes_ready(db)) is not None:
        return _refused(request, "fix/task_result.html", why, p=None)
    name = folder.rsplit("/", 1)[-1]
    syncs = request.app.state.syncs
    syncs.set_job_url(
        syncs.task_job(group, source, folder, kind, f"{p.title}: {name}"),
        _folder_page(group, folder),
    )
    return _started(request, p.title)


def _identify_plan(db, group_id: int, source_id: int, folder: str, action: str):
    group = db.get(LibraryGroup, group_id)
    source = db.get(Source, source_id)
    if group is None or source is None:
        raise HTTPException(404, "not found")
    return group, identify.plan(db, group, source, folder, action)


def _manual(m_title, m_year, m_tvdb, m_tmdb, m_imdb):
    ids = {
        k: v.strip() for k, v in (("tvdb", m_tvdb), ("tmdb", m_tmdb), ("imdb", m_imdb)) if v.strip()
    }
    if not m_title.strip() or not ids:
        raise fixes.FixRefused("Fill in a title and at least one id (TVDB, TMDB or IMDb)")
    year = int(m_year) if m_year.strip().isdigit() else None
    return MatchCandidate(title=m_title.strip(), year=year, ids=ids)


@router.get("/identify")
def identify_search(
    request: Request,
    group: int,
    source: int,
    folder: str,
    action: str,
    q: str = "",
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
):
    try:
        g, p = _identify_plan(db, group, source, folder, action)
    except fixes.FixRefused as exc:
        return templates.TemplateResponse(
            request, "fix/identify_result.html", {"p": None, "ok": False, "message": str(exc)}
        )
    name = identify.folder_name(folder)
    error, found, searched = "", [], ""
    try:
        found, searched = identify.search(db, box, g, p, q.strip() or name)
    except fixes.FixRefused as exc:
        error = str(exc)
    return templates.TemplateResponse(
        request,
        "fix/identify.html",
        {
            "p": p,
            "group": g,
            "name": name,
            "q": q.strip() or name,
            "candidates": [asdict(c) for c in found],
            "held_by": identify.held_by(db, g.kind, found),
            "searched": searched,
            "error": error,
        },
    )


@router.post("/identify/preview")
def identify_preview(
    request: Request,
    group: int = Form(...),
    source: int = Form(...),
    folder: str = Form(...),
    action: str = Form(...),
    candidate: str = Form(""),
    m_title: str = Form(""),
    m_year: str = Form(""),
    m_tvdb: str = Form(""),
    m_tmdb: str = Form(""),
    m_imdb: str = Form(""),
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
):
    try:
        g, p = _identify_plan(db, group, source, folder, action)
        cand = (
            fixes.parse_candidate(candidate)
            if candidate
            else _manual(m_title, m_year, m_tvdb, m_tmdb, m_imdb)
        )
        method, address, body = identify.call(box, p, cand)
    except fixes.FixRefused as exc:
        return templates.TemplateResponse(
            request, "fix/identify_result.html", {"p": None, "ok": False, "message": str(exc)}
        )
    return templates.TemplateResponse(
        request,
        "fix/identify_preview.html",
        {
            "p": p,
            "group": g,
            "cand": cand,
            "candidate_json": json.dumps(asdict(cand)),
            "method": method,
            "address": address,
            "body": json.dumps(body, indent=2),
            "writes_on": flag(db, ENABLED_KEY) and not flag(db, PAUSED_KEY),
        },
    )


@router.post("/identify")
def identify_apply(
    request: Request,
    group: int = Form(...),
    source: int = Form(...),
    folder: str = Form(...),
    action: str = Form(...),
    candidate: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        _g, p = _identify_plan(db, group, source, folder, action)
        fixes.parse_candidate(candidate)
    except fixes.FixRefused as exc:
        return _refused(request, "fix/identify_result.html", str(exc), p=None)
    if (why := _writes_ready(db)) is not None:
        return _refused(request, "fix/identify_result.html", why, p=None)
    name = folder.rsplit("/", 1)[-1]
    syncs = request.app.state.syncs
    syncs.set_job_url(
        syncs.identify_job(group, source, folder, action, candidate, f"{p.title}: {name}"),
        _folder_page(group, folder),
    )
    return _started(request, p.title)


def _trash_base(db, path, view) -> str:
    return trash.configured_dir(db) or (trash.library_root(path, view.roots) or "/")


def _trash_context(
    request, db, box, group: int, source: int, folder: str, paths: list[str], leftover=False
):
    g = db.get(LibraryGroup, group)
    arr = db.get(Source, source) if source else None
    if g is None or (source and arr is None):
        raise HTTPException(404, "not found")
    view = _view(request, db, g)
    files, problems = trash.check(db, box, g, view, folder, arr, paths, leftover=leftover)
    in_flight = request.app.state.syncs.busy_paths()
    problems = problems + [
        f"{p.rsplit('/', 1)[-1]} is already being deleted" for p in paths if p in in_flight
    ]
    return (
        g,
        arr,
        view,
        {
            "group": g,
            "arr": arr,
            "folder": folder,
            "files": files,
            "problems": problems,
            "paths": paths,
            "leftover": leftover,
            "on": trash.enabled(db),
            "writable": not trash.unwritable(db),
            "readonly": trash.unwritable(db),
            "total": sum(f.size for f in files),
            "copy_seconds": round(
                sum(
                    f.size
                    for f in files
                    if not trash.same_disk(f.path, _trash_base(db, f.path, view))
                )
                / trash.COPY_SPEED
            ),
        },
    )


@router.post("/trash/preview")
def trash_preview(
    request: Request,
    group: int = Form(...),
    source: int = Form(...),
    folder: str = Form(...),
    path: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
):
    _g, _arr, _view_, ctx = _trash_context(request, db, box, group, source, folder, path)
    return templates.TemplateResponse(request, "fix/trash.html", ctx)


def _import_plan(request, db, group: int, source: int, folder: str, path: str):
    g, src = db.get(LibraryGroup, group), db.get(Source, source)
    if g is None or src is None:
        raise HTTPException(404, "not found")
    return g, episode_import.plan(db, g, src, folder, path, _view(request, db, g).roots)


@router.get("/import")
def import_choose(
    request: Request,
    group: int,
    source: int,
    folder: str,
    path: str,
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
):
    """Pick which episode an orphaned file is, from Sonarr's own list."""
    try:
        g, p = _import_plan(request, db, group, source, folder, path)
        eps = episode_import.episodes(box, p)
    except fixes.FixRefused as exc:
        return _refused(request, "fix/identify_result.html", str(exc), p=None)
    return templates.TemplateResponse(
        request,
        "fix/import.html",
        {"group": g, "p": p, "episodes": eps, "label": episode_import.label},
    )


@router.post("/import/preview")
def import_preview(
    request: Request,
    group: int = Form(...),
    source: int = Form(...),
    folder: str = Form(...),
    path: str = Form(...),
    episode: int = Form(...),
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
):
    try:
        g, p = _import_plan(request, db, group, source, folder, path)
        seen = episode_import.preview(box, p, episode)
    except fixes.FixRefused as exc:
        return _refused(request, "fix/identify_result.html", str(exc), p=None)
    return templates.TemplateResponse(
        request,
        "fix/import_preview.html",
        {
            "group": g,
            "p": p,
            "seen": seen,
            "body": json.dumps(seen["body"], indent=2),
            "writes_on": _writes_ready(db) is None,
        },
    )


@router.post("/import")
def import_apply(
    request: Request,
    group: int = Form(...),
    source: int = Form(...),
    folder: str = Form(...),
    path: str = Form(...),
    episode: int = Form(...),
    db: Session = Depends(get_db),
):
    if (why := _writes_ready(db)) is not None:
        return _refused(request, "fix/identify_result.html", why, p=None)
    try:
        g, p = _import_plan(request, db, group, source, folder, path)
    except fixes.FixRefused as exc:
        return _refused(request, "fix/identify_result.html", str(exc), p=None)
    label = f"Importing {p.name} into {p.source.name}"
    syncs = request.app.state.syncs
    try:
        job = syncs.import_job(g.id, source, folder, path, episode, label)
    except fixes.FixRefused as exc:  # a delete or another import has this file
        return _refused(request, "fix/identify_result.html", str(exc), p=None)
    syncs.set_job_url(job, _folder_page(g.id, folder))
    return _started(request, label)


@router.get("/leftovers")
def leftovers_preview(
    request: Request,
    group: int,
    source: int,
    folder: str,
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
):
    """Every file in a folder the arr does not track, ready to move to the trash."""
    paths = trash.leftover_paths(folder) if os.path.isdir(folder) else []
    _g, _arr, _view_, ctx = _trash_context(request, db, box, group, source, folder, paths, True)
    return templates.TemplateResponse(request, "fix/trash.html", ctx)


@router.post("/trash")
def trash_apply(
    request: Request,
    group: int = Form(...),
    source: int = Form(...),
    folder: str = Form(...),
    path: list[str] = Form(default=[]),
    leftover: bool = Form(False),
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
):
    g, arr, view, ctx = _trash_context(request, db, box, group, source, folder, path, leftover)
    if not trash.enabled(db):
        return _refused(
            request,
            "fix/trash_result.html",
            "File changes are switched off. Turn them on under Settings → Writes first.",
        )
    if ctx["problems"] or not ctx["files"]:
        message = "Nothing was moved. " + "; ".join(ctx["problems"] or ["No files were chosen"])
        return _refused(request, "fix/trash_result.html", message)
    n = len(ctx["files"])
    label = f"Deleting {n} {'file' if n == 1 else 'files'} from {folder.rsplit('/', 1)[-1]}"
    syncs = request.app.state.syncs
    try:
        job = syncs.trash_job(g.id, folder, arr.id if arr else 0, path, label, leftover)
    except fixes.FixRefused as exc:  # another delete is already moving one of these files
        return _refused(request, "fix/trash_result.html", str(exc))
    syncs.set_job_url(job, _folder_page(g.id, folder))
    return _started(request, label)


@router.get("/refresh")
def refresh(
    request: Request,
    source: int,
    stage: str = "read",
    reread: str = "",
    token: str = "",
    old: int = 0,
    replaces: str = "",
    locate: str = "",
    job: int = 0,
    n: int = 0,
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
):
    """After a fix, in steps the dialog polls: wait until the app says its task is done (when it
    can), read that one show or film again (never the whole app), then reload the page."""
    src = db.get(Source, source)
    if src is None:
        raise HTTPException(404, "source not found")
    syncs = request.app.state.syncs
    base = {"source": source, "reread": reread}
    if old:
        base["old"] = old
    if replaces:
        base["replaces"] = replaces
    if locate:
        base["locate"] = locate
    state, text, url = "reading", "", ""
    if stage == "wait":
        try:
            with connector_for(src, box) as connector:
                finished = connector.task_done(token)
        except ConnectorError as exc:
            return templates.TemplateResponse(
                request, "fix/refresh.html", {"source": src, "state": "error", "text": str(exc)}
            )
        if finished or n >= 60:
            stage = "read"
        else:
            state, text = "reading", f"Waiting for {src.name} to finish…"
            url = "/fix/refresh?" + urlencode({**base, "stage": "wait", "token": token, "n": n + 1})
    if stage == "read":
        job = syncs.refresh_title(
            source,
            reread or None,
            old or None,
            replaces or None,
            json.loads(locate) if locate else None,
        )
        stage = "job"
    if stage == "job" and not url:
        status = syncs.title_job(job) or {"state": "error", "error": "the read was lost"}
        if status["state"] == "running":
            text = status.get("text") or "Reading just this one title…"
            url = "/fix/refresh?" + urlencode({**base, "stage": "job", "job": job})
        elif status["state"] == "done":
            state, text = "done", status.get("note", "")
        else:
            state, text = "error", status["error"]
    return templates.TemplateResponse(
        request, "fix/refresh.html", {"source": src, "state": state, "text": text, "url": url}
    )


def _item(db: Session, item_id: int) -> tuple[SourceItem, Source]:
    item = db.get(SourceItem, item_id)
    source = db.get(Source, item.source_id) if item else None
    if item is None or source is None or item.removed_at is not None:
        raise HTTPException(404, "item not found")
    return item, source


def _year(text: str) -> int | None:
    return int(text) if text.strip().isdigit() else None


def _search_page(request, db, box, item, source, q, year, error=""):
    candidates = []
    if not error:
        try:
            candidates = fixes.search(db, box, item, q, year)
        except fixes.FixRefused as exc:
            error = str(exc)
    return templates.TemplateResponse(
        request,
        "fix/search.html",
        {
            "item": item,
            "source": source,
            "q": q,
            "year": year,
            "ids": fixes.current_ids(item),
            "candidates": candidates,
            "error": error,
        },
    )


@router.get("/{item_id}/search")
def search(
    request: Request,
    item_id: int,
    q: str = "",
    year: str = "",
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
):
    item, source = _item(db, item_id)
    q = q.strip() or item.title
    yr = _year(year) if "year" in request.query_params else item.year
    return _search_page(request, db, box, item, source, q, yr)


@router.post("/{item_id}/preview")
def preview(
    request: Request,
    item_id: int,
    candidate: str = Form(...),
    q: str = Form(""),
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
):
    item, source = _item(db, item_id)
    try:
        cand = fixes.parse_candidate(candidate)
        method, address, body = fixes.preview_call(db, box, item, cand)
    except fixes.FixRefused as exc:
        return _search_page(request, db, box, item, source, q or item.title, item.year, str(exc))
    return templates.TemplateResponse(
        request,
        "fix/preview.html",
        {
            "item": item,
            "source": source,
            "cand": cand,
            "candidate_json": candidate,
            "before": fixes.current_ids(item),
            "q": q,
            "method": method,
            "address": address,
            "body": body,
            "writes_on": flag(db, ENABLED_KEY) and not flag(db, PAUSED_KEY),
        },
    )


@router.post("/{item_id}/apply")
def apply(
    request: Request,
    item_id: int,
    candidate: str = Form(...),
    db: Session = Depends(get_db),
):
    item, source = _item(db, item_id)
    try:
        fixes.parse_candidate(candidate)
    except fixes.FixRefused as exc:
        return _refused(request, "fix/result.html", str(exc), item=item, source=source)
    if (why := _writes_ready(db)) is not None:
        return _refused(request, "fix/result.html", why, item=item, source=source)
    label = f"Changing {source.name}'s match for {item.title}"
    syncs = request.app.state.syncs
    member = (
        db.execute(
            select(LibraryGroupMember.group_id).where(
                LibraryGroupMember.library_id == item.library_id
            )
        )
        .scalars()
        .first()
    )
    page = f"/presence?group={member}&filter=all&q={quote(item.title)}" if member else "/presence"
    syncs.set_job_url(syncs.match_job(item.id, candidate, label), page)
    return _started(request, label)
