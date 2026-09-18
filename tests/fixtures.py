"""Synthetic Riot match + timeline generator for tests (no network)."""
from __future__ import annotations

import random

POSITIONS = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
CHAMPS = {"TOP": "Garen", "JUNGLE": "LeeSin", "MIDDLE": "Ahri", "BOTTOM": "Jinx", "UTILITY": "Thresh"}


def make_match(
    match_id: str = "NA1_1",
    duration_mins: int = 30,
    winner: int = 100,
    puuids: list[str] | None = None,
    overrides: dict[int, dict] | None = None,
    queue_id: int = 0,
    seed: int = 1,
) -> tuple[dict, dict]:
    """
    Returns (match_json, timeline_json) with 10 participants (ids 1..10).
    overrides: participantId -> dict of participant fields (e.g. kills, deaths, cs_rate, gold_rate).
    """
    rng = random.Random(seed)
    puuids = puuids or [f"puuid-{i}" for i in range(1, 11)]
    overrides = overrides or {}
    participants = []
    for i in range(1, 11):
        team = 100 if i <= 5 else 200
        pos = POSITIONS[(i - 1) % 5]
        won = team == winner
        base = {
            "participantId": i, "puuid": puuids[i - 1], "teamId": team, "teamPosition": pos,
            "individualPosition": pos, "championId": 1 + i, "championName": CHAMPS[pos],
            "win": won, "kills": 4 if won else 2, "deaths": 2 if won else 4, "assists": 6 if won else 3,
            "totalMinionsKilled": 0 if pos == "UTILITY" else (150 if pos == "JUNGLE" else 200),
            "neutralMinionsKilled": 60 if pos == "JUNGLE" else 0,
            "goldEarned": 12000 if won else 10000, "visionScore": 60 if pos == "UTILITY" else 25,
            "totalDamageDealtToChampions": 20000 if pos != "UTILITY" else 8000,
            "totalDamageTaken": 25000 if pos == "TOP" else 15000, "timeCCingOthers": 30,
            "totalHealsOnTeammates": 3000 if pos == "UTILITY" else 0, "totalDamageShieldedOnTeammates": 0,
            "wardsKilled": 3, "visionWardsBoughtInGame": 2, "summonerLevel": 200,
            "riotIdGameName": f"Player{i}", "riotIdTagline": "NA1", "gameEndedInEarlySurrender": False,
            "cs_rate": None,
        }
        base.update(overrides.get(i, {}))
        cs_rate = base.pop("cs_rate")
        gold_rate = base.pop("gold_rate", None)
        base["_cs_rate"] = cs_rate
        base["_gold_rate"] = gold_rate
        participants.append(base)

    teams = [
        {"teamId": 100, "win": winner == 100, "objectives": {"baron": {"kills": 1 if winner == 100 else 0}, "dragon": {"kills": 3 if winner == 100 else 1},
                                                             "riftHerald": {"kills": 1}, "tower": {"kills": 8 if winner == 100 else 3}, "horde": {"kills": 3}}},
        {"teamId": 200, "win": winner == 200, "objectives": {"baron": {"kills": 1 if winner == 200 else 0}, "dragon": {"kills": 3 if winner == 200 else 1},
                                                             "riftHerald": {"kills": 0}, "tower": {"kills": 8 if winner == 200 else 3}, "horde": {"kills": 3}}},
    ]
    match = {"metadata": {"matchId": match_id, "participants": puuids},
             "info": {"gameDuration": duration_mins * 60, "gameCreation": 1_700_000_000_000, "queueId": queue_id,
                      "gameType": "CUSTOM_GAME" if queue_id == 0 else "MATCHED_GAME", "participants": participants, "teams": teams}}

    # timeline: one frame per minute
    frames = []
    for minute in range(0, duration_mins + 1):
        pf = {}
        for p in participants:
            i = p["participantId"]
            pos = p["teamPosition"]
            cs_rate = p["_cs_rate"] if p["_cs_rate"] is not None else (0.5 if pos == "UTILITY" else 6.5 if pos != "JUNGLE" else 5.0)
            gold_rate = p["_gold_rate"] if p["_gold_rate"] is not None else (330 if pos == "UTILITY" else 400)
            pf[str(i)] = {"participantId": i, "totalGold": int(500 + gold_rate * minute + rng.randint(-50, 50)),
                          "xp": int(300 * minute), "minionsKilled": int(cs_rate * minute) if pos != "JUNGLE" else 0,
                          "jungleMinionsKilled": int(cs_rate * minute) if pos == "JUNGLE" else 0, "level": min(18, 1 + minute // 2)}
        events = []
        if minute in (8, 14, 22, 27):
            events.append({"type": "CHAMPION_KILL", "killerId": 3, "victimId": 8, "assistingParticipantIds": [2], "timestamp": minute * 60000})
            events.append({"type": "CHAMPION_KILL", "killerId": 9, "victimId": 1, "assistingParticipantIds": [], "timestamp": minute * 60000 + 10})
        if minute == 10:
            events.append({"type": "ELITE_MONSTER_KILL", "killerId": 2, "monsterType": "DRAGON", "assistingParticipantIds": [4, 5], "timestamp": minute * 60000})
        if minute == 12:
            events.append({"type": "TURRET_PLATE_DESTROYED", "killerId": 1, "teamId": 200, "timestamp": minute * 60000})
            events.append({"type": "BUILDING_KILL", "killerId": 4, "buildingType": "TOWER_BUILDING", "assistingParticipantIds": [5], "timestamp": minute * 60000})
        if minute == 15:
            events.append({"type": "WARD_KILL", "killerId": 5, "timestamp": minute * 60000})
            events.append({"type": "WARD_PLACED", "creatorId": 5, "wardType": "CONTROL_WARD", "timestamp": minute * 60000})
        frames.append({"timestamp": minute * 60000, "participantFrames": pf, "events": events})
    for p in participants:
        p.pop("_cs_rate"); p.pop("_gold_rate")
    timeline = {"metadata": {"matchId": match_id}, "info": {"frameInterval": 60000, "frames": frames}}
    return match, timeline
