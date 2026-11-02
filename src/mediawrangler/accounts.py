"""Creating a person's missing accounts in the other apps (0.10).

Off until switched on per person and per app (UserSync.sync_account). Switching on
only adds the account to a dry run; nothing is made until an admin types a temporary
password for it and confirms. Passwords are never copied from an app and never stored
or logged. A new account may open the libraries that match the ones the person can
open elsewhere, matched through the library groups. History reaches it afterwards by
the ordinary watch writes (the person's per-app history switch).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .connectors import get_connector_class
from .connectors.base import Capability, ConnectorError
from .crypto import SecretBox
from .db import utcnow
from .models import (
    AppSetting,
    AppUser,
    AuditLog,
    Library,
    LibraryGroupMember,
    Source,
    SourceAccountKey,
    SourceUser,
    UserSync,
)
from .watch import apply_users, link_users

MIN_PASSWORD = 8
KEY_LABEL = "MediaWrangler"


@dataclass
class PlannedAccount:
    person_id: int
    person_name: str
    source_id: int
    source_name: str
    from_name: str  # the app the person already has a user in
    library_ids: list[str] | None  # None: the app's default
    source_type: str = ""
    libraries: list[str] = field(default_factory=list)
    blocked: str = ""  # why it cannot be made


def set_account(db: Session, person: AppUser, source_id: int, on: bool) -> None:
    row = db.execute(
        select(UserSync).where(UserSync.app_user_id == person.id, UserSync.source_id == source_id)
    ).scalar_one_or_none()
    if row is None:
        row = UserSync(app_user_id=person.id, source_id=source_id)
        db.add(row)
    row.sync_account = on


def _master_id(db: Session) -> int | None:
    row = db.get(AppSetting, "master_source_id")
    return int(row.value) if row is not None and row.value else None


def creatable_sources(db: Session) -> list[Source]:
    return [
        s
        for s in db.execute(select(Source).where(Source.enabled.is_(True)).order_by(Source.id))
        .scalars()
        .all()
        if Capability.CREATE_USER in get_connector_class(s.type).capabilities
    ]


def missing(db: Session) -> list[tuple[AppUser, Source, SourceUser]]:
    """(person, app, the user they have elsewhere) for each app that has no user for
    the person. The user is the master's where there is one."""
    master = _master_id(db)
    users: dict[int, list[SourceUser]] = {}
    for su in db.execute(
        select(SourceUser).where(
            SourceUser.removed_at.is_(None), SourceUser.app_user_id.is_not(None)
        )
    ).scalars():
        users.setdefault(su.app_user_id, []).append(su)
    out = []
    sources = creatable_sources(db)
    for person in db.execute(select(AppUser).order_by(AppUser.name)).scalars():
        mine = users.get(person.id, [])
        if not mine:
            continue
        origin = next((u for u in mine if u.source_id == master), mine[0])
        have = {u.source_id for u in mine}
        out.extend((person, s, origin) for s in sources if s.id not in have)
    return out


def _target_libraries(
    db: Session, origin: SourceUser, target: Source
) -> tuple[list[str] | None, list[str]]:
    """The target's library ids matching those the person may open in the origin app,
    with their names. None where the person is unrestricted there."""
    if origin.library_ids is None:
        return None, []
    groups = {m.library_id: m.group_id for m in db.execute(select(LibraryGroupMember)).scalars()}
    by_group: dict[int, list[Library]] = {}
    for lib in db.execute(select(Library).where(Library.source_id == target.id)).scalars():
        if lib.id in groups:
            by_group.setdefault(groups[lib.id], []).append(lib)
    chosen: dict[str, str] = {}
    origin_libs = db.execute(
        select(Library).where(
            Library.source_id == origin.source_id, Library.external_id.in_(origin.library_ids)
        )
    ).scalars()
    for lib in origin_libs:
        for match in by_group.get(groups.get(lib.id, -1), []):
            chosen[match.external_id] = match.name
    return list(chosen), sorted(chosen.values())


