from datetime import datetime
from sqlalchemy import String, Integer, Float, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from bot.db.database import Base


class PlayerRating(Base):
    __tablename__ = "player_ratings"
    __table_args__ = (UniqueConstraint("player_id", "role"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # OVERALL, TOP, JUNGLE, MID, BOTTOM, SUPPORT
    lp: Mapped[int] = mapped_column(Integer, server_default="0")
    mmr: Mapped[float] = mapped_column(Float, server_default="1000.0")
    games_played: Mapped[int] = mapped_column(Integer, server_default="0")
    wins: Mapped[int] = mapped_column(Integer, server_default="0")
    last_played_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    player: Mapped["Player"] = relationship("Player", back_populates="ratings")


class RoleBaseline(Base):
    """Running community averages per role — updated via Welford's algorithm after each game."""
    __tablename__ = "role_baselines"

    id: Mapped[int] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    avg_gold_diff: Mapped[float] = mapped_column(Float, server_default="0.0")
    avg_kp: Mapped[float] = mapped_column(Float, server_default="0.0")
    avg_vision: Mapped[float] = mapped_column(Float, server_default="0.0")
    avg_cs_pm: Mapped[float] = mapped_column(Float, server_default="0.0")
    sample_size: Mapped[int] = mapped_column(Integer, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
