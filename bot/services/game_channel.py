"""
A private space for each started inhouse.

  * A private thread under the queue channel (#inhouse-queue), named after the host, e.g.
    "Dan's inhouse". Discord shows a private thread only to the people added to it and to
    anyone with Manage Threads (Administrators, and the Inhouse Mod role made by /admin
    setup). The bot adds the lobby's players and host, and posts the teams, the draft, and
    how to get into the custom game there, so passwords and tournament codes are never public.
  * Two temporary voice channels, Blue and Red. Anyone can see them, only that team and staff
    can join. Players already sitting in voice are moved in when teams are set.

When the game has been over for GAME_CHANNEL_CLEANUP_MINUTES, the thread is archived and
locked (kept, so staff can read it back later), players in team voice go back to the Lobby
voice channel, and the team voice channels are deleted.

Getting into the game:
  * With tournament codes on (a Production key with Tournament API access), a real tournament
    code locked to exactly these players.
  * Otherwise a generated custom lobby name and password, with one named player (the host if
    they're playing, else blue side's first pick) creating the lobby.
"""
from __future__ import annotations

import hashlib
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import discord
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import config
from bot.models.lobby import Lobby
from bot.models.player import Player
from bot.services import settings
from bot.services.server_setup import REMEMBER_PREFIX, _only_what_bot_has

log = logging.getLogger("ranked-bot.gamechannel")

REASON = "Ranked Inhouse Bot: private game space"
PASSWORD_CHARS = "abcdefghjkmnpqrstuvwxyz23456789"      # no 0/o, 1/l/i to misread
PROVIDER_REGIONS = {
    "na1": "NA", "euw1": "EUW", "eun1": "EUNE", "kr": "KR", "br1": "BR", "la1": "LAN", "la2": "LAS",
    "oc1": "OCE", "tr1": "TR", "ru": "RU", "jp1": "JP", "ph2": "PH", "sg2": "SG", "th2": "TH",
    "tw2": "TW", "vn2": "VN", "me1": "ME",
}
KEY_PROVIDER = "tournament_provider_id"
KEY_TOURNAMENT = "tournament_id"
# What the bot needs in the queue channel to run game threads.
THREAD_PERMISSIONS = ("create_private_threads", "send_messages_in_threads")
STAFF_ADD_LIMIT = 25        # staff who can't already see private threads get added, up to this many


def thread_name(host_name: str, lobby: Lobby) -> str:
    kind = "casual inhouse" if not lobby.ranked else "inhouse"
    return f"{host_name}'s {kind}"[:100]


def voice_names(host_name: str) -> tuple[str, str]:
    return f"Blue · {host_name}'s inhouse"[:100], f"Red · {host_name}'s inhouse"[:100]


# --------------------------------------------------------------------------- #
# Who can see and join                                                        #
# --------------------------------------------------------------------------- #

def voice_overwrites(guild: Any, members: list[Any], staff_roles: list[Any], have: discord.Permissions) -> dict:
    """Visible to everyone so people can see a game is on; only this team, staff and the bot can join."""
    join = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, stream=True,
                                       use_voice_activation=True)
    raw: dict = {guild.default_role: discord.PermissionOverwrite(connect=False),
                 guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, move_members=True)}
    for role in staff_roles:
        raw[role] = join
    for m in members:
        raw[m] = join
    out = {}
    for target, ow in raw.items():
        kept = _only_what_bot_has(ow, have)
        if not kept.is_empty():
            out[target] = kept
    return out


def staff_roles(guild: Any, mod_role_id: int | None) -> list[Any]:
    roles = []
    for r in getattr(guild, "roles", []):
        if r is guild.default_role:
            continue
        perms = getattr(r, "permissions", None)
        if (mod_role_id and r.id == mod_role_id) or (perms is not None and perms.manage_guild):
            roles.append(r)
    return roles


def can_create_voice(have: discord.Permissions) -> bool:
    return bool(have.administrator or (have.manage_channels and have.manage_roles))


def sees_private_threads(member: Any) -> bool:
    p = getattr(member, "guild_permissions", None)
    return bool(p is not None and (p.administrator or p.manage_threads))


