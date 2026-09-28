"""
Queue bans: stop someone playing inhouses for a while. They can still use the rest of
the server. Bans end on their own when they expire; no background job is needed,
because every check compares against the current time.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models.ban import QueueBan

MAX_DURATION = timedelta(days=365)
PERMANENT_WORDS = {"permanent", "perm", "forever", "indefinite", "never"}
_UNITS = {
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "wk": 604800, "wks": 604800, "week": 604800, "weeks": 604800,
}
_PART = re.compile(r"(\d+)\s*([a-z]+)")
SUGGESTIONS = ("1 hour", "12 hours", "1 day", "3 days", "1 week", "2 weeks", "30 days", "permanent")


def parse_duration(text: str) -> timedelta | None:
    """
    "3d", "12h", "1w 2d", "90 minutes", "permanent" -> timedelta, or None for permanent.
    Raises ValueError with a message a moderator can act on.
    """
    t = (text or "").strip().lower()
    if t in PERMANENT_WORDS:
        return None
    parts = _PART.findall(t)
    leftover = _PART.sub("", t).replace(",", "").replace("and", "").strip()
    if not parts or leftover:
        raise ValueError("I couldn't read that duration. Use something like `30m`, `12h`, `3d`, `1w`, "
                         "`1w 2d`, or `permanent`.")
    seconds = 0
    for amount, unit in parts:
        if unit not in _UNITS:
            raise ValueError(f"Unknown time unit `{unit}`. Use m, h, d or w, e.g. `3d`.")
        seconds += int(amount) * _UNITS[unit]
    if seconds <= 0:
        raise ValueError("The duration has to be longer than zero.")
    if timedelta(seconds=seconds) > MAX_DURATION:
        raise ValueError("That's longer than a year. Use `permanent` instead.")
    return timedelta(seconds=seconds)


def format_duration(td: timedelta | None) -> str:
    if td is None:
        return "permanently"
    # Round UP to the minute: a fresh 3 day ban should read "3 days", not "2 days 23 hours".
    secs = math.ceil(td.total_seconds() / 60) * 60
    out = []
    for label, size in (("week", 604800), ("day", 86400), ("hour", 3600), ("minute", 60)):
        n, secs = divmod(secs, size)
        if n:
            out.append(f"{n} {label}{'s' if n != 1 else ''}")
    return " ".join(out[:2]) or "under a minute"


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_active(ban: QueueBan, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    exp = _aware(ban.expires_at)
    return ban.lifted_at is None and (exp is None or exp > now)


def remaining(ban: QueueBan, now: datetime | None = None) -> timedelta | None:
    now = now or datetime.now(timezone.utc)
    exp = _aware(ban.expires_at)
    return None if exp is None else max(timedelta(0), exp - now)


def when(ban: QueueBan) -> str:
    """Discord timestamp: shows in each reader's own time zone, and counts down."""
    exp = _aware(ban.expires_at)
    return "never (permanent)" if exp is None else f"<t:{int(exp.timestamp())}:f> (<t:{int(exp.timestamp())}:R>)"


async def active_ban(session: AsyncSession, guild_id: str, discord_id: str,
                     now: datetime | None = None) -> QueueBan | None:
    now = now or datetime.now(timezone.utc)
    rows = (await session.execute(select(QueueBan).where(
        QueueBan.guild_id == guild_id, QueueBan.discord_id == discord_id, QueueBan.lifted_at.is_(None),
        or_(QueueBan.expires_at.is_(None), QueueBan.expires_at > now)).order_by(QueueBan.id.desc()))).scalars().all()
    return next((b for b in rows if is_active(b, now)), None)


async def ban(session: AsyncSession, guild_id: str, discord_id: str, username: str, duration: timedelta | None,
              reason: str, banned_by: str, now: datetime | None = None) -> tuple[QueueBan, QueueBan | None]:
    """Create a ban. An existing active ban is closed and replaced. Returns (new ban, replaced ban)."""
    now = now or datetime.now(timezone.utc)
    old = await active_ban(session, guild_id, discord_id, now)
    if old is not None:
        old.lifted_at, old.lifted_by, old.lift_reason = now, banned_by, "Replaced by a new ban"
    new = QueueBan(guild_id=guild_id, discord_id=discord_id, username=username[:64], reason=reason.strip() or "No reason given",
                   banned_by=banned_by[:64], created_at=now, expires_at=None if duration is None else now + duration)
    session.add(new)
    await session.commit()
    return new, old


async def lift(session: AsyncSession, guild_id: str, discord_id: str, lifted_by: str, reason: str = "",
               now: datetime | None = None) -> QueueBan | None:
    now = now or datetime.now(timezone.utc)
    b = await active_ban(session, guild_id, discord_id, now)
    if b is None:
        return None
    b.lifted_at, b.lifted_by, b.lift_reason = now, lifted_by[:64], reason.strip() or None
    await session.commit()
    return b


async def list_active(session: AsyncSession, guild_id: str, now: datetime | None = None) -> list[QueueBan]:
    now = now or datetime.now(timezone.utc)
    rows = (await session.execute(select(QueueBan).where(QueueBan.guild_id == guild_id, QueueBan.lifted_at.is_(None))
                                  .order_by(QueueBan.expires_at))).scalars().all()
    return [b for b in rows if is_active(b, now)]


async def history(session: AsyncSession, guild_id: str, discord_id: str) -> list[QueueBan]:
    return list((await session.execute(select(QueueBan).where(
        QueueBan.guild_id == guild_id, QueueBan.discord_id == discord_id).order_by(QueueBan.id.desc()))).scalars().all())


def player_message(b: QueueBan, now: datetime | None = None, you: bool = True) -> str:
    who = "You're" if you else f"{b.username} is"
    if b.expires_at is None:
        span = "permanently banned from inhouses"
    else:
        span = f"banned from inhouses for another {format_duration(remaining(b, now))}, until {when(b)}"
    return f"{who} {span}. Reason: {b.reason}"
