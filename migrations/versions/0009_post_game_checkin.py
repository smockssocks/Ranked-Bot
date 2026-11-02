"""post-game check-in: players confirm the role they played and their champion

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-28

Idempotent: the bot also adds missing columns at startup.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = (
    ("lobbies", "result_match_id", sa.String(32)),
    ("lobbies", "checkin_deadline", sa.DateTime(timezone=True)),
    ("lobbies", "checkin_message_id", sa.String(32)),
    ("lobby_players", "confirmed_role", sa.String(16)),
    ("lobby_players", "confirmed_champion", sa.String(32)),
    ("lobby_players", "confirmed_at", sa.DateTime(timezone=True)),
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