def staff_to_add(roles: list[Any]) -> list[Any]:
    """Staff who won't see a private thread on their own (no Manage Threads), to add by hand."""
    out: dict[int, Any] = {}
    for r in roles:
        for m in getattr(r, "members", []) or []:
            if getattr(m, "bot", False) or sees_private_threads(m):
                continue
            out.setdefault(m.id, m)
    return list(out.values())[:STAFF_ADD_LIMIT]


# --------------------------------------------------------------------------- #
# Getting into the game                                                        #
# --------------------------------------------------------------------------- #

@dataclass
class JoinInfo:
    code: str | None           # tournament code, if one could be made
    name: str | None           # custom lobby name
    password: str | None
    creator: Player | None     # who makes the custom lobby
    note: str | None = None    # e.g. why no tournament code


def make_password(rng: random.Random, length: int = 6) -> str:
    return "".join(rng.choice(PASSWORD_CHARS) for _ in range(length))


def choose_creator(lobby: Lobby, players: dict[int, Player]) -> Player | None:
    """The host if they're playing; else blue side's first pick / captain; else any real player."""
    real = {pid: p for pid, p in players.items() if not p.is_test}
    host = next((p for p in real.values() if p.discord_id == lobby.host_discord_id), None)
    if host is not None:
        return host
    blue = [lp for lp in lobby.players if lp.team == 1 and lp.player_id in real]
    if lobby.captain1_id in real:
        return real[lobby.captain1_id]
    blue.sort(key=lambda lp: (lp.pick_order is None or lp.pick_order < 0, lp.pick_order or 0))
    if blue:
        return real[blue[0].player_id]
    return next(iter(real.values()), None)


def key_fingerprint(key: str) -> str:
    """Identifies a key without storing it. Riot ties providers to the key that made them."""
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def callback_problem(url: str) -> str | None:
    """Riot only accepts http(s) callback URLs on the default port."""
    u = urlparse(url or "")
    if u.scheme not in ("http", "https") or not u.hostname:
        return f"RIOT_TOURNAMENT_CALLBACK_URL must be an http:// or https:// address, not {url!r}."
    if u.port not in (None, 80, 443):
        return "RIOT_TOURNAMENT_CALLBACK_URL can't use a custom port; Riot only calls ports 80 and 443."
    return None


async def _tournament_code(session: AsyncSession, riot: Any, lobby: Lobby, puuids: list[str]) -> str:
    """
    Riot's flow is provider -> tournament -> codes. The provider and tournament are made once
    and remembered per key (a new key can't use an old key's provider), region and season.
    """
    problem = callback_problem(config.RIOT_TOURNAMENT_CALLBACK_URL)
    if problem:
        raise ValueError(problem)
    gid = lobby.guild_id
    fp = key_fingerprint(config.tournament_api_key())
    region = PROVIDER_REGIONS.get(config.RIOT_REGION.lower(), "NA")
    provider_key = f"{KEY_PROVIDER}:{fp}:{region}"
    tournament_key = f"{KEY_TOURNAMENT}:{fp}:{region}:s{config.CURRENT_SEASON}"
    provider = await settings.get_setting(session, gid, provider_key)
    if not provider:
        provider = str(await riot.create_tournament_provider(region, config.RIOT_TOURNAMENT_CALLBACK_URL))
        await settings.set_setting(session, gid, provider_key, provider)
    tournament = await settings.get_setting(session, gid, tournament_key)
    if not tournament:
        tournament = str(await riot.create_tournament(int(provider), f"Ranked Inhouses S{config.CURRENT_SEASON}"))
        await settings.set_setting(session, gid, tournament_key, tournament)
    return await riot.create_tournament_code(int(tournament), allowed_participants=puuids,
                                             metadata=f"lobby:{lobby.id}")


async def prepare_join(session: AsyncSession, lobby: Lobby, riot: Any | None = None,
                       rng: random.Random | None = None) -> JoinInfo:
    """Decide how players get into the game and store it on the lobby. Safe to call twice."""
    rng = rng or random.SystemRandom()
    players = {lp.player_id: await session.get(Player, lp.player_id) for lp in lobby.players}
    players = {k: v for k, v in players.items() if v is not None}
    creator = choose_creator(lobby, players)
    lobby.join_creator_id = creator.id if creator else None
    note = None
    if lobby.tournament_code is None and config.tournament_api_key() and riot is not None:
        puuids = [p.riot_puuid for p in players.values() if p.riot_puuid and not p.is_test]
        try:
            lobby.tournament_code = await _tournament_code(session, riot, lobby, puuids)
        except Exception as e:     # a key without tournament access, or Riot down: fall back
            log.warning("tournament code failed for lobby %s: %s", lobby.id, e)
            note = "A tournament code couldn't be made, so use the lobby name and password instead."
    if lobby.tournament_code is None and not lobby.join_password:
        lobby.join_name = f"Inhouse {lobby.id}-{rng.randint(1000, 9999)}"
        lobby.join_password = make_password(rng)
    await session.commit()
    return JoinInfo(code=lobby.tournament_code, name=lobby.join_name, password=lobby.join_password,
                    creator=creator, note=note)


