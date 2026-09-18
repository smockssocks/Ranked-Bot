"""
Turn raw Riot match-v5 + timeline JSON into the per-player metric dicts the rating
engine consumes. Pure functions, no I/O.

Where Riot already computes a stat (participant["challenges"]) we use it; where it
does not, or the field is missing, we derive it from the timeline frames/events.
All lane comparisons are against the player in the same teamPosition on the
other team, so a support is never compared to an ADC.
"""
from __future__ import annotations

import math
from typing import Any

from bot.services.rating_engine import METRICS, normalize_role

POSITIONS = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
EPIC_MONSTERS = {"DRAGON", "BARON_NASHOR", "RIFTHERALD", "HORDE", "ATAKHAN", "ELDER_DRAGON"}


def _frame_at(frames: list[dict], minute: int) -> dict | None:
    """Frame closest to `minute` (frames are ~60s apart)."""
    if not frames:
        return None
    target = minute * 60_000
    best = min(frames, key=lambda f: abs(f.get("timestamp", 0) - target))
    if abs(best.get("timestamp", 0) - target) > 90_000:
        return None
    return best


def _pf(frame: dict | None, pid: int) -> dict:
    if not frame:
        return {}
    return frame.get("participantFrames", {}).get(str(pid), {})


def _cs(pf: dict) -> int:
    return int(pf.get("minionsKilled", 0)) + int(pf.get("jungleMinionsKilled", 0))


def lane_opponents(participants: list[dict]) -> dict[int, int | None]:
    """participantId -> opposing participantId in the same position (or None)."""
    by_team_pos: dict[tuple[int, str], int] = {}
    for p in participants:
        pos = (p.get("teamPosition") or p.get("individualPosition") or "").upper()
        if pos in POSITIONS:
            by_team_pos[(p["teamId"], pos)] = p["participantId"]
    out: dict[int, int | None] = {}
    for p in participants:
        pos = (p.get("teamPosition") or p.get("individualPosition") or "").upper()
        other_team = 200 if p["teamId"] == 100 else 100
        out[p["participantId"]] = by_team_pos.get((other_team, pos))
    return out


def _timeline_events(timeline: dict | None) -> list[dict]:
    if not timeline:
        return []
    evs: list[dict] = []
    for f in timeline.get("info", {}).get("frames", []):
        evs.extend(f.get("events", []))
    return evs


