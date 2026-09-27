"""casual lobbies: lobbies.ranked; pick_order becomes the default mode

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-27

Idempotent: the bot also adds missing columns itself at startup (see
bot/db/database.py add_missing_columns), so this checks before adding.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    if not _has_column("lobbies", "ranked"):
        op.add_column("lobbies", sa.Column("ranked", sa.Boolean, server_default=sa.true(), nullable=False))
    op.alter_column("lobbies", "mode", server_default="pick_order")


def downgrade() -> None:
    op.alter_column("lobbies", "mode", server_default="captain")
    if _has_column("lobbies", "ranked"):
        op.drop_column("lobbies", "ranked")