# --------------------------------------------------------------------------- #
# Finding things                                                               #
# --------------------------------------------------------------------------- #

async def _member(guild: Any, discord_id: str) -> Any | None:
    if not str(discord_id).isdigit():
        return None                          # fillers have no Discord account
    m = guild.get_member(int(discord_id))
    if m is None and hasattr(guild, "fetch_member"):
        try:
            m = await guild.fetch_member(int(discord_id))
        except discord.HTTPException:
            m = None
    return m


async def find(guild: Any, channel_id: str | int | None) -> Any | None:
    """A channel or thread by ID. Threads aren't in the normal channel cache, and archived ones
    aren't cached at all, so this falls back to asking Discord."""
    if not channel_id or not str(channel_id).isdigit():
        return None
    cid = int(channel_id)
    getter = getattr(guild, "get_channel_or_thread", None) or guild.get_channel
    ch = getter(cid)
    if ch is None and hasattr(guild, "fetch_channel"):
        try:
            ch = await guild.fetch_channel(cid)
        except (discord.NotFound, discord.Forbidden):
            ch = None
    return ch


async def _host_name(guild: Any, session: AsyncSession, lobby: Lobby) -> str:
    m = await _member(guild, lobby.host_discord_id)
    if m is not None:
        return getattr(m, "display_name", None) or getattr(m, "name", None) or "Host"
    p = await session.scalar(select(Player).where(Player.discord_id == lobby.host_discord_id))
    return p.discord_username if p else "Host"


async def _people(guild: Any, session: AsyncSession, lobby: Lobby, team: int | None = None,
                  with_host: bool = False) -> list[Any]:
    ids = {lp.player_id for lp in lobby.players if team is None or lp.team == team}
    discord_ids = set()
    for pid in ids:
        p = await session.get(Player, pid)
        if p is not None and not p.is_test:
            discord_ids.add(p.discord_id)
    if with_host:
        discord_ids.add(lobby.host_discord_id)
    return [m for m in [await _member(guild, d) for d in sorted(discord_ids)] if m is not None]


async def _staff(guild: Any, session: AsyncSession, lobby: Lobby) -> list[Any]:
    mod_role = await settings.get_setting(session, lobby.guild_id, REMEMBER_PREFIX + "mod_role")
    return staff_roles(guild, int(mod_role) if mod_role and mod_role.isdigit() else None)


async def _queue_channel(guild: Any, lobby: Lobby) -> Any | None:
    """Where the lobby was posted; game threads hang off it."""
    ch = await find(guild, lobby.channel_id)
    parent = getattr(ch, "parent", None)
    if parent is not None and hasattr(ch, "archived"):     # the lobby was opened inside a thread
        ch = parent
    return ch if ch is not None and hasattr(ch, "create_thread") else None


# --------------------------------------------------------------------------- #
# The thread                                                                   #
# --------------------------------------------------------------------------- #

def thread_access_missing(channel: Any, me: Any) -> list[str]:
    p = channel.permissions_for(me)
    if p.administrator:
        return []
    return [n for n in THREAD_PERMISSIONS if not getattr(p, n)]


async def ensure_thread_access(guild: Any, channel: Any) -> list[str]:
    """
    Make sure the bot may open private threads in this channel. A lock on @everyone (like the
    one /admin setup puts on #inhouse-queue so players can't start threads) applies to the bot
    too, so the bot gives itself an exception where it can. Returns what is still missing.
    """
    missing = thread_access_missing(channel, guild.me)
    if not missing:
        return []
    have = guild.me.guild_permissions
    if not (channel.permissions_for(guild.me).manage_roles and all(getattr(have, n) for n in missing)):
        return missing
    ow = channel.overwrites_for(guild.me)
    for n in missing:
        setattr(ow, n, True)
    try:
        await channel.set_permissions(guild.me, overwrite=ow, reason=REASON)
    except discord.HTTPException as e:
        log.warning("could not allow threads for the bot in #%s: %s", getattr(channel, "name", "?"), e)
        return missing
    return []