def extract_all(match: dict, timeline: dict | None) -> dict[str, dict[str, Any]]:
    """
    Returns {puuid: metrics} for every participant. Each metrics dict contains all
    METRICS keys (floats) plus descriptive extras: role, team, win, champion_id,
    champion_name, kills/deaths/assists, cs_total, gold_earned, vision_score,
    damage_dealt, summoner_level, riot_id, duration_mins, objective counts.
    """
    info = match["info"]
    participants: list[dict] = info["participants"]
    duration_secs = int(info.get("gameDuration") or 0)
    # Older matches report gameDuration in ms
    if duration_secs > 20_000:
        duration_secs //= 1000
    duration_mins = max(1.0, duration_secs / 60.0)

    frames = timeline.get("info", {}).get("frames", []) if timeline else []
    events = _timeline_events(timeline)
    opp = lane_opponents(participants)

    f10, f15 = _frame_at(frames, 10), _frame_at(frames, 15)

    # --- team totals -------------------------------------------------------
    team_dmg: dict[int, float] = {100: 0.0, 200: 0.0}
    team_taken: dict[int, float] = {100: 0.0, 200: 0.0}
    team_kills: dict[int, int] = {100: 0, 200: 0}
    for p in participants:
        team_dmg[p["teamId"]] += float(p.get("totalDamageDealtToChampions", 0))
        team_taken[p["teamId"]] += float(p.get("totalDamageTaken", 0))
        team_kills[p["teamId"]] += int(p.get("kills", 0))

    team_obj_total: dict[int, int] = {100: 0, 200: 0}
    for t in info.get("teams", []):
        objs = t.get("objectives", {})
        total = 0
        for key in ("baron", "dragon", "riftHerald", "tower", "horde", "atakhan"):
            total += int(objs.get(key, {}).get("kills", 0))
        team_obj_total[t["teamId"]] = total

    # --- timeline derived per player --------------------------------------
    deaths_tl: dict[int, int] = {p["participantId"]: 0 for p in participants}
    obj_take_tl: dict[int, int] = {p["participantId"]: 0 for p in participants}
    plates_tl: dict[int, int] = {p["participantId"]: 0 for p in participants}
    solo_kills_tl: dict[int, int] = {p["participantId"]: 0 for p in participants}
    wards_killed_tl: dict[int, int] = {p["participantId"]: 0 for p in participants}
    control_wards_tl: dict[int, int] = {p["participantId"]: 0 for p in participants}
    for ev in events:
        et = ev.get("type")
        if et == "CHAMPION_KILL":
            v = ev.get("victimId")
            if v in deaths_tl:
                deaths_tl[v] += 1
            k = ev.get("killerId")
            if k in solo_kills_tl and not ev.get("assistingParticipantIds"):
                solo_kills_tl[k] += 1
        elif et in ("ELITE_MONSTER_KILL", "BUILDING_KILL"):
            involved = set(ev.get("assistingParticipantIds") or [])
            k = ev.get("killerId")
            if k:
                involved.add(k)
            for pid in involved:
                if pid in obj_take_tl:
                    obj_take_tl[pid] += 1
        elif et == "TURRET_PLATE_DESTROYED":
            k = ev.get("killerId")
            if k in plates_tl:
                plates_tl[k] += 1
        elif et == "WARD_KILL":
            k = ev.get("killerId")
            if k in wards_killed_tl:
                wards_killed_tl[k] += 1
        elif et == "WARD_PLACED" and ev.get("wardType") == "CONTROL_WARD":
            c = ev.get("creatorId")
            if c in control_wards_tl:
                control_wards_tl[c] += 1

    out: dict[str, dict[str, Any]] = {}
    for p in participants:
        pid = p["participantId"]
        team = p["teamId"]
        ch = p.get("challenges") or {}
        opp_pid = opp.get(pid)

        # Laning diffs vs lane opponent
        gd10 = gd15 = xd10 = cd10 = 0.0
        if opp_pid is not None:
            a10, b10 = _pf(f10, pid), _pf(f10, opp_pid)
            a15, b15 = _pf(f15, pid), _pf(f15, opp_pid)
            if a10 and b10:
                gd10 = float(a10.get("totalGold", 0) - b10.get("totalGold", 0))
                xd10 = float(a10.get("xp", 0) - b10.get("xp", 0))
                cd10 = float(_cs(a10) - _cs(b10))
            if a15 and b15:
                gd15 = float(a15.get("totalGold", 0) - b15.get("totalGold", 0))
            elif a10 and b10:
                gd15 = gd10  # game ended / no 15 frame
        elif "laningPhaseGoldExpAdvantage" in ch:
            # Riot's own laning advantage flag (-1..1) as a weak fallback
            gd10 = float(ch.get("earlyLaningPhaseGoldExpAdvantage", 0)) * 400.0
            gd15 = float(ch.get("laningPhaseGoldExpAdvantage", 0)) * 600.0

        traj = _lane_trajectory(frames, pid, opp_pid)

        kills = int(p.get("kills", 0))
        deaths = int(p.get("deaths", deaths_tl.get(pid, 0)))
        assists = int(p.get("assists", 0))
        cs_total = int(p.get("totalMinionsKilled", 0)) + int(p.get("neutralMinionsKilled", 0))
        gold = float(p.get("goldEarned", 0))
        dmg = float(p.get("totalDamageDealtToChampions", 0))
        taken = float(p.get("totalDamageTaken", 0))
        vision = float(p.get("visionScore", 0))
        tk = max(1, team_kills[team])

        kp = float(ch.get("killParticipation", (kills + assists) / tk))
        dmg_share = float(ch.get("teamDamagePercentage", dmg / team_dmg[team] if team_dmg[team] else 0.2))
        taken_share = float(ch.get("damageTakenOnTeamPercentage", taken / team_taken[team] if team_taken[team] else 0.2))

        obj_takedowns = None
        if any(k in ch for k in ("dragonTakedowns", "baronTakedowns", "turretTakedowns", "riftHeraldTakedowns")):
            obj_takedowns = (
                float(ch.get("dragonTakedowns", 0)) + float(ch.get("baronTakedowns", 0))
                + float(ch.get("riftHeraldTakedowns", 0)) + float(ch.get("turretTakedowns", 0))
                + float(ch.get("voidMonsterKill", 0))
            )
        if obj_takedowns is None:
            obj_takedowns = float(obj_take_tl.get(pid, 0))
        obj_part = obj_takedowns / max(1, team_obj_total[team])
        obj_part = min(1.0, obj_part)

        heal_shield = float(ch.get("effectiveHealAndShielding",
                                   p.get("totalHealsOnTeammates", 0) + p.get("totalDamageShieldedOnTeammates", 0)))
        kda_adj = (kills + 0.7 * assists) / max(1, deaths)

        m: dict[str, Any] = {
            "gold_diff_10": gd10,
            "gold_diff_15": gd15,
            "xp_diff_10": xd10,
            "cs_diff_10": cd10,
            "lane_trajectory": traj,
            "cs_per_min": cs_total / duration_mins,
            "gold_per_min": float(ch.get("goldPerMinute", gold / duration_mins)),
            "kill_participation": kp,
            "deaths_per_min": deaths / duration_mins,
            "kda_adj": min(kda_adj, 12.0),
            "damage_share": dmg_share,
            "damage_per_min": float(ch.get("damagePerMinute", dmg / duration_mins)),
            "damage_taken_share": taken_share,
            "solo_kills": float(ch.get("soloKills", solo_kills_tl.get(pid, 0))),
            "objective_participation": obj_part,
            "turret_plates": float(ch.get("turretPlatesTaken", plates_tl.get(pid, 0))),
            "vision_per_min": float(ch.get("visionScorePerMinute", vision / duration_mins)),
            "control_wards": float(ch.get("controlWardsPlaced", max(control_wards_tl.get(pid, 0), p.get("visionWardsBoughtInGame", 0)))),
            "wards_killed": float(ch.get("wardTakedowns", p.get("wardsKilled", wards_killed_tl.get(pid, 0)))),
            "heal_shield_per_min": heal_shield / duration_mins,
            "cc_per_min": float(p.get("timeCCingOthers", 0)) / duration_mins,
            # descriptive extras
            "role": normalize_role(p.get("teamPosition") or p.get("individualPosition")),
            "team": 1 if team == 100 else 2,
            "win": bool(p.get("win", False)),
            "champion_id": p.get("championId"),
            "champion_name": p.get("championName"),
            "kills": kills, "deaths": deaths, "assists": assists,
            "cs_total": cs_total, "gold_earned": int(gold), "vision_score": int(vision),
            "damage_dealt": int(dmg), "summoner_level": int(p.get("summonerLevel", 0)),
            "riot_id": f"{p.get('riotIdGameName', '')}#{p.get('riotIdTagline', '')}",
            "duration_mins": duration_mins,
            "lane_opponent_pid": opp_pid,
            "participant_id": pid,
        }
        for k in METRICS:
            m.setdefault(k, 0.0)
        out[p["puuid"]] = m
    return out


