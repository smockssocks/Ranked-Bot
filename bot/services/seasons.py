"""
Seasons.

The season number is saved in the database, so an admin can start a new one from Discord
(the New season button on the mod panel). CURRENT_SEASON in .env still works: whichever of
the two is higher wins, so raising it in .env and restarting also starts a new season.

What a new season does:
  * LP goes back to STARTING_LP and everyone plays placements again.
  * SEASON_CARRYOVER of each player's hidden rating (MMR) is kept, so teams and captains are
    fair from the first game. Role averages carry over too.
  * Last season's ratings, games and leaderboard stay in the database.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import config
from bot.models.lobby import Lobby
from bot.services import settings

SCOPE = "global"           # the season is the same for the whole bot


class SeasonError(Exception):
    pass


async def load(session: AsyncSession) -> int:
    """At startup: the saved season or the one in .env, whichever is higher."""
    saved = await settings.get_setting(session, SCOPE, settings.KEY_SEASON)
    season = max(int(saved) if saved and saved.isdigit() else 0, config.ENV_SEASON, 1)
    config.CURRENT_SEASON = season
    return season


async def start_new(session: AsyncSession) -> tuple[int, int]:
    """End the current season and start the next. Returns (old, new)."""
    busy = await session.scalar(select(Lobby).where(Lobby.status.in_(("drafting", "active"))))
    if busy is not None:
        raise SeasonError(f"Lobby #{busy.id} is still being played. Finish or cancel it first, so its "
                          f"result doesn't land in the wrong season.")
    old = config.CURRENT_SEASON
    new = old + 1
    await settings.set_setting(session, SCOPE, settings.KEY_SEASON, str(new))
    config.CURRENT_SEASON = new
    return old, new
