"""
Post-game pipeline:
  1. fetch match + timeline from Riot (or accept pre-fetched JSON)
  2. metrics.extract_all  -> per-player metric dicts
  3. rating_engine.performance_score for all 10 players (linked or not, so team
     means and in-game pools are complete)
  4. rating_engine.compute_update for every linked player, OVERALL + role rows
  5. update per-role community baselines (Welford)
  6. persist Game / GameParticipant rows with a human readable explanation
  7. run the anti-smurf detector for the players involved

reprocess_match() restores the exact pre-game state stored on each participant
row, then runs the pipeline again (use it for the most recent games only).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import config
from bot.models.game import Game, GameParticipant
from bot.models.player import Player
from bot.models.rating import PlayerRating, RoleBaseline
from bot.services import metrics as metrics_svc
from bot.services import rating_engine as re_
from bot.services.riot_api import RiotAPIError, RiotClient

log = logging.getLogger("ranked-bot.processor")


@dataclass
class PlayerResult:
    player: Player
    role: str
    team: int
    won: bool
    champion: str
    kda: str
    perf_score: float
    carry_factor: float
    lp_before: int
    lp_after: int
    lp_delta: int
    explanation: str
    top_positive: list[str]
    top_negative: list[str]
    placement: bool


@dataclass
class ProcessResult:
    game: Game
    results: list[PlayerResult] = field(default_factory=list)
    unlinked: list[str] = field(default_factory=list)
    remake: bool = False
    smurf_flags: list[Any] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Rating row helpers                                                          #
# --------------------------------------------------------------------------- #

async def get_or_create_rating(session: AsyncSession, player_id: int, role: str, season: int) -> PlayerRating:
    r = await session.scalar(
        select(PlayerRating).where(
            PlayerRating.player_id == player_id,
            PlayerRating.role == role,
            PlayerRating.season == season,
        )
    )
    if r is None:
        r = PlayerRating(
            player_id=player_id, role=role, season=season,
            lp=config.STARTING_LP, mmr=config.STARTING_MMR, rd=config.STARTING_RD,
            peak_lp=config.STARTING_LP,
        )
        session.add(r)
        await session.flush()
    return r


def _state(r: PlayerRating) -> re_.RatingState:
    return re_.RatingState(mmr=r.mmr, rd=r.rd, lp=r.lp, games=r.games_played,
                           perf_mean=r.perf_mean, perf_var=r.perf_var)


def _snapshot(r: PlayerRating) -> dict[str, float]:
    return {"lp": r.lp, "mmr": r.mmr, "rd": r.rd, "games_played": r.games_played, "wins": r.wins,
            "perf_mean": r.perf_mean, "perf_var": r.perf_var, "peak_lp": r.peak_lp, "streak": r.streak}


def _restore(r: PlayerRating, snap: dict[str, float]) -> None:
    r.lp = int(snap["lp"]); r.mmr = float(snap["mmr"]); r.rd = float(snap["rd"])
    r.games_played = int(snap["games_played"]); r.wins = int(snap["wins"])
    r.perf_mean = float(snap["perf_mean"]); r.perf_var = float(snap["perf_var"])
    r.peak_lp = int(snap.get("peak_lp", r.peak_lp)); r.streak = int(snap.get("streak", 0))


def _apply(r: PlayerRating, upd: re_.RatingUpdate, won: bool, now: datetime) -> None:
    r.lp = upd.lp_after
    r.mmr = upd.mmr_after
    r.rd = upd.rd_after
    r.perf_mean = upd.perf_mean_after
    r.perf_var = upd.perf_var_after
    r.games_played += 1
    if won:
        r.wins += 1
        r.streak = r.streak + 1 if r.streak >= 0 else 1
    else:
        r.streak = r.streak - 1 if r.streak <= 0 else -1
    r.peak_lp = max(r.peak_lp or 0, r.lp)
    r.last_played_at = now


def apply_inactivity(r: PlayerRating, now: datetime) -> None:
    """Grow RD for players who have not played in a while (called before an update)."""
    if r.last_played_at is None:
        return
    last = r.last_played_at if r.last_played_at.tzinfo else r.last_played_at.replace(tzinfo=timezone.utc)
    days = (now - last).total_seconds() / 86400.0
    if days > config.INACTIVITY_DECAY_DAYS:
        r.rd = re_.grow_rd(r.rd, days - config.INACTIVITY_DECAY_DAYS)


async def load_baselines(session: AsyncSession, season: int) -> dict[str, tuple[RoleBaseline, re_.Baseline]]:
    out: dict[str, tuple[RoleBaseline, re_.Baseline]] = {}
    for role in re_.ROLES:
        row = await session.scalar(select(RoleBaseline).where(RoleBaseline.role == role, RoleBaseline.season == season))
        if row is None:
            row = RoleBaseline(role=role, season=season, stats=None, sample_size=0)
            session.add(row)
            await session.flush()
        out[role] = (row, re_.Baseline.from_dict(row.stats, role))
    return out


# --------------------------------------------------------------------------- #
# Main pipeline                                                               #
# --------------------------------------------------------------------------- #

async def process_match(
    session: AsyncSession,
    match_id: str,
    submitted_by: str,
    lobby_id: int | None = None,
    auto_detected: bool = False,
    match_data: dict | None = None,
    timeline_data: dict | None = None,
    season: int | None = None,
) -> ProcessResult:
    season = season or config.CURRENT_SEASON
    now = datetime.now(timezone.utc)

    existing = await session.scalar(select(Game).where(Game.riot_match_id == match_id))
    if existing:
        if existing.status in ("processed", "remake"):
            raise ValueError(f"Match {match_id} has already been processed.")
        game = existing
        game.lobby_id = game.lobby_id or lobby_id
    else:
        game = Game(riot_match_id=match_id, region=config.RIOT_REGION, lobby_id=lobby_id,
                    submitted_by=submitted_by, submitted_at=now, season=season, auto_detected=auto_detected)
        session.add(game)
        await session.flush()

    if match_data is None or timeline_data is None:
        try:
            async with RiotClient() as riot:
                match_data = match_data or await riot.get_match(match_id)
                timeline_data = timeline_data or await riot.get_match_timeline(match_id)
        except RiotAPIError as e:
            game.status = "error"
            game.processing_notes = {"error": str(e)}
            await session.commit()
            raise

    info = match_data["info"]
    game.raw_riot_data = {"match": match_data, "timeline": timeline_data}
    dur = int(info.get("gameDuration") or 0)
    if dur > 20_000:
        dur //= 1000
    game.game_duration_secs = dur
    if info.get("gameCreation"):
        game.played_at = datetime.fromtimestamp(info["gameCreation"] / 1000, tz=timezone.utc)
    for team in info.get("teams", []):
        if team.get("win"):
            game.winner_team = 1 if team["teamId"] == 100 else 2
            break

    result = ProcessResult(game=game)

    if metrics_svc.is_remake(match_data, config.MIN_GAME_MINUTES):
        game.status = "remake"
        game.processed_at = now
        game.processing_notes = {"reason": f"shorter than {config.MIN_GAME_MINUTES} min or early surrender"}
        await session.commit()
        result.remake = True
        return result

    all_metrics = metrics_svc.extract_all(match_data, timeline_data)
    pools = metrics_svc.in_game_pools(all_metrics)
    duration_mins = max(1.0, dur / 60.0)
    baselines = await load_baselines(session, season)
    baselines_before = {role: bl.to_dict() for role, (_row, bl) in baselines.items()}

    # 1. performance scores for everyone
    perf: dict[str, re_.PerfResult] = {}
    for puuid, m in all_metrics.items():
        perf[puuid] = re_.performance_score(m, m["role"], baselines[m["role"]][1], pools, duration_mins)

    # 2. link players / ratings
    linked: dict[str, Player] = {}
    overall: dict[str, PlayerRating] = {}
    role_rows: dict[str, PlayerRating] = {}
    for puuid, m in all_metrics.items():
        p = await session.scalar(select(Player).where(Player.riot_puuid == puuid))
        if p is None:
            result.unlinked.append(m["riot_id"])
            continue
        linked[puuid] = p
        o = await get_or_create_rating(session, p.id, "OVERALL", season)
        rr = await get_or_create_rating(session, p.id, m["role"], season)
        apply_inactivity(o, now)
        apply_inactivity(rr, now)
        overall[puuid] = o
        role_rows[puuid] = rr

    def team_mmrs(team: int) -> list[float]:
        return [overall[pu].mmr if pu in overall else config.STARTING_MMR
                for pu, m in all_metrics.items() if m["team"] == team]

    t1, t2 = team_mmrs(1), team_mmrs(2)
    game.team1_expected_win = re_.expected_win(t1, t2)
    team_ps = {1: [perf[pu].score for pu, m in all_metrics.items() if m["team"] == 1],
               2: [perf[pu].score for pu, m in all_metrics.items() if m["team"] == 2]}

    # 3. rating updates for linked players
    for puuid, p in linked.items():
        m = all_metrics[puuid]
        pr = perf[puuid]
        team = m["team"]
        own, opp = (t1, t2) if team == 1 else (t2, t1)
        o, rr = overall[puuid], role_rows[puuid]
        snap_o, snap_r = _snapshot(o), _snapshot(rr)

        upd_o = re_.compute_update(_state(o), m["win"], pr.score, team_ps[team], own, opp, config.PLACEMENT_GAMES)
        upd_r = re_.compute_update(_state(rr), m["win"], pr.score, team_ps[team], own, opp, config.PLACEMENT_GAMES)
        placement = o.games_played < config.PLACEMENT_GAMES
        _apply(o, upd_o, m["win"], now)
        _apply(rr, upd_r, m["win"], now)

        old = await session.scalar(select(GameParticipant).where(
            GameParticipant.game_id == game.id, GameParticipant.player_id == p.id))
        if old:
            await session.delete(old)
            await session.flush()

        stored_metrics = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()}
        stored_metrics["_z"] = {k: round(v, 3) for k, v in pr.z.items()}
        stored_metrics["_contrib"] = {k: round(v, 3) for k, v in pr.contributions.items()}
        stored_metrics["_overall_before"] = snap_o
        stored_metrics["_role_before"] = snap_r
        stored_metrics["_role_lp_delta"] = upd_r.lp_delta

        session.add(GameParticipant(
            game_id=game.id, player_id=p.id, team=team, role=m["role"],
            champion_id=m["champion_id"], champion_name=m["champion_name"], win=m["win"],
            kills=m["kills"], deaths=m["deaths"], assists=m["assists"], cs_total=m["cs_total"],
            vision_score=m["vision_score"], damage_dealt=m["damage_dealt"], gold_earned=m["gold_earned"],
            impact_score=pr.score, carry_factor=upd_o.carry_factor, expected_win=upd_o.expected_win,
            metrics=stored_metrics, explanation=upd_o.explanation,
            lp_before=snap_o["lp"], lp_after=o.lp, lp_delta=upd_o.lp_delta,
            mmr_before=snap_o["mmr"], mmr_after=o.mmr, rd_before=snap_o["rd"], rd_after=o.rd,
        ))
        result.results.append(PlayerResult(
            player=p, role=m["role"], team=team, won=m["win"], champion=m["champion_name"] or "?",
            kda=f"{m['kills']}/{m['deaths']}/{m['assists']}", perf_score=pr.score,
            carry_factor=upd_o.carry_factor, lp_before=int(snap_o["lp"]), lp_after=o.lp,
            lp_delta=upd_o.lp_delta, explanation=upd_o.explanation,
            top_positive=pr.top_positive, top_negative=pr.top_negative, placement=placement,
        ))

    # 4. learn baselines from all 10 players
    for puuid, m in all_metrics.items():
        row, bl = baselines[m["role"]]
        bl.update(m)
        row.stats = bl.to_dict()
        row.sample_size = bl.n

    game.status = "processed"
    game.processed_at = now
    game.processing_notes = {"unlinked": result.unlinked, "duration_mins": round(duration_mins, 1),
                             "_baselines_before": baselines_before}
    await session.commit()

    # 5. anti-smurf pass (never breaks processing)
    if config.SMURF_ENABLED and linked:
        try:
            from bot.services import smurf_detector
            result.smurf_flags = await smurf_detector.evaluate_after_game(
                session, [p.id for p in linked.values()], season)
        except Exception:  # pragma: no cover - defensive
            log.exception("smurf detector failed for %s", match_id)

    log.info("Processed %s: %d linked, %d unlinked", match_id, len(linked), len(result.unlinked))
    return result


async def rollback_match(session: AsyncSession, match_id: str) -> Game:
    """Restore every participant's pre-game rating state and mark the game rolled_back."""
    game = await session.scalar(select(Game).where(Game.riot_match_id == match_id))
    if not game:
        raise ValueError(f"Match {match_id} not found in database.")
    if game.status != "processed":
        raise ValueError(f"Match {match_id} is not in processed state (it is '{game.status}').")
    for part in list(game.participants):
        mtr = part.metrics or {}
        o = await session.scalar(select(PlayerRating).where(
            PlayerRating.player_id == part.player_id, PlayerRating.role == "OVERALL", PlayerRating.season == game.season))
        rr = await session.scalar(select(PlayerRating).where(
            PlayerRating.player_id == part.player_id, PlayerRating.role == part.role, PlayerRating.season == game.season))
        if o and "_overall_before" in mtr:
            _restore(o, mtr["_overall_before"])
        elif o:
            o.lp = max(0, o.lp - part.lp_delta); o.games_played = max(0, o.games_played - 1)
            if part.win: o.wins = max(0, o.wins - 1)
        if rr and "_role_before" in mtr:
            _restore(rr, mtr["_role_before"])
        await session.delete(part)
    snap = (game.processing_notes or {}).get("_baselines_before")
    if snap:
        for role, d in snap.items():
            row = await session.scalar(select(RoleBaseline).where(RoleBaseline.role == role, RoleBaseline.season == game.season))
            if row:
                row.stats = d
                row.sample_size = int(d.get("n", 0))
    game.status = "rolled_back"
    game.processed_at = None
    await session.commit()
    return game


async def reprocess_match(session: AsyncSession, match_id: str, submitted_by: str) -> ProcessResult:
    game = await rollback_match(session, match_id)
    raw = game.raw_riot_data or {}
    return await process_match(
        session, match_id, submitted_by=submitted_by, lobby_id=game.lobby_id,
        match_data=raw.get("match"), timeline_data=raw.get("timeline"), season=game.season,
    )
