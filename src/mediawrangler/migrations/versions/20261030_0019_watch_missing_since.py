"""played items a read left out wait for a second read

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-30 14:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("watch_state", sa.Column("missing_since", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("watch_state", "missing_since")
