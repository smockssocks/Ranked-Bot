"""
Lobby lifecycle: create -> queue -> teams (captain draft / balanced / pick order)
-> optional drafter.lol draft -> game -> auto-detected or submitted result.
DB is the source of truth so the bot survives restarts mid-lobby.

Modes
  captain     two captains (highest MMR by default) snake-draft players, then roles are
              auto-suggested from preferences and captains can reassign with /inhouse role.
  balanced    the bot searches all 126 team splits for the smallest MMR gap and best role fit.
  pick_order  balanced teams, but roles inside each team go by queue join order
              (first come first serve).
"""
from __future__ import annotations

import itertools
import logging
import random
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import config
from bot.models.lobby import Lobby, LobbyPlayer
from bot.models.player import Player
from bot.models.rating import PlayerRating
from bot.services.rating_engine import ROLES, normalize_role, ROLE_DISPLAY

log = logging.getLogger("ranked-bot.lobby")

MODES = ("captain", "balanced", "pick_order")
ACTIVE_STATUSES = ("waiting", "drafting", "active")


class LobbyError(Exception):
    pass


# ------------------------------------------------------------------ #
# Create / cancel / lookup                                             #
# ------------------------------------------------------------------ #

async def create_lobby(session: AsyncSession, guild_id: str, channel_id: str, host_discord_id: str,
                       mode: str = "captain", max_players: int | None = None) -> Lobby:
    if mode not in MODES:
        raise LobbyError(f"Unknown mode '{mode}'. Choose from: {', '.join(MODES)}")
    existing = await session.scalar(select(Lobby).where(
        Lobby.guild_id == guild_id, Lobby.channel_id == channel_id, Lobby.status.in_(ACTIVE_STATUSES)))
    if existing:
        raise LobbyError("A lobby is already active in this channel. Cancel or finish it first.")
    lobby = Lobby(guild_id=guild_id, channel_id=channel_id, host_discord_id=host_discord_id, mode=mode,
                  max_players=max_players or config.LOBBY_SIZE)
    session.add(lobby)
    await session.commit()
    await session.refresh(lobby)
    return lobby


async def cancel_lobby(session: AsyncSession, lobby: Lobby, requestor_discord_id: str, is_admin: bool = False) -> Lobby:
    if lobby.host_discord_id != requestor_discord_id and not is_admin:
        raise LobbyError("Only the host (or an admin) can cancel the lobby.")
    lobby.status = "cancelled"
    await session.commit()
    return lobby


async def get_active_lobby(session: AsyncSession, guild_id: str, channel_id: str | None = None) -> Lobby | None:
    q = select(Lobby).where(Lobby.guild_id == guild_id, Lobby.status.in_(ACTIVE_STATUSES))
    if channel_id:
        q = q.where(Lobby.channel_id == channel_id)
    return await session.scalar(q.order_by(Lobby.id.desc()))


async def get_active_lobby_for_guild(session: AsyncSession, guild_id: str) -> Lobby | None:
    return await get_active_lobby(session, guild_id)


async def active_lobbies(session: AsyncSession) -> list[Lobby]:
    return list((await session.execute(select(Lobby).where(Lobby.status == "active"))).scalars().all())


# ------------------------------------------------------------------ #
# Queue                                                                #
# ------------------------------------------------------------------ #

async def queue_player(session: AsyncSession, lobby: Lobby, discord_id: str,
                       preferred_role: str | None = None, secondary_role: str | None = None) -> LobbyPlayer:
    if lobby.status != "waiting":
        raise LobbyError("The lobby is no longer accepting players.")
    p = await session.scalar(select(Player).where(Player.discord_id == discord_id))
    if p is None or p.riot_puuid is None:
        raise LobbyError("You need to link your Riot account first: `/link GameName#TAG`.")
    if any(lp.player_id == p.id for lp in lobby.players):
        raise LobbyError("You are already in the queue.")
    if len(lobby.players) >= lobby.max_players:
        raise LobbyError("The lobby is full.")
    pref = normalize_role(preferred_role) if preferred_role else None
    sec = normalize_role(secondary_role) if secondary_role else None
    lp = LobbyPlayer(lobby_id=lobby.id, player_id=p.id, preferred_role=pref, secondary_role=sec,
                     joined_at=datetime.now(timezone.utc))
    session.add(lp)
    await session.commit()
    await session.refresh(lobby)
    return lp


