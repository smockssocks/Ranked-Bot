"""
Async Riot API client.
Handles: account lookup, match data, timeline, and tournament codes.
"""
from __future__ import annotations
import asyncio
import logging
import re
import socket

import aiohttp
from bot import config as _cfg

log = logging.getLogger("ranked-bot.riot")

_PLATFORM_HOSTS = {
    "americas": "americas.api.riotgames.com",   # na1, br1, la1, la2
    "europe":   "europe.api.riotgames.com",     # euw1, eun1, tr1, ru, me1
    "asia":     "asia.api.riotgames.com",       # kr, jp1
    "sea":      "sea.api.riotgames.com",        # oc1, ph2, sg2, th2, tw2, vn2
}

_REGION_HOSTS = {
    r: f"{r}.api.riotgames.com"
    for r in ("na1", "euw1", "eun1", "kr", "br1", "la1", "la2", "oc1", "tr1", "ru", "jp1", "ph2", "sg2", "th2", "tw2", "vn2", "me1")
}


def _platform_host() -> str:
    return _PLATFORM_HOSTS.get(_cfg.RIOT_PLATFORM, "americas.api.riotgames.com")


def _region_host() -> str:
    return _REGION_HOSTS.get(_cfg.RIOT_REGION, f"{_cfg.RIOT_REGION}.api.riotgames.com")


_PLATFORM_PREFIXES = {r.upper() for r in _REGION_HOSTS}
_MATCH_WITH_PREFIX = re.compile(r"\b([A-Za-z]{2,4}\d?)[_\-\s]?(\d{8,})\b")
_LONG_DIGITS = re.compile(r"\d{8,}")
# Region slugs used in stats-site URLs, e.g. leagueofgraphs.com/match/euw/7012345678
_URL_SLUG = re.compile(r"/([a-z]{2,4})/(\d{8,})", re.I)
_SLUG_TO_PLATFORM = {
    "na": "NA1", "euw": "EUW1", "eune": "EUN1", "kr": "KR", "br": "BR1", "lan": "LA1", "las": "LA2",
    "oce": "OC1", "tr": "TR1", "ru": "RU", "jp": "JP1", "ph": "PH2", "sg": "SG2", "th": "TH2",
    "tw": "TW2", "vn": "VN2", "me": "ME1",
}


def normalize_match_id(raw: str, region: str | None = None) -> str:
    """
    Turn whatever a player pastes into the match-v5 ID Riot expects, e.g. NA1_5650481942.

    The League client's post-game screen shows only the number, so a bare number gets
    this server's region prefix. Also accepted: na1_123, NA1-123, "NA1 123", and links
    from sites like leagueofgraphs that contain the number. Raises ValueError with a
    readable message when there is no plausible game ID in the text.
    """
    text = (raw or "").strip()
    region = (region or _cfg.RIOT_REGION or "na1").upper()
    m = _MATCH_WITH_PREFIX.search(text)
    if m and m.group(1).upper() in _PLATFORM_PREFIXES:
        return f"{m.group(1).upper()}_{m.group(2)}"
    u = _URL_SLUG.search(text)
    if u and u.group(1).lower() in _SLUG_TO_PLATFORM:
        return f"{_SLUG_TO_PLATFORM[u.group(1).lower()]}_{u.group(2)}"
    digits = _LONG_DIGITS.findall(text)
    if digits:
        return f"{region}_{max(digits, key=len)}"
    raise ValueError(
        "That doesn't look like a game ID. Use the number from the post-game screen in the "
        f"League client, for example `5650481942`. The bot adds the region (`{region}_`) itself."
    )


class RiotUnavailable(Exception):
    """Raised when no Riot API key is configured."""


class RiotAPIError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.body = message
        super().__init__(f"Riot API {status}: {message}")


class RiotConnectionError(RiotAPIError):
    """
    The request never reached Riot: the address lookup (DNS) failed, the connection
    was refused, TLS failed, or it timed out. A problem with the network of the
    computer running the bot, never with the API key or the ID that was looked up.
    Subclasses RiotAPIError so every existing handler already catches it.
    """
    def __init__(self, host: str, kind: str, detail: str):
        self.host = host
        self.kind = kind          # "dns" | "timeout" | "tls" | "connect"
        super().__init__(0, f"{kind} error reaching {host}: {detail}")


