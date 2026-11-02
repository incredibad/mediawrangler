"""user libraries

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-28 20:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("source_user", sa.Column("library_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("source_user", "library_ids")