async def dequeue_player(session: AsyncSession, lobby: Lobby, discord_id: str) -> None:
    if lobby.status != "waiting":
        raise LobbyError("Teams are already being made; ask the host to cancel if needed.")
    p = await session.scalar(select(Player).where(Player.discord_id == discord_id))
    lp = next((x for x in lobby.players if p and x.player_id == p.id), None)
    if lp is None:
        raise LobbyError("You are not in this queue.")
    await session.delete(lp)
    await session.commit()
    await session.refresh(lobby)


# ------------------------------------------------------------------ #
# Ratings lookup                                                       #
# ------------------------------------------------------------------ #

async def lobby_ratings(session: AsyncSession, lobby: Lobby, season: int | None = None) -> dict[int, dict]:
    """player_id -> {mmr, lp, games, role_games: {role: games}, name}"""
    season = season or config.CURRENT_SEASON
    out: dict[int, dict] = {}
    for lp in lobby.players:
        rows = (await session.execute(select(PlayerRating).where(
            PlayerRating.player_id == lp.player_id, PlayerRating.season == season))).scalars().all()
        overall = next((r for r in rows if r.role == "OVERALL"), None)
        p = await session.get(Player, lp.player_id)
        out[lp.player_id] = {
            "mmr": overall.mmr if overall else config.STARTING_MMR,
            "lp": overall.lp if overall else config.STARTING_LP,
            "games": overall.games_played if overall else 0,
            "role_games": {r.role: r.games_played for r in rows if r.role != "OVERALL"},
            "role_mmr": {r.role: r.mmr for r in rows if r.role != "OVERALL"},
            "name": p.discord_username if p else str(lp.player_id),
        }
    return out


# ------------------------------------------------------------------ #
# Role assignment                                                      #
# ------------------------------------------------------------------ #

def _role_fit(lp: LobbyPlayer, role: str, info: dict | None) -> float:
    score = 0.0
    if lp.preferred_role == role:
        score += 3.0
    elif lp.secondary_role == role:
        score += 2.0
    elif lp.preferred_role or lp.secondary_role:
        score -= 0.5   # they asked for something else
    if info:
        games = info.get("role_games", {}).get(role, 0)
        score += min(1.0, games / 10.0)
    return score


def assign_roles_best_fit(team: list[LobbyPlayer], ratings: dict[int, dict] | None = None) -> float:
    """Assign ROLES to a 5-player team maximising total role fit. Returns the fit score."""
    ratings = ratings or {}
    best, best_perm = -1e9, None
    for perm in itertools.permutations(ROLES):
        s = sum(_role_fit(lp, role, ratings.get(lp.player_id)) for lp, role in zip(team, perm))
        if s > best:
            best, best_perm = s, perm
    for lp, role in zip(team, best_perm or ROLES):
        lp.assigned_role = role
    return best


