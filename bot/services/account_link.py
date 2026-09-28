"""
Proving a player owns the Riot account they link.

Riot removed the in-client verification code years ago, so bots use the profile
icon check: the bot names one of the starter icons every account owns, the player
switches to it in the League client, and the bot confirms the change through the
Riot API. Only someone logged in to the account can do that.

Rules:
  * /link starts a challenge that lasts VERIFY_MINUTES. Verify completes it.
  * Linking the account you already have (e.g. after a Riot ID rename) just refreshes
    the name if it was already verified, otherwise it runs the check once to verify it.
  * Switching to a DIFFERENT account is not self-service: a moderator unlinks the old
    one first. This stops people hopping between mains and smurfs at will.
  * If someone else already linked the account, proving ownership moves it to you.
    Their Discord profile keeps its own ratings; the caller tells the mods.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import config
from bot.models.link import LinkRequest
from bot.models.player import Player

STARTER_ICONS = tuple(range(0, 29))   # owned by every League account
VERIFY_MINUTES = 10
DDRAGON_FALLBACK_VERSION = "14.24.1"


class LinkError(Exception):
    pass


@dataclass
class Challenge:
    riot_id: str
    icon_id: int
    current_icon: int | None
    expires_at: datetime
    reclaim_from: str | None      # who currently has this account linked, if anyone


@dataclass
class Linked:
    player: Player
    riot_id: str
    transferred_from: str | None = None
    refreshed_only: bool = False
    flag: Any = None


def parse_riot_id(text: str) -> tuple[str, str]:
    text = (text or "").strip()
    if "#" not in text:
        raise LinkError("Use your full Riot ID, name and tag, for example `/link Danman#NA1`.")
    name, tag = text.rsplit("#", 1)
    if not name.strip() or not tag.strip():
        raise LinkError("Use your full Riot ID, name and tag, for example `/link Danman#NA1`.")
    return name.strip(), tag.strip()


def pick_icon(current: int | None, rng: random.Random | None = None) -> int:
    rng = rng or random.Random()
    return rng.choice([i for i in STARTER_ICONS if i != current])


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _player(session: AsyncSession, discord_id: str) -> Player | None:
    return await session.scalar(select(Player).where(Player.discord_id == discord_id))


async def begin(session: AsyncSession, riot: Any, discord_id: str, discord_name: str, riot_id_text: str,
                rng: random.Random | None = None, now: datetime | None = None) -> Challenge | Linked:
    now = now or datetime.now(timezone.utc)
    name, tag = parse_riot_id(riot_id_text)
    account = await riot.get_account_by_riot_id(name, tag)
    puuid = account["puuid"]
    riot_id = f"{account.get('gameName', name)}#{account.get('tagLine', tag)}"

    me = await _player(session, discord_id)
    if me is not None and me.riot_puuid and me.riot_puuid != puuid:
        raise LinkError(
            f"You're already linked to **{me.summoner_name}**. Switching to a different account needs a "
            f"moderator: ask them to run `/admin unlink` on you, then link the new one.")
    if me is not None and me.riot_puuid == puuid and me.link_verified:
        me.summoner_name = riot_id
        me.discord_username = discord_name
        await session.commit()
        return Linked(player=me, riot_id=riot_id, refreshed_only=True)

    summoner = await riot.get_summoner_by_puuid(puuid)
    current_icon = summoner.get("profileIconId")
    holder = await session.scalar(select(Player).where(Player.riot_puuid == puuid, Player.discord_id != discord_id))

    req = await session.scalar(select(LinkRequest).where(LinkRequest.discord_id == discord_id))
    if req is not None and req.puuid == puuid and _aware(req.expires_at) > now:
        pass    # same account, still valid: keep the same icon so the player isn't sent chasing a new one
    else:
        if req is None:
            req = LinkRequest(discord_id=discord_id, puuid=puuid, riot_id=riot_id, icon_id=0, expires_at=now)
            session.add(req)
        req.puuid, req.riot_id = puuid, riot_id
        req.icon_id = pick_icon(current_icon, rng)
        req.expires_at = now + timedelta(minutes=VERIFY_MINUTES)
    await session.commit()
    return Challenge(riot_id=riot_id, icon_id=req.icon_id, current_icon=current_icon,
                     expires_at=_aware(req.expires_at), reclaim_from=holder.discord_username if holder else None)


async def verify(session: AsyncSession, riot: Any, discord_id: str, discord_name: str,
                 now: datetime | None = None) -> Linked:
    from bot.services import smurf_detector
    from bot.services.game_processor import get_or_create_rating

    now = now or datetime.now(timezone.utc)
    req = await session.scalar(select(LinkRequest).where(LinkRequest.discord_id == discord_id))
    if req is None:
        raise LinkError("There's no link in progress. Run `/link YourName#TAG` first.")
    if _aware(req.expires_at) <= now:
        await session.delete(req)
        await session.commit()
        raise LinkError("That link request expired. Run `/link` again to get a new icon.")

    summoner = await riot.get_summoner_by_puuid(req.puuid)
    icon = summoner.get("profileIconId")
    if icon != req.icon_id:
        raise LinkError(f"Your profile icon is still **#{icon}**, not **#{req.icon_id}**. Change it in the League "
                        f"client, give it about a minute to update, then press **Verify** again.")

    holder = await session.scalar(select(Player).where(Player.riot_puuid == req.puuid,
                                                        Player.discord_id != discord_id))
    transferred_from = None
    if holder is not None:
        transferred_from = holder.discord_username
        holder.riot_puuid, holder.summoner_name, holder.link_verified = None, None, False
        await session.flush()        # free the unique riot_puuid before giving it to the owner

    me = await _player(session, discord_id)
    if me is None:
        me = Player(discord_id=discord_id, discord_username=discord_name)
        session.add(me)
        await session.flush()
    entries = None
    try:
        entries = await riot.get_league_entries_by_puuid(req.puuid)
    except Exception:
        pass
    me.riot_puuid, me.summoner_name, me.discord_username = req.puuid, req.riot_id, discord_name
    me.link_verified = True
    me.linked_at = now
    me.summoner_level = int(summoner.get("summonerLevel", 0) or 0) or None
    me.riot_rank_snapshot = {"entries": entries or [], "summoner_level": me.summoner_level}
    await get_or_create_rating(session, me.id, "OVERALL", config.CURRENT_SEASON)
    riot_id = req.riot_id
    await session.delete(req)
    await session.commit()
    flag = await smurf_detector.evaluate_link(session, me, summoner, entries)
    return Linked(player=me, riot_id=riot_id, transferred_from=transferred_from, flag=flag)


async def cancel(session: AsyncSession, discord_id: str) -> bool:
    req = await session.scalar(select(LinkRequest).where(LinkRequest.discord_id == discord_id))
    if req is None:
        return False
    await session.delete(req)
    await session.commit()
    return True


_ddragon_version: str | None = None


async def icon_image_url(icon_id: int) -> str:
    """Data Dragon image of a profile icon, for showing the player which one to pick."""
    global _ddragon_version
    if _ddragon_version is None:
        try:
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
                async with s.get("https://ddragon.leagueoflegends.com/api/versions.json") as r:
                    _ddragon_version = (await r.json())[0]
        except Exception:
            _ddragon_version = DDRAGON_FALLBACK_VERSION
    return f"https://ddragon.leagueoflegends.com/cdn/{_ddragon_version}/img/profileicon/{icon_id}.png"
