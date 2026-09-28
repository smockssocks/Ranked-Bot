"""temporary team voice channels on lobbies

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-28

Idempotent: the bot also adds missing columns at startup.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = (
    ("team1_voice_id", sa.String(32)),
    ("team2_voice_id", sa.String(32)),
)


def _has(column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("lobbies")}


def upgrade() -> None:
    for name, type_ in COLUMNS:
        if not _has(name):
            op.add_column("lobbies", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(COLUMNS):
        if _has(name):
            op.drop_column("lobbies", name)
