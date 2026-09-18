"""ranked v2: uncertainty ratings, seasons, metrics, smurf flags, chat memory, settings

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-18
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # players
    op.add_column("players", sa.Column("summoner_level", sa.Integer, nullable=True))
    op.add_column("players", sa.Column("riot_rank_snapshot", sa.JSON, nullable=True))
    op.add_column("players", sa.Column("linked_at", sa.DateTime(timezone=True), nullable=True))

    # player_ratings
    op.add_column("player_ratings", sa.Column("season", sa.Integer, server_default="1", nullable=False))
    op.add_column("player_ratings", sa.Column("rd", sa.Float, server_default="350.0", nullable=False))
    op.add_column("player_ratings", sa.Column("perf_mean", sa.Float, server_default="0.0", nullable=False))
    op.add_column("player_ratings", sa.Column("perf_var", sa.Float, server_default="1.0", nullable=False))
    op.add_column("player_ratings", sa.Column("peak_lp", sa.Integer, server_default="1000", nullable=False))
    op.add_column("player_ratings", sa.Column("streak", sa.Integer, server_default="0", nullable=False))
    op.alter_column("player_ratings", "lp", server_default="1000")
    op.alter_column("player_ratings", "mmr", server_default="1500.0")
    op.drop_constraint("player_ratings_player_id_role_key", "player_ratings", type_="unique")
    op.create_unique_constraint("uq_player_ratings_player_role_season", "player_ratings", ["player_id", "role", "season"])
    # existing rows started at 0 LP under the old model: lift them to the new starting LP
    op.execute("UPDATE player_ratings SET lp = lp + 1000, peak_lp = lp + 1000")

    # role_baselines -> JSON stats
    op.add_column("role_baselines", sa.Column("season", sa.Integer, server_default="1", nullable=False))
    op.add_column("role_baselines", sa.Column("stats", sa.JSON, nullable=True))
    for col in ("avg_gold_diff", "avg_kp", "avg_vision", "avg_cs_pm"):
        op.drop_column("role_baselines", col)
    op.drop_constraint("role_baselines_role_key", "role_baselines", type_="unique")
    op.create_unique_constraint("uq_role_baselines_role_season", "role_baselines", ["role", "season"])

    # lobbies
    op.add_column("lobbies", sa.Column("drafter_series_id", sa.String(64), nullable=True))
    op.add_column("lobbies", sa.Column("drafter_links", sa.JSON, nullable=True))
    op.add_column("lobbies", sa.Column("drafter_result", sa.JSON, nullable=True))
    op.add_column("lobbies", sa.Column("team1_name", sa.String(35), nullable=True))
    op.add_column("lobbies", sa.Column("team2_name", sa.String(35), nullable=True))
    op.add_column("lobbies", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("lobby_players", sa.Column("secondary_role", sa.String(16), nullable=True))

    # games / participants
    op.add_column("games", sa.Column("season", sa.Integer, server_default="1", nullable=False))
    op.add_column("games", sa.Column("auto_detected", sa.Boolean, server_default="false", nullable=False))
    op.add_column("game_participants", sa.Column("carry_factor", sa.Float, nullable=True))
    op.add_column("game_participants", sa.Column("expected_win", sa.Float, nullable=True))
    op.add_column("game_participants", sa.Column("metrics", sa.JSON, nullable=True))
    op.add_column("game_participants", sa.Column("explanation", sa.Text, nullable=True))
    op.add_column("game_participants", sa.Column("rd_before", sa.Float, server_default="350.0", nullable=False))
    op.add_column("game_participants", sa.Column("rd_after", sa.Float, server_default="350.0", nullable=False))
    op.alter_column("game_participants", "mmr_before", server_default="1500.0")
    op.alter_column("game_participants", "mmr_after", server_default="1500.0")

    # new tables
    op.create_table(
        "smurf_flags",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("player_id", sa.Integer, sa.ForeignKey("players.id"), nullable=False),
        sa.Column("matched_player_id", sa.Integer, sa.ForeignKey("players.id"), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("evidence", sa.JSON, nullable=True),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("status", sa.String(16), server_default="open", nullable=False),
        sa.Column("resolved_by", sa.String(64), nullable=True),
        sa.Column("mod_message_id", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("guild_id", sa.String(32), nullable=False),
        sa.Column("channel_id", sa.String(32), nullable=False),
        sa.Column("discord_id", sa.String(32), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("tokens", sa.Integer, server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_chat_messages_lookup", "chat_messages", ["guild_id", "channel_id", "discord_id"])
    op.create_table(
        "chat_memories",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("guild_id", sa.String(32), nullable=False),
        sa.Column("discord_id", sa.String(32), nullable=False),
        sa.Column("summary", sa.Text, server_default="", nullable=False),
        sa.Column("turns_since_summary", sa.Integer, server_default="0", nullable=False),
        sa.Column("total_turns", sa.Integer, server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("guild_id", "discord_id"),
    )
    op.create_table(
        "chat_usage",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("day", sa.String(10), unique=True, nullable=False),
        sa.Column("prompt_tokens", sa.Integer, server_default="0", nullable=False),
        sa.Column("completion_tokens", sa.Integer, server_default="0", nullable=False),
        sa.Column("requests", sa.Integer, server_default="0", nullable=False),
    )
    op.create_table(
        "guild_settings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("guild_id", sa.String(32), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("guild_id", "key"),
    )


def downgrade() -> None:
    op.drop_table("guild_settings")
    op.drop_table("chat_usage")
    op.drop_table("chat_memories")
    op.drop_index("ix_chat_messages_lookup", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_table("smurf_flags")
    for col in ("rd_after", "rd_before", "explanation", "metrics", "expected_win", "carry_factor"):
        op.drop_column("game_participants", col)
    op.drop_column("games", "auto_detected")
    op.drop_column("games", "season")
    op.drop_column("lobby_players", "secondary_role")
    for col in ("started_at", "team2_name", "team1_name", "drafter_result", "drafter_links", "drafter_series_id"):
        op.drop_column("lobbies", col)
    op.drop_constraint("uq_role_baselines_role_season", "role_baselines", type_="unique")
    op.create_unique_constraint("role_baselines_role_key", "role_baselines", ["role"])
    op.drop_column("role_baselines", "stats")
    op.drop_column("role_baselines", "season")
    for col in ("avg_gold_diff", "avg_kp", "avg_vision", "avg_cs_pm"):
        op.add_column("role_baselines", sa.Column(col, sa.Float, server_default="0.0"))
    op.execute("UPDATE player_ratings SET lp = GREATEST(lp - 1000, 0)")
    op.drop_constraint("uq_player_ratings_player_role_season", "player_ratings", type_="unique")
    op.create_unique_constraint("player_ratings_player_id_role_key", "player_ratings", ["player_id", "role"])
    for col in ("streak", "peak_lp", "perf_var", "perf_mean", "rd", "season"):
        op.drop_column("player_ratings", col)
    for col in ("linked_at", "riot_rank_snapshot", "summoner_level"):
        op.drop_column("players", col)
