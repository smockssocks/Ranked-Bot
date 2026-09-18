from datetime import datetime
from sqlalchemy import String, Integer, DateTime, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from bot.db.database import Base


class ChatMessage(Base):
    """Short-term memory: recent turns per (channel, user)."""
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    guild_id: Mapped[str] = mapped_column(String(32), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(32), nullable=False)
    discord_id: Mapped[str] = mapped_column(String(32), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user / assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatMemory(Base):
    """Long-term memory: a rolling summary per user, refreshed every N turns (cheap)."""
    __tablename__ = "chat_memories"
    __table_args__ = (UniqueConstraint("guild_id", "discord_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    guild_id: Mapped[str] = mapped_column(String(32), nullable=False)
    discord_id: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, server_default="", default="")
    turns_since_summary: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    total_turns: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ChatUsage(Base):
    """Daily token spend so the OpenRouter bill stays bounded."""
    __tablename__ = "chat_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)  # YYYY-MM-DD (UTC)
    prompt_tokens: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    requests: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
