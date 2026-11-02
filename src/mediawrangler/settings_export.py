"""Settings export and import (0.12).

One versioned JSON file holds what takes effort to set up again: connectors (with their
keys, in plain text, so the file works with any `secret.key`), the master, library groups
and their members, manual match decisions, people, their app switches and the links between
people and app accounts. Sources are named, never numbered, so the file imports into any
database. It holds no admin account, sessions, API keys, watch data or sync history.

Import only adds and updates, matching by name; it never deletes. Libraries and app
accounts that a sync has not read yet are created as placeholders, which the next sync
fills in. Switching an app's history sync on resets its `since` to the import time, so
nothing older than the import is ever written back as an unwatch.
"""

from __future__ import annotations

import json
from datetime import UTC

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import __version__
from .crypto import SecretBox, SecretDecryptError
from .db import utcnow
from .groups import MASTER_KEY
from .models import (
    AppSetting,
    AppUser,
    Library,
    LibraryGroup,
    LibraryGroupMember,
    MatchDecision,
    Source,
    SourceUser,
    UserSync,
)
from .physical import NOT_MANAGED_KEY
from .sync import DISK_AUTOCREATE_KEY

FORMAT = 1
KEPT_SETTINGS = (DISK_AUTOCREATE_KEY, NOT_MANAGED_KEY)
SOURCE_FIELDS = (
    "type",
    "base_url",
    "path_map",
    "options",
    "schedule_minutes",
    "schedule_at",
    "quick_minutes",
    "quick_at",
    "enabled",
)


class SettingsFileError(Exception):
    """The file is not a settings export this version can read."""


def _reveal(box: SecretBox, token: str | None) -> str | None:
    if not token:
        return None
    try:
        return box.decrypt(token)
    except SecretDecryptError:
        return None


def export_settings(db: Session, box: SecretBox) -> dict:
    sources = list(db.execute(select(Source).order_by(Source.id)).scalars())
    names = {s.id: s.name for s in sources}
    people = list(db.execute(select(AppUser).order_by(AppUser.name)).scalars())
    person_names = {p.id: p.name for p in people}

    master = db.get(AppSetting, MASTER_KEY)
    libraries = list(
        db.execute(select(Library).where(Library.group_assigned.is_(True)).order_by(Library.id))
        .scalars()
        .all()
    )
    lib_by_id = {lib.id: lib for lib in libraries}
    groups = []
    for g in db.execute(
        select(LibraryGroup).order_by(LibraryGroup.position, LibraryGroup.id)
    ).scalars():
        members = db.execute(
            select(LibraryGroupMember).where(LibraryGroupMember.group_id == g.id)
        ).scalars()
        groups.append(
            {
                "name": g.name,
                "kind": g.kind,
                "presence_enabled": g.presence_enabled,
                "position": g.position,
                "import_landing": g.import_landing,
                "libraries": [
                    [names[lib.source_id], lib.external_id]
                    for m in members
                    if (lib := lib_by_id.get(m.library_id)) and lib.source_id in names
                ],
            }
        )

    return {
        "format": FORMAT,
        "app_version": __version__,
        "created_at": utcnow().replace(tzinfo=UTC).isoformat(),
        "note": "Connector keys are in plain text. Keep this file private.",
        "sources": [
            {
                "name": s.name,
                **{f: getattr(s, f) for f in SOURCE_FIELDS},
                "secret": _reveal(box, s.secret_enc),
                "webhook_secret": _reveal(box, s.webhook_secret_enc),
            }
            for s in sources
        ],
        "master": names.get(master.value) if master is not None else None,
        "settings": {
            k: row.value for k in KEPT_SETTINGS if (row := db.get(AppSetting, k)) is not None
        },
        "libraries": [
            {
                "source": names[lib.source_id],
                "external_id": lib.external_id,
                "name": lib.name,
                "kind": lib.kind,
                "paths": lib.paths,
            }
            for lib in libraries
            if lib.source_id in names
        ],
        "groups": groups,
        "decisions": [
            {
                "kind": d.kind,
                "a": [names[d.source_a_id], d.external_a],
                "b": [names[d.source_b_id], d.external_b],
                "decision": d.decision,
            }
            for d in db.execute(select(MatchDecision).order_by(MatchDecision.id)).scalars()
            if d.source_a_id in names and d.source_b_id in names
        ],
        "people": [
            {
                "name": p.name,
                "writes_filter": p.writes_filter,
                "apps": [
                    {
                        "source": names[u.source_id],
                        "sync_account": u.sync_account,
                        "sync_history": u.sync_history,
                    }
                    for u in db.execute(
                        select(UserSync).where(UserSync.app_user_id == p.id)
                    ).scalars()
                    if u.source_id in names
                ],
            }
            for p in people
        ],
        "app_accounts": [
            {
                "source": names[su.source_id],
                "external_id": su.external_id,
                "name": su.name,
                "person": person_names.get(su.app_user_id),
            }
            for su in db.execute(
                select(SourceUser).where(
                    (SourceUser.app_user_id.is_not(None)) | (SourceUser.link_assigned.is_(True))
                )
            ).scalars()
            if su.source_id in names
        ],
    }


