"""
Lobby lifecycle: create -> queue -> teams (captain draft / balanced / pick order)
-> optional drafter.lol draft -> game -> auto-detected or submitted result.
DB is the source of truth so the bot survives restarts mid-lobby.

Modes
  pick_order  The default, and the one that makes the ladder measure all-round skill.
              No role queue: teams are balanced on MMR alone and each player gets a
              champ-select pick position 1-5. Roles are claimed in champ select in pick
              order, and the role actually played is read from Riot's match data.
              Pick positions rotate: whoever has had late picks recently gets early ones.
  balanced    the bot searches all 126 team splits for the smallest MMR gap and best
              fit to stated role preferences (role queue).
  captain     two captains (highest MMR by default) snake-draft players, then roles are
              auto-suggested from preferences and captains can reassign with /inhouse role.
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
from bot.services import bans
from bot.services.rating_engine import ROLES, normalize_role, ROLE_DISPLAY

log = logging.getLogger("ranked-bot.lobby")

MODES = ("pick_order", "balanced", "captain")
ACTIVE_STATUSES = ("waiting", "drafting", "active")


class LobbyError(Exception):
    pass


# ------------------------------------------------------------------ #
# Create / cancel / lookup                                             #
# ------------------------------------------------------------------ #

async def create_lobby(session: AsyncSession, guild_id: str, channel_id: str, host_discord_id: str,
                       mode: str = "pick_order", max_players: int | None = None, ranked: bool = True) -> Lobby:
    if mode not in MODES:
        raise LobbyError(f"Unknown mode '{mode}'. Choose from: {', '.join(MODES)}")
    banned = await bans.active_ban(session, guild_id, host_discord_id)
    if banned is not None:
        raise LobbyError(bans.player_message(banned) + " You can't host a lobby while banned.")
    existing = await session.scalar(select(Lobby).where(
        Lobby.guild_id == guild_id, Lobby.channel_id == channel_id, Lobby.status.in_(ACTIVE_STATUSES)))
    if existing:
        raise LobbyError("A lobby is already active in this channel. Cancel or finish it first.")
    lobby = Lobby(guild_id=guild_id, channel_id=channel_id, host_discord_id=host_discord_id, mode=mode,
                  max_players=max_players or config.LOBBY_SIZE, ranked=ranked)
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
    banned = await bans.active_ban(session, lobby.guild_id, discord_id)
    if banned is not None:
        raise LobbyError(bans.player_message(banned))
    p = await session.scalar(select(Player).where(Player.discord_id == discord_id))
    if p is None or p.riot_puuid is None:
        raise LobbyError("You need to link your Riot account first: `/link GameName#TAG`.")
    if any(lp.player_id == p.id for lp in lobby.players):
        raise LobbyError("You are already in the queue.")
    if len(lobby.players) >= lobby.max_players:
        raise LobbyError("The lobby is full.")
    if lobby.mode == "pick_order":
        # No role queue: roles are claimed in champ select by pick position.
        preferred_role = secondary_role = None
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


def _require_host_or_admin(lobby: Lobby, requester_discord_id: str, is_admin: bool, action: str) -> None:
    if lobby.host_discord_id != requester_discord_id and not is_admin:
        raise LobbyError(f"Only the lobby host or an admin can {action}.")


async def force_queue(session: AsyncSession, lobby: Lobby, requester_discord_id: str, target_discord_id: str,
                      is_admin: bool = False, preferred_role: str | None = None,
                      secondary_role: str | None = None) -> LobbyPlayer:
    """Host or admin puts another player into the queue."""
    _require_host_or_admin(lobby, requester_discord_id, is_admin, "force-queue players")
    if lobby.status != "waiting":
        raise LobbyError("Teams are already made, so nobody else can join this lobby.")
    banned = await bans.active_ban(session, lobby.guild_id, target_discord_id)
    if banned is not None:
        raise LobbyError(bans.player_message(banned, you=False) + " A moderator can lift it with `/queueban lift`.")
    target = await session.scalar(select(Player).where(Player.discord_id == target_discord_id))
    if target is None or target.riot_puuid is None:
        raise LobbyError("That player hasn't linked a Riot account, so their games could not be scored. "
                         "They need to run `/link` first, or an admin can use `/admin link`.")
    if any(lp.player_id == target.id for lp in lobby.players):
        raise LobbyError(f"{target.discord_username} is already in the queue.")
    if len(lobby.players) >= lobby.max_players:
        raise LobbyError("The lobby is full.")
    return await queue_player(session, lobby, target_discord_id, preferred_role, secondary_role)


async def force_remove(session: AsyncSession, lobby: Lobby, requester_discord_id: str, target_discord_id: str,
                       is_admin: bool = False) -> Player:
    """Host or admin takes a player out of the queue, e.g. someone who went AFK."""
    _require_host_or_admin(lobby, requester_discord_id, is_admin, "remove players")
    if lobby.status != "waiting":
        raise LobbyError("Teams are already made. To change players, cancel the lobby and open a new one.")
    target = await session.scalar(select(Player).where(Player.discord_id == target_discord_id))
    lp = next((x for x in lobby.players if target and x.player_id == target.id), None)
    if lp is None:
        raise LobbyError("That player is not in this lobby.")
    await session.delete(lp)
    await session.commit()
    await session.refresh(lobby)
    return target


async def remove_banned_player(session: AsyncSession, guild_id: str,
                               discord_id: str) -> tuple[Lobby | None, Lobby | None]:
    """
    After a ban: take the player out of any lobby that is still filling.
    Returns (lobby they were removed from, lobby with teams already made that they are still in).
    A game already under way is left alone; the ban applies from their next queue.
    """
    p = await session.scalar(select(Player).where(Player.discord_id == discord_id))
    if p is None:
        return None, None
    rows = (await session.execute(
        select(Lobby).join(LobbyPlayer, LobbyPlayer.lobby_id == Lobby.id)
        .where(Lobby.guild_id == guild_id, LobbyPlayer.player_id == p.id,
               Lobby.status.in_(ACTIVE_STATUSES)))).scalars().unique().all()
    removed = started = None
    for lobby in rows:
        if lobby.status == "waiting":
            lp = next((x for x in lobby.players if x.player_id == p.id), None)
            if lp is not None:
                await session.delete(lp)
                await session.commit()
                await session.refresh(lobby)
                removed = lobby
        else:
            started = lobby
    return removed, started


# ------------------------------------------------------------------ #
# Test fillers                                                         #
# ------------------------------------------------------------------ #

FILLER_PREFIX = "filler-"


async def test_fill(session: AsyncSession, lobby: Lobby, count: int | None = None,
                    rng: random.Random | None = None) -> list[Player]:
    """
    Fill the lobby with filler players for testing. Fillers get a spread of ratings so
    balancing has something to do, and random role preferences for role-queue modes.
    Existing idle fillers are reused before new ones are made.
    """
    rng = rng or random.Random()
    if lobby.status != "waiting":
        raise LobbyError("Fillers can only be added while the lobby is filling.")
    room = lobby.max_players - len(lobby.players)
    n = room if count is None else min(count, room)
    if n <= 0:
        raise LobbyError("The lobby is already full.")
    from bot.services.game_processor import get_or_create_rating

    in_lobby = {lp.player_id for lp in lobby.players}
    fillers = (await session.execute(select(Player).where(Player.is_test.is_(True))
                                     .order_by(Player.id))).scalars().all()
    idle = [f for f in fillers if f.id not in in_lobby]
    next_no = 1 + max((int(f.discord_id[len(FILLER_PREFIX):]) for f in fillers
                       if f.discord_id.startswith(FILLER_PREFIX) and f.discord_id[len(FILLER_PREFIX):].isdigit()),
                      default=0)
    added: list[Player] = []
    for _ in range(n):
        if idle:
            f = idle.pop(0)
        else:
            f = Player(discord_id=f"{FILLER_PREFIX}{next_no}", discord_username=f"Filler {next_no}",
                       riot_puuid=f"{FILLER_PREFIX}{next_no}", summoner_name=f"Filler{next_no}#TEST",
                       is_test=True, link_verified=False)
            session.add(f)
            await session.flush()
            r = await get_or_create_rating(session, f.id, "OVERALL", config.CURRENT_SEASON)
            r.mmr = round(max(1100.0, min(1900.0, rng.gauss(config.STARTING_MMR, 180.0))), 1)
            next_no += 1
        roles = rng.sample(list(ROLES), 2)
        await queue_player(session, lobby, f.discord_id, roles[0], roles[1])
        added.append(f)
    return added


async def test_clear(session: AsyncSession) -> tuple[int, int]:
    """
    Remove every filler and everything that references one. Lobbies that already
    had teams made with fillers in them are cancelled, since those teams no longer
    exist. Returns (fillers removed, lobbies cancelled).
    """
    from sqlalchemy import delete, update
    from bot.models.game import GameParticipant
    from bot.models.rating import PlayerRating
    from bot.models.smurf import SmurfFlag

    ids = list((await session.execute(select(Player.id).where(Player.is_test.is_(True)))).scalars().all())
    if not ids:
        return 0, 0
    touched = (await session.execute(
        select(Lobby).join(LobbyPlayer, LobbyPlayer.lobby_id == Lobby.id)
        .where(LobbyPlayer.player_id.in_(ids), Lobby.status.in_(("drafting", "active"))))).scalars().unique().all()
    for lobby in touched:
        lobby.status = "cancelled"
    await session.execute(update(Lobby).where(Lobby.captain1_id.in_(ids)).values(captain1_id=None))
    await session.execute(update(Lobby).where(Lobby.captain2_id.in_(ids)).values(captain2_id=None))
    await session.execute(delete(LobbyPlayer).where(LobbyPlayer.player_id.in_(ids)))
    await session.execute(delete(PlayerRating).where(PlayerRating.player_id.in_(ids)))
    await session.execute(delete(GameParticipant).where(GameParticipant.player_id.in_(ids)))
    await session.execute(delete(SmurfFlag).where(SmurfFlag.player_id.in_(ids)))
    await session.execute(update(SmurfFlag).where(SmurfFlag.matched_player_id.in_(ids)).values(matched_player_id=None))
    await session.execute(delete(Player).where(Player.id.in_(ids)))
    await session.commit()
    return len(ids), len(touched)


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


PICK_PRIOR_GAMES = 2.0   # pseudo-games at the middle pick (3) so one unlucky game does not dominate
PICK_HISTORY_LIMIT = 20  # only recent pick-order games count toward priority


def pick_priority(player_ids: list[int], history: dict[int, list[int]], rng: random.Random | None = None) -> list[int]:
    """
    Order players for champ-select picks, earliest pick first.

    Fairness rule: whoever has been picking LATE recently picks EARLY now. Each
    player's average past pick position (1 = first, 5 = last) is shrunk toward the
    middle (3) by PICK_PRIOR_GAMES, so newcomers sit in the middle and one game
    cannot swing things. Higher average = more owed = earlier pick. Ties are
    broken at random so identical histories do not always resolve the same way.
    """
    rng = rng or random.Random()
    def owed(pid: int) -> float:
        past = history.get(pid, [])[-PICK_HISTORY_LIMIT:]
        return (sum(past) + PICK_PRIOR_GAMES * 3.0) / (len(past) + PICK_PRIOR_GAMES)
    return sorted(player_ids, key=lambda pid: (-owed(pid), rng.random()))


async def pick_history(session: AsyncSession, player_ids: list[int]) -> dict[int, list[int]]:
    """Past pick positions from completed pick-order lobbies, oldest first."""
    rows = (await session.execute(
        select(LobbyPlayer.player_id, LobbyPlayer.pick_order)
        .join(Lobby, Lobby.id == LobbyPlayer.lobby_id)
        .where(Lobby.mode == "pick_order", Lobby.status == "completed",
               LobbyPlayer.pick_order.is_not(None), LobbyPlayer.player_id.in_(player_ids))
        .order_by(Lobby.id)
    )).all()
    out: dict[int, list[int]] = {}
    for pid, pos in rows:
        out.setdefault(pid, []).append(int(pos))
    return out


def assign_pick_positions(team: list[LobbyPlayer], history: dict[int, list[int]],
                          rng: random.Random | None = None) -> None:
    """Give each player on a team a pick position 1-5. Clears any assigned role."""
    order = pick_priority([lp.player_id for lp in team], history, rng)
    pos = {pid: i + 1 for i, pid in enumerate(order)}
    for lp in team:
        lp.pick_order = pos[lp.player_id]
        lp.assigned_role = None


PICK_ORDER_GAP_TOLERANCE = 10.0  # MMR; splits this close to the fairest are treated as equal


def best_balanced_split(players: list[LobbyPlayer], ratings: dict[int, dict], use_roles: bool = True,
                        rng: random.Random | None = None) -> tuple[list[LobbyPlayer], list[LobbyPlayer]]:
    """
    Search all splits of 10 into 5v5.
    use_roles=True  (balanced mode): minimise MMR gap, trading it against role-preference fit.
    use_roles=False (pick order):    MMR gap only. Picks at random among splits within
                                     PICK_ORDER_GAP_TOLERANCE of the fairest, so the same ten
                                     people do not get identical teams every night.
    """
    n = len(players)
    half = n // 2
    idx = list(range(n))
    seen = set()
    if not use_roles:
        rng = rng or random.Random()
        splits = []
        for combo in itertools.combinations(idx, half):
            comp = tuple(i for i in idx if i not in combo)
            key = frozenset([combo, comp])
            if key in seen:
                continue
            seen.add(key)
            t1 = [players[i] for i in combo]
            t2 = [players[i] for i in comp]
            gap = abs(sum(ratings[p.player_id]["mmr"] for p in t1) - sum(ratings[p.player_id]["mmr"] for p in t2)) / half
            splits.append((gap, t1, t2))
        best_gap = min(g for g, _, _ in splits)
        close = [(t1, t2) for g, t1, t2 in splits if g <= best_gap + PICK_ORDER_GAP_TOLERANCE]
        t1, t2 = rng.choice(close)
        return list(t1), list(t2)
    best_key, best = None, None
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
    """balanced / pick_order modes: build teams (and roles or pick positions), set lobby active."""
    if len(lobby.players) < lobby.max_players:
        raise LobbyError(f"Need {lobby.max_players} players, have {len(lobby.players)}.")
    ratings = await lobby_ratings(session, lobby)
    pick_order = lobby.mode == "pick_order"
    team1, team2 = best_balanced_split(list(lobby.players), ratings, use_roles=not pick_order)
    if random.random() < 0.5:   # random side
        team1, team2 = team2, team1
    for lp in team1:
        lp.team = 1
    for lp in team2:
        lp.team = 2
    if pick_order:
        history = await pick_history(session, [lp.player_id for lp in lobby.players])
        assign_pick_positions(team1, history)
        assign_pick_positions(team2, history)
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

async def start_captain_draft(session: AsyncSession, lobby: Lobby, random_captains: bool = False,
                              captain_ids: tuple[int, int] | None = None) -> tuple[Player, Player]:
    """captain_ids = (blue captain, red captain) as player IDs, when chosen by a host or admin."""
    if len(lobby.players) < lobby.max_players:
        raise LobbyError(f"Need {lobby.max_players} players to start the draft.")
    ratings = await lobby_ratings(session, lobby)
    ids = [lp.player_id for lp in lobby.players]
    if captain_ids is not None:
        cap1_id, cap2_id = captain_ids
    elif random_captains:
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
            lp.team, lp.pick_order = None, None
    await session.commit()
    return await session.get(Player, cap1_id), await session.get(Player, cap2_id)


async def force_captains(session: AsyncSession, lobby: Lobby, requester_discord_id: str, is_admin: bool,
                         blue_captain_id: int, red_captain_id: int) -> tuple[Player, Player]:
    """
    Host or admin chooses the two captains. Works on a full lobby that is still filling
    (and starts the draft), or during a draft (which restarts it with the new captains).
    """
    _require_host_or_admin(lobby, requester_discord_id, is_admin, "choose captains")
    if lobby.mode != "captain":
        raise LobbyError("Captains are only used in Captain draft lobbies. Open one with "
                         "`/inhouse create mode:Captain draft`.")
    if lobby.status not in ("waiting", "drafting"):
        raise LobbyError("Teams are already set. Cancel the lobby and open a new one to change captains.")
    if blue_captain_id == red_captain_id:
        raise LobbyError("Pick two different captains.")
    in_lobby = {lp.player_id for lp in lobby.players}
    if blue_captain_id not in in_lobby or red_captain_id not in in_lobby:
        raise LobbyError("Both captains have to be in this lobby.")
    return await start_captain_draft(session, lobby, captain_ids=(blue_captain_id, red_captain_id))


def current_captain_id(lobby: Lobby) -> int | None:
    ds = lobby.draft_state or {}
    if lobby.status != "drafting" or ds.get("picks_made", 0) >= len(ds.get("pick_order", [])):
        return None
    turn = ds["pick_order"][ds["picks_made"]]
    return lobby.captain1_id if turn == 1 else lobby.captain2_id


async def captain_pick(session: AsyncSession, lobby: Lobby, requester_discord_id: str, pick_player_id: int,
                       is_admin: bool = False) -> dict:
    """
    The captain whose turn it is picks a player. The host or an admin may pick on that
    captain's behalf (a filler captain, or someone AFK), but never if they are the
    OTHER captain, so a host-captain cannot choose players for the opposing team.
    Fillers are the exception: picking for a filler captain is always allowed to a
    host or admin, so a draft can be tested alone.
    """
    if lobby.status != "drafting":
        raise LobbyError("No draft in progress.")
    ds = dict(lobby.draft_state or {})
    turn = ds["pick_order"][ds["picks_made"]]
    cap = await session.get(Player, lobby.captain1_id if turn == 1 else lobby.captain2_id)
    other_cap_id = lobby.captain2_id if turn == 1 else lobby.captain1_id
    if cap is None or cap.discord_id != requester_discord_id:
        requester = await session.scalar(select(Player).where(Player.discord_id == requester_discord_id))
        trusted = is_admin or requester_discord_id == lobby.host_discord_id
        is_other_captain = requester is not None and requester.id == other_cap_id
        if not trusted or (is_other_captain and not (cap is not None and cap.is_test)):
            raise LobbyError("It's not your turn to pick.")
    pick = await session.get(Player, pick_player_id)
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
    if lobby.mode == "pick_order":
        raise LobbyError("This is a pick order lobby: there are no assigned roles. "
                         "Players claim roles in champ select in pick order.")
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
        if p and p.riot_puuid and not p.is_test:     # fillers have no real Riot account
            out[p.riot_puuid] = p.id
    return out


async def complete_lobby(session: AsyncSession, lobby: Lobby) -> None:
    lobby.status = "completed"
    await session.commit()


def team_lines(team: list[LobbyPlayer], names: dict[int, str], mode: str = "balanced") -> list[str]:
    if mode == "pick_order":
        ordered = sorted(team, key=lambda x: x.pick_order or 99)
        return [f"**Pick {lp.pick_order}** {names.get(lp.player_id, '?')}" for lp in ordered]
    ordered = sorted(team, key=lambda x: ROLES.index(x.assigned_role) if x.assigned_role in ROLES else 99)
    return [f"**{ROLE_DISPLAY.get(lp.assigned_role, '?')}** {names.get(lp.player_id, '?')}" for lp in ordered]
