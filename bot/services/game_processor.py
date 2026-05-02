"""
Orchestrates post-game processing:
  1. Fetch match + timeline from Riot API
  2. Extract per-player per-interval stats
  3. Build P matrix (normalize across 10 players)
  4. Build W matrix (once per game, shared)
  5. Call rating_engine.compute_lp_delta for each player
  6. Write results to DB
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from bot.models.player import Player
from bot.models.game import Game, GameParticipant
from bot.models.rating import PlayerRating
from bot.services import rating_engine
from bot.services.riot_api import RiotClient, RiotAPIError
from bot.services.rating_engine import DEFAULT_WEIGHT_CONFIG as ENGINE_DEFAULT_CONFIG
from bot.config import RIOT_REGION

log = logging.getLogger("ranked-bot.processor")


async def process_match(
    session: AsyncSession,
    match_id: str,
    submitted_by: str,
    lobby_id: int | None = None,
    weight_config: dict | None = None,
) -> Game:
    """
    Full pipeline: fetch Riot data → compute LP deltas → persist to DB.
    Returns the Game ORM object with status='processed' (or 'error').
    """
    # Check for duplicate
    existing = await session.scalar(select(Game).where(Game.riot_match_id == match_id))
    if existing:
        if existing.status == "processed":
            raise ValueError(f"Match {match_id} has already been processed.")
        # Allow re-running a failed/pending game
        game = existing
    else:
        game = Game(
            riot_match_id=match_id,
            region=RIOT_REGION,
            lobby_id=lobby_id,
            submitted_by=submitted_by,
            submitted_at=datetime.now(timezone.utc),
        )
        session.add(game)
        await session.flush()

    wc = weight_config or ENGINE_DEFAULT_CONFIG
    game.weight_config = _serialize_weight_config(wc)

    try:
        async with RiotClient() as riot:
            match_data = await riot.get_match(match_id)
            timeline_data = await riot.get_match_timeline(match_id)
    except RiotAPIError as e:
        game.status = "error"
        game.processing_notes = {"error": str(e)}
        await session.commit()
        raise

    game.raw_riot_data = {"match": match_data, "timeline": timeline_data}

    info = match_data["info"]
    game.game_duration_secs = info.get("gameDuration", 0)
    game_duration_mins = game.game_duration_secs / 60.0

    played_ms = info.get("gameCreation", 0)
    if played_ms:
        game.played_at = datetime.fromtimestamp(played_ms / 1000, tz=timezone.utc)

    # Determine winner team (100 = blue = team1, 200 = red = team2)
    for team in info.get("teams", []):
        if team.get("win"):
            game.winner_team = 1 if team["teamId"] == 100 else 2
            break

    # Extract per-player timeline stats
    per_player_stats = RiotClient.extract_per_player_timeline(
        match_data, timeline_data, interval_mins=5.0
    )

    participants = info["participants"]
    puuid_to_participant = {p["puuid"]: p for p in participants}

    # Build W matrix once — same for all players in this game
    W = rating_engine.build_weight_matrix(game_duration_mins, weight_config=wc)

    # Build P matrices for all players first (needed for normalization)
    puuid_to_P: dict[str, object] = {}
    for puuid, interval_stats in per_player_stats.items():
        P = rating_engine.build_performance_matrix(interval_stats, game_duration_mins)
        puuid_to_P[puuid] = P

    all_P_matrices = list(puuid_to_P.values())

    # Fetch LP for all players to compute team averages
    puuid_to_db_player: dict[str, Player] = {}
    puuid_to_rating: dict[str, PlayerRating] = {}
    for p_data in participants:
        puuid = p_data["puuid"]
        db_player = await session.scalar(select(Player).where(Player.riot_puuid == puuid))
        if db_player is None:
            log.warning("Player with PUUID %s not linked in DB — skipping LP update", puuid[:12])
            continue
        puuid_to_db_player[puuid] = db_player

        rating = await session.scalar(
            select(PlayerRating).where(
                PlayerRating.player_id == db_player.id,
                PlayerRating.role == "OVERALL",
            )
        )
        if rating is None:
            rating = PlayerRating(player_id=db_player.id, role="OVERALL")
            session.add(rating)
            await session.flush()
        puuid_to_rating[puuid] = rating

    # Team average LP
    team1_pids = [p["puuid"] for p in participants if p["teamId"] == 100]
    team2_pids = [p["puuid"] for p in participants if p["teamId"] == 200]

    def avg_lp(puuids: list[str]) -> float:
        lps = [puuid_to_rating[pu].lp for pu in puuids if pu in puuid_to_rating]
        return sum(lps) / len(lps) if lps else 0.0

    team1_avg = avg_lp(team1_pids)
    team2_avg = avg_lp(team2_pids)

    game.team1_expected_win = rating_engine._win_probability(team1_avg, team2_avg)

    # Process each participant
    for p_data in participants:
        puuid = p_data["puuid"]
        participant_data = puuid_to_participant[puuid]
        won = participant_data.get("win", False)
        team_num = 1 if participant_data["teamId"] == 100 else 2
        role = (
            participant_data.get("teamPosition")
            or participant_data.get("individualPosition")
            or "UNKNOWN"
        ).upper()

        P_raw = puuid_to_P.get(puuid)
        if P_raw is None:
            continue

        P_norm = rating_engine.normalize_performance_matrix(P_raw, all_P_matrices)

        db_player = puuid_to_db_player.get(puuid)
        if db_player is None:
            continue

        rating = puuid_to_rating.get(puuid)
        if rating is None:
            continue

        team_avg = team1_avg if team_num == 1 else team2_avg
        opp_avg  = team2_avg if team_num == 1 else team1_avg

        lp_before = rating.lp
        lp_delta = rating_engine.compute_lp_delta(
            P_norm, W,
            player_lp=lp_before,
            won=won,
            team_avg_lp=team_avg,
            opponent_avg_lp=opp_avg,
        )
        impact_score = float((P_norm * W).sum())

        # Update rating
        rating.lp = max(0, lp_before + lp_delta)
        rating.games_played += 1
        if won:
            rating.wins += 1
        rating.last_played_at = datetime.now(timezone.utc)

        # Remove stale participant row if reprocessing
        old_part = await session.scalar(
            select(GameParticipant).where(
                GameParticipant.game_id == game.id,
                GameParticipant.player_id == db_player.id,
            )
        )
        if old_part:
            await session.delete(old_part)
            await session.flush()

        participant_row = GameParticipant(
            game_id=game.id,
            player_id=db_player.id,
            team=team_num,
            role=role,
            champion_id=participant_data.get("championId"),
            champion_name=participant_data.get("championName"),
            win=won,
            kills=participant_data.get("kills", 0),
            deaths=participant_data.get("deaths", 0),
            assists=participant_data.get("assists", 0),
            cs_total=participant_data.get("totalMinionsKilled", 0) + participant_data.get("neutralMinionsKilled", 0),
            vision_score=participant_data.get("visionScore", 0),
            damage_dealt=participant_data.get("totalDamageDealtToChampions", 0),
            gold_earned=participant_data.get("goldEarned", 0),
            impact_score=impact_score,
            lp_before=lp_before,
            lp_after=rating.lp,
            lp_delta=lp_delta,
            mmr_before=rating.mmr,
            mmr_after=rating.mmr,
        )
        session.add(participant_row)

    game.status = "processed"
    game.processed_at = datetime.now(timezone.utc)
    await session.commit()

    log.info("Processed match %s — %d players updated", match_id, len(puuid_to_db_player))
    return game


async def reprocess_match(
    session: AsyncSession,
    match_id: str,
    submitted_by: str,
) -> Game:
    """
    Roll back LP changes from a previously processed game, then reprocess.
    Uses the weight_config stored at time of original processing.
    """
    game = await session.scalar(select(Game).where(Game.riot_match_id == match_id))
    if not game:
        raise ValueError(f"Match {match_id} not found in database.")

    # Roll back LP for all participants
    for part in game.participants:
        rating = await session.scalar(
            select(PlayerRating).where(
                PlayerRating.player_id == part.player_id,
                PlayerRating.role == "OVERALL",
            )
        )
        if rating:
            rating.lp = max(0, rating.lp - part.lp_delta)
            rating.games_played = max(0, rating.games_played - 1)
            if part.win:
                rating.wins = max(0, rating.wins - 1)
        await session.delete(part)

    game.status = "pending"
    game.processed_at = None
    await session.flush()

    stored_wc = _deserialize_weight_config(game.weight_config)

    return await process_match(
        session, match_id, submitted_by=submitted_by,
        lobby_id=game.lobby_id, weight_config=stored_wc,
    )


def _serialize_weight_config(wc: dict) -> dict:
    return {metric: {"w0": cfg["w0"], "alpha": cfg["alpha"]} for metric, cfg in wc.items()}


def _deserialize_weight_config(raw: dict | None) -> dict | None:
    if not raw:
        return None
    return {metric: {"w0": v["w0"], "alpha": v["alpha"]} for metric, v in raw.items()}
