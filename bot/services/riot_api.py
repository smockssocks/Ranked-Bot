"""
Async Riot API client.
Handles: account lookup, match data, timeline, and tournament codes.
"""
from __future__ import annotations
import asyncio
import logging
import aiohttp
from bot import config as _cfg

log = logging.getLogger("ranked-bot.riot")

_PLATFORM_HOSTS = {
    "americas": "americas.api.riotgames.com",
    "europe":   "europe.api.riotgames.com",
    "asia":     "asia.api.riotgames.com",
}

_REGION_HOSTS = {
    r: f"{r}.api.riotgames.com"
    for r in ("na1", "euw1", "eun1", "kr", "br1", "la1", "la2", "oc1", "tr1", "ru", "jp1", "ph2", "sg2", "th2", "tw2", "vn2", "me1")
}


def _platform_host() -> str:
    return _PLATFORM_HOSTS.get(_cfg.RIOT_PLATFORM, "americas.api.riotgames.com")


def _region_host() -> str:
    return _REGION_HOSTS.get(_cfg.RIOT_REGION, f"{_cfg.RIOT_REGION}.api.riotgames.com")


class RiotUnavailable(Exception):
    """Raised when no Riot API key is configured."""


class RiotAPIError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"Riot API {status}: {message}")


class RiotClient:
    def __init__(self, session: aiohttp.ClientSession | None = None):
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *_):
        if self._owns_session and self._session:
            await self._session.close()

    async def _get(self, host: str, path: str, api_key: str | None = None, retries: int = 3) -> dict:
        url = f"https://{host}{path}"
        key = api_key or _cfg.RIOT_API_KEY
        if not key:
            raise RiotUnavailable("RIOT_API_KEY is not configured.")
        headers = {"X-Riot-Token": key}
        delay = 1.0
        for attempt in range(retries):
            async with self._session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()
                if resp.status == 429:
                    retry_after = float(resp.headers.get("Retry-After", delay))
                    log.warning("Rate limited on %s, waiting %.1fs", path, retry_after)
                    await asyncio.sleep(retry_after)
                    delay *= 2
                    continue
                if resp.status == 404:
                    raise RiotAPIError(404, f"Not found: {path}")
                text = await resp.text()
                raise RiotAPIError(resp.status, text)
        raise RiotAPIError(429, f"Exhausted retries for {path}")

    async def _post(self, host: str, path: str, payload: dict, api_key: str | None = None) -> dict | list:
        url = f"https://{host}{path}"
        key = api_key or _cfg.RIOT_API_KEY
        if not key:
            raise RiotUnavailable("RIOT_API_KEY is not configured.")
        headers = {"X-Riot-Token": key, "Content-Type": "application/json"}
        async with self._session.post(url, json=payload, headers=headers) as resp:
            if resp.status in (200, 201):
                return await resp.json()
            text = await resp.text()
            raise RiotAPIError(resp.status, text)

    # ------------------------------------------------------------------ #
    # Account                                                              #
    # ------------------------------------------------------------------ #

    async def get_account_by_riot_id(self, game_name: str, tag_line: str) -> dict:
        """Returns {puuid, gameName, tagLine}."""
        platform_host = _platform_host()
        path = f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
        return await self._get(platform_host, path)

    async def get_account_by_puuid(self, puuid: str) -> dict:
        platform_host = _platform_host()
        path = f"/riot/account/v1/accounts/by-puuid/{puuid}"
        return await self._get(platform_host, path)

    # ------------------------------------------------------------------ #
    # Match                                                                #
    # ------------------------------------------------------------------ #

    async def get_match(self, match_id: str) -> dict:
        """Full match data (participants, teams, game duration)."""
        platform_host = _platform_host()
        path = f"/lol/match/v5/matches/{match_id}"
        return await self._get(platform_host, path)

    async def get_match_timeline(self, match_id: str) -> dict:
        """Minute-by-minute frame data used to build the P matrix."""
        platform_host = _platform_host()
        path = f"/lol/match/v5/matches/{match_id}/timeline"
        return await self._get(platform_host, path)

    async def get_recent_match_ids(
        self, puuid: str, queue: int | None = None, count: int = 1,
        match_type: str | None = None, start_time: int | None = None,
    ) -> list[str]:
        """Recent match IDs for a player. queue=0 is custom games; None = all queues."""
        platform_host = _platform_host()
        params = [f"count={count}"]
        if queue is not None:
            params.append(f"queue={queue}")
        if match_type:
            params.append(f"type={match_type}")
        if start_time:
            params.append(f"startTime={start_time}")
        path = f"/lol/match/v5/matches/by-puuid/{puuid}/ids?{'&'.join(params)}"
        return await self._get(platform_host, path)

    # ------------------------------------------------------------------ #
    # Summoner / League (anti-smurf signals)                               #
    # ------------------------------------------------------------------ #

    async def get_summoner_by_puuid(self, puuid: str) -> dict:
        """{id, accountId, puuid, profileIconId, revisionDate, summonerLevel}"""
        return await self._get(_region_host(), f"/lol/summoner/v4/summoners/by-puuid/{puuid}")

    async def get_league_entries_by_puuid(self, puuid: str) -> list[dict]:
        """Ranked entries (tier/rank/wins/losses) per queue for this account."""
        try:
            return await self._get(_region_host(), f"/lol/league/v4/entries/by-puuid/{puuid}")
        except RiotAPIError as e:
            if e.status == 404:
                return []
            raise

    # ------------------------------------------------------------------ #
    # Tournament                                                           #
    # ------------------------------------------------------------------ #

    async def create_tournament_provider(self, region: str, callback_url: str) -> int:
        """Step 1 of tournament code creation. Returns provider ID."""
        platform_host = _platform_host()
        payload = {"region": region.upper(), "url": callback_url}
        result = await self._post(
            platform_host,
            "/lol/tournament/v5/providers",
            payload,
            api_key=_cfg.RIOT_TOURNAMENT_API_KEY or _cfg.RIOT_API_KEY,
        )
        return int(result)

    async def create_tournament(self, provider_id: int, name: str) -> int:
        """Step 2. Returns tournament ID."""
        platform_host = _platform_host()
        payload = {"providerId": provider_id, "name": name}
        result = await self._post(
            platform_host,
            "/lol/tournament/v5/tournaments",
            payload,
            api_key=_cfg.RIOT_TOURNAMENT_API_KEY or _cfg.RIOT_API_KEY,
        )
        return int(result)

    async def create_tournament_code(
        self,
        tournament_id: int,
        team_size: int = 5,
        allowed_summoner_ids: list[str] | None = None,
        metadata: str = "",
    ) -> str:
        """Step 3. Returns the tournament code string."""
        platform_host = _platform_host()
        payload = {
            "mapType": "SUMMONERS_RIFT",
            "pickType": "TOURNAMENT_DRAFT",
            "spectatorType": "ALL",
            "teamSize": team_size,
            "metadata": metadata,
        }
        if allowed_summoner_ids:
            payload["allowedSummonerIds"] = allowed_summoner_ids
        codes = await self._post(
            platform_host,
            f"/lol/tournament/v5/codes?tournamentId={tournament_id}&count=1",
            payload,
            api_key=_cfg.RIOT_TOURNAMENT_API_KEY or _cfg.RIOT_API_KEY,
        )
        return codes[0]

    async def get_match_ids_for_tournament_code(self, tournament_code: str) -> list[str]:
        platform_host = _platform_host()
        path = f"/lol/tournament/v5/codes/{tournament_code}/ids"
        return await self._get(
            platform_host,
            path,
            api_key=_cfg.RIOT_TOURNAMENT_API_KEY or _cfg.RIOT_API_KEY,
        )

