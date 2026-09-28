from datetime import datetime
from sqlalchemy import String, DateTime, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from bot.db.database import Base


class QueueBan(Base):
    """
    A ban from playing inhouses (not from the Discord server). Keyed by Discord ID so
    people who never linked an account can be banned too. A ban is active while it is
    not lifted and not expired; expires_at NULL means permanent. Lifted and expired
    bans are kept as history.
    """
    __tablename__ = "queue_bans"

    id: Mapped[int] = mapped_column(primary_key=True)
    guild_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    discord_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    banned_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lifted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lift_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
