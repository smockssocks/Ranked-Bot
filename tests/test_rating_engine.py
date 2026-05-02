"""
Unit tests for the isolated rating engine.
Run with: pytest tests/test_rating_engine.py -v
No DB, no Discord, no Riot API required.
"""
import numpy as np
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.services.rating_engine import (
    build_weight_matrix,
    build_performance_matrix,
    normalize_performance_matrix,
    compute_lp_delta,
    _baseline_impact,
    _win_probability,
    METRIC_ORDER,
    DEFAULT_WEIGHT_CONFIG,
)


# --- build_weight_matrix ---

def test_weight_matrix_shape():
    W = build_weight_matrix(game_duration_mins=35.0, interval_mins=5.0)
    assert W.shape == (7, len(METRIC_ORDER))


def test_weight_matrix_decays_over_time():
    W = build_weight_matrix(game_duration_mins=30.0, interval_mins=5.0)
    # Each column should be strictly decreasing (early time = higher weight)
    for j in range(W.shape[1]):
        col = W[:, j]
        assert all(col[i] > col[i + 1] for i in range(len(col) - 1)), \
            f"Column {j} ({METRIC_ORDER[j]}) is not strictly decreasing"


def test_weight_matrix_respects_custom_config():
    custom_config = {metric: {"w0": 2.0, "alpha": 0.0} for metric in METRIC_ORDER}
    W = build_weight_matrix(game_duration_mins=25.0, interval_mins=5.0, weight_config=custom_config)
    # alpha=0 means no decay → all weights should equal w0=2.0
    assert np.allclose(W, 2.0)


def test_weight_matrix_short_game():
    W = build_weight_matrix(game_duration_mins=3.0, interval_mins=5.0)
    # Less than one interval → m=1
    assert W.shape[0] == 1


# --- build_performance_matrix ---

def test_performance_matrix_shape():
    stats = [{"gold_differential": 100.0} for _ in range(7)]
    P = build_performance_matrix(stats, game_duration_mins=35.0)
    assert P.shape == (7, len(METRIC_ORDER))


def test_performance_matrix_missing_keys_default_to_zero():
    stats = [{}]  # no keys
    P = build_performance_matrix(stats, game_duration_mins=5.0)
    assert np.all(P == 0.0)


def test_performance_matrix_values_populated():
    stats = [
        {"gold_differential": 200.0, "kill_participation": 0.8, "cs_per_min": 7.5}
    ]
    P = build_performance_matrix(stats, game_duration_mins=5.0)
    assert P[0, METRIC_ORDER.index("gold_differential")] == 200.0
    assert P[0, METRIC_ORDER.index("kill_participation")] == 0.8
    assert P[0, METRIC_ORDER.index("cs_per_min")] == 7.5
    assert P[0, METRIC_ORDER.index("deaths_negated")] == 0.0


# --- normalize_performance_matrix ---

def test_normalization_mean_zero():
    np.random.seed(42)
    all_players = [np.random.randn(7, 6) for _ in range(10)]
    normed = normalize_performance_matrix(all_players[0], all_players)
    stacked = np.stack([normalize_performance_matrix(p, all_players) for p in all_players])
    assert np.allclose(stacked.mean(axis=0), 0.0, atol=1e-10)


def test_normalization_handles_zero_std():
    # All players identical → std=0 → should not crash, output should be 0
    all_players = [np.ones((5, 6)) for _ in range(10)]
    normed = normalize_performance_matrix(all_players[0], all_players)
    assert np.all(normed == 0.0)


# --- _baseline_impact ---

def test_baseline_at_zero_lp():
    assert _baseline_impact(0) == 0.0


def test_baseline_interpolates():
    val = _baseline_impact(200)  # halfway between 0 (0.0) and 400 (0.5)
    assert abs(val - 0.25) < 1e-9


def test_baseline_clamps_above_max():
    val = _baseline_impact(9999)
    assert val == _baseline_impact(2000)


# --- _win_probability ---

def test_win_prob_even_teams():
    prob = _win_probability(1000.0, 1000.0)
    assert abs(prob - 0.5) < 1e-9


def test_win_prob_favored_team():
    prob = _win_probability(1400.0, 1000.0)
    assert prob > 0.5


def test_win_prob_underdog():
    prob = _win_probability(600.0, 1000.0)
    assert prob < 0.5


# --- compute_lp_delta ---

def _make_equal_matrices(value: float = 0.0):
    m, k = 7, len(METRIC_ORDER)
    P = np.full((m, k), value)
    W = build_weight_matrix(35.0)
    return P, W


def test_winner_gets_positive_lp():
    P, W = _make_equal_matrices(0.5)
    delta = compute_lp_delta(P, W, player_lp=500, won=True,
                              team_avg_lp=500.0, opponent_avg_lp=500.0)
    assert delta > 0


def test_loser_gets_negative_lp():
    P, W = _make_equal_matrices(0.5)
    delta = compute_lp_delta(P, W, player_lp=500, won=False,
                              team_avg_lp=500.0, opponent_avg_lp=500.0)
    assert delta < 0


def test_lp_delta_within_bounds_win():
    from bot.services.rating_engine import LP_MIN_WIN, LP_MAX_WIN
    P, W = _make_equal_matrices(1.0)
    delta = compute_lp_delta(P, W, player_lp=800, won=True,
                              team_avg_lp=800.0, opponent_avg_lp=800.0)
    assert LP_MIN_WIN <= delta <= LP_MAX_WIN


def test_lp_delta_within_bounds_loss():
    from bot.services.rating_engine import LP_MIN_LOSS, LP_MAX_LOSS
    P, W = _make_equal_matrices(0.5)
    delta = compute_lp_delta(P, W, player_lp=800, won=False,
                              team_avg_lp=800.0, opponent_avg_lp=800.0)
    assert -LP_MAX_LOSS <= delta <= -LP_MIN_LOSS


def test_upset_win_gives_more_lp():
    # Use zero performance so win-probability is the only variable (no performance bonus)
    P, W = _make_equal_matrices(0.0)
    # Underdog wins → more LP than favorite winning
    delta_upset = compute_lp_delta(P, W, player_lp=500, won=True,
                                    team_avg_lp=400.0, opponent_avg_lp=800.0)
    delta_stomp = compute_lp_delta(P, W, player_lp=500, won=True,
                                    team_avg_lp=800.0, opponent_avg_lp=400.0)
    assert delta_upset > delta_stomp


def test_high_performance_increases_win_lp():
    W = build_weight_matrix(35.0)
    m, k = W.shape
    P_great = np.full((m, k), 2.0)
    P_poor  = np.full((m, k), -1.0)
    delta_great = compute_lp_delta(P_great, W, player_lp=500, won=True,
                                    team_avg_lp=500.0, opponent_avg_lp=500.0)
    delta_poor  = compute_lp_delta(P_poor,  W, player_lp=500, won=True,
                                    team_avg_lp=500.0, opponent_avg_lp=500.0)
    assert delta_great >= delta_poor


def test_shape_mismatch_raises():
    P = np.zeros((7, 6))
    W = np.zeros((5, 6))  # wrong m
    with pytest.raises(AssertionError):
        compute_lp_delta(P, W, player_lp=500, won=True,
                         team_avg_lp=500.0, opponent_avg_lp=500.0)
