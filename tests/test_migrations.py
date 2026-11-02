"""Migrations must never take other tables' rows with them."""

from sqlalchemy import text

from mediawrangler import db as dbmod
from mediawrangler.db import Database, run_migrations


def test_foreign_keys_are_off_while_migrating_and_back_on_after(tmp_path, monkeypatch):
    seen = {}
    real = dbmod.command.upgrade

    def spy(cfg, revision):
        conn = cfg.attributes["connection"]
        seen["during"] = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
        return real(cfg, revision)

    monkeypatch.setattr(dbmod.command, "upgrade", spy)
    database = Database(tmp_path / "m.db")
    run_migrations(database.engine)
    assert seen["during"] == 0  # a table rebuild would otherwise cascade into child tables
    with database.engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
