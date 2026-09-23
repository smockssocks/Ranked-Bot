"""Runtime per-guild settings stored in the DB (fall back to env config)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import config
from bot.models.settings import GuildSetting

KEY_MOD_CHANNEL = "mod_channel_id"
KEY_QUEUE_CHANNEL = "queue_channel_id"
KEY_RESULTS_CHANNEL = "results_channel_id"
KEY_SEASON = "season"


async def get_setting(session: AsyncSession, guild_id: str, key: str, default: str | None = None) -> str | None:
    row = await session.scalar(select(GuildSetting).where(GuildSetting.guild_id == guild_id, GuildSetting.key == key))
    return row.value if row else default


async def set_setting(session: AsyncSession, guild_id: str, key: str, value: str) -> None:
    row = await session.scalar(select(GuildSetting).where(GuildSetting.guild_id == guild_id, GuildSetting.key == key))
    if row is None:
        session.add(GuildSetting(guild_id=guild_id, key=key, value=value))
    else:
        row.value = value
    await session.commit()


async def mod_channel_id(session: AsyncSession, guild_id: str) -> int:
    v = await get_setting(session, guild_id, KEY_MOD_CHANNEL)
    return int(v) if v else config.MOD_CHANNEL_ID


async def queue_channel_id(session: AsyncSession, guild_id: str) -> int:
    """The one channel inhouse queues live in. 0 means "anywhere"."""
    v = await get_setting(session, guild_id, KEY_QUEUE_CHANNEL)
    if v is not None:
        return int(v) if v else 0
    return config.QUEUE_CHANNEL_ID


def wrong_channel_message(queue_channel: int, current_channel: int) -> str | None:
    """
    Pure decision: should this inhouse command be refused here?
    Returns the message to show the player, or None when the command may run.
    """
    if not queue_channel or queue_channel == current_channel:
        return None
    return (f"Inhouse queues run in <#{queue_channel}>.\n"
            f"Head over there to join. Your rank commands like `/rank` and "
            f"`/leaderboard` still work anywhere.")


async def results_channel_id(session: AsyncSession, guild_id: str) -> int:
    v = await get_setting(session, guild_id, KEY_RESULTS_CHANNEL)
    return int(v) if v else config.RESULTS_CHANNEL_ID


async def current_season(session: AsyncSession, guild_id: str) -> int:
    v = await get_setting(session, guild_id, KEY_SEASON)
    return int(v) if v else config.CURRENT_SEASON
