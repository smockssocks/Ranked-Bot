"""Runtime per-guild settings stored in the DB (fall back to env config)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import config
from bot.models.settings import GuildSetting

KEY_MOD_CHANNEL = "mod_channel_id"
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


async def results_channel_id(session: AsyncSession, guild_id: str) -> int:
    v = await get_setting(session, guild_id, KEY_RESULTS_CHANNEL)
    return int(v) if v else config.RESULTS_CHANNEL_ID


async def current_season(session: AsyncSession, guild_id: str) -> int:
    v = await get_setting(session, guild_id, KEY_SEASON)
    return int(v) if v else config.CURRENT_SEASON
