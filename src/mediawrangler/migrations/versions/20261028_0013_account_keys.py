"""account keys

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-28 12:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_account_key",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.String(length=128), nullable=False),
        sa.Column("account_name", sa.String(length=128), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("key_enc", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("source_id", "account_id"),
    )
    op.create_index("ix_source_account_key_source_id", "source_account_key", ["source_id"])


def downgrade() -> None:
    op.drop_index("ix_source_account_key_source_id", table_name="source_account_key")
    op.drop_table("source_account_key")
