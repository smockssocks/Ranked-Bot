"""private game channels and join details on lobbies

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-28

Idempotent: the bot also adds missing columns at startup.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = (
    ("game_channel_id", sa.String(32)),
    ("join_name", sa.String(64)),
    ("join_password", sa.String(32)),
    ("join_creator_id", sa.Integer),
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
