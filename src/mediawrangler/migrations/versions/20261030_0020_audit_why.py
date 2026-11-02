"""say why a write was made

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-30 16:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("write_job", sa.Column("why", sa.String(length=200), nullable=True))
    op.add_column("audit_log", sa.Column("why", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_log", "why")
    op.drop_column("write_job", "why")
