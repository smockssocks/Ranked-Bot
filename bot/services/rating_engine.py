"""
Rating engine: pure math, no DB, no Discord.

Model overview
--------------
Every player carries, per role and overall:
  mmr  - hidden skill estimate (Glicko-style mean, starts 1500)
  rd   - rating deviation: how unsure we are (starts 350, shrinks with games,
         grows with inactivity). New players move fast, veterans move slowly.
  lp   - the visible ladder number (starts 1000). Tiers are derived from LP.
  perf_mean / perf_var - running performance score stats (consistency).

Per game, for each player we compute a PERFORMANCE SCORE (PS): a role-weighted,
champion-agnostic z-score composite of what the Riot API tells us about the game.
Lane metrics are relative to the lane opponent, team metrics are shares of the
team total, everything is normalised against a per-role community baseline that
learns from every processed game (Welford). Each metric is clipped so no single
absurd stat can dominate. Metric weights are phase-tagged (early / mid / late) and
re-weighted by game length: the "time dependent operator" idea from the design
notes. A 19 minute stomp is judged mostly on laning; a 45 minute game mostly on
teamfights, objectives and deaths.

LP delta = outcome term + performance term
  outcome term      = 2K * (S - E)          S = 1 win / 0 loss, E = expected win prob
  performance term  = K * Wp * tanh(perf / 2) * consistency_mult
                      Wp = 1.0 on a win, 1.4 for a good performance in a loss
  perf              = 0.7 * PS + 0.3 * (PS - team mean PS)      ("carry factor")
  K                 = BASE_K * uncertainty(rd)                    (placements move faster)

Bounds: a win never loses LP (min +2), a loss can net positive LP (max +10) only
for a genuinely exceptional performance (PS around +2 sigma or better, i.e. the
player clearly outplayed the lobby while their team collapsed).

The public surface used by the rest of the bot:
  performance_score(metrics, role, baseline, in_game_pool, duration_mins) -> PerfResult
  expected_win(team_mmrs, opp_mmrs) -> float
  compute_update(...) -> RatingUpdate
  role_weights(role) -> dict
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

ROLES = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
ROLE_ALIASES = {
    "MID": "MIDDLE", "MIDDLE": "MIDDLE",
    "ADC": "BOTTOM", "BOT": "BOTTOM", "BOTTOM": "BOTTOM",
    "SUPPORT": "UTILITY", "SUP": "UTILITY", "SUPP": "UTILITY", "UTILITY": "UTILITY",
    "TOP": "TOP", "JUNGLE": "JUNGLE", "JG": "JUNGLE", "JNG": "JUNGLE",
}
ROLE_DISPLAY = {"TOP": "Top", "JUNGLE": "Jungle", "MIDDLE": "Mid", "BOTTOM": "Bot", "UTILITY": "Support"}

BASE_K = 20.0
OUTCOME_WEIGHT = 2.0       # even game win = +BASE_K*OUTCOME_WEIGHT*0.5 = +20
PERF_WEIGHT_WIN = 1.0      # bonus/penalty scale on a win
PERF_WEIGHT_LOSS_POS = 1.4 # mitigation scale for a good performance in a loss
PERF_WEIGHT_LOSS_NEG = 1.0 # extra penalty scale for a bad performance in a loss
PERF_SOFTNESS = 2.0        # tanh(perf / PERF_SOFTNESS)
WIN_MIN, WIN_MAX = 2, 60
LOSS_MIN, LOSS_MAX = -60, 10
Z_CLIP = 2.5
MMR_PERF_WEIGHT = 0.35     # how much performance moves hidden MMR (vs outcome)
MMR_SCALE = 400.0          # logistic scale for expected win
RD_MIN, RD_MAX = 60.0, 350.0
RD_SHRINK_C = 120.0        # per game information; smaller = faster convergence
RD_GROWTH_PER_DAY = 3.0    # inactivity growth
EWMA_ALPHA = 0.25

# Phase tags. Weight multipliers are functions of game length (minutes).
PHASE_EARLY, PHASE_MID, PHASE_LATE, PHASE_ALL = "early", "mid", "late", "all"


def phase_multiplier(phase: str, duration_mins: float) -> float:
    """
    Smoothly re-weight metrics by game length.
      early metrics: 1.35 at 18 min -> 0.75 at 40+ min
      late  metrics: 0.6  at 18 min -> 1.3  at 40+ min
      mid   metrics: peak around 30 min
    """
    d = max(15.0, min(50.0, duration_mins))
    t = (d - 15.0) / 35.0  # 0..1
    if phase == PHASE_EARLY:
        return 1.35 - 0.6 * t
    if phase == PHASE_LATE:
        return 0.6 + 0.7 * t
    if phase == PHASE_MID:
        return 0.85 + 0.5 * math.sin(math.pi * t)  # peaks mid-length
    return 1.0


# --------------------------------------------------------------------------- #
# Metric definitions                                                          #
# --------------------------------------------------------------------------- #
# name -> (phase, prior_mean, prior_std). Priors are used until the community
# baseline has enough samples; they are deliberately generic inhouse-level values.
# Metrics that are lane-relative (diffs) or shares have priors centred near their
# natural neutral point.

METRICS: dict[str, tuple[str, float, float]] = {
    # laning (vs lane opponent, from timeline)
    "gold_diff_10":        (PHASE_EARLY, 0.0, 600.0),
    "gold_diff_15":        (PHASE_EARLY, 0.0, 900.0),
    "xp_diff_10":          (PHASE_EARLY, 0.0, 500.0),
    "cs_diff_10":          (PHASE_EARLY, 0.0, 12.0),
    "lane_trajectory":     (PHASE_EARLY, 0.0, 1.0),    # time-weighted gold-diff curve (pre-scaled)
    # economy
    "cs_per_min":          (PHASE_ALL, 6.0, 1.6),
    "gold_per_min":        (PHASE_ALL, 380.0, 60.0),
    # combat
    "kill_participation":  (PHASE_MID, 0.52, 0.16),
    "deaths_per_min":      (PHASE_ALL, 0.17, 0.08),    # negative weight
    "kda_adj":             (PHASE_ALL, 2.5, 1.6),
    "damage_share":        (PHASE_LATE, 0.20, 0.07),
    "damage_per_min":      (PHASE_LATE, 650.0, 250.0),
    "damage_taken_share":  (PHASE_LATE, 0.20, 0.07),
    "solo_kills":          (PHASE_MID, 0.5, 0.9),
    # objectives / macro
    "objective_participation": (PHASE_MID, 0.45, 0.22),
    "turret_plates":       (PHASE_EARLY, 1.0, 1.3),
    # vision
    "vision_per_min":      (PHASE_ALL, 0.9, 0.45),
    "control_wards":       (PHASE_MID, 1.5, 1.6),
    "wards_killed":        (PHASE_MID, 3.0, 3.0),
    # utility
    "heal_shield_per_min": (PHASE_LATE, 80.0, 120.0),
    "cc_per_min":          (PHASE_LATE, 0.6, 0.5),
}

# Role weight profiles. Positive = more is better, negative = less is better.
# They are normalised by sum of absolute weights so PS is comparable across roles.
_LANER_BASE = {
    "gold_diff_10": 1.0, "gold_diff_15": 1.0, "xp_diff_10": 0.6, "cs_diff_10": 0.7,
    "lane_trajectory": 0.8,
    "cs_per_min": 0.7, "gold_per_min": 0.5,
    "kill_participation": 0.8, "deaths_per_min": -1.4, "kda_adj": 0.6,
    "damage_share": 1.1, "damage_per_min": 0.6, "solo_kills": 0.4,
    "objective_participation": 0.6, "turret_plates": 0.4,
    "vision_per_min": 0.4, "control_wards": 0.2, "wards_killed": 0.2,
}

ROLE_WEIGHTS: dict[str, dict[str, float]] = {
    "TOP": {**_LANER_BASE, "damage_taken_share": 0.5, "cc_per_min": 0.2, "kill_participation": 0.6},
    "MIDDLE": {**_LANER_BASE, "kill_participation": 1.0, "damage_share": 1.2},
    "BOTTOM": {**_LANER_BASE, "damage_share": 1.4, "damage_per_min": 0.8, "solo_kills": 0.2},
    "JUNGLE": {
        "gold_diff_10": 0.5, "gold_diff_15": 0.6, "xp_diff_10": 0.4, "cs_diff_10": 0.3,
        "lane_trajectory": 0.4,
        "cs_per_min": 0.5, "gold_per_min": 0.5,
        "kill_participation": 1.4, "deaths_per_min": -1.3, "kda_adj": 0.6,
        "damage_share": 0.6, "damage_per_min": 0.4, "damage_taken_share": 0.3, "solo_kills": 0.2,
        "objective_participation": 1.6, "turret_plates": 0.2,
        "vision_per_min": 0.8, "control_wards": 0.4, "wards_killed": 0.5, "cc_per_min": 0.3,
    },
    "UTILITY": {
        "gold_diff_10": 0.4, "gold_diff_15": 0.4, "xp_diff_10": 0.4, "cs_diff_10": 0.0,
        "lane_trajectory": 0.4,
        "cs_per_min": 0.0, "gold_per_min": 0.3,
        "kill_participation": 1.4, "deaths_per_min": -1.3, "kda_adj": 0.6,
        "damage_share": 0.3, "damage_per_min": 0.2, "damage_taken_share": 0.3,
        "objective_participation": 0.9, "turret_plates": 0.1,
        "vision_per_min": 1.5, "control_wards": 0.8, "wards_killed": 0.7,
        "heal_shield_per_min": 0.9, "cc_per_min": 0.9,
    },
}


def normalize_role(role: str | None) -> str:
    if not role:
        return "MIDDLE"
    return ROLE_ALIASES.get(role.upper(), "MIDDLE")


def role_weights(role: str) -> dict[str, float]:
    return ROLE_WEIGHTS[normalize_role(role)]


# --------------------------------------------------------------------------- #
# Baselines (community averages per role, learned online)                     #
# --------------------------------------------------------------------------- #

@dataclass
class Baseline:
    """Welford running mean/variance for one role, keyed by metric."""
    role: str
    n: int = 0
    mean: dict[str, float] = field(default_factory=dict)
    m2: dict[str, float] = field(default_factory=dict)

    def std(self, metric: str) -> float:
        if self.n < 2 or metric not in self.m2:
            return 0.0
        return math.sqrt(max(self.m2[metric] / (self.n - 1), 1e-12))

    def update(self, metrics: dict[str, float]) -> None:
        self.n += 1
        for k in METRICS:
            x = float(metrics.get(k, 0.0))
            mu = self.mean.get(k, 0.0)
            delta = x - mu
            mu += delta / self.n
            self.mean[k] = mu
            self.m2[k] = self.m2.get(k, 0.0) + delta * (x - mu)

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "n": self.n, "mean": self.mean, "m2": self.m2}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None, role: str) -> "Baseline":
        if not d:
            return cls(role=role)
        return cls(role=role, n=int(d.get("n", 0)), mean=dict(d.get("mean", {})), m2=dict(d.get("m2", {})))


def _blend_stats(metric: str, baseline: Baseline | None, pool: list[float] | None) -> tuple[float, float]:
    """
    Mean/std used to z-score a metric. Blend of:
      prior (hard coded) -> community baseline (as n grows) -> in-game 10 player pool (small share).
    """
    _phase, p_mean, p_std = METRICS[metric]
    mean, std = p_mean, p_std
    if baseline and baseline.n >= 2 and metric in baseline.mean:
        w = min(1.0, baseline.n / 60.0)          # full trust after ~60 games in role
        b_std = baseline.std(metric) or p_std
        mean = (1 - w) * p_mean + w * baseline.mean[metric]
        std = (1 - w) * p_std + w * max(b_std, 0.25 * p_std)
    if pool and len(pool) >= 4:
        pm = sum(pool) / len(pool)
        pv = sum((v - pm) ** 2 for v in pool) / (len(pool) - 1)
        ps = math.sqrt(pv) if pv > 0 else std
        mean = 0.8 * mean + 0.2 * pm
        std = 0.8 * std + 0.2 * max(ps, 0.25 * p_std)
    return mean, max(std, 1e-6)


@dataclass
class PerfResult:
    score: float                       # PS, roughly -3..+3
    z: dict[str, float]                # per-metric clipped z-scores
    contributions: dict[str, float]    # weight * phase * z (for explanations)
    top_positive: list[str]
    top_negative: list[str]


def performance_score(
    metrics: dict[str, float],
    role: str,
    baseline: Baseline | None = None,
    in_game_pool: dict[str, list[float]] | None = None,
    duration_mins: float = 30.0,
) -> PerfResult:
    """Compute the champion-agnostic performance score for one player."""
    role = normalize_role(role)
    weights = ROLE_WEIGHTS[role]
    z: dict[str, float] = {}
    contrib: dict[str, float] = {}
    total_abs = 0.0
    total = 0.0
    for metric, w in weights.items():
        if w == 0 or metric not in METRICS:
            continue
        phase = METRICS[metric][0]
        pool = in_game_pool.get(metric) if in_game_pool else None
        mean, std = _blend_stats(metric, baseline, pool)
        raw = float(metrics.get(metric, mean))
        zval = max(-Z_CLIP, min(Z_CLIP, (raw - mean) / std))
        z[metric] = zval
        eff_w = w * phase_multiplier(phase, duration_mins)
        c = eff_w * zval
        contrib[metric] = c
        total += c
        total_abs += abs(eff_w)
    score = (total / total_abs) * 2.0 if total_abs else 0.0   # scale so |PS| ~ 2 at avg |z|=1
    score = max(-3.0, min(3.0, score))
    ordered = sorted(contrib.items(), key=lambda kv: kv[1])
    top_neg = [k for k, v in ordered[:3] if v < -0.05]
    top_pos = [k for k, v in ordered[::-1][:3] if v > 0.05]
    return PerfResult(score=score, z=z, contributions=contrib, top_positive=top_pos, top_negative=top_neg)


# --------------------------------------------------------------------------- #
# Expected outcome / rating updates                                           #
# --------------------------------------------------------------------------- #

def expected_win(team_mmrs: Iterable[float], opp_mmrs: Iterable[float]) -> float:
    t = list(team_mmrs)
    o = list(opp_mmrs)
    if not t or not o:
        return 0.5
    diff = (sum(t) / len(t)) - (sum(o) / len(o))
    return 1.0 / (1.0 + math.exp(-diff / MMR_SCALE))


def uncertainty_multiplier(rd: float) -> float:
    """K multiplier: 1.0 at RD_MIN -> 2.0 at RD_MAX."""
    rd = max(RD_MIN, min(RD_MAX, rd))
    return 1.0 + 1.0 * (rd - RD_MIN) / (RD_MAX - RD_MIN)


def shrink_rd(rd: float) -> float:
    """After a game we learned something: 1/rd'^2 = 1/rd^2 + 1/c^2."""
    new = 1.0 / math.sqrt(1.0 / (rd * rd) + 1.0 / (RD_SHRINK_C * RD_SHRINK_C))
    return max(RD_MIN, new)