def _lane_trajectory(frames: list[dict], pid: int, opp_pid: int | None) -> float:
    """
    Time-weighted gold-diff curve vs lane opponent over minutes 4..20, scaled so
    a typical value is ~unit. Early minutes weigh more (lane is what the player
    controls); later minutes reflect the whole team. Returns 0 if no opponent.
    """
    if opp_pid is None or not frames:
        return 0.0
    total, wsum = 0.0, 0.0
    for f in frames:
        minute = f.get("timestamp", 0) / 60_000
        if minute < 4 or minute > 20:
            continue
        a, b = _pf(f, pid), _pf(f, opp_pid)
        if not a or not b:
            continue
        diff = float(a.get("totalGold", 0) - b.get("totalGold", 0))
        scale = 60.0 * minute + 200.0        # expected spread grows with time
        w = math.exp(-0.06 * minute)
        total += w * (diff / scale)
        wsum += w
    return (total / wsum) * 2.5 if wsum else 0.0


def in_game_pools(all_metrics: dict[str, dict[str, Any]]) -> dict[str, list[float]]:
    """{metric: [value for each of the 10 players]} for in-game normalisation."""
    pools: dict[str, list[float]] = {k: [] for k in METRICS}
    for m in all_metrics.values():
        for k in METRICS:
            pools[k].append(float(m.get(k, 0.0)))
    return pools


def is_remake(match: dict, min_minutes: float) -> bool:
    info = match.get("info", {})
    dur = int(info.get("gameDuration") or 0)
    if dur > 20_000:
        dur //= 1000
    if dur < min_minutes * 60:
        return True
    return any(p.get("gameEndedInEarlySurrender") for p in info.get("participants", []))