async def create(guild: Any, session: AsyncSession, lobby: Lobby) -> Any | None:
    """Create the lobby's private thread (or return the existing one). None when not possible."""
    if lobby.game_channel_id:
        existing = await find(guild, lobby.game_channel_id)
        if existing is not None:
            return existing
    if not config.GAME_CHANNELS_ENABLED:
        return None
    parent = await _queue_channel(guild, lobby)
    if parent is None:
        return None
    missing = await ensure_thread_access(guild, parent)
    if missing:
        log.warning("can't open a game thread in #%s, the bot is missing: %s",
                    getattr(parent, "name", "?"), ", ".join(missing))
        return None
    moderator = parent.permissions_for(guild.me).manage_threads
    thread = await parent.create_thread(
        name=thread_name(await _host_name(guild, session, lobby), lobby),
        type=discord.ChannelType.private_thread,
        # Players can't pull friends in. Without Manage Threads the bot needs this on to add people.
        invitable=not moderator,
        auto_archive_duration=1440, reason=REASON)
    lobby.game_channel_id = str(thread.id)
    await session.commit()
    people = await _people(guild, session, lobby, with_host=True)
    for m in people + staff_to_add(await _staff(guild, session, lobby)):
        try:
            await thread.add_user(m)
        except discord.HTTPException as e:
            log.warning("could not add %s to game thread %s: %s", getattr(m, "id", "?"), thread.id, e)
    return thread


async def close_thread(guild: Any, lobby: Lobby) -> None:
    ch = await find(guild, lobby.game_channel_id)
    if ch is None:
        return
    if not hasattr(ch, "archived"):          # a #game-N text channel from an older version
        await ch.delete(reason=f"Lobby #{lobby.id} is over")
        return
    kw: dict = {"archived": True}
    if ch.permissions_for(guild.me).manage_threads:
        kw["locked"] = True                   # nobody can reopen it by posting
    await ch.edit(**kw)


# --------------------------------------------------------------------------- #
# Team voice                                                                   #
# --------------------------------------------------------------------------- #

async def _inhouse_category(guild: Any, session: AsyncSession, lobby: Lobby) -> Any | None:
    cat_id = await settings.get_setting(session, lobby.guild_id, REMEMBER_PREFIX + "cat_inhouse")
    category = guild.get_channel(int(cat_id)) if cat_id and cat_id.isdigit() else None
    if category is None:
        queue = await find(guild, lobby.channel_id)
        category = getattr(getattr(queue, "parent", None) or queue, "category", None)
    return category


async def _lobby_voice(guild: Any, session: AsyncSession, lobby: Lobby) -> Any | None:
    """Where players go back to after the game: the Lobby voice channel from /admin setup."""
    vid = await settings.get_setting(session, lobby.guild_id, REMEMBER_PREFIX + "vc_lobby")
    ch = guild.get_channel(int(vid)) if vid and vid.isdigit() else None
    if ch is None:
        ch = next((c for c in getattr(guild, "voice_channels", []) if c.name.lower() == "lobby"), None)
    return ch


def can_move(guild: Any) -> bool:
    p = guild.me.guild_permissions
    return bool(p.administrator or p.move_members)


async def move_into(guild: Any, members: list[Any], channel: Any) -> int:
    """Move members who are already in a voice channel here. Never pulls anyone into voice."""
    if not can_move(guild):
        return 0
    afk = getattr(guild, "afk_channel", None)
    moved = 0
    for m in members:
        now = getattr(getattr(m, "voice", None), "channel", None)
        if now is None or now.id == channel.id or (afk is not None and now.id == afk.id):
            continue
        try:
            await m.move_to(channel, reason=REASON)
            moved += 1
        except discord.HTTPException as e:
            log.warning("could not move %s to %s: %s", getattr(m, "id", "?"), getattr(channel, "name", "?"), e)
    return moved


