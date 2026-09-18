from bot.services import metrics
from bot.services.rating_engine import METRICS
from tests.fixtures import make_match


def test_extract_all_returns_all_players_and_metric_keys():
    match, tl = make_match()
    out = metrics.extract_all(match, tl)
    assert len(out) == 10
    for m in out.values():
        for k in METRICS:
            assert k in m
        assert m["role"] in ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")


def test_lane_opponents_by_position():
    match, _ = make_match()
    opp = metrics.lane_opponents(match["info"]["participants"])
    assert opp[1] == 6 and opp[6] == 1 and opp[5] == 10


def test_lane_diffs_reflect_gold_lead():
    # participant 3 (blue mid) farms much better than participant 8 (red mid)
    match, tl = make_match(overrides={3: {"gold_rate": 480, "cs_rate": 8.5}, 8: {"gold_rate": 330, "cs_rate": 5.0}})
    out = metrics.extract_all(match, tl)
    mid_blue = out["puuid-3"]; mid_red = out["puuid-8"]
    assert mid_blue["gold_diff_10"] > 800 and mid_red["gold_diff_10"] < -800
    assert mid_blue["cs_diff_10"] > 20
    assert mid_blue["lane_trajectory"] > 0 > mid_red["lane_trajectory"]


def test_timeline_fallbacks_when_no_challenges():
    match, tl = make_match()
    out = metrics.extract_all(match, tl)
    jg = out["puuid-2"]
    assert jg["objective_participation"] > 0          # dragon kill at 10
    assert out["puuid-5"]["control_wards"] >= 1        # WARD_PLACED control ward
    assert out["puuid-1"]["turret_plates"] == 1
    assert out["puuid-8"]["deaths"] == 4               # from participant
    assert out["puuid-3"]["solo_kills"] == 0           # kills had assists
    assert out["puuid-9"]["solo_kills"] == 4


def test_challenges_take_priority():
    match, tl = make_match(overrides={3: {"challenges": {"killParticipation": 0.91, "teamDamagePercentage": 0.4, "soloKills": 3}}})
    out = metrics.extract_all(match, tl)
    assert out["puuid-3"]["kill_participation"] == 0.91
    assert out["puuid-3"]["damage_share"] == 0.4
    assert out["puuid-3"]["solo_kills"] == 3


def test_support_cs_and_vision():
    match, tl = make_match()
    sup = metrics.extract_all(match, tl)["puuid-5"]
    assert sup["cs_per_min"] < 1.0 and sup["vision_per_min"] > 1.5
    assert sup["heal_shield_per_min"] > 0


def test_remake_detection():
    short, _ = make_match(duration_mins=10)
    assert metrics.is_remake(short, 15)
    normal, _ = make_match(duration_mins=30)
    assert not metrics.is_remake(normal, 15)
    normal["info"]["participants"][0]["gameEndedInEarlySurrender"] = True
    assert metrics.is_remake(normal, 15)


def test_duration_in_ms_is_handled():
    match, tl = make_match(duration_mins=30)
    match["info"]["gameDuration"] = 30 * 60 * 1000
    out = metrics.extract_all(match, tl)
    assert abs(out["puuid-1"]["duration_mins"] - 30) < 0.01


def test_in_game_pools():
    match, tl = make_match()
    pools = metrics.in_game_pools(metrics.extract_all(match, tl))
    assert len(pools["cs_per_min"]) == 10