# Network blips are common on home connections; retry these before giving up.
CONNECT_RETRIES = 3
CONNECT_BACKOFF_SECS = 1.5
REQUEST_TIMEOUT_SECS = 30


def classify_connection_error(exc: BaseException) -> str:
    dns_cls = getattr(aiohttp, "ClientConnectorDNSError", None)
    text = str(exc).lower()
    if (dns_cls is not None and isinstance(exc, dns_cls)) or isinstance(exc, socket.gaierror) \
            or "getaddrinfo" in text or "name or service not known" in text or "nodename nor servname" in text:
        return "dns"
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    ssl_cls = tuple(c for c in (getattr(aiohttp, "ClientSSLError", None),
                                getattr(aiohttp, "ClientConnectorCertificateError", None)) if c)
    if ssl_cls and isinstance(exc, ssl_cls):
        return "tls"
    return "connect"


def friendly_error(e: Exception, context: str = "account", match_id: str | None = None) -> str:
    """
    Turn a Riot failure into something a server admin can actually act on.
    context: "account" when looking up a Riot ID, "match" when fetching a game.
    """
    if isinstance(e, RiotUnavailable):
        return ("The bot has no Riot API key configured. An admin needs to put one in the "
                "`.env` file and restart the bot.")
    if isinstance(e, RiotConnectionError):
        if e.kind == "dns":
            return (f"**The bot couldn't find Riot's server** (`{e.host}`).\n"
                    "This is a network problem on the computer running the bot. Your API key and "
                    "the ID you entered were never checked. It is usually temporary, so try again in a minute.\n"
                    "Admin, if it keeps happening: turn off any VPN, pause ad-blocking or DNS-filtering "
                    "software, and run CHECK-RIOT-KEY.bat to test the connection.")
        if e.kind == "timeout":
            return "Riot took too long to answer. Try again in a minute."
        return ("**The bot couldn't connect to Riot's servers.**\n"
                "Admin: check the bot computer's internet connection, and that a firewall or "
                "antivirus is not blocking Python.")
    if not isinstance(e, RiotAPIError):
        return f"Unexpected error talking to Riot: {e}"
    body = (getattr(e, "body", "") or "").lower()
    if e.status in (401, 403):
        if "unknown" in body:
            return ("**Riot rejected the bot's API key.**\n"
                    "Admin: this usually means the bot was not restarted after the key was "
                    "changed, or the key was copied incompletely. Stop the bot, run "
                    "`tools/check_riot_key.py` (or CHECK-RIOT-KEY.bat on Windows) to see "
                    "exactly what is wrong, then start it again.")
        return ("**The bot's Riot API key has expired.**\n"
                "Admin: development keys last only 24 hours. Get a fresh key at "
                "https://developer.riotgames.com, paste it into `.env`, and restart the bot. "
                "Apply for a Personal API Key there to stop this happening daily.")
    if e.status == 404 and context == "match":
        shown = f"`{match_id}`" if match_id else "that game"
        return (f"Riot has no record of {shown}.\n"
                "- Games can take a few minutes to appear after they end. Try again shortly.\n"
                f"- Check the number, and that the region is right. This bot is set to "
                f"`{_cfg.RIOT_REGION.upper()}`; a game played on another server has a different prefix.\n"
                "- Only games that actually finished are recorded. A lobby that dodged or "
                "was abandoned in champ select has no match.")
    if e.status == 400 and context == "match":
        return (f"Riot rejected {f'`{match_id}`' if match_id else 'that ID'} as malformed. Use the number "
                "from the post-game screen, for example `5650481942`.")
    if e.status == 404:
        return ("That Riot ID does not exist. Check the spelling and remember it is "
                "`GameName#TAG`, exactly as shown in the League client. The tag is the part "
                "after the `#`, and it is not always your region.")
    if e.status == 429:
        return "Riot is rate limiting the bot. Wait a minute and try again."
    if e.status >= 500:
        return "Riot's API is having problems right now. Try again in a few minutes."
    return f"Riot API error {e.status}. Details: {getattr(e, 'body', '')[:200]}"