async def open_team_voice(guild: Any, session: AsyncSession, lobby: Lobby) -> tuple[Any, Any] | None:
    """
    Blue and Red voice for this game, joinable only by that team and staff. Run again after
    teams change and it updates who may join. Moves players who are in voice.
    """
    if not config.TEAM_VOICE_ENABLED:
        return None
    have = guild.me.guild_permissions
    if not can_create_voice(have):
        return None
    roles = await _staff(guild, session, lobby)
    teams = {t: await _people(guild, session, lobby, team=t) for t in (1, 2)}
    existing = [await find(guild, lobby.team1_voice_id), await find(guild, lobby.team2_voice_id)]
    if all(existing):
        for t, ch in zip((1, 2), existing):
            await ch.edit(overwrites=voice_overwrites(guild, teams[t], roles, have), reason=REASON)
        chans = tuple(existing)
    else:
        for ch in existing:                    # half a pair is no use; start over
            if ch is not None:
                await ch.delete(reason=REASON)
        category = await _inhouse_category(guild, session, lobby)
        names = voice_names(await _host_name(guild, session, lobby))
        chans = []
        for t, name in zip((1, 2), names):
            chans.append(await guild.create_voice_channel(
                name, category=category, overwrites=voice_overwrites(guild, teams[t], roles, have),
                reason=REASON))
        chans = tuple(chans)
        lobby.team1_voice_id, lobby.team2_voice_id = str(chans[0].id), str(chans[1].id)
        await session.commit()
    if config.TEAM_VOICE_AUTO_MOVE:
        for t, ch in zip((1, 2), chans):
            await move_into(guild, teams[t], ch)
    return chans


async def close_team_voice(guild: Any, session: AsyncSession, lobby: Lobby) -> None:
    back = await _lobby_voice(guild, session, lobby)
    for vid in (lobby.team1_voice_id, lobby.team2_voice_id):
        await _step(_close_voice(guild, vid, back, lobby))


async def _close_voice(guild: Any, vid: str | None, back: Any | None, lobby: Lobby) -> None:
    ch = await find(guild, vid)
    if ch is None:
        return
    if back is not None:
        await move_into(guild, list(getattr(ch, "members", []) or []), back)
    await ch.delete(reason=f"Lobby #{lobby.id} is over")


# --------------------------------------------------------------------------- #
# Lookups and cleanup                                                          #
# --------------------------------------------------------------------------- #

async def lobby_for_channel(session: AsyncSession, guild_id: str, channel_id: int | None) -> Lobby | None:
    """The live lobby whose private thread this is, if any."""
    if not channel_id:
        return None
    return await session.scalar(select(Lobby).where(
        Lobby.guild_id == guild_id, Lobby.game_channel_id == str(channel_id),
        Lobby.status.in_(("waiting", "drafting", "active"))))


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def due_for_cleanup(session: AsyncSession, now: datetime | None = None) -> list[Lobby]:
    """Finished lobbies whose thread or team voice has outlived GAME_CHANNEL_CLEANUP_MINUTES."""
    minutes = config.GAME_CHANNEL_CLEANUP_MINUTES
    if minutes <= 0:
        return []
    now = now or datetime.now(timezone.utc)
    rows = (await session.execute(select(Lobby).where(
        or_(Lobby.game_channel_id.is_not(None), Lobby.team1_voice_id.is_not(None),
            Lobby.team2_voice_id.is_not(None)),
        Lobby.status.in_(("completed", "cancelled"))))).scalars().all()
    return [l for l in rows if (_aware(l.updated_at) or now) <= now - timedelta(minutes=minutes)]


class _Retry(Exception):
    pass


async def _step(coro) -> None:
    """Run one cleanup step. Gone or forbidden counts as done (retrying won't help); anything
    else Discord refuses is retried on the next pass."""
    try:
        await coro
    except discord.NotFound:
        pass
    except discord.Forbidden as e:
        log.warning("not allowed to clean up (%s); leaving it for staff", e)
    except discord.HTTPException as e:
        raise _Retry(str(e)) from e


async def clean_up(guild: Any, session: AsyncSession, lobby: Lobby) -> bool:
    """Close the thread and remove team voice. Returns False to retry later."""
    try:
        await _step(close_thread(guild, lobby))
        await close_team_voice(guild, session, lobby)
    except _Retry as e:
        log.warning("could not clean up lobby %s yet: %s", lobby.id, e)
        return False
    lobby.game_channel_id = lobby.team1_voice_id = lobby.team2_voice_id = None
    await session.commit()
    return True
