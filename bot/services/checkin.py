"""
Post-game check-in. When a lobby's game is found, LP waits a few minutes while each player
confirms two things from dropdowns:

  * the role they actually played. Riot often leaves roles blank in custom games, and the
    role decides who a player is compared against, so this is what keeps scores and per-role
    stats right. These roles replace Riot's for scoring.
  * which champion on their team was theirs. Just a check: the bot already knows from the
    linked account. A different answer usually means someone played on another account, so
    the mods are told; it never changes whose game it was.

The game is scored when everyone has confirmed, when CHECKIN_MINUTES runs out (anyone who
didn't answer keeps the bot's best guess), or when the host or a mod presses Finish now.

The bot's guess for a role: Riot's, if Riot gave one; otherwise the role from the draft.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot import config
from bot.models.game import Game
from bot.models.lobby import Lobby, LobbyPlayer
from bot.models.player import Player
from bot.services import game_processor
from bot.services.rating_engine import ROLES

RIOT_ROLES = {"TOP": "TOP", "JUNGLE": "JUNGLE", "MIDDLE": "MIDDLE", "MID": "MIDDLE", "BOTTOM": "BOTTOM",
              "BOT": "BOTTOM", "ADC": "BOTTOM", "UTILITY": "UTILITY", "SUPPORT": "UTILITY"}


class CheckinError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def running(lobby: Lobby) -> bool:
    return lobby.status == "confirming" and lobby.checkin_deadline is not None


def riot_role(participant: dict) -> str | None:
    """Riot's position for a player, or None when Riot left it blank (common in custom games)."""
    raw = (participant.get("teamPosition") or participant.get("individualPosition") or "").upper()
    return RIOT_ROLES.get(raw)


@dataclass
class Row:
    player: Player
    lobby_player: LobbyPlayer
    team: int | None                 # 1 = blue, 2 = red
    guess_role: str | None
    guess_champion: str | None       # what their linked account played; None if it wasn't in the game
    role: str | None                 # confirmed, else the guess
    champion: str | None
    confirmed: bool


async def _game(session: AsyncSession, lobby: Lobby) -> Game | None:
    if not lobby.result_match_id:
        return None
    return await session.scalar(select(Game).where(Game.riot_match_id == lobby.result_match_id))


def _participants(game: Game | None) -> list[dict]:
    raw = (game.raw_riot_data or {}) if game else {}
    return (raw.get("match") or {}).get("info", {}).get("participants", [])


async def _fresh(session: AsyncSession, lobby: Lobby) -> list[LobbyPlayer]:
    return list((await session.execute(
        select(LobbyPlayer).where(LobbyPlayer.lobby_id == lobby.id)
        .execution_options(populate_existing=True))).scalars().all())


async def rows(session: AsyncSession, lobby: Lobby) -> list[Row]:
    parts = {p.get("puuid"): p for p in _participants(await _game(session, lobby))}
    out = []
    for lp in sorted(await _fresh(session, lobby), key=lambda x: (x.team or 9, x.pick_order or 0, x.id)):
        p = await session.get(Player, lp.player_id)
        if p is None:
            continue
        part = parts.get(p.riot_puuid)
        team = (1 if part["teamId"] == 100 else 2) if part else lp.team
        guess_role = (riot_role(part) if part else None) or lp.assigned_role
        guess_champ = part.get("championName") if part else None
        out.append(Row(p, lp, team, guess_role, guess_champ,
                       lp.confirmed_role or guess_role, lp.confirmed_champion or guess_champ,
                       lp.confirmed_at is not None))
    return out


async def team_champions(session: AsyncSession, lobby: Lobby, team: int | None) -> list[str]:
    """The champions played on this side of the game, for the champion dropdown."""
    want = 100 if team == 1 else 200
    return [p.get("championName") or "?" for p in _participants(await _game(session, lobby))
            if team is None or p.get("teamId") == want]