class RiotClient:
    def __init__(self, session: aiohttp.ClientSession | None = None):
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self):
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECS))
        return self

    async def __aexit__(self, *_):
        if self._owns_session and self._session:
            await self._session.close()

    async def _request(self, method: str, host: str, path: str, api_key: str | None = None,
                       payload: dict | None = None) -> dict | list:
        url = f"https://{host}{path}"
        key = api_key or _cfg.RIOT_API_KEY
        if not key:
            raise RiotUnavailable("RIOT_API_KEY is not configured.")
        headers = {"X-Riot-Token": key}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        rate_delay, rate_tries, conn_tries = 1.0, 0, 0
        while True:
            try:
                async with self._session.request(method, url, headers=headers, json=payload) as resp:
                    if resp.status in (200, 201):
                        return await resp.json()
                    if resp.status == 429 and rate_tries < 3:
                        rate_tries += 1
                        wait = float(resp.headers.get("Retry-After", rate_delay))
                        log.warning("Rate limited on %s, waiting %.1fs", path, wait)
                        await asyncio.sleep(wait)
                        rate_delay *= 2
                        continue
                    if resp.status == 404:
                        raise RiotAPIError(404, f"Not found: {path}")
                    raise RiotAPIError(resp.status, await resp.text())
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError, socket.gaierror) as e:
                conn_tries += 1
                kind = classify_connection_error(e)
                if conn_tries >= CONNECT_RETRIES:
                    log.error("Giving up on %s after %d tries (%s): %s", host, conn_tries, kind, e)
                    raise RiotConnectionError(host, kind, str(e)) from e
                log.warning("Could not reach %s (%s), retry %d/%d", host, kind, conn_tries, CONNECT_RETRIES - 1)
                await asyncio.sleep(CONNECT_BACKOFF_SECS * conn_tries)

    async def _get(self, host: str, path: str, api_key: str | None = None) -> dict:
        return await self._request("GET", host, path, api_key=api_key)

    async def _post(self, host: str, path: str, payload: dict, api_key: str | None = None) -> dict | list:
        return await self._request("POST", host, path, api_key=api_key, payload=payload)

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
            api_key=_cfg.tournament_api_key() or _cfg.RIOT_API_KEY,
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
            api_key=_cfg.tournament_api_key() or _cfg.RIOT_API_KEY,
        )
        return int(result)

    async def create_tournament_code(
        self,
        tournament_id: int,
        allowed_participants: list[str] | None = None,
        team_size: int = 5,
        metadata: str = "",
    ) -> str:
        """
        Step 3 (tournament-v5). Returns a code players paste into the League client.
        allowedParticipants are PUUIDs (v4 used summoner IDs), and v5 requires enoughPlayers:
        true only when the allowed list can fill both teams.
        """
        platform_host = _platform_host()
        participants = list(allowed_participants or [])
        payload = {
            "mapType": "SUMMONERS_RIFT",
            "pickType": "TOURNAMENT_DRAFT",
            "spectatorType": "ALL",
            "teamSize": team_size,
            "metadata": metadata[:1000],
            "enoughPlayers": len(participants) >= team_size * 2,
        }
        if participants:
            payload["allowedParticipants"] = participants
        codes = await self._post(
            platform_host,
            f"/lol/tournament/v5/codes?tournamentId={tournament_id}&count=1",
            payload,
            api_key=_cfg.tournament_api_key() or _cfg.RIOT_API_KEY,
        )
        return codes[0]

    async def tournament_access(self) -> int:
        """
        Ask Riot, without creating anything, whether the tournament key may use tournament-v5.
        Looks up a code that cannot exist: 403 means no tournament access, while 400/404 mean
        the key got past Riot's access check. Returns the HTTP status.
        """
        try:
            await self._get(_platform_host(), "/lol/tournament/v5/codes/NA0000-ACCESSCHECK",
                            api_key=_cfg.tournament_api_key() or _cfg.RIOT_API_KEY)
            return 200
        except RiotAPIError as e:
            return e.status

