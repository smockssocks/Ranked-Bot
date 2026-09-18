"""End-to-end: synthetic Riot JSON -> DB ratings, rollback, reprocess, smurf hooks (sqlite)."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from bot import config
from bot.models.game import Game, GameParticipant
from bot.models.player import Player
from bot.models.rating import PlayerRating, RoleBaseline
from bot.services import game_processor
from tests.fixtures import make_match


async def _seed_players(session, n=10, prefix="puuid-"):
    players = []
    for i in range(1, n + 1):
        p = Player(discord_id=str(1000 + i), discord_username=f"user{i}", riot_puuid=f"{prefix}{i}", summoner_name=f"Player{i}#NA1")
        session.add(p)
        players.append(p)
    await session.commit()
    return players


async def test_process_match_updates_ratings(db):
    async with db() as session:
        await _seed_players(session)
        match, tl = make_match("NA1_100", winner=100)
        res = await game_processor.process_match(session, "NA1_100", "test", match_data=match, timeline_data=tl)
        assert not res.remake and len(res.results) == 10 and res.unlinked == []
        game = await session.scalar(select(Game).where(Game.riot_match_id == "NA1_100"))
        assert game.status == "processed" and game.winner_team == 1
        winners = [r for r in res.results if r.won]
        losers = [r for r in res.results if not r.won]
        assert all(r.lp_delta > 0 for r in winners)
        assert all(r.lp_delta < 0 for r in losers)
        assert all(r.placement for r in res.results)
        # OVERALL + role rows exist
        rows = (await session.execute(select(PlayerRating))).scalars().all()
        assert len(rows) == 20
        overall = [r for r in rows if r.role == "OVERALL"]
        assert all(r.games_played == 1 and r.rd < config.STARTING_RD for r in overall)
        # baselines learned
        bl = (await session.execute(select(RoleBaseline))).scalars().all()
        assert all(b.sample_size == 2 for b in bl)
        # participant rows carry the explanation and snapshots
        part = await session.scalar(select(GameParticipant).limit(1))
        assert part.explanation and "_overall_before" in part.metrics


async def test_duplicate_rejected_and_unlinked_skipped(db):
    async with db() as session:
        await _seed_players(session, n=8)   # players 9,10 unlinked
        match, tl = make_match("NA1_101")
        res = await game_processor.process_match(session, "NA1_101", "t", match_data=match, timeline_data=tl)
        assert len(res.results) == 8 and len(res.unlinked) == 2
        with pytest.raises(ValueError):
            await game_processor.process_match(session, "NA1_101", "t", match_data=match, timeline_data=tl)


async def test_remake_no_lp(db):
    async with db() as session:
        await _seed_players(session)
        match, tl = make_match("NA1_102", duration_mins=8)
        res = await game_processor.process_match(session, "NA1_102", "t", match_data=match, timeline_data=tl)
        assert res.remake
        r = await session.scalar(select(PlayerRating))
        assert r is None or r.games_played == 0


async def test_rollback_and_reprocess_restore_state(db):
    async with db() as session:
        await _seed_players(session)
        m1, t1 = make_match("NA1_103", winner=100)
        await game_processor.process_match(session, "NA1_103", "t", match_data=m1, timeline_data=t1)
        before = {r.player_id: (r.lp, r.mmr, r.rd, r.games_played) for r in (await session.execute(
            select(PlayerRating).where(PlayerRating.role == "OVERALL"))).scalars()}
        await game_processor.rollback_match(session, "NA1_103")
        after = {r.player_id: (r.lp, r.mmr, r.rd, r.games_played) for r in (await session.execute(
            select(PlayerRating).where(PlayerRating.role == "OVERALL"))).scalars()}
        assert all(v == (config.STARTING_LP, config.STARTING_MMR, config.STARTING_RD, 0) for v in after.values())
        g = await session.scalar(select(Game).where(Game.riot_match_id == "NA1_103"))
        assert g.status == "rolled_back"
        res = await game_processor.process_match(session, "NA1_103", "t", match_data=m1, timeline_data=t1)
        again = {r.player_id: (r.lp, r.mmr, r.rd, r.games_played) for r in (await session.execute(
            select(PlayerRating).where(PlayerRating.role == "OVERALL"))).scalars()}
        assert again == before
        # reprocess uses stored raw data
        res2 = await game_processor.reprocess_match(session, "NA1_103", "t")
        assert res2.game.status == "processed"


async def test_carry_in_loss_loses_less_than_teammates(db):
    async with db() as session:
        await _seed_players(session)
        # red loses; red mid (8) dominates lane + KP while red top (6) ints
        match, tl = make_match("NA1_104", winner=100, overrides={
            8: {"gold_rate": 520, "cs_rate": 9.0, "kills": 9, "deaths": 1, "assists": 8, "totalDamageDealtToChampions": 40000,
                "challenges": {"killParticipation": 0.9, "teamDamagePercentage": 0.42}},
            3: {"gold_rate": 300, "cs_rate": 4.5},
            6: {"deaths": 12, "kills": 0, "assists": 1, "gold_rate": 250},
        })
        res = await game_processor.process_match(session, "NA1_104", "t", match_data=match, timeline_data=tl)
        by = {r.player.riot_puuid: r for r in res.results}
        assert by["puuid-8"].perf_score > 1.0
        assert by["puuid-8"].lp_delta > by["puuid-6"].lp_delta + 15
        assert by["puuid-8"].carry_factor > 0.5


async def test_inactivity_grows_rd(db):
    async with db() as session:
        players = await _seed_players(session)
        r = await game_processor.get_or_create_rating(session, players[0].id, "OVERALL", 1)
        r.rd = 60; r.last_played_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        await session.commit()
        game_processor.apply_inactivity(r, datetime.now(timezone.utc))
        assert r.rd > 60
