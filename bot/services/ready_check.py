"""
Ready check: when a lobby fills, every player has to press Accept before it starts, like
League's queue pop.

  * Everyone accepts in time: the ready check ends and the lobby starts.
  * Someone declines (or leaves): they're out of the lobby and can't queue again for
    READY_CHECK_COOLDOWN_MINUTES. Everyone else keeps their spot and the queue reopens.
  * Time runs out: everyone who hadn't accepted is removed with the same cooldown.

Fillers from /admin testfill accept automatically, so one person can test the whole flow.

The deadline lives in the database, so a ready check survives a bot restart. Ending one is
a single conditional UPDATE, so when the last Accept and the timeout (or two last Accepts)
land together, exactly one of them gets to act on it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot import config
from bot.models.lobby import Lobby, LobbyPlayer
from bot.models.player import Player


class ReadyCheckError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def running(lobby: Lobby) -> bool:
    return lobby.ready_deadline is not None and lobby.status == "waiting"


def unix(dt: datetime) -> int:
    return int(_aware(dt).timestamp())


# --------------------------------------------------------------------------- #
# Cooldowns                                                                    #
# --------------------------------------------------------------------------- #

def cooldown_left(player: Player, now: datetime | None = None) -> datetime | None:
    """When the player may queue again, or None if they already can."""
    until = _aware(player.queue_cooldown_until)
    return until if until is not None and until > (now or _now()) else None


def cooldown_message(until: datetime) -> str:
    return (f"You declined or missed a ready check, so you can't queue until <t:{unix(until)}:t> "
            f"(<t:{unix(until)}:R>).")


def _give_cooldown(player: Player, now: datetime) -> None:
    minutes = config.READY_CHECK_COOLDOWN_MINUTES
    if minutes > 0 and not player.is_test:
        player.queue_cooldown_until = now + timedelta(minutes=minutes)


# --------------------------------------------------------------------------- #
# The check                                                                    #
# --------------------------------------------------------------------------- #

@dataclass
class Row:
    player: Player
    accepted: bool


async def _fresh(session: AsyncSession, lobby: Lobby) -> list[LobbyPlayer]:
    """The lobby's players as the database has them now. Accepts arrive from several button
    presses at once, each in its own session, so cached rows can be out of date."""
    return list((await session.execute(
        select(LobbyPlayer).where(LobbyPlayer.lobby_id == lobby.id)
        .execution_options(populate_existing=True))).scalars().all())


async def rows(session: AsyncSession, lobby: Lobby) -> list[Row]:
    """Each player in the lobby and whether they've accepted, in queue order."""
    out = []
    for lp in sorted(await _fresh(session, lobby), key=lambda x: (_aware(x.joined_at) or _now(), x.id)):
        p = await session.get(Player, lp.player_id)
        if p is not None:
            out.append(Row(p, lp.accepted_at is not None))
    return out


async def everyone_accepted(session: AsyncSession, lobby: Lobby) -> bool:
    players = await _fresh(session, lobby)
    return len(players) >= lobby.max_players and all(lp.accepted_at is not None for lp in players)


async def begin(session: AsyncSession, lobby: Lobby, now: datetime | None = None) -> None:
    """Start a ready check on a full lobby. Everyone has to accept again, except fillers."""
    if lobby.status != "waiting":
        raise ReadyCheckError("This lobby has already started.")
    if len(lobby.players) < lobby.max_players:
        raise ReadyCheckError("The lobby isn't full yet.")
    now = now or _now()
    lobby.ready_deadline = now + timedelta(seconds=config.READY_CHECK_SECONDS)
    lobby.ready_message_id = None
    for lp in lobby.players:
        p = await session.get(Player, lp.player_id)
        lp.accepted_at = now if p is not None and p.is_test else None
    await session.commit()


async def _lobby_player(session: AsyncSession, lobby: Lobby, discord_id: str) -> tuple[Player, LobbyPlayer]:
    p = await session.scalar(select(Player).where(Player.discord_id == discord_id))
    lp = next((x for x in lobby.players if p is not None and x.player_id == p.id), None)
    if lp is None:
        raise ReadyCheckError("You're not in this lobby, so there's nothing for you to accept.")
    return p, lp


async def accept(session: AsyncSession, lobby: Lobby, discord_id: str, now: datetime | None = None) -> bool:
    """Returns False if they had already accepted."""
    if not running(lobby):
        raise ReadyCheckError("This ready check is over.")
    _, lp = await _lobby_player(session, lobby, discord_id)
    if lp.accepted_at is not None:
        return False
    lp.accepted_at = now or _now()
    await session.commit()
    return True


async def end(session: AsyncSession, lobby: Lobby) -> bool:
    """Close the ready check. True only for the one caller that actually closed it."""
    res = await session.execute(update(Lobby).where(Lobby.id == lobby.id, Lobby.ready_deadline.is_not(None))
                                .values(ready_deadline=None))
    await session.commit()
    await session.refresh(lobby)
    return res.rowcount == 1


async def decline(session: AsyncSession, lobby: Lobby, discord_id: str, now: datetime | None = None) -> Player:
    """The player is out, with a cooldown; the ready check ends and the queue reopens."""
    if not running(lobby):
        raise ReadyCheckError("This ready check is over.")
    now = now or _now()
    p, lp = await _lobby_player(session, lobby, discord_id)
    if not await end(session, lobby):
        raise ReadyCheckError("This ready check is over.")
    await session.delete(lp)
    _give_cooldown(p, now)
    await session.commit()
    await session.refresh(lobby)
    return p


async def expire(session: AsyncSession, lobby: Lobby, now: datetime | None = None) -> list[Player] | None:
    """
    Time's up: remove everyone who didn't accept, with a cooldown, and return them. None if
    something else already ended the check. [] means everyone had accepted after all.
    """
    now = now or _now()
    if not await end(session, lobby):
        return None
    removed = []
    for lp in await _fresh(session, lobby):
        if lp.accepted_at is None:
            p = await session.get(Player, lp.player_id)
            await session.delete(lp)
            if p is not None:
                _give_cooldown(p, now)
                removed.append(p)
    await session.commit()
    await session.refresh(lobby)
    return removed


async def due(session: AsyncSession, now: datetime | None = None) -> list[Lobby]:
    now = now or _now()
    rows_ = (await session.execute(select(Lobby).where(
        Lobby.status == "waiting", Lobby.ready_deadline.is_not(None)))).scalars().all()
    return [l for l in rows_ if _aware(l.ready_deadline) <= now]


async def lobby_for_message(session: AsyncSession, message_id: int) -> Lobby | None:
    return await session.scalar(select(Lobby).where(Lobby.ready_message_id == str(message_id)))
