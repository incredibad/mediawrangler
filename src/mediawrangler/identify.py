"""Telling apps what a folder is, from the Physical page (0.11).

Two fixes share one dialog: "add to Sonarr/Radarr" (an arr does not track a folder that is on
the share) and "tell Silo what this folder is" (Silo skipped it because it could not identify
it). Both start from a search of an arr's own database; Silo's can also be filled in by hand.
Like every fix, nothing is sent until the person has seen the exact call and pressed the
button; the write needs the global write switch, and each is recorded in the audit log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .connectors import Capability, ConnectorError, get_connector_class
from .connectors.base import MatchCandidate
from .connectors.paths import PathMap
from .crypto import SecretBox
from .db import utcnow
from .fixes import FixRefused
from .models import AuditLog, LibraryGroup, Source, SourceItem
from .presence import group_libraries
from .sync import connector_for
from .writes import ENABLED_KEY, PAUSED_KEY, flag

ACTIONS = {"add": Capability.ADD, "override": Capability.OVERRIDE}


def _connector_class(source: Source):
    try:
        return get_connector_class(source.type)
    except Exception:
        return None


def can_do(source: Source, action: str, group: LibraryGroup) -> bool:
    cls = _connector_class(source)
    if cls is None or ACTIONS[action] not in cls.capabilities:
        return False
    return action != "add" or getattr(cls, "root_kind", None) == group.kind


def folder_name(folder: str) -> str:
    """The folder's name without a trailing (year): what to search for."""
    return re.sub(r"\s*\(\d{4}\)\s*$", "", PurePosixPath(folder).name)


@dataclass
class Plan:
    source: Source
    action: str
    folder: str
    app_path: str
    library_id: str  # the app's own library id (Silo) or root folder (arr)
    kind: str  # "series" or "movie"

    @property
    def title(self) -> str:
        if self.action == "add":
            return f"Add to {self.source.name}"
        return f"Tell {self.source.name} what this folder is"


def plan(db: Session, group: LibraryGroup, source: Source, folder: str, action: str) -> Plan:
    if action not in ACTIONS or not can_do(source, action, group):
        raise FixRefused(f"{source.name} cannot do that from here")
    path_map = PathMap.from_pairs(source.path_map or [])
    app_path = path_map.reverse(folder)
    if app_path is None:
        raise FixRefused(
            f"No path rule translates this folder into {source.name}'s paths; "
            "set the library up on the Libraries page first"
        )
    libs = [lib for lib in group_libraries(db, group.id) if lib.source_id == source.id]
    lib = next(
        (
            lib
            for lib in libs
            if any(
                (mapped := path_map.apply(p)) and folder.startswith(mapped.rstrip("/") + "/")
                for p in lib.paths or []
            )
        ),
        libs[0] if libs else None,
    )
    if lib is None:
        raise FixRefused(f"{source.name} has no library in this group")
    return Plan(source, action, folder, app_path, lib.external_id, group.kind)


def lookup_source(db: Session, group: LibraryGroup, target: Source) -> Source | None:
    """Whose database to search: the target itself if it can (an arr), else an enabled arr
    that handles this kind of title."""
    candidates = [target] + list(
        db.execute(select(Source).where(Source.enabled.is_(True), Source.id != target.id)).scalars()
    )
    for s in candidates:
        cls = _connector_class(s)
        if cls and Capability.LOOKUP in cls.capabilities and cls.root_kind == group.kind:
            return s
    return None


def search(
    db: Session, box: SecretBox, group: LibraryGroup, p: Plan, term: str
) -> tuple[list[MatchCandidate], str]:
    """(candidates, name of the app searched). Empty with a name of "" when no app can."""
    src = lookup_source(db, group, p.source)
    if src is None:
        return [], ""
    try:
        with connector_for(src, box) as connector:
            return connector.lookup(term), src.name
    except ConnectorError as exc:
        raise FixRefused(f"{src.name} could not search: {exc}") from exc


def held_by(db: Session, kind: str, candidates: list[MatchCandidate]) -> list[list[str]]:
    """For each candidate, the names of the apps that already hold a title with one of its
    IDs, so the person can see which search result the rest of their apps agree on."""
    wanted = {t: {c.ids[t] for c in candidates if c.ids.get(t)} for t in ("tvdb", "tmdb", "imdb")}
    clauses = [getattr(SourceItem, t).in_(v) for t, v in wanted.items() if v]
    if not clauses:
        return [[] for _ in candidates]
    rows = db.execute(
        select(SourceItem.tvdb, SourceItem.tmdb, SourceItem.imdb, Source.name)
        .join(Source, Source.id == SourceItem.source_id)
        .where(SourceItem.kind == kind, SourceItem.removed_at.is_(None), or_(*clauses))
    ).all()
    types = list(wanted)
    return [
        sorted(
            {r.name for r in rows if any(c.ids.get(t) == r[i] for i, t in enumerate(types) if r[i])}
        )
        for c in candidates
    ]


def call(box: SecretBox, p: Plan, cand: MatchCandidate) -> tuple[str, str, object]:
    """(method, full address, body) of what apply will send first."""
    try:
        with connector_for(p.source, box) as connector:
            if p.action == "add":
                method, path, body = connector.add_call(cand, p.app_path)
            else:
                method, path, body = connector.override_call(p.library_id, p.app_path, cand, p.kind)
    except ConnectorError as exc:
        raise FixRefused(f"{p.source.name} could not prepare that: {exc}") from exc
    return method, f"{p.source.base_url.rstrip('/')}{path}", body


def run(db: Session, box: SecretBox, p: Plan, cand: MatchCandidate) -> tuple[bool, str, str | None]:
    """Send it. Returns (ok, message, the app's id for the title when it gives one); always
    writes the audit log."""
    if not flag(db, ENABLED_KEY):
        raise FixRefused(
            "Writes to your apps are switched off. Turn them on under Settings → Writes first."
        )
    if flag(db, PAUSED_KEY):
        raise FixRefused("Writes are paused. Resume them under Settings → Writes first.")
    error, new_id = None, None
    try:
        with connector_for(p.source, box) as connector:
            if p.action == "add":
                new_id = connector.add_title(cand, p.app_path)
            else:
                connector.set_override(p.library_id, p.app_path, cand, p.kind)
    except ConnectorError as exc:
        error = str(exc)[:240]
    what = f"“{cand.title}”" + (f" ({cand.year})" if cand.year else "")
    sentence = (
        f"Added {what} to {p.source.name}, tracking {p.app_path}"
        if p.action == "add"
        else f"Told {p.source.name} that {p.app_path} is {what}"
    )
    db.add(
        AuditLog(
            at=utcnow(),
            app_user_id=0,
            source_id=p.source.id,
            source_name=p.source.name,
            user_name="—",
            label=PurePosixPath(p.folder).name[:255],
            item_id=p.app_path[:160],
            op="add" if p.action == "add" else "override",
            result="error" if error else "applied",
            detail=error,
            why=sentence[:200],
        )
    )
    db.commit()
    if error:
        return False, error, None
    return True, f"{sentence}.", new_id
