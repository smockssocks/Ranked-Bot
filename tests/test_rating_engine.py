"""Rating engine behaviour: pure math, no DB."""
import math

import pytest

from bot.services import rating_engine as re_
from bot.services.ranks import division_for_lp, tier_for_lp


def _avg_metrics(role="MIDDLE"):
    return {k: v[1] for k, v in re_.METRICS.items()}


def test_role_normalisation():
    assert re_.normalize_role("mid") == "MIDDLE"
    assert re_.normalize_role("Support") == "UTILITY"
    assert re_.normalize_role("adc") == "BOTTOM"
    assert re_.normalize_role(None) == "MIDDLE"


def test_phase_multiplier_short_vs_long():
    assert re_.phase_multiplier("early", 18) > re_.phase_multiplier("early", 45)
    assert re_.phase_multiplier("late", 18) < re_.phase_multiplier("late", 45)
    assert re_.phase_multiplier("all", 30) == 1.0


def test_average_performance_scores_zero():
    ps = re_.performance_score(_avg_metrics(), "MIDDLE", duration_mins=30)
    assert abs(ps.score) < 1e-9
    assert ps.top_positive == [] and ps.top_negative == []


def test_good_lane_and_low_deaths_positive():
    m = _avg_metrics()
    m["gold_diff_10"] = 900; m["gold_diff_15"] = 1500; m["cs_diff_10"] = 15; m["deaths_per_min"] = 0.03
    m["damage_share"] = 0.32; m["kill_participation"] = 0.75
    ps = re_.performance_score(m, "MIDDLE", duration_mins=28)
    assert ps.score > 1.0
    assert "gold_diff_10" in ps.top_positive or "gold_diff_15" in ps.top_positive


def test_inting_is_negative_even_with_cs():
    m = _avg_metrics()
    m["cs_per_min"] = 9.0; m["deaths_per_min"] = 0.45; m["kda_adj"] = 0.4; m["damage_share"] = 0.1
    ps = re_.performance_score(m, "TOP", duration_mins=30)
    assert ps.score < -0.5
    assert "deaths_per_min" in ps.top_negative


def test_support_weights_vision_not_cs():
    w = re_.role_weights("UTILITY")
    assert w["cs_per_min"] == 0.0 and w["vision_per_min"] > w["damage_share"]


def test_z_clip_limits_single_stat():
    m = _avg_metrics()
    m["solo_kills"] = 50   # absurd
    ps = re_.performance_score(m, "MIDDLE", duration_mins=30)
    assert ps.z["solo_kills"] == re_.Z_CLIP
    assert ps.score < 0.6   # one stat cannot carry the score


def test_baseline_learns_and_shifts_normalisation():
    bl = re_.Baseline(role="MIDDLE")
    for _ in range(80):
        m = _avg_metrics(); m["cs_per_min"] = 9.0
        bl.update(m)
    m = _avg_metrics(); m["cs_per_min"] = 9.0
    # once the server average is 9 cs/min, 9 cs/min is no longer impressive
    ps_with = re_.performance_score(m, "MIDDLE", baseline=bl, duration_mins=30)
    ps_without = re_.performance_score(m, "MIDDLE", duration_mins=30)
    assert ps_with.score < ps_without.score
    rt = re_.Baseline.from_dict(bl.to_dict(), "MIDDLE")
    assert rt.n == 80 and abs(rt.mean["cs_per_min"] - 9.0) < 1e-9


def test_expected_win():
    assert abs(re_.expected_win([1500] * 5, [1500] * 5) - 0.5) < 1e-9
    assert 0.6 < re_.expected_win([1700] * 5, [1500] * 5) < 0.7
    assert re_.expected_win([], []) == 0.5


VET = re_.RatingState(mmr=1500, rd=60, lp=1000, games=30, perf_var=0.5)
T = [1500.0] * 5


def _upd(won, ps, state=VET, own=T, opp=T):
    return re_.compute_update(state, won, ps, [ps, 0, 0, 0, 0], own, opp)


