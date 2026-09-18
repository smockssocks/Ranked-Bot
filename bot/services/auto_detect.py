"""
Self-sustaining result pickup. For every lobby in status "active" the loop asks
Riot for the recent CUSTOM games (queue 0) of two of its players, and when a
match contains at least AUTO_DETECT_MIN_LOBBY_PLAYERS of the lobby's PUUIDs it
is processed automatically and the lobby is completed. No /submit needed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import config
from bot.models.game import Game
from bot.models.lobby import Lobby
from bot.services import game_processor, lobby_manager
from bot.services.riot_api import RiotAPIError, RiotClient, RiotUnavailable

log = logging.getLogger("ranked-bot.autodetect")


def match_belongs_to_lobby(match: dict, lobby_puuids: set[str], min_players: int) -> bool:
    info = match.get("info", {})
    if int(info.get("queueId", -1)) != 0 and str(info.get("gameType", "")).upper() != "CUSTOM_GAME":
        return False
    puuids = {p.get("puuid") for p in info.get("participants", [])}
    return len(puuids & lobby_puuids) >= min_players


async def find_lobby_match(riot: RiotClient, session: AsyncSession, lobby: Lobby) -> tuple[str, dict] | None:
    """Return (match_id, match_json) for a new custom game played by this lobby, if any."""
    puuid_map = await lobby_manager.lobby_puuids(session, lobby)
    if len(puuid_map) < config.AUTO_DETECT_MIN_LOBBY_PLAYERS:
        return None
    started = lobby.started_at or lobby.created_at or datetime.now(timezone.utc)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    start_epoch = int((started - timedelta(minutes=10)).timestamp())
    candidates: list[str] = []
    for puuid in list(puuid_map)[:2]:
        try:
            ids = await riot.get_recent_match_ids(puuid, queue=0, count=3, start_time=start_epoch)
        except RiotAPIError as e:
            log.warning("match id lookup failed for lobby %s: %s", lobby.id, e)
            continue
        for mid in ids:
            if mid not in candidates:
                candidates.append(mid)
    for mid in candidates:
        if await session.scalar(select(Game).where(Game.riot_match_id == mid)):
            continue
        try:
            match = await riot.get_match(mid)
        except RiotAPIError as e:
            log.warning("match fetch failed %s: %s", mid, e)
            continue
        if match_belongs_to_lobby(match, set(puuid_map), config.AUTO_DETECT_MIN_LOBBY_PLAYERS):
            return mid, match
    return None


async def poll_once(session_factory) -> list[tuple[Lobby, game_processor.ProcessResult]]:
    """One pass over active lobbies. Returns processed (lobby, result) pairs for announcing."""
    out: list[tuple[Lobby, game_processor.ProcessResult]] = []
    async with session_factory() as session:
        lobbies = await lobby_manager.active_lobbies(session)
        if not lobbies:
            return out
        now = datetime.now(timezone.utc)
        try:
            async with RiotClient() as riot:
                for lobby in lobbies:
                    started = lobby.started_at or lobby.created_at
                    if started and started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    if started and now - started > timedelta(hours=config.AUTO_DETECT_MAX_AGE_HOURS):
                        log.info("lobby %s timed out without a detected game; cancelling", lobby.id)
                        lobby.status = "cancelled"
                        await session.commit()
                        continue
                    found = await find_lobby_match(riot, session, lobby)
                    if not found:
                        continue
                    mid, match = found
                    try:
                        timeline = await riot.get_match_timeline(mid)
                    except RiotAPIError as e:
                        log.warning("timeline fetch failed %s: %s", mid, e)
                        timeline = {"info": {"frames": []}}
                    try:
                        result = await game_processor.process_match(
                            session, mid, submitted_by="auto-detect", lobby_id=lobby.id, auto_detected=True,
                            match_data=match, timeline_data=timeline)
                    except ValueError as e:
                        log.info("skip %s: %s", mid, e)
                        continue
                    await lobby_manager.complete_lobby(session, lobby)
                    out.append((lobby, result))
        except RiotUnavailable:
            log.debug("auto-detect skipped: no Riot key")
    return out
