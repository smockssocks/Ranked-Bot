"""
Anti-smurf detection. Three independent signals, all reviewed by humans:

  account_signal        at link time: low summoner level, no/limited ranked history,
                        or a Riot rank far above what a fresh account should have.
  performance_anomaly   after games: a newcomer whose performance score is far above
                        what their hidden MMR predicts (plays like a much better player).
  fingerprint_match     after games: a newcomer whose champion pool, role split,
                        stat profile and play-time pattern look like an existing
                        member's. Two accounts that were ever in the same game are
                        never matched (they cannot be one person).

Pure helpers (build_fingerprint, similarity) are testable without a DB.
"""
from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import config
from bot.models.game import Game, GameParticipant
from bot.models.player import Player
from bot.models.rating import PlayerRating
from bot.models.smurf import SmurfFlag
from bot.services import rating_engine as re_

log = logging.getLogger("ranked-bot.smurf")

PERF_KEYS = ["cs_per_min", "kill_participation", "vision_per_min", "damage_share", "deaths_per_min", "kda_adj", "gold_per_min"]
_TIER_ORDER = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"]


@dataclass
class Fingerprint:
    player_id: int
    games: int
    champions: Counter = field(default_factory=Counter)      # champion_name -> games
    roles: Counter = field(default_factory=Counter)          # role -> games
    perf: dict[str, float] = field(default_factory=dict)     # mean of PERF_KEYS + perf_score
    hours: list[float] = field(default_factory=lambda: [0.0] * 24)   # play-hour histogram (UTC)
    game_ids: set[int] = field(default_factory=set)
    first_game: datetime | None = None
    last_game: datetime | None = None


def build_fingerprint(player_id: int, rows: list[tuple[GameParticipant, Game]]) -> Fingerprint:
    fp = Fingerprint(player_id=player_id, games=len(rows))
    sums: dict[str, float] = {k: 0.0 for k in PERF_KEYS + ["perf_score"]}
    for part, game in rows:
        if part.champion_name:
            fp.champions[part.champion_name] += 1
        fp.roles[part.role] += 1
        m = part.metrics or {}
        for k in PERF_KEYS:
            sums[k] += float(m.get(k, 0.0))
        sums["perf_score"] += float(part.impact_score or 0.0)
        fp.game_ids.add(game.id)
        if game.played_at:
            played = game.played_at if game.played_at.tzinfo else game.played_at.replace(tzinfo=timezone.utc)
            fp.hours[played.hour] += 1
            fp.first_game = min(fp.first_game, played) if fp.first_game else played
            fp.last_game = max(fp.last_game, played) if fp.last_game else played
    n = max(1, len(rows))
    fp.perf = {k: v / n for k, v in sums.items()}
    return fp


def _cosine(a: dict[str, float] | Counter, b: dict[str, float] | Counter) -> float:
    keys = set(a) | set(b)
    if not keys:
        return 0.0
    dot = sum(a.get(k, 0) * b.get(k, 0) for k in keys)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def _hist_sim(a: list[float], b: list[float]) -> float:
    sa, sb = sum(a), sum(b)
    if not sa or not sb:
        return 0.5  # unknown: neutral
    # histogram intersection on normalised, lightly smoothed histograms
    pa = [(v + 0.2) / (sa + 4.8) for v in a]
    pb = [(v + 0.2) / (sb + 4.8) for v in b]
    return sum(min(x, y) for x, y in zip(pa, pb))


def _perf_sim(a: dict[str, float], b: dict[str, float]) -> float:
    d2 = 0.0
    n = 0
    for k in PERF_KEYS:
        std = re_.METRICS[k][2]
        diff = (a.get(k, 0.0) - b.get(k, 0.0)) / std
        d2 += diff * diff
        n += 1
    diff = a.get("perf_score", 0.0) - b.get("perf_score", 0.0)
    d2 += diff * diff
    n += 1
    return math.exp(-math.sqrt(d2 / n) * 1.6)


def similarity(new: Fingerprint, old: Fingerprint) -> tuple[float, dict[str, float]]:
    """0..1 similarity between a newcomer's fingerprint and an existing member's."""
    if new.game_ids & old.game_ids:
        return 0.0, {"same_game": 1.0}
    champ = _cosine(new.champions, old.champions)
    role = _cosine(new.roles, old.roles)
    perf = _perf_sim(new.perf, old.perf)
    hours = _hist_sim(new.hours, old.hours)
    # A large overlapping active period with no shared games is suspicious in itself;
    # sequential activity (old account stops, new starts) is the classic pattern.
    seq_bonus = 0.0
    if old.last_game and new.first_game and old.last_game <= new.first_game:
        seq_bonus = 0.05
    score = 0.45 * champ + 0.15 * role + 0.30 * perf + 0.10 * hours + seq_bonus
    return min(1.0, score), {"champion_pool": champ, "roles": role, "stat_profile": perf, "play_hours": hours, "sequential": seq_bonus}


# --------------------------------------------------------------------------- #
# Account signal (link time)                                                  #
# --------------------------------------------------------------------------- #

