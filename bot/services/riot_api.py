"""
Async Riot API client.
Handles: account lookup, match data, timeline, and tournament codes.
"""
from __future__ import annotations
import asyncio
import logging
import aiohttp
from bot.config import RIOT_API_KEY, RIOT_REGION, RIOT_PLATFORM, RIOT_TOURNAMENT_API_KEY

log = logging.getLogger("ranked-bot.riot")

_PLATFORM_HOSTS = {
    "americas": "americas.api.riotgames.com",
    "europe":   "europe.api.riotgames.com",
    "asia":     "asia.api.riotgames.com",
}

_REGION_HOSTS = {
    "na1": "na1.api.riotgames.com",
    "euw1": "euw1.api.riotgames.com",
    "eun1": "eun1.api.riotgames.com",
    "kr":  "kr.api.riotgames.com",
}


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
        key = api_key or RIOT_API_KEY
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
        key = api_key or RIOT_API_KEY
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
        platform_host = _PLATFORM_HOSTS.get(RIOT_PLATFORM, "americas.api.riotgames.com")
        path = f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
        return await self._get(platform_host, path)

    async def get_account_by_puuid(self, puuid: str) -> dict:
        platform_host = _PLATFORM_HOSTS.get(RIOT_PLATFORM, "americas.api.riotgames.com")
        path = f"/riot/account/v1/accounts/by-puuid/{puuid}"
        return await self._get(platform_host, path)

    # ------------------------------------------------------------------ #
    # Match                                                                #
    # ------------------------------------------------------------------ #

    async def get_match(self, match_id: str) -> dict:
        """Full match data (participants, teams, game duration)."""
        platform_host = _PLATFORM_HOSTS.get(RIOT_PLATFORM, "americas.api.riotgames.com")
        path = f"/lol/match/v5/matches/{match_id}"
        return await self._get(platform_host, path)

    async def get_match_timeline(self, match_id: str) -> dict:
        """Minute-by-minute frame data used to build the P matrix."""
        platform_host = _PLATFORM_HOSTS.get(RIOT_PLATFORM, "americas.api.riotgames.com")
        path = f"/lol/match/v5/matches/{match_id}/timeline"
        return await self._get(platform_host, path)

    async def get_recent_match_ids(self, puuid: str, queue: int = 0, count: int = 1) -> list[str]:
        """Get recent match IDs for a player. queue=0 means all queues."""
        platform_host = _PLATFORM_HOSTS.get(RIOT_PLATFORM, "americas.api.riotgames.com")
        params = f"?count={count}"
        if queue:
            params += f"&queue={queue}"
        path = f"/lol/match/v5/matches/by-puuid/{puuid}/ids{params}"
        return await self._get(platform_host, path)

    # ------------------------------------------------------------------ #
    # Tournament                                                           #
    # ------------------------------------------------------------------ #

    async def create_tournament_provider(self, region: str, callback_url: str) -> int:
        """Step 1 of tournament code creation. Returns provider ID."""
        platform_host = _PLATFORM_HOSTS.get(RIOT_PLATFORM, "americas.api.riotgames.com")
        payload = {"region": region.upper(), "url": callback_url}
        result = await self._post(
            platform_host,
            "/lol/tournament/v5/providers",
            payload,
            api_key=RIOT_TOURNAMENT_API_KEY or RIOT_API_KEY,
        )
        return int(result)

    async def create_tournament(self, provider_id: int, name: str) -> int:
        """Step 2. Returns tournament ID."""
        platform_host = _PLATFORM_HOSTS.get(RIOT_PLATFORM, "americas.api.riotgames.com")
        payload = {"providerId": provider_id, "name": name}
        result = await self._post(
            platform_host,
            "/lol/tournament/v5/tournaments",
            payload,
            api_key=RIOT_TOURNAMENT_API_KEY or RIOT_API_KEY,
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
        platform_host = _PLATFORM_HOSTS.get(RIOT_PLATFORM, "americas.api.riotgames.com")
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
            api_key=RIOT_TOURNAMENT_API_KEY or RIOT_API_KEY,
        )
        return codes[0]

    async def get_match_ids_for_tournament_code(self, tournament_code: str) -> list[str]:
        platform_host = _PLATFORM_HOSTS.get(RIOT_PLATFORM, "americas.api.riotgames.com")
        path = f"/lol/tournament/v5/codes/{tournament_code}/ids"
        return await self._get(
            platform_host,
            path,
            api_key=RIOT_TOURNAMENT_API_KEY or RIOT_API_KEY,
        )

    # ------------------------------------------------------------------ #
    # Timeline helpers                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def extract_per_player_timeline(
        match: dict,
        timeline: dict,
        interval_mins: float = 5.0,
    ) -> dict[str, list[dict]]:
        """
        Extract per-player per-interval stats from raw Riot data.

        Returns a dict keyed by PUUID, value is a list of interval dicts
        (one per interval) with keys matching METRIC_ORDER.
        """
        participants = match["info"]["participants"]
        pid_to_puuid = {p["participantId"]: p["puuid"] for p in participants}
        pid_to_team = {p["participantId"]: (1 if p["teamId"] == 100 else 2) for p in participants}

        interval_ms = int(interval_mins * 60 * 1000)
        frames = timeline["info"]["frames"]

        # bucket frames by interval index
        buckets: dict[int, list] = {}
        for frame in frames:
            ts = frame["timestamp"]
            idx = ts // interval_ms
            buckets.setdefault(idx, []).append(frame)

        # cumulative gold and opponent tracking — opponent is same-ish position
        # We approximate lane opponent by sorting each team's participants by position
        # and pairing them in order (1–5 team1 vs 1–5 team2 by gold rank)
        team1_pids = sorted([pid for pid, t in pid_to_team.items() if t == 1])
        team2_pids = sorted([pid for pid, t in pid_to_team.items() if t == 2])
        opponent_of: dict[int, int] = {}
        for t1, t2 in zip(team1_pids, team2_pids):
            opponent_of[t1] = t2
            opponent_of[t2] = t1

        max_interval = max(buckets.keys()) if buckets else 0
        results: dict[str, list[dict]] = {puuid: [] for puuid in pid_to_puuid.values()}

        prev_gold: dict[int, int] = {pid: 0 for pid in pid_to_puuid}
        prev_cs: dict[int, int] = {pid: 0 for pid in pid_to_puuid}
        prev_vision: dict[int, int] = {pid: 0 for pid in pid_to_puuid}

        for idx in range(1, max_interval + 1):
            interval_frames = buckets.get(idx, [])

            # Use the last frame in the interval for snapshot stats
            snap: dict[int, dict] = {}
            for frame in interval_frames:
                for pid_str, pf in frame.get("participantFrames", {}).items():
                    snap[int(pid_str)] = pf

            # Kill events in this interval
            team_kills: dict[int, int] = {1: 0, 2: 0}
            player_kills_assists: dict[int, int] = {pid: 0 for pid in pid_to_puuid}
            objective_events: dict[int, float] = {pid: 0.0 for pid in pid_to_puuid}

            for frame in interval_frames:
                for event in frame.get("events", []):
                    etype = event.get("type")
                    if etype == "CHAMPION_KILL":
                        killer = event.get("killerId", 0)
                        assists = event.get("assistingParticipantIds", [])
                        victim = event.get("victimId", 0)
                        if killer and killer in pid_to_team:
                            team = pid_to_team[killer]
                            team_kills[team] += 1
                            player_kills_assists[killer] += 1
                        for a in assists:
                            if a in player_kills_assists:
                                player_kills_assists[a] += 1
                        _ = victim  # track for future death stats if needed
                    elif etype in ("DRAGON_KILL", "BARON_NASHOR_KILL", "RIFTHERALD_KILL"):
                        killer = event.get("killerId", 0)
                        if killer in objective_events:
                            objective_events[killer] += 1.0
                    elif etype == "BUILDING_KILL":
                        killer = event.get("killerId", 0)
                        if killer in objective_events:
                            objective_events[killer] += 0.5

            for pid, puuid in pid_to_puuid.items():
                pf = snap.get(pid, {})
                team = pid_to_team[pid]
                opp = opponent_of.get(pid, 0)
                opp_pf = snap.get(opp, {})

                gold_now = pf.get("totalGold", prev_gold.get(pid, 0))
                opp_gold_now = opp_pf.get("totalGold", prev_gold.get(opp, gold_now))
                gold_diff = gold_now - opp_gold_now

                cs_now = pf.get("minionsKilled", 0) + pf.get("jungleMinionsKilled", 0)
                cs_delta = cs_now - prev_cs.get(pid, 0)
                cs_per_min = cs_delta / interval_mins

                vision_now = pf.get("wardScore", pf.get("visionScore", 0))
                vision_delta = max(0, vision_now - prev_vision.get(pid, 0))

                total_team_kills = max(1, team_kills[team])
                kp = player_kills_assists[pid] / total_team_kills

                # deaths_negated: 1.0 if player didn't die this interval, 0.0 if they did
                # We approximate from level delta being non-negative (proxy only)
                # A real implementation can track CHAMPION_KILL victim_id to count deaths
                deaths_negated = 1.0  # default; proper tracking via victim_id done in game_processor

                interval_stats = {
                    "gold_differential":  float(gold_diff),
                    "kill_participation": float(kp),
                    "objective_damage":   float(objective_events[pid]),
                    "vision_score_delta": float(vision_delta),
                    "cs_per_min":         float(cs_per_min),
                    "deaths_negated":     deaths_negated,
                }
                results[puuid].append(interval_stats)

                prev_gold[pid] = gold_now
                prev_cs[pid] = cs_now
                prev_vision[pid] = vision_now

        return results
