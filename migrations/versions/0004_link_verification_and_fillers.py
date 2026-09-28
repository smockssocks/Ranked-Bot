"""link verification and test fillers: players.link_verified, players.is_test, link_requests

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-28

Idempotent: the bot also creates missing tables and columns at startup.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _insp():
    return sa.inspect(op.get_bind())


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in _insp().get_columns(table)}


def upgrade() -> None:
    if not _has_column("players", "link_verified"):
        op.add_column("players", sa.Column("link_verified", sa.Boolean, server_default=sa.false(), nullable=False))
    if not _has_column("players", "is_test"):
        op.add_column("players", sa.Column("is_test", sa.Boolean, server_default=sa.false(), nullable=False))
    if "link_requests" not in _insp().get_table_names():
        op.create_table(
            "link_requests",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("discord_id", sa.String(32), unique=True, nullable=False),
            sa.Column("puuid", sa.String(78), nullable=False),
            sa.Column("riot_id", sa.String(64), nullable=False),
            sa.Column("icon_id", sa.Integer, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    if "link_requests" in _insp().get_table_names():
        op.drop_table("link_requests")
    for col in ("is_test", "link_verified"):
        if _has_column("players", col):
            op.drop_column("players", col)
