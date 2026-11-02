"""writes filter is free text

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-28 20:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite does not enforce VARCHAR lengths, so the longer filter needs no change
    # there. (A table rebuild here wiped everything that pointed at app_user.)
    if op.get_bind().dialect.name == "sqlite":
        return
    op.alter_column(
        "app_user", "writes_filter", existing_type=sa.String(length=128), type_=sa.Text()
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    op.alter_column(
        "app_user", "writes_filter", existing_type=sa.Text(), type_=sa.String(length=128)
    )
