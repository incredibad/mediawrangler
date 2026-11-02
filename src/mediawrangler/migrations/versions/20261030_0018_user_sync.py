"""per person, per app sync switches

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-30 09:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_sync",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "app_user_id",
            sa.Integer(),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("source.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sync_account", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sync_history", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("since", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("app_user_id", "source_id"),
    )
    op.create_index("ix_user_sync_app_user_id", "user_sync", ["app_user_id"])
    op.create_index("ix_user_sync_source_id", "user_sync", ["source_id"])
    # Whoever was switched on keeps being written to, in every app they have a user in.
    # app_user is left as it is (its writes_mode and writes_since columns go unused).
    op.execute(
        """
        INSERT INTO user_sync (app_user_id, source_id, sync_account, sync_history, since)
        SELECT DISTINCT au.id, su.source_id, 0, 1, au.writes_since
        FROM app_user au
        JOIN source_user su ON su.app_user_id = au.id AND su.removed_at IS NULL
        WHERE au.writes_mode = 'on'
        """
    )


def downgrade() -> None:
    op.drop_table("user_sync")
