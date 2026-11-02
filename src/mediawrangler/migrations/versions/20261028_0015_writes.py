"""writes

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-28 18:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_user",
        sa.Column("writes_mode", sa.String(length=4), nullable=False, server_default="off"),
    )
    op.add_column("app_user", sa.Column("writes_since", sa.DateTime(), nullable=True))
    op.add_column("app_user", sa.Column("writes_filter", sa.String(length=128), nullable=True))
    op.create_table(
        "write_job",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("app_user_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("source_user_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.String(length=160), nullable=False),
        sa.Column("kind", sa.String(length=2), nullable=False),
        sa.Column("canonical_id", sa.Integer(), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("episode", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("op", sa.String(length=8), nullable=False),
        sa.Column("played_at", sa.DateTime(), nullable=True),
        sa.Column("before", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("done_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["app_user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_user_id"], ["source_user.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_write_job_status", "write_job", ["status", "id"])
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("app_user_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("source_name", sa.String(length=64), nullable=False),
        sa.Column("user_name", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("item_id", sa.String(length=160), nullable=False),
        sa.Column("op", sa.String(length=8), nullable=False),
        sa.Column("before", sa.String(length=16), nullable=False),
        sa.Column("after", sa.String(length=16), nullable=False),
        sa.Column("result", sa.String(length=8), nullable=False),
        sa.Column("detail", sa.String(length=255), nullable=True),
        sa.Column("job_id", sa.Integer(), nullable=True),
    )
    op.create_index("ix_audit_log_at", "audit_log", ["at"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_at", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_write_job_status", table_name="write_job")
    op.drop_table("write_job")
    op.drop_column("app_user", "writes_filter")
    op.drop_column("app_user", "writes_since")
    op.drop_column("app_user", "writes_mode")
