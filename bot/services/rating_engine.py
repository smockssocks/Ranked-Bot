"""
Isolated rating engine — pure numpy math, no DB, no Discord.
To change the ranking model, only edit this file.

Public interface (the only functions the rest of the codebase calls):
  build_weight_matrix(game_duration_mins, interval_mins, weight_config) -> ndarray
  build_performance_matrix(timeline_stats, game_duration_mins, interval_mins) -> ndarray
  compute_lp_delta(P, W, player_lp, won, team_avg_lp, opponent_avg_lp) -> int
"""

from __future__ import annotations
import numpy as np
from typing import Any

# Default weight config — can be overridden by passing weight_config= args.
# Keys must match the column order in METRIC_ORDER.
METRIC_ORDER = [
    "gold_differential",
    "kill_participation",
    "objective_damage",
    "vision_score_delta",
    "cs_per_min",
    "deaths_negated",
]

DEFAULT_WEIGHT_CONFIG: dict[str, dict[str, float]] = {
    "gold_differential":  {"w0": 1.0, "alpha": 0.03},
    "kill_participation": {"w0": 0.8, "alpha": 0.02},
    "objective_damage":   {"w0": 1.2, "alpha": 0.04},
    "vision_score_delta": {"w0": 0.6, "alpha": 0.01},
    "cs_per_min":         {"w0": 0.9, "alpha": 0.05},
    "deaths_negated":     {"w0": 0.7, "alpha": 0.02},
}

# LP delta scaling constants
LP_SCALE = 15.0
LP_MAX_WIN = 25
LP_MAX_LOSS = 20
LP_MIN_WIN = 8
LP_MIN_LOSS = 8

# LP → expected raw impact anchors (linear interpolation between these)
LP_BASELINE_ANCHORS: list[tuple[int, float]] = [
    (0,    0.0),
    (400,  0.5),
    (800,  1.2),
    (1200, 2.0),
    (1600, 2.9),
    (2000, 3.8),
]


def build_weight_matrix(
    game_duration_mins: float,
    interval_mins: float = 5.0,
    weight_config: dict[str, dict[str, float]] | None = None,
) -> np.ndarray:
    """
    Build the (m × k) weight matrix W where W[i, j] = w0_j * exp(-alpha_j * t_i).

    m = number of complete intervals in the game
    k = number of metrics (len(METRIC_ORDER))
    """
    if weight_config is None:
        weight_config = DEFAULT_WEIGHT_CONFIG

    m = max(1, int(game_duration_mins / interval_mins))
    k = len(METRIC_ORDER)
    W = np.zeros((m, k), dtype=np.float64)

    for j, metric in enumerate(METRIC_ORDER):
        cfg = weight_config.get(metric, {"w0": 1.0, "alpha": 0.02})
        w0 = cfg["w0"]
        alpha = cfg["alpha"]
        for i in range(m):
            t_i = (i + 1) * interval_mins
            W[i, j] = w0 * np.exp(-alpha * t_i)

    return W


def build_performance_matrix(
    timeline_stats: list[dict[str, Any]],
    game_duration_mins: float,
    interval_mins: float = 5.0,
) -> np.ndarray:
    """
    Build the (m × k) performance matrix P from per-interval stats.

    timeline_stats: list of dicts, one per interval, keys matching METRIC_ORDER.
    Missing keys default to 0.0.
    """
    m = max(1, int(game_duration_mins / interval_mins))
    k = len(METRIC_ORDER)
    P = np.zeros((m, k), dtype=np.float64)

    for i, interval_data in enumerate(timeline_stats[:m]):
        for j, metric in enumerate(METRIC_ORDER):
            P[i, j] = float(interval_data.get(metric, 0.0))

    return P


def normalize_performance_matrix(
    P: np.ndarray,
    all_player_matrices: list[np.ndarray],
) -> np.ndarray:
    """
    Z-score normalize P against all 10 players in the game.
    This removes champion/role bias — a support with 0 gold diff isn't penalized
    vs a carry with +500 when both are "average for their role."

    all_player_matrices: list of (m × k) arrays for every player in the lobby.
    Returns a normalized copy of P.
    """
    stacked = np.stack(all_player_matrices, axis=0)  # (10, m, k)
    mean = stacked.mean(axis=0)  # (m, k)
    std = stacked.std(axis=0)    # (m, k)
    std = np.where(std == 0, 1.0, std)  # avoid divide-by-zero
    return (P - mean) / std


def _baseline_impact(player_lp: int) -> float:
    """Linear interpolation of expected impact score for a given LP value."""
    anchors = LP_BASELINE_ANCHORS
    if player_lp <= anchors[0][0]:
        return anchors[0][1]
    if player_lp >= anchors[-1][0]:
        return anchors[-1][1]
    for i in range(len(anchors) - 1):
        lp_lo, val_lo = anchors[i]
        lp_hi, val_hi = anchors[i + 1]
        if lp_lo <= player_lp <= lp_hi:
            t = (player_lp - lp_lo) / (lp_hi - lp_lo)
            return val_lo + t * (val_hi - val_lo)
    return 0.0


def _win_probability(team_avg_lp: float, opponent_avg_lp: float) -> float:
    """Logistic win probability estimate based on average team LP difference."""
    lp_diff = team_avg_lp - opponent_avg_lp
    return 1.0 / (1.0 + np.exp(-lp_diff / 200.0))


def compute_lp_delta(
    P: np.ndarray,
    W: np.ndarray,
    player_lp: int,
    won: bool,
    team_avg_lp: float,
    opponent_avg_lp: float,
) -> int:
    """
    Compute the LP delta for a single player after a game.

    Steps:
    1. Impact score I = sum(P ⊙ W)  (Hadamard product, then sum)
    2. Compare I to expected baseline for player's LP
    3. Apply win/loss sign, scale by performance above/below baseline
    4. Adjust magnitude based on matchup difficulty (expected win probability)
    5. Clamp to min/max LP limits

    Returns an integer LP delta (positive = gain, negative = loss).
    """
    assert P.shape == W.shape, f"P shape {P.shape} != W shape {W.shape}"

    I = float(np.sum(P * W))

    expected_I = _baseline_impact(player_lp)
    performance_delta = I - expected_I

    win_prob = _win_probability(team_avg_lp, opponent_avg_lp)

    if won:
        # Winning against a stronger team → more LP. Against weaker → less LP.
        base_gain = LP_SCALE * (1.0 - win_prob) * 2
        performance_bonus = performance_delta * LP_SCALE * 0.5
        lp_delta = base_gain + performance_bonus
        lp_delta = int(round(np.clip(lp_delta, LP_MIN_WIN, LP_MAX_WIN)))
    else:
        # Losing to a weaker team → more LP loss. To stronger → less LP loss.
        base_loss = LP_SCALE * win_prob * 2
        performance_reduction = performance_delta * LP_SCALE * 0.5
        lp_delta = -(base_loss - performance_reduction)
        lp_delta = int(round(np.clip(lp_delta, -LP_MAX_LOSS, -LP_MIN_LOSS)))

    return lp_delta