def plan_accounts(db: Session) -> list[PlannedAccount]:
    """What switching accounts on would create. Reads only."""
    switched = {
        (u.app_user_id, u.source_id)
        for u in db.execute(select(UserSync).where(UserSync.sync_account.is_(True))).scalars()
    }
    taken: dict[int, set[str]] = {}
    for su in db.execute(select(SourceUser).where(SourceUser.removed_at.is_(None))).scalars():
        taken.setdefault(su.source_id, set()).add(su.name.lower())
    plan = []
    for person, source, origin in missing(db):
        if (person.id, source.id) not in switched:
            continue
        ids, names = _target_libraries(db, origin, source)
        origin_source = db.get(Source, origin.source_id)
        item = PlannedAccount(
            person.id,
            person.name,
            source.id,
            source.name,
            origin_source.name if origin_source else "",
            ids,
            source.type,
            names,
        )
        if person.name.lower() in taken.get(source.id, set()):
            item.blocked = f"{source.name} already has a user called {person.name}; link it first"
        plan.append(item)
    return plan


@dataclass
class Created:
    account: PlannedAccount
    ok: bool
    detail: str = ""
    link: str = ""  # a one-time set-password link, shown once and never kept
    expires: str = ""
    notes: list[str] = field(default_factory=list)


def _after_create(
    db: Session,
    box: SecretBox,
    source: Source,
    acct: PlannedAccount,
    new_id: str,
    by_link: bool,
    done: Created,
) -> None:
    """What follows making an account: the key MediaWrangler needs to read and write its
    watch state (Silo only), and the set-password link where no password was typed. The
    account exists either way, so a failure here is a note, not a failed creation."""
    from .sync import connector_for

    try:
        with connector_for(source, box) as connector:
            if hasattr(connector, "create_account_key"):
                key_id, key = connector.create_account_key(new_id, KEY_LABEL)
                db.add(
                    SourceAccountKey(
                        source_id=source.id,
                        account_id=new_id,
                        account_name=acct.person_name,
                        key_id=key_id,
                        key_enc=box.encrypt(key),
                        state="ok",
                    )
                )
                db.commit()  # Silo shows the key once
    except ConnectorError as exc:
        done.notes.append(
            f"the {source.name} key was not made ({exc}); make it on the connector page"
        )
    if not by_link:
        return
    try:
        with connector_for(source, box) as connector:
            done.link, done.expires = connector.password_reset_link(new_id)
    except ConnectorError as exc:
        done.notes.append(f"no set-password link ({exc}); use Silo's own reset for this user")


def create_accounts(
    db: Session, box: SecretBox, passwords: dict[tuple[int, int], str]
) -> list[Created]:
    """Make the planned accounts that were given a usable password. Each is audited."""
    from .sync import connector_for

    results: list[Created] = []
    touched: set[int] = set()
    for acct in plan_accounts(db):
        if acct.blocked:
            results.append(Created(acct, False, acct.blocked))
            continue
        password = passwords.get((acct.person_id, acct.source_id), "")
        source = db.get(Source, acct.source_id)
        can_link = hasattr(get_connector_class(source.type), "password_reset_link")
        by_link = can_link and not password  # no password typed: they set their own by link
        if by_link:
            password = secrets.token_urlsafe(24)  # never shown; the link replaces it
        elif len(password) < MIN_PASSWORD:
            results.append(Created(acct, False, f"password needs {MIN_PASSWORD}+ characters"))
            continue
        new_id, error = "", ""
        try:
            with connector_for(source, box) as connector:
                new_id = connector.create_user(acct.person_name, password, acct.library_ids)
        except ConnectorError as exc:
            error = str(exc)[:255]
        db.add(
            AuditLog(
                at=utcnow(),
                app_user_id=acct.person_id,
                source_id=source.id,
                source_name=source.name,
                user_name=acct.person_name,
                label="Create account",
                item_id=new_id,
                op="account",
                before="",
                after="created" if not error else "",
                result="applied" if not error else "error",
                detail=error or None,
                why=f"Switched on for {acct.person_name}; user as in {acct.from_name}",
            )
        )
        db.commit()
        if error:
            results.append(Created(acct, False, error))
            continue
        touched.add(source.id)
        done = Created(acct, True)
        _after_create(db, box, source, acct, new_id, by_link, done)
        results.append(done)
    for sid in touched:  # learn the new users now, so they link by name
        source = db.get(Source, sid)
        try:
            with connector_for(source, box) as connector:
                apply_users(db, source, connector.users())
        except ConnectorError:
            pass  # the next sync finds them
    if touched:
        link_users(db)
    return results
