from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import select

from bot import config
from bot.models.player import Player
from bot.models.rating import PlayerRating
from bot.models.smurf import SmurfFlag
from bot.services import smurf_detector as sd
from bot.services import game_processor
from tests.fixtures import make_match


def _fp(pid, champs, roles, perf, hours, games, ids=None, first=None, last=None):
    fp = sd.Fingerprint(player_id=pid, games=games, champions=Counter(champs), roles=Counter(roles), perf=perf,
                        hours=hours, game_ids=set(ids or []), first_game=first, last_game=last)
    return fp


BASE_PERF = {"cs_per_min": 7.5, "kill_participation": 0.6, "vision_per_min": 1.0, "damage_share": 0.27,
             "deaths_per_min": 0.1, "kda_adj": 4.0, "gold_per_min": 420, "perf_score": 1.2}


def test_similarity_high_for_same_profile():
    hours = [0] * 24; hours[20] = 5; hours[21] = 4
    old = _fp(1, {"Yasuo": 6, "Zed": 3, "Akali": 1}, {"MIDDLE": 10}, BASE_PERF, hours, 10, ids=[1, 2, 3],
              first=datetime(2026, 1, 1, tzinfo=timezone.utc), last=datetime(2026, 2, 1, tzinfo=timezone.utc))
    new = _fp(2, {"Yasuo": 3, "Zed": 2}, {"MIDDLE": 5}, {**BASE_PERF, "cs_per_min": 7.2}, hours, 5, ids=[9, 10],
              first=datetime(2026, 3, 1, tzinfo=timezone.utc), last=datetime(2026, 3, 5, tzinfo=timezone.utc))
    score, parts = sd.similarity(new, old)
    assert score >= config.SMURF_SIMILARITY_THRESHOLD
    assert parts["champion_pool"] > 0.9 and parts["sequential"] > 0


def test_similarity_low_for_different_profile():
    hours_a = [0] * 24; hours_a[20] = 5
    hours_b = [0] * 24; hours_b[3] = 5
    old = _fp(1, {"Yasuo": 6, "Zed": 3}, {"MIDDLE": 9}, BASE_PERF, hours_a, 9)
    new = _fp(2, {"Thresh": 4, "Nami": 2}, {"UTILITY": 6},
              {"cs_per_min": 1.0, "kill_participation": 0.7, "vision_per_min": 2.0, "damage_share": 0.08,
               "deaths_per_min": 0.2, "kda_adj": 2.0, "gold_per_min": 300, "perf_score": -0.3}, hours_b, 6)
    score, _ = sd.similarity(new, old)
    assert score < 0.5


def test_same_game_means_not_same_person():
    old = _fp(1, {"Yasuo": 6}, {"MIDDLE": 6}, BASE_PERF, [1] * 24, 6, ids=[5, 6])
    new = _fp(2, {"Yasuo": 6}, {"MIDDLE": 6}, BASE_PERF, [1] * 24, 6, ids=[6, 7])
    assert sd.similarity(new, old)[0] == 0.0


def test_account_signal():
    s, ev = sd.account_signal({"summonerLevel": 35}, [])
    assert s >= 0.5 and ev["no_ranked_history"]
    s2, ev2 = sd.account_signal({"summonerLevel": 40}, [{"queueType": "RANKED_SOLO_5x5", "tier": "DIAMOND", "rank": "II", "wins": 30, "losses": 8}])
    assert s2 > s and ev2["high_tier_low_level"] and "high_wr_low_games" in ev2
    s3, _ = sd.account_signal({"summonerLevel": 400}, [{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "I", "wins": 200, "losses": 190}])
    assert s3 < 0.2


async def test_link_flag_persisted(db):
    async with db() as session:
        p = Player(discord_id="1", discord_username="fresh", riot_puuid="x", summoner_name="Fresh#NA1")
        session.add(p); await session.commit()
        flag = await sd.evaluate_link(session, p, {"summonerLevel": 30}, [])
        assert flag is not None and flag.kind == "account_signal"
        assert await sd.evaluate_link(session, p, {"summonerLevel": 30}, []) is None  # no duplicate


async def test_performance_anomaly_after_games(db):
    async with db() as session:
        for i in range(1, 11):
            session.add(Player(discord_id=str(i), discord_username=f"u{i}", riot_puuid=f"puuid-{i}"))
        await session.commit()
        p3 = await session.scalar(select(Player).where(Player.riot_puuid == "puuid-3"))
        # Simulate a newcomer whose running PS is very high after 4 games
        r = await game_processor.get_or_create_rating(session, p3.id, "OVERALL", 1)
        r.games_played = 4; r.perf_mean = 2.1; r.mmr = 1550
        await session.commit()
        flags = await sd.evaluate_after_game(session, [p3.id], 1)
        assert any(f.kind == "performance_anomaly" for f in flags)
        assert (await session.scalar(select(SmurfFlag))) is not None


async def test_fingerprint_match_via_processor(db):
    """A 'main' with 4 games, then a fresh account with 3 games on the same champs, same stats, mains stops playing."""
    async with db() as session:
        for i in range(1, 12):
            session.add(Player(discord_id=str(i), discord_username=f"u{i}", riot_puuid=f"puuid-{i}"))
        await session.commit()
        # 4 games where main (puuid-3, blue mid) plays; give everyone identical baseline
        for g in range(4):
            m, t = make_match(f"NA1_m{g}", seed=g)
            m["info"]["gameCreation"] = 1_700_000_000_000 + g * 86_400_000
            await game_processor.process_match(session, f"NA1_m{g}", "t", match_data=m, timeline_data=t)
        # now main's account is replaced by puuid-11 in the same seat (same champ Ahri, same role, same stats)
        puuids = [f"puuid-{i}" for i in range(1, 11)]
        puuids[2] = "puuid-11"
        for g in range(3):
            m, t = make_match(f"NA1_s{g}", seed=10 + g, puuids=puuids)
            m["info"]["gameCreation"] = 1_700_000_000_000 + (10 + g) * 86_400_000
            res = await game_processor.process_match(session, f"NA1_s{g}", "t", match_data=m, timeline_data=t)
        p11 = await session.scalar(select(Player).where(Player.riot_puuid == "puuid-11"))
        p3 = await session.scalar(select(Player).where(Player.riot_puuid == "puuid-3"))
        flags = (await session.execute(select(SmurfFlag).where(SmurfFlag.kind == "fingerprint_match"))).scalars().all()
        assert any(f.player_id == p11.id and f.matched_player_id == p3.id for f in flags), [f.summary for f in flags]
