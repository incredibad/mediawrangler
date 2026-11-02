"""flag files that do not belong in a library

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-31 18:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("media_file", sa.Column("flag", sa.String(length=12), nullable=True))


def downgrade() -> None:
    op.drop_column("media_file", "flag")
