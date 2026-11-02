"""MediaWrangler's own JSON API."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import __version__
from ..models import Source
from .deps import get_db, require_api_access

router = APIRouter(prefix="/api")


@router.get("/health")
def health() -> dict:
    """Unauthenticated liveness check (used by the Docker healthcheck)."""
    return {"status": "ok", "version": __version__}


@router.get("/v1/status", dependencies=[Depends(require_api_access)])
def status(db: Session = Depends(get_db)) -> dict:
    sources = db.execute(select(Source).order_by(Source.type, Source.name)).scalars().all()
    return {
        "version": __version__,
        "sources": [
            {
                "id": s.id,
                "type": s.type,
                "name": s.name,
                "enabled": s.enabled,
                "last_test_ok": s.last_test_ok,
                "last_test_at": s.last_test_at.isoformat() + "Z" if s.last_test_at else None,
            }
            for s in sources
        ],
    }
