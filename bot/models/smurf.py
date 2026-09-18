from datetime import datetime
from sqlalchemy import String, Integer, Float, DateTime, ForeignKey, JSON, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from bot.db.database import Base


class SmurfFlag(Base):
    """A suspicion raised by the anti-smurf detector, reviewed by mods."""
    __tablename__ = "smurf_flags"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False)
    matched_player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # account_signal / fingerprint_match / performance_anomaly
    score: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), server_default="open", default="open")  # open/confirmed/dismissed
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mod_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