def test_even_game_average_perf_about_20():
    assert 18 <= _upd(True, 0.0).lp_delta <= 22
    assert -22 <= _upd(False, 0.0).lp_delta <= -18


def test_win_never_negative_loss_can_be_positive():
    assert _upd(True, -3.0).lp_delta >= re_.WIN_MIN
    assert _upd(False, 2.5).lp_delta > 0            # carried a loss -> small gain
    assert _upd(False, 1.0).lp_delta < 0            # good but not exceptional -> still a loss
    assert _upd(False, 3.0).lp_delta <= re_.LOSS_MAX


def test_performance_monotonic():
    wins = [_upd(True, ps).lp_delta for ps in (-2, -1, 0, 1, 2)]
    losses = [_upd(False, ps).lp_delta for ps in (-2, -1, 0, 1, 2)]
    assert wins == sorted(wins) and losses == sorted(losses)


def test_upset_and_favourite():
    upset = _upd(True, 0.0, own=[1300.0] * 5, opp=[1500.0] * 5).lp_delta
    stomp = _upd(True, 0.0, own=[1700.0] * 5, opp=[1500.0] * 5).lp_delta
    assert upset > stomp
    lose_as_fav = _upd(False, 0.0, own=[1700.0] * 5, opp=[1500.0] * 5).lp_delta
    lose_as_dog = _upd(False, 0.0, own=[1300.0] * 5, opp=[1500.0] * 5).lp_delta
    assert lose_as_fav < lose_as_dog


def test_carry_factor_rewards_outperforming_team():
    carried = re_.compute_update(VET, False, 1.5, [1.5, 1.5, 1.5, 1.5, 1.5], T, T)
    carry = re_.compute_update(VET, False, 1.5, [1.5, -1.0, -1.0, -1.0, -1.0], T, T)
    assert carry.lp_delta > carried.lp_delta
    assert carry.carry_factor > 0 > carried.carry_factor - 1e-9 or carried.carry_factor == 0


def test_new_player_moves_faster_and_rd_shrinks():
    new = re_.RatingState(mmr=1500, rd=350, lp=1000, games=0)
    u_new = re_.compute_update(new, True, 0.0, [0] * 5, T, T)
    u_vet = _upd(True, 0.0)
    assert u_new.lp_delta > 1.7 * u_vet.lp_delta
    assert u_new.rd_after < 200
    assert u_new.mmr_after - 1500 > 40


def test_inactivity_grows_rd():
    assert re_.grow_rd(60, 30) > 60
    assert re_.grow_rd(60, 0) == 60
    assert re_.grow_rd(340, 1000) == re_.RD_MAX


def test_consistency_tracking():
    s = re_.RatingState(games=5, perf_mean=1.0, perf_var=0.2)
    u = re_.compute_update(s, True, 1.0, [1.0] * 5, T, T)
    assert u.perf_var_after < 0.2 + 1e-9
    u2 = re_.compute_update(s, True, -2.0, [-2.0] * 5, T, T)
    assert u2.perf_var_after > u.perf_var_after
    assert 0 < re_.consistency_score(0.5) < 1


def test_explanation_text():
    u = _upd(False, 2.2)
    assert "Loss" in u.explanation and "LP" in u.explanation


def test_tiers():
    assert tier_for_lp(1000)[0] == "Silver"
    assert tier_for_lp(0)[0] == "Wood"
    assert tier_for_lp(5000)[0] == "Legend"
    assert division_for_lp(1000).startswith("Silver")
    assert division_for_lp(1099) == "Silver I"
    assert division_for_lp(900) == "Silver IV"


def test_expected_perf_for_mmr_bounds():
    assert re_.expected_perf_for_mmr(1500) == 0
    assert re_.expected_perf_for_mmr(9000) == 1.5
    assert re_.expected_perf_for_mmr(-9000) == -1.5