def grow_rd(rd: float, days_inactive: float) -> float:
    if days_inactive <= 0:
        return rd
    return min(RD_MAX, math.sqrt(rd * rd + (RD_GROWTH_PER_DAY * days_inactive) ** 2))


def consistency_score(perf_var: float) -> float:
    """0..1, 1 = perfectly consistent. perf_var is EWMA variance of PS."""
    return 1.0 / (1.0 + max(0.0, perf_var))


@dataclass
class RatingState:
    mmr: float = 1500.0
    rd: float = 350.0
    lp: int = 1000
    games: int = 0
    perf_mean: float = 0.0
    perf_var: float = 1.0


@dataclass
class RatingUpdate:
    lp_delta: int
    lp_after: int
    mmr_after: float
    rd_after: float
    perf_mean_after: float
    perf_var_after: float
    expected_win: float
    perf_score: float
    carry_factor: float
    outcome_term: float
    performance_term: float
    k: float
    explanation: str


def compute_update(
    state: RatingState,
    won: bool,
    perf_score: float,
    team_perf_scores: Iterable[float],
    team_mmrs: Iterable[float],
    opp_mmrs: Iterable[float],
    placement_games: int = 5,
) -> RatingUpdate:
    """
    Compute LP/MMR/RD changes for one player from one game.
    team_perf_scores includes the player's own score.
    """
    team_ps = list(team_perf_scores)
    team_mean = sum(team_ps) / len(team_ps) if team_ps else perf_score
    carry = perf_score - team_mean
    perf = 0.7 * perf_score + 0.3 * carry

    E = expected_win(team_mmrs, opp_mmrs)
    S = 1.0 if won else 0.0
    K = BASE_K * uncertainty_multiplier(state.rd)
    if state.games < placement_games:
        K *= 1.15  # placements: a touch more movement on top of high RD

    cons = consistency_score(state.perf_var)
    cons_mult = 0.9 + 0.2 * cons
    perf_signal = math.tanh(perf / PERF_SOFTNESS)
    if won:
        wp = PERF_WEIGHT_WIN
    else:
        wp = PERF_WEIGHT_LOSS_POS if perf_signal > 0 else PERF_WEIGHT_LOSS_NEG
    # Consistency only amplifies credit, never punishment, so newcomers are not double-taxed.
    perf_term = K * wp * perf_signal * (cons_mult if perf_signal > 0 else 1.0)
    outcome_term = K * OUTCOME_WEIGHT * (S - E)

    raw = outcome_term + perf_term
    if won:
        lp_delta = int(round(max(WIN_MIN, min(WIN_MAX, raw))))
    else:
        lp_delta = int(round(max(LOSS_MIN, min(LOSS_MAX, raw))))

    lp_after = max(0, state.lp + lp_delta)

    # Hidden MMR: mostly outcome, some performance so matchmaking learns quickly.
    mmr_k = 24.0 * (max(RD_MIN, min(RD_MAX, state.rd)) / RD_MIN)   # 24 at RD 60 -> 140 at RD 350
    mmr_after = state.mmr + mmr_k * ((S - E) + MMR_PERF_WEIGHT * perf_signal)
    rd_after = shrink_rd(state.rd)

    # Consistency tracking (EWMA of PS and its variance)
    if state.games == 0:
        pm_after, pv_after = perf_score, 1.0
    else:
        diff = perf_score - state.perf_mean
        pm_after = state.perf_mean + EWMA_ALPHA * diff
        pv_after = (1 - EWMA_ALPHA) * (state.perf_var + EWMA_ALPHA * diff * diff)

    expl = _explain(won, E, perf_score, carry, outcome_term, perf_term, lp_delta)
    return RatingUpdate(
        lp_delta=lp_delta, lp_after=lp_after, mmr_after=mmr_after, rd_after=rd_after,
        perf_mean_after=pm_after, perf_var_after=pv_after, expected_win=E,
        perf_score=perf_score, carry_factor=carry, outcome_term=outcome_term,
        performance_term=perf_term, k=K, explanation=expl,
    )


def _explain(won: bool, E: float, ps: float, carry: float, ot: float, pt: float, delta: int) -> str:
    res = "Win" if won else "Loss"
    fav = "favoured" if E > 0.55 else ("underdog" if E < 0.45 else "even")
    perf_word = (
        "elite" if ps >= 2.0 else "strong" if ps >= 1.0 else "solid" if ps >= 0.3
        else "average" if ps > -0.3 else "weak" if ps > -1.0 else "poor"
    )
    carry_word = "carried" if carry >= 0.8 else ("outperformed teammates" if carry >= 0.3 else
                 "was carried" if carry <= -0.8 else "underperformed teammates" if carry <= -0.3 else "matched teammates")
    return (
        f"{res} as {fav} team (expected {E*100:.0f}%): outcome {ot:+.1f}. "
        f"Performance {perf_word} (PS {ps:+.2f}, {carry_word}): {pt:+.1f}. Net {delta:+d} LP."
    )


def expected_perf_for_mmr(mmr: float) -> float:
    """
    What PS we expect from a player of a given hidden skill *within our pool*.
    Used by the anti-smurf detector to spot newcomers who wildly exceed it.
    """
    return max(-1.5, min(1.5, (mmr - 1500.0) / 400.0 * 0.5))