def _aware(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def assign_roles_pick_order(team: list[LobbyPlayer]) -> None:
    """First come first serve: earlier joiners get their preferred role."""
    ordered = sorted(team, key=lambda x: _aware(x.joined_at))
    free = list(ROLES)
    later: list[LobbyPlayer] = []
    for lp in ordered:
        if lp.preferred_role in free:
            lp.assigned_role = lp.preferred_role
            free.remove(lp.preferred_role)
        elif lp.secondary_role in free:
            lp.assigned_role = lp.secondary_role
            free.remove(lp.secondary_role)
        else:
            later.append(lp)
    for lp in later:
        lp.assigned_role = free.pop(0)


def best_balanced_split(players: list[LobbyPlayer], ratings: dict[int, dict]) -> tuple[list[LobbyPlayer], list[LobbyPlayer]]:
    """Search all splits of 10 into 5v5; minimise MMR gap, break ties with role fit."""
    n = len(players)
    half = n // 2
    best_key, best = None, None
    idx = list(range(n))
    seen = set()
    for combo in itertools.combinations(idx, half):
        comp = tuple(i for i in idx if i not in combo)
        key = frozenset([combo, comp])
        if key in seen:
            continue
        seen.add(key)
        t1 = [players[i] for i in combo]
        t2 = [players[i] for i in comp]
        gap = abs(sum(ratings[p.player_id]["mmr"] for p in t1) - sum(ratings[p.player_id]["mmr"] for p in t2)) / half
        fit = assign_roles_best_fit(t1, ratings) + assign_roles_best_fit(t2, ratings)
        # 10 MMR of gap is worth about 1 point of role fit
        score = gap / 10.0 - fit
        if best_key is None or score < best_key:
            best_key, best = score, (list(t1), list(t2))
    return best  # type: ignore[return-value]


async def make_teams(session: AsyncSession, lobby: Lobby) -> tuple[list[LobbyPlayer], list[LobbyPlayer]]:
    """balanced / pick_order modes: build teams and roles, set lobby active."""
    if len(lobby.players) < lobby.max_players:
        raise LobbyError(f"Need {lobby.max_players} players, have {len(lobby.players)}.")
    ratings = await lobby_ratings(session, lobby)
    team1, team2 = best_balanced_split(list(lobby.players), ratings)
    if random.random() < 0.5:   # random side
        team1, team2 = team2, team1
    for lp in team1:
        lp.team = 1
    for lp in team2:
        lp.team = 2
    if lobby.mode == "pick_order":
        assign_roles_pick_order(team1)
        assign_roles_pick_order(team2)
    else:
        assign_roles_best_fit(team1, ratings)
        assign_roles_best_fit(team2, ratings)
    lobby.team1_name, lobby.team2_name = "Blue", "Red"
    lobby.status = "active"
    lobby.started_at = datetime.now(timezone.utc)
    await session.commit()
    return team1, team2


# ------------------------------------------------------------------ #
# Captain draft                                                        #
# ------------------------------------------------------------------ #

async def start_captain_draft(session: AsyncSession, lobby: Lobby, random_captains: bool = False) -> tuple[Player, Player]:
    if len(lobby.players) < lobby.max_players:
        raise LobbyError(f"Need {lobby.max_players} players to start the draft.")
    ratings = await lobby_ratings(session, lobby)
    ids = [lp.player_id for lp in lobby.players]
    if random_captains:
        random.shuffle(ids)
        cap1_id, cap2_id = ids[0], ids[1]
    else:
        ordered = sorted(ids, key=lambda pid: ratings[pid]["mmr"], reverse=True)
        cap1_id, cap2_id = ordered[0], ordered[1]
        if random.random() < 0.5:
            cap1_id, cap2_id = cap2_id, cap1_id
    lobby.captain1_id, lobby.captain2_id = cap1_id, cap2_id
    lobby.status = "drafting"
    pool = [pid for pid in ids if pid not in (cap1_id, cap2_id)]
    # snake for 8 picks: 1 2 2 1 1 2 2 1
    lobby.draft_state = {"picks_made": 0, "pick_order": [1, 2, 2, 1, 1, 2, 2, 1][: len(pool)], "pool": pool}
    for lp in lobby.players:
        if lp.player_id == cap1_id:
            lp.team, lp.pick_order = 1, -1
        elif lp.player_id == cap2_id:
            lp.team, lp.pick_order = 2, -1
        else:
            lp.team = None
    await session.commit()
    return await session.get(Player, cap1_id), await session.get(Player, cap2_id)


def current_captain_id(lobby: Lobby) -> int | None:
    ds = lobby.draft_state or {}
    if lobby.status != "drafting" or ds.get("picks_made", 0) >= len(ds.get("pick_order", [])):
        return None
    turn = ds["pick_order"][ds["picks_made"]]
    return lobby.captain1_id if turn == 1 else lobby.captain2_id


async def captain_pick(session: AsyncSession, lobby: Lobby, captain_discord_id: str, pick_discord_id: str) -> dict:
    if lobby.status != "drafting":
        raise LobbyError("No draft in progress.")
    ds = dict(lobby.draft_state or {})
    turn = ds["pick_order"][ds["picks_made"]]
    cap = await session.get(Player, lobby.captain1_id if turn == 1 else lobby.captain2_id)
    if cap is None or cap.discord_id != captain_discord_id:
        raise LobbyError("It's not your turn to pick.")
    pick = await session.scalar(select(Player).where(Player.discord_id == pick_discord_id))
    if pick is None or pick.id not in ds["pool"]:
        raise LobbyError("That player is not available to pick.")
    for lp in lobby.players:
        if lp.player_id == pick.id:
            lp.team, lp.pick_order = turn, ds["picks_made"]
    ds["pool"] = [x for x in ds["pool"] if x != pick.id]
    ds["picks_made"] += 1
    if ds["picks_made"] >= len(ds["pick_order"]):
        # last player (if any) goes to the team with fewer players
        for pid in ds["pool"]:
            t1 = sum(1 for lp in lobby.players if lp.team == 1)
            t2 = sum(1 for lp in lobby.players if lp.team == 2)
            for lp in lobby.players:
                if lp.player_id == pid:
                    lp.team = 1 if t1 <= t2 else 2
        ds["pool"] = []
        ratings = await lobby_ratings(session, lobby)
        assign_roles_best_fit([lp for lp in lobby.players if lp.team == 1], ratings)
        assign_roles_best_fit([lp for lp in lobby.players if lp.team == 2], ratings)
        lobby.team1_name = f"Team {ratings[lobby.captain1_id]['name']}"[:35]
        lobby.team2_name = f"Team {ratings[lobby.captain2_id]['name']}"[:35]
        lobby.status = "active"
        lobby.started_at = datetime.now(timezone.utc)
    lobby.draft_state = ds
    await session.commit()
    return ds


async def set_role(session: AsyncSession, lobby: Lobby, requester_discord_id: str, target_discord_id: str,
                   role: str, is_admin: bool = False) -> LobbyPlayer:
    """Captain (or host/admin) moves a teammate to a role; swaps with whoever had it."""
    if lobby.status != "active":
        raise LobbyError("Teams are not set yet.")
    role = normalize_role(role)
    target = await session.scalar(select(Player).where(Player.discord_id == target_discord_id))
    tlp = next((lp for lp in lobby.players if target and lp.player_id == target.id), None)
    if tlp is None or tlp.team is None:
        raise LobbyError("That player is not on a team in this lobby.")
    if not is_admin and requester_discord_id != lobby.host_discord_id:
        req = await session.scalar(select(Player).where(Player.discord_id == requester_discord_id))
        cap_id = lobby.captain1_id if tlp.team == 1 else lobby.captain2_id
        if req is None or req.id != cap_id:
            raise LobbyError("Only that team's captain, the host or an admin can change roles.")
    holder = next((lp for lp in lobby.players if lp.team == tlp.team and lp.assigned_role == role), None)
    if holder and holder is not tlp:
        holder.assigned_role = tlp.assigned_role
    tlp.assigned_role = role
    await session.commit()
    return tlp


# ------------------------------------------------------------------ #
# Misc                                                                 #
# ------------------------------------------------------------------ #

async def lobby_puuids(session: AsyncSession, lobby: Lobby) -> dict[str, int]:
    """puuid -> player_id for everyone on a team."""
    out: dict[str, int] = {}
    for lp in lobby.players:
        p = await session.get(Player, lp.player_id)
        if p and p.riot_puuid:
            out[p.riot_puuid] = p.id
    return out


async def complete_lobby(session: AsyncSession, lobby: Lobby) -> None:
    lobby.status = "completed"
    await session.commit()


def team_lines(team: list[LobbyPlayer], names: dict[int, str]) -> list[str]:
    ordered = sorted(team, key=lambda x: ROLES.index(x.assigned_role) if x.assigned_role in ROLES else 99)
    return [f"**{ROLE_DISPLAY.get(lp.assigned_role, '?')}** {names.get(lp.player_id, '?')}" for lp in ordered]
