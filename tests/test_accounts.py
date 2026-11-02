"""Creating missing accounts in the other apps (0.10)."""

# ruff: noqa: F811  (the `synced` fixture is shared from test_watch)

import pytest
from sqlalchemy import select

from mediawrangler import accounts
from mediawrangler.db import utcnow
from mediawrangler.models import AuditLog, Source, SourceUser, UserSync

from . import fakes
from .conftest import post
from .test_watch import _person
from .test_watch import synced as synced  # re-exported: pytest finds fixtures by module


@pytest.fixture(autouse=True)
def _clean_fakes():
    fakes.CREATED.clear()
    yield
    fakes.CREATED.clear()


def _without_silo_user(db, name):
    """Jordan has a Jellyfin user only, as before her Silo account was made."""
    silo = db.execute(select(Source).where(Source.type == "silo")).scalar_one()
    su = db.execute(
        select(SourceUser).where(SourceUser.source_id == silo.id, SourceUser.name == name)
    ).scalar_one()
    su.removed_at = utcnow()
    db.commit()
    return silo


def test_nothing_is_planned_until_an_account_is_switched_on(synced):
    db, *_ = synced
    silo = _without_silo_user(db, "Jordan")
    al = _person(db, "Jordan")
    assert [(p.name, s.name) for p, s, _o in accounts.missing(db)] == [("Jordan", silo.name)]
    assert accounts.plan_accounts(db) == []
    accounts.set_account(db, al, silo.id, True)
    db.commit()
    plan = accounts.plan_accounts(db)
    assert [(a.person_name, a.source_name, a.blocked) for a in plan] == [("Jordan", silo.name, "")]
    assert plan[0].from_name  # made from her Jellyfin user
    row = db.execute(select(UserSync)).scalar_one()
    assert row.sync_account and not row.sync_history  # history stays its own switch


def test_an_account_is_made_only_with_a_password_and_is_audited(synced, app):
    db, *_ = synced
    silo = _without_silo_user(db, "Jordan")
    al = _person(db, "Jordan")
    accounts.set_account(db, al, silo.id, True)
    db.commit()
    key = (al.id, silo.id)
    short = accounts.create_accounts(db, app.state.box, {key: "short"})
    assert [r.ok for r in short] == [False] and not fakes.CREATED
    done = accounts.create_accounts(db, app.state.box, {key: "temp-pass-123"})
    assert [r.ok for r in done] == [True]
    (app_, what, body) = fakes.CREATED[0]
    assert (app_, what) == ("silo", "user")
    assert body["username"] == "Jordan" and body["role"] == "user"
    assert body["password"] == "temp-pass-123" and body["require_password_change"] is True
    assert body["email"].endswith("@mediawrangler.invalid")
    log = db.execute(select(AuditLog).where(AuditLog.op == "account")).scalar_one()
    assert log.result == "applied" and log.user_name == "Jordan"
    assert "temp-pass-123" not in " ".join(str(v) for v in vars(log).values())


def test_a_name_already_taken_in_the_app_is_not_made_twice(synced, app):
    db, *_ = synced
    silo = _without_silo_user(db, "Jordan")
    al = _person(db, "Jordan")
    other = _person(db, "alex")
    # Silo still has a user with her name, but it is linked to someone else.
    su = db.execute(
        select(SourceUser).where(SourceUser.source_id == silo.id, SourceUser.name == "Jordan")
    ).scalar_one()
    su.removed_at, su.app_user_id = None, other.id
    db.commit()
    accounts.set_account(db, al, silo.id, True)
    db.commit()
    plan = accounts.plan_accounts(db)
    assert plan and "already has a user" in plan[0].blocked
    results = accounts.create_accounts(db, app.state.box, {(al.id, silo.id): "temp-pass-123"})
    assert [r.ok for r in results] == [False] and not fakes.CREATED


