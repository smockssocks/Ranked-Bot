from datetime import datetime
from sqlalchemy import String, DateTime, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from bot.db.database import Base


class GuildSetting(Base):
    """Per-guild key/value settings changed at runtime (mod channel, results channel, ...)."""
    __tablename__ = "guild_settings"
    __table_args__ = (UniqueConstraint("guild_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    guild_id: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
