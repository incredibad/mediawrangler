"""Telling Sonarr which episode an orphaned file is (0.12.84).

A file Sonarr tracks the folder for but has no record of is often an episode under the wrong
name (a special saved as the next episode of a season). The person picks the episode from
Sonarr's own list; Sonarr's Manual Import records it. A file already inside the show's folder is
imported where it is, so Sonarr is then asked to rename it, which moves it into the episode's
season folder under its own naming format. MediaWrangler never touches the file
itself. Like every fix, nothing is sent until the person has seen the exact call, the write
needs the global write switch, and it is recorded in the audit log.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import PurePosixPath

from sqlalchemy.orm import Session

from .connectors import ConnectorError
from .connectors.paths import PathMap, is_video
from .crypto import SecretBox
from .db import utcnow
from .fixes import FixRefused
from .models import AuditLog, LibraryGroup, Source, SourceItem
from .sync import connector_for
from .tasks import find_item
from .writes import ENABLED_KEY, PAUSED_KEY, flag


@dataclass
class Plan:
    source: Source
    item: SourceItem
    folder: str
    path: str  # on the share, as MediaWrangler sees it
    app_path: str  # the same file in Sonarr's paths

    @property
    def name(self) -> str:
        return PurePosixPath(self.path).name


def can_do(source: Source, group: LibraryGroup) -> bool:
    return source.type == "sonarr" and group.kind == "series"


def plan(
    db: Session, group: LibraryGroup, source: Source, folder: str, path: str, roots: list[str]
) -> Plan:
    if not can_do(source, group):
        raise FixRefused(f"{source.name} cannot be told which episode a file is")
    if not path.startswith(folder.rstrip("/") + "/"):
        raise FixRefused("That file is not inside this folder")
    if os.path.islink(path) or not os.path.isfile(path):
        raise FixRefused("That file is not on the share any more")
    if not is_video(path):
        raise FixRefused("Only a video file can be an episode")
    item = find_item(db, group, source, folder, roots)
    if item is None:
        raise FixRefused(f"{source.name} does not track this folder")
    app_path = PathMap.from_pairs(source.path_map or []).reverse(path)
    if app_path is None:
        raise FixRefused(
            f"No path rule translates this file into {source.name}'s paths; "
            "set the library up on the Libraries page first"
        )
    return Plan(source, item, folder, path, app_path)


def episodes(box: SecretBox, p: Plan) -> list[dict]:
    try:
        with connector_for(p.source, box) as connector:
            return connector.episodes(p.item.external_id)
    except ConnectorError as exc:
        raise FixRefused(f"{p.source.name} could not list the episodes: {exc}") from exc


def label(ep: dict) -> str:
    return f"S{ep['season']:02d}E{ep['episode']:02d} · {ep['title']}"


def preview(box: SecretBox, p: Plan, episode_id: int) -> dict:
    """What Sonarr makes of the file and the episode, and the exact call apply will send.
    Raises FixRefused when the episode has a file already or Sonarr would refuse it."""
    try:
        with connector_for(p.source, box) as connector:
            eps = connector.episodes(p.item.external_id)
            ep = next((e for e in eps if e["id"] == episode_id), None)
            if ep is None:
                raise FixRefused(f"{p.source.name} has no such episode for this show")
            if ep["has_file"]:
                raise FixRefused(
                    f"{label(ep)} already has a file in {p.source.name}; "
                    "replacing one is not offered here"
                )
            found = connector.import_candidate(p.item.external_id, p.app_path)
            method, path, body = connector.import_call(
                p.item.external_id, p.app_path, episode_id, found
            )
    except ConnectorError as exc:
        raise FixRefused(f"{p.source.name} could not check the file: {exc}") from exc
    return {
        "episode": ep,
        "label": label(ep),
        "quality": ((found.get("quality") or {}).get("quality") or {}).get("name") or "Unknown",
        "languages": [x.get("name") for x in found.get("languages") or [] if x.get("name")],
        "rejections": [r.get("reason") for r in found.get("rejections") or [] if r.get("reason")],
        "found": found,
        "method": method,
        "address": f"{p.source.base_url.rstrip('/')}{path}",
        "body": body,
    }


def run(db: Session, box: SecretBox, p: Plan, episode_id: int) -> str:
    """Send the import. Returns Sonarr's command id; raises FixRefused (after recording the
    failure in the audit log) when it is refused."""
    if not flag(db, ENABLED_KEY):
        raise FixRefused(
            "Writes to your apps are switched off. Turn them on under Settings → Writes first."
        )
    if flag(db, PAUSED_KEY):
        raise FixRefused("Writes are paused. Resume them under Settings → Writes first.")
    seen = preview(box, p, episode_id)  # checked again just before it is sent
    if seen["rejections"]:
        raise FixRefused(f"{p.source.name} would refuse it: " + "; ".join(seen["rejections"]))
    error, token = None, None
    try:
        with connector_for(p.source, box) as connector:
            token = connector.import_episode(
                p.item.external_id, p.app_path, episode_id, seen["found"]
            )
    except ConnectorError as exc:
        error = str(exc)[:240]
    db.add(
        AuditLog(
            at=utcnow(),
            app_user_id=0,
            source_id=p.source.id,
            source_name=p.source.name,
            user_name="—",
            label=p.name[:255],
            item_id=p.app_path[:160],
            op="import",
            result="error" if error else "applied",
            detail=error,
            why=f"Told {p.source.name} that {p.name} is {seen['label']}"[:200],
        )
    )
    db.commit()
    if error:
        raise FixRefused(f"{p.source.name} did not take the import: {error}")
    return token


def rename(db: Session, box: SecretBox, p: Plan, episode_id: int) -> tuple[str, str] | None:
    """After the import: Sonarr renames this one file into place. Returns (its command id, the
    new path in the show's folder), or None when the file already had its proper name."""
    try:
        with connector_for(p.source, box) as connector:
            return connector.rename_episode(p.item.external_id, episode_id)
    except ConnectorError as exc:
        raise FixRefused(
            f"{p.source.name} imported the file but could not rename it: {exc}"
        ) from exc
