"""
A private #game-N channel for each started inhouse.

Visible only to the lobby's players, its host, the moderator role, and staff roles
(anything with Manage Server; Administrators see every channel anyway). The bot posts
the teams, the draft, and how to get into the custom game there, so lobby passwords
and tournament codes are never public. The channel is deleted a while after the game
ends (GAME_CHANNEL_CLEANUP_MINUTES).

Getting into the game:
  * With a Riot tournament key (Production keys only), a real tournament code locked to
    exactly these players.
  * Otherwise a generated custom lobby name and password, with one named player (the
    host if they're playing, else blue side's first pick) creating the lobby.
"""
from __future__ import annotations

import logging
import random
import string
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import config
from bot.models.lobby import Lobby, LobbyPlayer
from bot.models.player import Player
from bot.services import settings
from bot.services.server_setup import BOT_ACCESS, REMEMBER_PREFIX, _only_what_bot_has

log = logging.getLogger("ranked-bot.gamechannel")

REASON = "Ranked Inhouse Bot: private game channel"
PASSWORD_CHARS = "abcdefghjkmnpqrstuvwxyz23456789"      # no 0/o, 1/l/i to misread
PROVIDER_REGIONS = {
    "na1": "NA", "euw1": "EUW", "eun1": "EUNE", "kr": "KR", "br1": "BR", "la1": "LAN", "la2": "LAS",
    "oc1": "OCE", "tr1": "TR", "ru": "RU", "jp1": "JP", "ph2": "PH", "sg2": "SG", "th2": "TH",
    "tw2": "TW", "vn2": "VN", "me1": "ME",
}
KEY_PROVIDER = "tournament_provider_id"
KEY_TOURNAMENT = "tournament_id"


def channel_name(lobby: Lobby) -> str:
    return f"game-{lobby.id}"


# --------------------------------------------------------------------------- #
# Who can see it                                                              #
# --------------------------------------------------------------------------- #

def overwrites(guild: Any, members: list[Any], staff_roles: list[Any], have: discord.Permissions) -> dict:
    """Hidden from everyone; open to these members, staff roles and the bot."""
    player_access = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                                embed_links=True, attach_files=True)
    raw: dict = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                 guild.me: discord.PermissionOverwrite(**BOT_ACCESS)}
    for role in staff_roles:
        raw[role] = player_access
    for m in members:
        raw[m] = player_access
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


def can_create(have: discord.Permissions) -> bool:
    return bool(have.administrator or (have.manage_channels and have.manage_roles))


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


async def _tournament_code(session: AsyncSession, riot: Any, lobby: Lobby, puuids: list[str]) -> str:
    gid = lobby.guild_id
    provider = await settings.get_setting(session, gid, KEY_PROVIDER)
    if not provider:
        region = PROVIDER_REGIONS.get(config.RIOT_REGION.lower(), "NA")
        provider = str(await riot.create_tournament_provider(region, config.RIOT_TOURNAMENT_CALLBACK_URL))
        await settings.set_setting(session, gid, KEY_PROVIDER, provider)
    tournament = await settings.get_setting(session, gid, KEY_TOURNAMENT)
    if not tournament:
        tournament = str(await riot.create_tournament(int(provider), "Ranked Inhouses"))
        await settings.set_setting(session, gid, KEY_TOURNAMENT, tournament)
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
    if lobby.tournament_code is None and config.RIOT_TOURNAMENT_API_KEY and riot is not None:
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
# Creating and finding the channel                                             #
# --------------------------------------------------------------------------- #

async def _member(guild: Any, discord_id: str) -> Any | None:
    if not discord_id.isdigit():
        return None                          # fillers have no Discord account
    m = guild.get_member(int(discord_id))
    if m is None and hasattr(guild, "fetch_member"):
        try:
            m = await guild.fetch_member(int(discord_id))
        except discord.HTTPException:
            m = None
    return m


async def create(guild: Any, session: AsyncSession, lobby: Lobby) -> Any | None:
    """Create the private channel for a lobby (or return the existing one)."""
    if lobby.game_channel_id:
        existing = guild.get_channel(int(lobby.game_channel_id))
        if existing is not None:
            return existing
    have = guild.me.guild_permissions
    if not config.GAME_CHANNELS_ENABLED or not can_create(have):
        return None
    ids = {lp.player_id for lp in lobby.players}
    people = [await session.get(Player, pid) for pid in ids]
    discord_ids = {p.discord_id for p in people if p is not None and not p.is_test}
    discord_ids.add(lobby.host_discord_id)
    members = [m for m in [await _member(guild, d) for d in sorted(discord_ids)] if m is not None]

    mod_role = await settings.get_setting(session, lobby.guild_id, REMEMBER_PREFIX + "mod_role")
    roles = staff_roles(guild, int(mod_role) if mod_role and mod_role.isdigit() else None)

    category = None
    cat_id = await settings.get_setting(session, lobby.guild_id, REMEMBER_PREFIX + "cat_inhouse")
    if cat_id and cat_id.isdigit():
        category = guild.get_channel(int(cat_id))
    if category is None:
        queue = guild.get_channel(int(lobby.channel_id)) if str(lobby.channel_id).isdigit() else None
        category = getattr(queue, "category", None)

    kind = "casual" if not lobby.ranked else "ranked"
    ch = await guild.create_text_channel(
        channel_name(lobby), category=category, overwrites=overwrites(guild, members, roles, have),
        topic=f"Lobby #{lobby.id} ({kind}). Only its players and staff can see this. Deleted after the game.",
        reason=REASON)
    lobby.game_channel_id = str(ch.id)
    await session.commit()
    return ch


async def lobby_for_channel(session: AsyncSession, guild_id: str, channel_id: int | None) -> Lobby | None:
    """The live lobby whose private channel this is, if any."""
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
    """Finished lobbies whose channel has outlived GAME_CHANNEL_CLEANUP_MINUTES."""
    minutes = config.GAME_CHANNEL_CLEANUP_MINUTES
    if minutes <= 0:
        return []
    now = now or datetime.now(timezone.utc)
    rows = (await session.execute(select(Lobby).where(
        Lobby.game_channel_id.is_not(None), Lobby.status.in_(("completed", "cancelled"))))).scalars().all()
    return [l for l in rows if (_aware(l.updated_at) or now) <= now - timedelta(minutes=minutes)]