def test_the_users_page_switch_and_create(synced, logged_in, app):
    db, *_ = synced
    silo = _without_silo_user(db, "Jordan")
    al = _person(db, "Jordan")
    hx = {"headers": {"HX-Request": "true"}}
    assert "Dry run" not in logged_in.get("/settings/users").text
    resp = post(logged_in, f"/settings/users/account/{al.id}/{silo.id}", {"on": "on"}, **hx)
    assert 'id="accounts"' in resp.text and "Dry run" in resp.text and "Jordan" in resp.text
    resp = post(
        logged_in,
        "/settings/users/accounts/create",
        {f"pw_{al.id}_{silo.id}": "temp-pass-123"},
        **hx,
    )
    assert "Created" in resp.text and fakes.CREATED
    # Asking for an account that is not missing is refused.
    alex = _person(db, "alex")
    assert (
        post(
            logged_in, f"/settings/users/account/{alex.id}/{silo.id}", {"on": "on"}, **hx
        ).status_code
        == 404
    )


def test_a_jellyfin_account_gets_the_matching_libraries(synced, app):
    db, *_ = synced
    jf = db.execute(select(Source).where(Source.type == "jellyfin")).scalar_one()
    silo = db.execute(select(Source).where(Source.type == "silo")).scalar_one()
    alex = _person(db, "alex")
    db.execute(
        select(SourceUser).where(SourceUser.source_id == jf.id, SourceUser.app_user_id == alex.id)
    ).scalar_one().removed_at = utcnow()
    origin = db.execute(
        select(SourceUser).where(SourceUser.source_id == silo.id, SourceUser.app_user_id == alex.id)
    ).scalar_one()
    from mediawrangler.models import Library

    silo_lib = db.execute(select(Library).where(Library.source_id == silo.id)).scalars().first()
    origin.library_ids = [silo_lib.external_id]
    accounts.set_account(db, alex, jf.id, True)
    db.commit()
    (plan,) = accounts.plan_accounts(db)
    assert plan.libraries and plan.library_ids  # the group's Jellyfin library
    done = accounts.create_accounts(db, app.state.box, {(alex.id, jf.id): "temp-pass-123"})
    assert [r.ok for r in done] == [True]
    kinds = [(a, w) for a, w, _b in fakes.CREATED]
    assert kinds == [("jellyfin", "user"), ("jellyfin", "policy")]
    policy = fakes.CREATED[1][2]
    assert policy["EnableAllFolders"] is False and policy["EnabledFolders"] == plan.library_ids


def test_silo_account_without_a_password_gets_a_key_and_a_set_password_link(synced, app):
    from mediawrangler.models import SourceAccountKey

    db, *_ = synced
    silo = _without_silo_user(db, "Jordan")
    al = _person(db, "Jordan")
    accounts.set_account(db, al, silo.id, True)
    db.commit()
    (done,) = accounts.create_accounts(db, app.state.box, {(al.id, silo.id): ""})
    assert done.ok and done.link == "https://silo.example/reset/one-time" and done.expires
    kinds = [(a, w) for a, w, _b in fakes.CREATED]
    assert kinds == [("silo", "user"), ("silo", "reset")]
    body = fakes.CREATED[0][2]
    assert body["password"] and len(body["password"]) >= 24  # made up here, never shown
    key = db.execute(
        select(SourceAccountKey).where(SourceAccountKey.account_id == "9")
    ).scalar_one()
    assert key.state == "ok" and app.state.box.decrypt(key.key_enc) == "key-9"
    log = db.execute(select(AuditLog).where(AuditLog.op == "account")).scalar_one()
    assert done.link not in " ".join(str(v) for v in vars(log).values())


def test_a_jellyfin_account_still_needs_a_typed_password(synced, app):
    db, *_ = synced
    jf = db.execute(select(Source).where(Source.type == "jellyfin")).scalar_one()
    alex = _person(db, "alex")
    db.execute(
        select(SourceUser).where(SourceUser.source_id == jf.id, SourceUser.app_user_id == alex.id)
    ).scalar_one().removed_at = utcnow()
    accounts.set_account(db, alex, jf.id, True)
    db.commit()
    (done,) = accounts.create_accounts(db, app.state.box, {(alex.id, jf.id): ""})
    assert not done.ok and "password needs" in done.detail and not fakes.CREATED
