"""
The season ladder. One ordering, used everywhere: /leaderboard, the #leaderboard channel,
and choosing captains, so "#1 on the leaderboard" means the same thing in all three.

Order: players with a ranked game this season, by LP, then hidden rating (MMR), then wins.
Players who haven't played yet this season aren't on the board; where they still need a
place (captain choice), they come after everyone on it, by MMR, which carries over from
last season.
"""
from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import config
from bot.models.player import Player
from bot.models.rating import PlayerRating


async def standings(session: AsyncSession, season: int | None = None,
                    limit: int | None = None) -> list[tuple[PlayerRating, Player]]:
    season = season or config.CURRENT_SEASON
    q = (select(PlayerRating, Player).join(Player, Player.id == PlayerRating.player_id)
         .where(PlayerRating.role == "OVERALL", PlayerRating.season == season,
                PlayerRating.games_played > 0, Player.is_test.is_(False))
         .order_by(desc(PlayerRating.lp), desc(PlayerRating.mmr), desc(PlayerRating.wins), Player.id))
    if limit:
        q = q.limit(limit)
    return [(r, p) for r, p in (await session.execute(q)).all()]


async def positions(session: AsyncSession, season: int | None = None) -> dict[int, int]:
    """player_id -> place on the ladder (1 = top)."""
    return {p.id: i for i, (_, p) in enumerate(await standings(session, season), 1)}


async def ladder_order(session: AsyncSession, player_ids: list[int], season: int | None = None) -> list[int]:
    """These players, best first: by ladder place, then (not on the ladder yet) by MMR."""
    season = season or config.CURRENT_SEASON
    place = await positions(session, season)
    mmr: dict[int, float] = {}
    for pid in player_ids:
        r = await session.scalar(select(PlayerRating).where(
            PlayerRating.player_id == pid, PlayerRating.role == "OVERALL", PlayerRating.season == season))
        if r is None:
            from bot.services.game_processor import get_or_create_rating
            r = await get_or_create_rating(session, pid, "OVERALL", season)   # carries last season over
        mmr[pid] = r.mmr
    return sorted(player_ids, key=lambda pid: (pid not in place, place.get(pid, 0), -mmr[pid], pid))
