from datetime import datetime
from sqlalchemy import String, Integer, Float, DateTime, ForeignKey, JSON, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from bot.db.database import Base


class PlayerRating(Base):
    """
    One row per (player, role, season). role is OVERALL or TOP/JUNGLE/MIDDLE/BOTTOM/UTILITY.
    lp is the visible ladder, mmr/rd the hidden Glicko-style skill estimate,
    perf_mean/perf_var the running performance score (consistency).
    """
    __tablename__ = "player_ratings"
    __table_args__ = (UniqueConstraint("player_id", "role", "season"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    season: Mapped[int] = mapped_column(Integer, server_default="1", default=1)
    lp: Mapped[int] = mapped_column(Integer, server_default="1000", default=1000)
    mmr: Mapped[float] = mapped_column(Float, server_default="1500.0", default=1500.0)
    rd: Mapped[float] = mapped_column(Float, server_default="350.0", default=350.0)
    games_played: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    wins: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    perf_mean: Mapped[float] = mapped_column(Float, server_default="0.0", default=0.0)
    perf_var: Mapped[float] = mapped_column(Float, server_default="1.0", default=1.0)
    peak_lp: Mapped[int] = mapped_column(Integer, server_default="1000", default=1000)
    streak: Mapped[int] = mapped_column(Integer, server_default="0", default=0)  # +n win streak / -n loss streak
    last_played_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    player: Mapped["Player"] = relationship("Player", back_populates="ratings")


class RoleBaseline(Base):
    """Community per-role metric baseline (Welford state) so PS is normalised to *this* server."""
    __tablename__ = "role_baselines"
    __table_args__ = (UniqueConstraint("role", "season"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    season: Mapped[int] = mapped_column(Integer, server_default="1", default=1)
    stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    sample_size: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
