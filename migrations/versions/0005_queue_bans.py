"""queue bans

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-28

Idempotent: the bot also creates missing tables at startup.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if "queue_bans" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "queue_bans",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("guild_id", sa.String(32), nullable=False, index=True),
        sa.Column("discord_id", sa.String(32), nullable=False, index=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("banned_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lifted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lifted_by", sa.String(64), nullable=True),
        sa.Column("lift_reason", sa.Text, nullable=True),
    )


def downgrade() -> None:
    if "queue_bans" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("queue_bans")
