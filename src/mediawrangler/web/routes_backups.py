"""Settings -> Backups: make, download, delete and restore full backups, and export or
import the settings file (0.12)."""

import os
import shutil
import signal
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from .. import settings_export
from ..backups import (
    KEEP_NIGHTLY,
    NIGHTLY_AT,
    BackupError,
    delete_backup,
    find_backup,
    list_backups,
    stage_restore,
)
from ..config import Settings
from ..crypto import SecretBox
from .deps import get_box, get_db, require_user, verify_csrf
from .templating import templates

router = APIRouter(prefix="/settings", dependencies=[Depends(verify_csrf), Depends(require_user)])


def _problem(kind: str, exc: Exception) -> RedirectResponse:
    query = urlencode({"error": kind, "detail": str(exc)})
    return RedirectResponse(f"/settings/backups?{query}", status_code=303)


def restart_soon() -> None:
    """Stop the app a moment from now so the container's restart policy brings it back
    up, which is when a staged restore is applied."""
    threading.Timer(1.5, os.kill, (os.getpid(), signal.SIGTERM)).start()


def restarting_page(request: Request) -> Response:
    getattr(request.app.state, "restart", restart_soon)()
    return templates.TemplateResponse(request, "restarting.html", {})


def stage_upload(settings: Settings, upload: UploadFile) -> str:
    """Save an uploaded backup to a scratch file, check it and stage it."""
    folder = Path(tempfile.mkdtemp(prefix=".upload-", dir=settings.data_dir))
    try:
        scratch = folder / "upload"
        with scratch.open("wb") as out:
            shutil.copyfileobj(upload.file, out)
        return stage_restore(settings, scratch)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@router.get("/backups")
def backups_page(request: Request, error: str = "", detail: str = ""):
    state = request.app.state
    manager = state.backups
    return templates.TemplateResponse(
        request,
        "settings/backups.html",
        {
            "backups": list_backups(state.settings),
            "nightly_at": NIGHTLY_AT,
            "keep": KEEP_NIGHTLY,
            "last_error": manager.last_error,
            "running": manager.running,
            "next_run": manager.next_run(),
            "problem": error,
            "detail": detail,
        },
    )


@router.post("/backups/run")
def backups_run(request: Request):
    try:
        made = request.app.state.backups.run("manual")
    except Exception as exc:
        raise HTTPException(500, f"the backup failed: {exc}") from exc
    if made is None:
        return RedirectResponse("/settings/backups?error=busy", status_code=303)
    return RedirectResponse("/settings/backups?msg=backup_made", status_code=303)


@router.get("/backups/download/{name}")
def backups_download(request: Request, name: str):
    path = find_backup(request.app.state.settings, name)
    if path is None:
        raise HTTPException(404, "no such backup")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@router.post("/backups/delete/{name}")
def backups_delete(request: Request, name: str):
    if not delete_backup(request.app.state.settings, name):
        raise HTTPException(404, "no such backup")
    return RedirectResponse("/settings/backups?msg=backup_deleted", status_code=303)


@router.post("/backups/restore/{name}")
def backups_restore(request: Request, name: str):
    path = find_backup(request.app.state.settings, name)
    if path is None:
        raise HTTPException(404, "no such backup")
    try:
        stage_restore(request.app.state.settings, path)
    except BackupError as exc:
        return _problem("restore", exc)
    return restarting_page(request)


@router.post("/backups/restore-upload")
def backups_restore_upload(request: Request, file: UploadFile = File(...)):
    try:
        stage_upload(request.app.state.settings, file)
    except BackupError as exc:
        return _problem("restore", exc)
    return restarting_page(request)


@router.get("/export")
def settings_download(db: Session = Depends(get_db), box: SecretBox = Depends(get_box)):
    return Response(
        settings_export.export_bytes(db, box),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="mediawrangler-settings.json"'},
    )


@router.post("/import")
def settings_import(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    box: SecretBox = Depends(get_box),
):
    try:
        data = settings_export.parse(file.file.read())
        settings_export.import_settings(db, box, data)
    except settings_export.SettingsFileError as exc:
        return _problem("import", exc)
    return RedirectResponse("/settings/backups?msg=settings_imported", status_code=303)
