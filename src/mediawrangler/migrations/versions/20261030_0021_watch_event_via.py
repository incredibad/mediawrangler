"""note how each watch event was learned

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-30 19:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("watch_event", sa.Column("via", sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column("watch_event", "via")
