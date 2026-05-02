"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-02
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "players",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("discord_id", sa.String(32), unique=True, nullable=False),
        sa.Column("discord_username", sa.String(64), nullable=False),
        sa.Column("riot_puuid", sa.String(78), unique=True, nullable=True),
        sa.Column("summoner_name", sa.String(64), nullable=True),
        sa.Column("riot_region", sa.String(8), server_default="na1"),
        sa.Column("is_active", sa.Boolean, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "player_ratings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("player_id", sa.Integer, sa.ForeignKey("players.id"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("lp", sa.Integer, server_default="0"),
        sa.Column("mmr", sa.Float, server_default="1000.0"),
        sa.Column("games_played", sa.Integer, server_default="0"),
        sa.Column("wins", sa.Integer, server_default="0"),
        sa.Column("last_played_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("player_id", "role"),
    )

    op.create_table(
        "role_baselines",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("role", sa.String(16), unique=True, nullable=False),
        sa.Column("avg_gold_diff", sa.Float, server_default="0.0"),
        sa.Column("avg_kp", sa.Float, server_default="0.0"),
        sa.Column("avg_vision", sa.Float, server_default="0.0"),
        sa.Column("avg_cs_pm", sa.Float, server_default="0.0"),
        sa.Column("sample_size", sa.Integer, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "lobbies",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("guild_id", sa.String(32), nullable=False),
        sa.Column("channel_id", sa.String(32), nullable=False),
        sa.Column("message_id", sa.String(32), nullable=True),
        sa.Column("host_discord_id", sa.String(32), nullable=False),
        sa.Column("mode", sa.String(16), server_default="captain"),
        sa.Column("status", sa.String(16), server_default="waiting"),
        sa.Column("max_players", sa.Integer, server_default="10"),
        sa.Column("tournament_code", sa.String(64), nullable=True),
        sa.Column("captain1_id", sa.Integer, sa.ForeignKey("players.id"), nullable=True),
        sa.Column("captain2_id", sa.Integer, sa.ForeignKey("players.id"), nullable=True),
        sa.Column("draft_state", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "lobby_players",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("lobby_id", sa.Integer, sa.ForeignKey("lobbies.id"), nullable=False),
        sa.Column("player_id", sa.Integer, sa.ForeignKey("players.id"), nullable=False),
        sa.Column("preferred_role", sa.String(16), nullable=True),
        sa.Column("team", sa.Integer, nullable=True),
        sa.Column("assigned_role", sa.String(16), nullable=True),
        sa.Column("pick_order", sa.Integer, nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("lobby_id", "player_id"),
    )

    op.create_table(
        "games",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("lobby_id", sa.Integer, sa.ForeignKey("lobbies.id"), nullable=True),
        sa.Column("riot_match_id", sa.String(32), unique=True, nullable=False),
        sa.Column("region", sa.String(8), server_default="na1"),
        sa.Column("status", sa.String(16), server_default="pending"),
        sa.Column("winner_team", sa.Integer, nullable=True),
        sa.Column("game_duration_secs", sa.Integer, nullable=True),
        sa.Column("team1_expected_win", sa.Float, nullable=True),
        sa.Column("weight_config", sa.JSON, nullable=True),
        sa.Column("raw_riot_data", sa.JSON, nullable=True),
        sa.Column("processing_notes", sa.JSON, nullable=True),
        sa.Column("played_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_by", sa.String(64), nullable=True),
    )

    op.create_table(
        "game_participants",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("game_id", sa.Integer, sa.ForeignKey("games.id"), nullable=False),
        sa.Column("player_id", sa.Integer, sa.ForeignKey("players.id"), nullable=False),
        sa.Column("team", sa.Integer, nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("champion_id", sa.Integer, nullable=True),
        sa.Column("champion_name", sa.String(32), nullable=True),
        sa.Column("win", sa.Boolean, nullable=False),
        sa.Column("kills", sa.Integer, server_default="0"),
        sa.Column("deaths", sa.Integer, server_default="0"),
        sa.Column("assists", sa.Integer, server_default="0"),
        sa.Column("cs_total", sa.Integer, server_default="0"),
        sa.Column("vision_score", sa.Integer, server_default="0"),
        sa.Column("damage_dealt", sa.Integer, server_default="0"),
        sa.Column("gold_earned", sa.Integer, server_default="0"),
        sa.Column("impact_score", sa.Float, nullable=True),
        sa.Column("lp_before", sa.Integer, server_default="0"),
        sa.Column("lp_after", sa.Integer, server_default="0"),
        sa.Column("lp_delta", sa.Integer, server_default="0"),
        sa.Column("mmr_before", sa.Float, server_default="1000.0"),
        sa.Column("mmr_after", sa.Float, server_default="1000.0"),
        sa.UniqueConstraint("game_id", "player_id"),
    )


def downgrade() -> None:
    op.drop_table("game_participants")
    op.drop_table("games")
    op.drop_table("lobby_players")
    op.drop_table("lobbies")
    op.drop_table("role_baselines")
    op.drop_table("player_ratings")
    op.drop_table("players")