async def begin(session: AsyncSession, lobby: Lobby, match_id: str, match: dict, timeline: dict | None,
                submitted_by: str = "auto-detect", auto_detected: bool = True, now: datetime | None = None) -> None:
    """The lobby's game was found: hold it, and ask the players to confirm."""
    now = now or _now()
    game = await session.scalar(select(Game).where(Game.riot_match_id == match_id))
    if game is None:
        game = Game(riot_match_id=match_id, region=config.RIOT_REGION, lobby_id=lobby.id, season=config.CURRENT_SEASON,
                    submitted_by=submitted_by, submitted_at=now, auto_detected=auto_detected, status="pending")
        session.add(game)
    elif game.status not in ("pending", "error", "rolled_back"):
        raise CheckinError(f"Game {match_id} has already been recorded.")
    game.lobby_id = lobby.id
    game.raw_riot_data = {"match": match, "timeline": timeline or {"info": {"frames": []}}}
    lobby.status = "confirming"
    lobby.result_match_id = match_id
    lobby.checkin_deadline = now + timedelta(minutes=config.CHECKIN_MINUTES)
    lobby.checkin_message_id = None
    for lp in lobby.players:
        lp.confirmed_role = lp.confirmed_champion = None
        lp.confirmed_at = None
    await session.commit()


async def confirm(session: AsyncSession, lobby: Lobby, discord_id: str, role: str, champion: str | None,
                  now: datetime | None = None) -> Row:
    if not running(lobby):
        raise CheckinError("This check-in is over.")
    if role not in ROLES:
        raise CheckinError("Choose a role.")
    mine = next((r for r in await rows(session, lobby) if r.player.discord_id == discord_id), None)
    if mine is None:
        raise CheckinError("You weren't in this game.")
    if champion is not None and champion not in await team_champions(session, lobby, mine.team):
        raise CheckinError("Choose one of your team's champions.")
    mine.lobby_player.confirmed_role = role
    mine.lobby_player.confirmed_champion = champion
    mine.lobby_player.confirmed_at = now or _now()
    await session.commit()
    return mine


async def everyone_confirmed(session: AsyncSession, lobby: Lobby) -> bool:
    rs = [r for r in await rows(session, lobby) if not r.player.is_test]
    return all(r.confirmed for r in rs)


def duplicate_roles(rs: list[Row]) -> list[tuple[int, str]]:
    """(team, role) taken by more than one player, so the check-in can warn about it."""
    seen: dict[tuple[int, str], int] = {}
    for r in rs:
        if r.team and r.role:
            seen[(r.team, r.role)] = seen.get((r.team, r.role), 0) + 1
    return sorted(k for k, n in seen.items() if n > 1)


def mismatches(rs: list[Row]) -> list[Row]:
    """Players whose chosen champion isn't what their linked account played."""
    return [r for r in rs if r.confirmed and r.lobby_player.confirmed_champion
            and r.lobby_player.confirmed_champion != r.guess_champion]


async def end(session: AsyncSession, lobby: Lobby) -> bool:
    """Close the check-in. True only for the one caller that actually closed it."""
    res = await session.execute(update(Lobby).where(Lobby.id == lobby.id, Lobby.checkin_deadline.is_not(None))
                                .values(checkin_deadline=None))
    await session.commit()
    await session.refresh(lobby)
    return res.rowcount == 1


async def finalize(session: AsyncSession, lobby: Lobby) -> game_processor.ProcessResult:
    """Score the game with the confirmed roles (or the guesses) and close the lobby."""
    game = await _game(session, lobby)
    if game is None or not (game.raw_riot_data or {}).get("match"):
        raise CheckinError("The game's data is missing. A mod can submit it again.")
    overrides = {r.player.riot_puuid: r.role for r in await rows(session, lobby)
                 if r.player.riot_puuid and r.role and r.guess_champion is not None}
    raw = game.raw_riot_data
    try:
        result = await game_processor.process_match(
            session, lobby.result_match_id, submitted_by=game.submitted_by or "check-in", lobby_id=lobby.id,
            auto_detected=bool(game.auto_detected), match_data=raw["match"], timeline_data=raw.get("timeline"),
            ranked=lobby.ranked, role_overrides=overrides)
    finally:
        lobby.status = "completed"
        await session.commit()
    return result


async def due(session: AsyncSession, now: datetime | None = None) -> list[Lobby]:
    now = now or _now()
    found = (await session.execute(select(Lobby).where(
        Lobby.status == "confirming", Lobby.checkin_deadline.is_not(None)))).scalars().all()
    return [l for l in found if _aware(l.checkin_deadline) <= now]


async def lobby_for_message(session: AsyncSession, message_id: int) -> Lobby | None:
    return await session.scalar(select(Lobby).where(Lobby.checkin_message_id == str(message_id)))