def export_bytes(db: Session, box: SecretBox) -> bytes:
    return json.dumps(export_settings(db, box), indent=2, ensure_ascii=False).encode()


def parse(raw: bytes) -> dict:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SettingsFileError("this is not a settings file (it is not valid JSON)") from exc
    if not isinstance(data, dict) or "format" not in data or "sources" not in data:
        raise SettingsFileError("this is not a MediaWrangler settings file")
    if not isinstance(data["format"], int) or data["format"] > FORMAT:
        raise SettingsFileError(
            "this settings file was made by a newer MediaWrangler; update this one first"
        )
    return data


def import_settings(db: Session, box: SecretBox, data: dict) -> dict[str, int]:
    """Add and update from an export; returns what was added or changed."""
    done = dict.fromkeys(
        ("sources", "groups", "libraries", "decisions", "people", "app_accounts"), 0
    )
    now = utcnow()
    try:
        by_name: dict[str, Source] = {}
        for item in data.get("sources", []):
            src = db.execute(select(Source).where(Source.name == item["name"])).scalar_one_or_none()
            if src is None:
                src = Source(name=item["name"], type=item["type"], base_url=item["base_url"])
                db.add(src)
            for f in SOURCE_FIELDS:
                if f in item:
                    setattr(src, f, item[f])
            if item.get("secret"):
                src.secret_enc = box.encrypt(item["secret"])
            if item.get("webhook_secret"):
                src.webhook_secret_enc = box.encrypt(item["webhook_secret"])
            by_name[src.name] = src
            done["sources"] += 1
        db.flush()

        def source(name: str) -> Source | None:
            if name not in by_name:
                found = db.execute(select(Source).where(Source.name == name)).scalar_one_or_none()
                if found is None:
                    return None
                by_name[name] = found
            return by_name[name]

        master = source(data["master"]) if data.get("master") else None
        if master is not None:
            row = db.get(AppSetting, MASTER_KEY)
            if row is None:
                db.add(AppSetting(key=MASTER_KEY, value=master.id))
            else:
                row.value = master.id
        for key, value in (data.get("settings") or {}).items():
            if key in KEPT_SETTINGS:
                row = db.get(AppSetting, key)
                if row is None:
                    db.add(AppSetting(key=key, value=value))
                else:
                    row.value = value

        def library(src: Source, external_id: str, name: str, kind: str, paths: list) -> Library:
            lib = db.execute(
                select(Library).where(
                    Library.source_id == src.id, Library.external_id == external_id
                )
            ).scalar_one_or_none()
            if lib is None:
                lib = Library(
                    source_id=src.id, external_id=external_id, name=name, kind=kind, paths=paths
                )
                db.add(lib)
                done["libraries"] += 1
            lib.group_assigned = True
            return lib

        for item in data.get("libraries", []):
            if src := source(item["source"]):
                library(src, item["external_id"], item["name"], item["kind"], item.get("paths", []))
        db.flush()

        for item in data.get("groups", []):
            group = db.execute(
                select(LibraryGroup).where(LibraryGroup.name == item["name"])
            ).scalar_one_or_none()
            if group is None:
                group = LibraryGroup(name=item["name"], kind=item["kind"])
                db.add(group)
            group.kind = item["kind"]
            group.presence_enabled = item.get("presence_enabled", True)
            group.position = item.get("position", 0)
            group.import_landing = item.get("import_landing")
            db.flush()
            for source_name, external_id in item.get("libraries", []):
                src = source(source_name)
                if src is None:
                    continue
                lib = db.execute(
                    select(Library).where(
                        Library.source_id == src.id, Library.external_id == external_id
                    )
                ).scalar_one_or_none()
                if lib is None:
                    continue
                member = db.execute(
                    select(LibraryGroupMember).where(LibraryGroupMember.library_id == lib.id)
                ).scalar_one_or_none()
                if member is None:
                    db.add(LibraryGroupMember(group_id=group.id, library_id=lib.id))
                else:
                    member.group_id = group.id
            done["groups"] += 1

        for item in data.get("decisions", []):
            a, b = source(item["a"][0]), source(item["b"][0])
            if a is None or b is None:
                continue
            exists = db.execute(
                select(MatchDecision).where(
                    MatchDecision.source_a_id == a.id,
                    MatchDecision.external_a == item["a"][1],
                    MatchDecision.source_b_id == b.id,
                    MatchDecision.external_b == item["b"][1],
                )
            ).scalar_one_or_none()
            if exists is None:
                db.add(
                    MatchDecision(
                        kind=item["kind"],
                        source_a_id=a.id,
                        external_a=item["a"][1],
                        source_b_id=b.id,
                        external_b=item["b"][1],
                        decision=item["decision"],
                    )
                )
                done["decisions"] += 1
            else:
                exists.decision = item["decision"]

        people: dict[str, AppUser] = {}

        def person(name: str) -> AppUser:
            if name not in people:
                found = db.execute(select(AppUser).where(AppUser.name == name)).scalar_one_or_none()
                if found is None:
                    found = AppUser(name=name)
                    db.add(found)
                    db.flush()
                people[name] = found
            return people[name]

        for item in data.get("people", []):
            p = person(item["name"])
            if item.get("writes_filter") is not None:
                p.writes_filter = item["writes_filter"]
            for app in item.get("apps", []):
                src = source(app["source"])
                if src is None:
                    continue
                us = db.execute(
                    select(UserSync).where(
                        UserSync.app_user_id == p.id, UserSync.source_id == src.id
                    )
                ).scalar_one_or_none()
                if us is None:
                    us = UserSync(app_user_id=p.id, source_id=src.id)
                    db.add(us)
                us.sync_account = bool(app.get("sync_account"))
                if app.get("sync_history") and not us.sync_history:
                    us.since = now
                us.sync_history = bool(app.get("sync_history"))
            done["people"] += 1

        for item in data.get("app_accounts", []):
            src = source(item["source"])
            if src is None:
                continue
            su = db.execute(
                select(SourceUser).where(
                    SourceUser.source_id == src.id, SourceUser.external_id == item["external_id"]
                )
            ).scalar_one_or_none()
            if su is None:
                su = SourceUser(
                    source_id=src.id, external_id=item["external_id"], name=item["name"]
                )
                db.add(su)
            su.link_assigned = True
            su.app_user_id = person(item["person"]).id if item.get("person") else None
            done["app_accounts"] += 1
        db.commit()
    except (KeyError, TypeError, ValueError) as exc:
        db.rollback()
        raise SettingsFileError(f"the settings file is damaged ({exc!r})") from exc
    except Exception:
        db.rollback()
        raise
    return done
