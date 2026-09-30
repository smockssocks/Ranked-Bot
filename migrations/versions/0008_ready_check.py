"""ready checks: accept/decline when a lobby fills, and the decline cooldown

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-28

Idempotent: the bot also adds missing columns at startup.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = (
    ("lobbies", "ready_deadline", sa.DateTime(timezone=True)),
    ("lobbies", "ready_message_id", sa.String(32)),
    ("lobby_players", "accepted_at", sa.DateTime(timezone=True)),
    ("players", "queue_cooldown_until", sa.DateTime(timezone=True)),
)


def _has(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    for table, name, type_ in COLUMNS:
        if not _has(table, name):
            op.add_column(table, sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for table, name, _ in reversed(COLUMNS):
        if _has(table, name):
            op.drop_column(table, name)
