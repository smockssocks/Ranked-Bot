from datetime import datetime
from sqlalchemy import String, Integer, Float, Boolean, DateTime, ForeignKey, JSON, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from bot.db.database import Base


class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(primary_key=True)
    lobby_id: Mapped[int | None] = mapped_column(ForeignKey("lobbies.id"), nullable=True)
    riot_match_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    region: Mapped[str] = mapped_column(String(8), server_default="na1")
    status: Mapped[str] = mapped_column(String(16), server_default="pending")  # pending/processed/error
    winner_team: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 1 or 2
    game_duration_secs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    team1_expected_win: Mapped[float | None] = mapped_column(Float, nullable=True)
    weight_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    raw_riot_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    processing_notes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    played_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    participants: Mapped[list["GameParticipant"]] = relationship("GameParticipant", back_populates="game", lazy="selectin")


class GameParticipant(Base):
    __tablename__ = "game_participants"
    __table_args__ = (UniqueConstraint("game_id", "player_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), nullable=False)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False)
    team: Mapped[int] = mapped_column(Integer, nullable=False)  # 1 or 2
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    champion_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    champion_name: Mapped[str | None] = mapped_column(String(32), nullable=True)
    win: Mapped[bool] = mapped_column(Boolean, nullable=False)
    kills: Mapped[int] = mapped_column(Integer, server_default="0")
    deaths: Mapped[int] = mapped_column(Integer, server_default="0")
    assists: Mapped[int] = mapped_column(Integer, server_default="0")
    cs_total: Mapped[int] = mapped_column(Integer, server_default="0")
    vision_score: Mapped[int] = mapped_column(Integer, server_default="0")
    damage_dealt: Mapped[int] = mapped_column(Integer, server_default="0")
    gold_earned: Mapped[int] = mapped_column(Integer, server_default="0")
    impact_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    lp_before: Mapped[int] = mapped_column(Integer, server_default="0")
    lp_after: Mapped[int] = mapped_column(Integer, server_default="0")
    lp_delta: Mapped[int] = mapped_column(Integer, server_default="0")
    mmr_before: Mapped[float] = mapped_column(Float, server_default="1000.0")
    mmr_after: Mapped[float] = mapped_column(Float, server_default="1000.0")

    game: Mapped["Game"] = relationship("Game", back_populates="participants")
    player: Mapped["Player"] = relationship("Player", back_populates="game_participants")
