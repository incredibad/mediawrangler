"""files moved to trash

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-31 12:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trash_item",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("original_path", sa.String(length=1024), nullable=False),
        sa.Column("trash_path", sa.String(length=1024), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("folder", sa.String(length=1024), nullable=False),
        sa.Column("why", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=8), nullable=False, server_default="trashed"),
        sa.Column("trashed_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_trash_item_status", "trash_item", ["status"])


def downgrade() -> None:
    op.drop_index("ix_trash_item_status", table_name="trash_item")
    op.drop_table("trash_item")