def account_signal(summoner: dict | None, league_entries: list[dict] | None) -> tuple[float, dict[str, Any]]:
    """Score 0..1 that a freshly linked account is not the player's main."""
    level = int((summoner or {}).get("summonerLevel", 0) or 0)
    evidence: dict[str, Any] = {"summoner_level": level}
    score = 0.0
    if level and level < config.SMURF_LOW_LEVEL:
        score += 0.5 * (1 - level / config.SMURF_LOW_LEVEL)
    solo = next((e for e in (league_entries or []) if e.get("queueType") == "RANKED_SOLO_5x5"), None)
    if solo:
        tier = str(solo.get("tier", "")).upper()
        wins, losses = int(solo.get("wins", 0)), int(solo.get("losses", 0))
        evidence.update({"solo_tier": tier, "solo_rank": solo.get("rank"), "solo_wins": wins, "solo_losses": losses})
        total = wins + losses
        wr = wins / total if total else 0
        if total and wr >= 0.65 and total < 60:
            score += 0.3 * min(1.0, (wr - 0.65) / 0.2 + 0.5)
            evidence["high_wr_low_games"] = round(wr, 2)
        if tier in _TIER_ORDER and _TIER_ORDER.index(tier) >= _TIER_ORDER.index("DIAMOND") and level < 100:
            score += 0.35
            evidence["high_tier_low_level"] = True
    else:
        evidence["solo_tier"] = None
        if level and level < config.SMURF_LOW_LEVEL:
            score += 0.3
            evidence["no_ranked_history"] = True
    return min(1.0, score), evidence


async def evaluate_link(session: AsyncSession, player: Player, summoner: dict | None, league_entries: list[dict] | None) -> SmurfFlag | None:
    if not config.SMURF_ENABLED:
        return None
    score, evidence = account_signal(summoner, league_entries)
    if score < 0.5:
        return None
    if await _has_open_flag(session, player.id, "account_signal"):
        return None
    flag = SmurfFlag(
        player_id=player.id, kind="account_signal", score=round(score, 3), evidence=evidence,
        summary=(f"{player.discord_username} linked {player.summoner_name}: level {evidence.get('summoner_level')}, "
                 f"solo {evidence.get('solo_tier') or 'unranked'}"),
    )
    session.add(flag)
    await session.commit()
    return flag


# --------------------------------------------------------------------------- #
# After-game signals                                                          #
# --------------------------------------------------------------------------- #

async def _has_open_flag(session: AsyncSession, player_id: int, kind: str, matched_id: int | None = None) -> bool:
    q = select(SmurfFlag).where(SmurfFlag.player_id == player_id, SmurfFlag.kind == kind,
                                SmurfFlag.status.in_(["open", "confirmed"]))
    if matched_id is not None:
        q = q.where(SmurfFlag.matched_player_id == matched_id)
    return (await session.scalar(q)) is not None


async def _rows_for(session: AsyncSession, player_id: int, season: int) -> list[tuple[GameParticipant, Game]]:
    res = await session.execute(
        select(GameParticipant, Game).join(Game, Game.id == GameParticipant.game_id)
        .where(GameParticipant.player_id == player_id, Game.season == season, Game.status == "processed")
    )
    return [(p, g) for p, g in res.all()]


async def evaluate_after_game(session: AsyncSession, player_ids: list[int], season: int) -> list[SmurfFlag]:
    flags: list[SmurfFlag] = []
    if not config.SMURF_ENABLED:
        return flags

    # --- performance anomaly ------------------------------------------------
    for pid in player_ids:
        r = await session.scalar(select(PlayerRating).where(
            PlayerRating.player_id == pid, PlayerRating.role == "OVERALL", PlayerRating.season == season))
        if not r or r.games_played < config.SMURF_MIN_GAMES or r.games_played > 12:
            continue
        expected = re_.expected_perf_for_mmr(r.mmr)
        excess = r.perf_mean - expected
        if r.perf_mean >= 1.0 and excess >= 1.0 and not await _has_open_flag(session, pid, "performance_anomaly"):
            p = await session.get(Player, pid)
            flag = SmurfFlag(
                player_id=pid, kind="performance_anomaly", score=round(min(1.0, excess / 2.0), 3),
                evidence={"games": r.games_played, "perf_mean": round(r.perf_mean, 2),
                          "expected_perf": round(expected, 2), "mmr": round(r.mmr), "lp": r.lp},
                summary=(f"{p.discord_username if p else pid} averages PS {r.perf_mean:+.2f} over "
                         f"{r.games_played} games (expected about {expected:+.2f})"),
            )
            session.add(flag)
            flags.append(flag)

    # --- fingerprint match ---------------------------------------------------
    newcomers: list[Fingerprint] = []
    for pid in player_ids:
        rows = await _rows_for(session, pid, season)
        if config.SMURF_MIN_GAMES <= len(rows) <= 15:
            newcomers.append(build_fingerprint(pid, rows))
    if newcomers:
        # `!= False` rather than is_(True): sqlite stores boolean server defaults as text
        others = (await session.execute(select(Player.id).where(Player.is_active != False))).scalars().all()  # noqa: E712
        cache: dict[int, Fingerprint] = {}
        for new in newcomers:
            for oid in others:
                if oid == new.player_id:
                    continue
                if oid not in cache:
                    cache[oid] = build_fingerprint(oid, await _rows_for(session, oid, season))
                old = cache[oid]
                if old.games < config.SMURF_MIN_GAMES:
                    continue
                score, parts = similarity(new, old)
                if score >= config.SMURF_SIMILARITY_THRESHOLD and not await _has_open_flag(
                        session, new.player_id, "fingerprint_match", oid):
                    pn, po = await session.get(Player, new.player_id), await session.get(Player, oid)
                    shared = [c for c, _ in (new.champions & old.champions).most_common(5)]
                    flag = SmurfFlag(
                        player_id=new.player_id, matched_player_id=oid, kind="fingerprint_match",
                        score=round(score, 3),
                        evidence={**{k: round(v, 3) for k, v in parts.items()}, "shared_champions": shared,
                                  "new_games": new.games, "old_games": old.games},
                        summary=(f"{pn.discord_username if pn else new.player_id} plays like "
                                 f"{po.discord_username if po else oid} (similarity {score:.0%}; "
                                 f"shared champs: {', '.join(shared) or 'none'})"),
                    )
                    session.add(flag)
                    flags.append(flag)
    if flags:
        await session.commit()
    return flags
