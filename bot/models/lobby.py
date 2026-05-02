from datetime import datetime
from sqlalchemy import String, Integer, Boolean, DateTime, ForeignKey, JSON, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from bot.db.database import Base


class Lobby(Base):
    __tablename__ = "lobbies"

    id: Mapped[int] = mapped_column(primary_key=True)
    guild_id: Mapped[str] = mapped_column(String(32), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(32), nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    host_discord_id: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), server_default="captain")  # captain / pick_order
    status: Mapped[str] = mapped_column(String(16), server_default="waiting")
    # waiting / drafting / active / completed / cancelled
    max_players: Mapped[int] = mapped_column(Integer, server_default="10")
    tournament_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    captain1_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True)
    captain2_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True)
    draft_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    players: Mapped[list["LobbyPlayer"]] = relationship("LobbyPlayer", back_populates="lobby", lazy="selectin")


class LobbyPlayer(Base):
    __tablename__ = "lobby_players"
    __table_args__ = (UniqueConstraint("lobby_id", "player_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lobby_id: Mapped[int] = mapped_column(ForeignKey("lobbies.id"), nullable=False)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False)
    preferred_role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    team: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assigned_role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    pick_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    lobby: Mapped["Lobby"] = relationship("Lobby", back_populates="players")
    player: Mapped["Player"] = relationship("Player", back_populates="lobby_players")
