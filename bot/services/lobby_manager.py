"""
Lobby state management: queue, team balancing, tournament code integration.
Holds in-memory state per guild; DB is the source of truth for restarts.
"""
from __future__ import annotations
import asyncio
import logging
import random
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from bot.models.player import Player
from bot.models.lobby import Lobby, LobbyPlayer
from bot.models.rating import PlayerRating
from bot.config import LOBBY_SIZE, LOBBY_TIMEOUT_SECS

log = logging.getLogger("ranked-bot.lobby")

ROLES = ["TOP", "JUNGLE", "MID", "BOTTOM", "SUPPORT"]


class LobbyError(Exception):
    pass


# ------------------------------------------------------------------ #
# Create / cancel                                                      #
# ------------------------------------------------------------------ #

async def create_lobby(
    session: AsyncSession,
    guild_id: str,
    channel_id: str,
    host_discord_id: str,
    mode: str = "captain",
) -> Lobby:
    """Open a new lobby. Raises if one is already active in this channel."""
    existing = await session.scalar(
        select(Lobby).where(
            Lobby.guild_id == guild_id,
            Lobby.channel_id == channel_id,
            Lobby.status.in_(["waiting", "drafting", "active"]),
        )
    )
    if existing:
        raise LobbyError("A lobby is already active in this channel. Cancel it first.")

    lobby = Lobby(
        guild_id=guild_id,
        channel_id=channel_id,
        host_discord_id=host_discord_id,
        mode=mode,
    )
    session.add(lobby)
    await session.commit()
    await session.refresh(lobby)
    return lobby


async def cancel_lobby(
    session: AsyncSession,
    lobby_id: int,
    requestor_discord_id: str,
) -> Lobby:
    lobby = await _get_active_lobby(session, lobby_id)
    if lobby.host_discord_id != requestor_discord_id:
        raise LobbyError("Only the host can cancel the lobby.")
    lobby.status = "cancelled"
    await session.commit()
    return lobby


# ------------------------------------------------------------------ #
# Queue / dequeue                                                      #
# ------------------------------------------------------------------ #

async def queue_player(
    session: AsyncSession,
    lobby: Lobby,
    discord_id: str,
    preferred_role: str | None = None,
) -> LobbyPlayer:
    """Add a player to the queue. Raises if full or already in."""
    db_player = await session.scalar(select(Player).where(Player.discord_id == discord_id))
    if db_player is None:
        raise LobbyError("You are not registered. Ask an admin to link your Riot account first.")

    if db_player.riot_puuid is None:
        raise LobbyError("Your Riot account is not linked. Ask an admin to run /admin link.")

    already_in = await session.scalar(
        select(LobbyPlayer).where(
            LobbyPlayer.lobby_id == lobby.id,
            LobbyPlayer.player_id == db_player.id,
        )
    )
    if already_in:
        raise LobbyError("You are already in the queue.")

    if len(lobby.players) >= lobby.max_players:
        raise LobbyError("The lobby is full.")

    role = preferred_role.upper() if preferred_role else None
    if role and role not in ROLES:
        raise LobbyError(f"Invalid role. Choose from: {', '.join(ROLES)}")

    lp = LobbyPlayer(
        lobby_id=lobby.id,
        player_id=db_player.id,
        preferred_role=role,
    )
    session.add(lp)
    await session.commit()
    await session.refresh(lobby)
    return lp


async def dequeue_player(
    session: AsyncSession,
    lobby: Lobby,
    discord_id: str,
) -> None:
    db_player = await session.scalar(select(Player).where(Player.discord_id == discord_id))
    if db_player is None:
        raise LobbyError("You are not in the queue.")

    lp = await session.scalar(
        select(LobbyPlayer).where(
            LobbyPlayer.lobby_id == lobby.id,
            LobbyPlayer.player_id == db_player.id,
        )
    )
    if lp is None:
        raise LobbyError("You are not in this queue.")

    await session.delete(lp)
    await session.commit()


# ------------------------------------------------------------------ #
# Team balancing                                                       #
# ------------------------------------------------------------------ #

async def balance_teams(
    session: AsyncSession,
    lobby: Lobby,
) -> tuple[list[LobbyPlayer], list[LobbyPlayer]]:
    """
    Balance 10 queued players into 2 teams of 5.
    Minimizes the difference between team average LP ratings.
    Returns (team1_players, team2_players).
    """
    if len(lobby.players) < lobby.max_players:
        raise LobbyError(f"Need {lobby.max_players} players, have {len(lobby.players)}.")

    # Fetch LP for each player
    player_lps: list[tuple[LobbyPlayer, int]] = []
    for lp_row in lobby.players:
        rating = await session.scalar(
            select(PlayerRating).where(
                PlayerRating.player_id == lp_row.player_id,
                PlayerRating.role == "OVERALL",
            )
        )
        lp = rating.lp if rating else 0
        player_lps.append((lp_row, lp))

    # Sort by LP descending, then snake-draft: 1,2,2,1,1,2,2,1,1,2
    player_lps.sort(key=lambda x: x[1], reverse=True)
    team1: list[LobbyPlayer] = []
    team2: list[LobbyPlayer] = []
    snake = [1, 2, 2, 1, 1, 2, 2, 1, 1, 2]
    for (lp_row, _), t in zip(player_lps, snake):
        if t == 1:
            team1.append(lp_row)
            lp_row.team = 1
        else:
            team2.append(lp_row)
            lp_row.team = 2

    # Assign roles: match preferred roles first, fill gaps randomly
    _assign_roles(team1)
    _assign_roles(team2)

    lobby.status = "active"
    await session.commit()
    return team1, team2


