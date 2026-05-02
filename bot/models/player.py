from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from bot.db.database import Base


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)
    discord_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    discord_username: Mapped[str] = mapped_column(String(64), nullable=False)
    riot_puuid: Mapped[str | None] = mapped_column(String(78), unique=True, nullable=True)
    summoner_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    riot_region: Mapped[str] = mapped_column(String(8), server_default="na1")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    ratings: Mapped[list["PlayerRating"]] = relationship("PlayerRating", back_populates="player", lazy="selectin")
    game_participants: Mapped[list["GameParticipant"]] = relationship("GameParticipant", back_populates="player")
    lobby_players: Mapped[list["LobbyPlayer"]] = relationship("LobbyPlayer", back_populates="player")
