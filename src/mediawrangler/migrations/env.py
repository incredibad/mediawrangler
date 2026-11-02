from alembic import context
from sqlalchemy import create_engine

from mediawrangler import models  # noqa: F401  (registers tables)
from mediawrangler.db import Base

config = context.config
target_metadata = Base.metadata


def run_migrations() -> None:
    connection = config.attributes.get("connection")
    if connection is None:
        # CLI use (e.g. autogenerate): MW_ALEMBIC_URL or a scratch database.
        import os

        url = os.environ.get("MW_ALEMBIC_URL", "sqlite:///./data/mediawrangler.db")
        with create_engine(url).connect() as conn:
            _configure_and_run(conn)
            conn.commit()
    else:
        _configure_and_run(connection)


def _configure_and_run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # SQLite needs batch mode for ALTER TABLE
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


run_migrations()