def _assign_roles(team: list[LobbyPlayer]) -> None:
    """Greedy role assignment: honour preferences first, then fill remaining randomly."""
    remaining_roles = list(ROLES)
    unassigned: list[LobbyPlayer] = []

    # First pass: give players their preferred role if available
    for player in team:
        if player.preferred_role and player.preferred_role in remaining_roles:
            player.assigned_role = player.preferred_role
            remaining_roles.remove(player.preferred_role)
        else:
            unassigned.append(player)

    # Second pass: assign remaining roles randomly
    random.shuffle(remaining_roles)
    for player, role in zip(unassigned, remaining_roles):
        player.assigned_role = role


# ------------------------------------------------------------------ #
# Captain draft                                                        #
# ------------------------------------------------------------------ #

async def start_captain_draft(
    session: AsyncSession,
    lobby: Lobby,
) -> tuple[Player, Player]:
    """Randomly assign two captains from queued players and transition to drafting."""
    if len(lobby.players) < lobby.max_players:
        raise LobbyError(f"Need {lobby.max_players} players to start draft.")

    player_ids = [lp.player_id for lp in lobby.players]
    random.shuffle(player_ids)
    cap1_id, cap2_id = player_ids[0], player_ids[1]

    lobby.captain1_id = cap1_id
    lobby.captain2_id = cap2_id
    lobby.status = "drafting"
    lobby.draft_state = {
        "turn": 1,  # 1 = captain1's pick, 2 = captain2's pick
        "picks_made": 0,
        "pick_order": [1, 2, 2, 1, 1, 2, 2, 1, 1, 2],  # snake draft
        "pool": [p for p in player_ids if p not in (cap1_id, cap2_id)],
    }

    # Assign captains to their teams
    for lp in lobby.players:
        if lp.player_id == cap1_id:
            lp.team = 1
        elif lp.player_id == cap2_id:
            lp.team = 2

    await session.commit()

    cap1 = await session.get(Player, cap1_id)
    cap2 = await session.get(Player, cap2_id)
    return cap1, cap2


async def captain_pick(
    session: AsyncSession,
    lobby: Lobby,
    captain_discord_id: str,
    pick_discord_id: str,
) -> dict:
    """Captain picks a player. Returns updated draft state."""
    if lobby.status != "drafting":
        raise LobbyError("No draft in progress.")

    ds = lobby.draft_state
    current_turn = ds["pick_order"][ds["picks_made"]]

    # Identify which captain is acting
    cap1 = await session.get(Player, lobby.captain1_id)
    cap2 = await session.get(Player, lobby.captain2_id)
    acting_captain = cap1 if current_turn == 1 else cap2
    if acting_captain.discord_id != captain_discord_id:
        raise LobbyError("It's not your turn to pick.")

    # Find the player being picked
    pick_player = await session.scalar(select(Player).where(Player.discord_id == pick_discord_id))
    if pick_player is None:
        raise LobbyError("Player not found.")
    if pick_player.id not in ds["pool"]:
        raise LobbyError("That player is not available to pick.")

    # Assign team
    for lp in lobby.players:
        if lp.player_id == pick_player.id:
            lp.team = current_turn
            lp.pick_order = ds["picks_made"]
            break

    ds["pool"].remove(pick_player.id)
    ds["picks_made"] += 1

    if ds["picks_made"] >= len(ds["pick_order"]):
        # Draft complete
        lobby.status = "active"
        remaining = list(ds["pool"])
        for lp in lobby.players:
            if lp.player_id in remaining:
                lp.team = 1 if ds["picks_made"] % 2 == 0 else 2

        # Assign roles to each team
        team1 = [lp for lp in lobby.players if lp.team == 1]
        team2 = [lp for lp in lobby.players if lp.team == 2]
        _assign_roles(team1)
        _assign_roles(team2)

    await session.commit()
    return lobby.draft_state


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

async def get_active_lobby_for_guild(session: AsyncSession, guild_id: str) -> Lobby | None:
    return await session.scalar(
        select(Lobby).where(
            Lobby.guild_id == guild_id,
            Lobby.status.in_(["waiting", "drafting", "active"]),
        )
    )


async def _get_active_lobby(session: AsyncSession, lobby_id: int) -> Lobby:
    lobby = await session.get(Lobby, lobby_id)
    if not lobby:
        raise LobbyError("Lobby not found.")
    if lobby.status in ("completed", "cancelled"):
        raise LobbyError("This lobby is already closed.")
    return lobby
