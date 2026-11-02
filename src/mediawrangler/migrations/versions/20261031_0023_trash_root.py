"""remember which trash folder each file went to

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-31 14:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trash_item", sa.Column("trash_root", sa.String(length=1024), nullable=True))


def downgrade() -> None:
    op.drop_column("trash_item", "trash_root")
