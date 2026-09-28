from datetime import datetime
from sqlalchemy import String, Integer, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column
from bot.db.database import Base


class LinkRequest(Base):
    """
    A pending /link: the player must set their League profile icon to icon_id
    before expires_at to prove they own the account. One per Discord user.
    """
    __tablename__ = "link_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    discord_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    puuid: Mapped[str] = mapped_column(String(78), nullable=False)
    riot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    icon_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
