import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
DISCORD_GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])

RIOT_API_KEY = os.environ["RIOT_API_KEY"]
RIOT_REGION = os.getenv("RIOT_REGION", "na1")
RIOT_PLATFORM = os.getenv("RIOT_PLATFORM", "americas")
RIOT_TOURNAMENT_API_KEY = os.getenv("RIOT_TOURNAMENT_API_KEY", "")

DATABASE_URL = os.environ["DATABASE_URL"]

LOBBY_SIZE = 10
LOBBY_TIMEOUT_SECS = 3600

# --- Rating engine weight config ---
# w0[j] = initial weight for metric j
# alpha[j] = exponential decay rate for metric j
METRIC_NAMES = [
    "gold_differential",
    "kill_participation",
    "objective_damage",
    "vision_score_delta",
    "cs_per_min",
    "deaths_negated",
]

WEIGHT_INITIAL = {
    "gold_differential":  1.0,
    "kill_participation": 0.8,
    "objective_damage":   1.2,
    "vision_score_delta": 0.6,
    "cs_per_min":         0.9,
    "deaths_negated":     0.7,
}

WEIGHT_DECAY = {
    "gold_differential":  0.03,
    "kill_participation": 0.02,
    "objective_damage":   0.04,
    "vision_score_delta": 0.01,
    "cs_per_min":         0.05,
    "deaths_negated":     0.02,
}

TIMELINE_INTERVAL_MINS = 5.0

# LP gain/loss limits and scaling
LP_SCALE = 15.0
LP_MAX_WIN = 25
LP_MAX_LOSS = 20
LP_MIN_WIN = 8
LP_MIN_LOSS = 8

# MMR/LP anchors: maps LP → expected raw impact score for baseline comparison
LP_BASELINE_ANCHORS = [
    (0,    0.0),
    (400,  0.5),
    (800,  1.2),
    (1200, 2.0),
    (1600, 2.9),
    (2000, 3.8),
]
