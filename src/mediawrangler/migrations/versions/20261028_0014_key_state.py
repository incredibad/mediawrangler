"""key state

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-28 15:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "source_account_key",
        sa.Column("state", sa.String(length=8), nullable=False, server_default="ok"),
    )
    op.add_column("source_account_key", sa.Column("checked_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("source_account_key", "checked_at")
    op.drop_column("source_account_key", "state")
