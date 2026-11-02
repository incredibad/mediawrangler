"""local

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-27 12:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def _key_columns() -> list[sa.Column]:
    return [
        sa.Column("app_user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=2), nullable=False),
        sa.Column("canonical_id", sa.Integer(), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("episode", sa.Integer(), nullable=False),
    ]


def upgrade() -> None:
    op.add_column("app_user", sa.Column("local_seeded_at", sa.DateTime(), nullable=True))
    fk = sa.ForeignKeyConstraint(["app_user_id"], ["app_user.id"], ondelete="CASCADE")
    op.create_table(
        "local_state",
        sa.Column("id", sa.Integer(), nullable=False),
        *_key_columns(),
        sa.Column("played", sa.Boolean(), nullable=False),
        sa.Column("position_seconds", sa.Float(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("last_played_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        fk,
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("app_user_id", "kind", "canonical_id", "season", "episode"),
    )
    op.create_index("ix_local_state_app_user_id", "local_state", ["app_user_id"])
    op.create_table(
        "local_play",
        sa.Column("id", sa.Integer(), nullable=False),
        *_key_columns(),
        sa.Column("day", sa.String(length=10), nullable=False),
        sa.Column("played_at", sa.DateTime(), nullable=True),
        sa.Column("sources", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(["app_user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("app_user_id", "kind", "canonical_id", "season", "episode", "day"),
    )
    op.create_index("ix_local_play_app_user_id", "local_play", ["app_user_id"])
    op.create_table(
        "local_event",
        sa.Column("id", sa.Integer(), nullable=False),
        *_key_columns(),
        sa.Column("change", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["app_user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_local_event_app_user_id", "local_event", ["app_user_id"])
    op.create_index("ix_local_event_user_at", "local_event", ["app_user_id", "at"])
    op.create_table(
        "local_cursor",
        sa.Column("source_user_id", sa.Integer(), nullable=False),
        sa.Column("last_event_id", sa.Integer(), nullable=False),
        sa.Column("held_count", sa.Integer(), nullable=False),
        sa.Column("held_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["source_user_id"], ["source_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("source_user_id"),
    )


def downgrade() -> None:
    op.drop_table("local_cursor")
    op.drop_table("local_event")
    op.drop_table("local_play")
    op.drop_table("local_state")
    op.drop_column("app_user", "local_seeded_at")
