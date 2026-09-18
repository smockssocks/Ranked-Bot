"""
Central configuration. Every value is read from the environment (.env is loaded).
Import never crashes: missing secrets become empty strings so tests and tooling
can import the package. main.py validates the values it truly needs at startup.
"""
from __future__ import annotations
import os
from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# --- Discord ---------------------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DISCORD_GUILD_ID = _int("DISCORD_GUILD_ID", 0)
MOD_CHANNEL_ID = _int("MOD_CHANNEL_ID", 0)          # smurf flags / alerts go here
RESULTS_CHANNEL_ID = _int("RESULTS_CHANNEL_ID", 0)  # optional: post-game summaries
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")

# --- Riot ------------------------------------------------------------------
RIOT_API_KEY = os.getenv("RIOT_API_KEY", "")
RIOT_REGION = os.getenv("RIOT_REGION", "na1")
RIOT_PLATFORM = os.getenv("RIOT_PLATFORM", "americas")
RIOT_TOURNAMENT_API_KEY = os.getenv("RIOT_TOURNAMENT_API_KEY", "")
RIOT_TOURNAMENT_CALLBACK_URL = os.getenv("RIOT_TOURNAMENT_CALLBACK_URL", "https://example.com/riot-callback")

# --- Database --------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./ranked_bot.db")

# --- Drafter.lol -----------------------------------------------------------
DRAFTER_API_KEY = os.getenv("DRAFTER_API_KEY", "")
DRAFTER_API_BASE = os.getenv("DRAFTER_API_BASE", "https://api.drafter.lol")
DRAFTER_SERIES_PATH = os.getenv("DRAFTER_SERIES_PATH", "/api/series")
DRAFTER_DRAFT_PATH = os.getenv("DRAFTER_DRAFT_PATH", "/api/draft")
DRAFTER_POLL_SECS = _int("DRAFTER_POLL_SECS", 20)
DRAFTER_FEARLESS = _bool("DRAFTER_FEARLESS", False)

# --- OpenRouter chat -------------------------------------------------------
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
OPENROUTER_SUMMARY_MODEL = os.getenv("OPENROUTER_SUMMARY_MODEL", OPENROUTER_MODEL)
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
CHAT_BOT_NAME = os.getenv("CHAT_BOT_NAME", "Ranked Bot")
CHAT_CHANNEL_IDS = [int(x) for x in os.getenv("CHAT_CHANNEL_IDS", "").replace(" ", "").split(",") if x]
CHAT_FOLLOWUP_WINDOW_SECS = _int("CHAT_FOLLOWUP_WINDOW_SECS", 120)
CHAT_HISTORY_TURNS = _int("CHAT_HISTORY_TURNS", 8)
CHAT_SUMMARY_EVERY = _int("CHAT_SUMMARY_EVERY", 10)
CHAT_DAILY_TOKEN_BUDGET = _int("CHAT_DAILY_TOKEN_BUDGET", 300_000)
CHAT_MAX_REPLY_TOKENS = _int("CHAT_MAX_REPLY_TOKENS", 350)
CHAT_PERSONA = os.getenv(
    "CHAT_PERSONA",
    "You are the ranked inhouse bot that runs this Discord server's custom games. "
    "You are friendly, a little competitive, and concise. You know the server's ranking "
    "system inside out and explain LP changes honestly. Never invent stats you were not given.",
)

# --- Lobby -----------------------------------------------------------------
LOBBY_SIZE = _int("LOBBY_SIZE", 10)
LOBBY_TIMEOUT_SECS = _int("LOBBY_TIMEOUT_SECS", 3600)
AUTO_DETECT_ENABLED = _bool("AUTO_DETECT_ENABLED", True)
AUTO_DETECT_POLL_SECS = _int("AUTO_DETECT_POLL_SECS", 120)
AUTO_DETECT_MIN_LOBBY_PLAYERS = _int("AUTO_DETECT_MIN_LOBBY_PLAYERS", 8)
AUTO_DETECT_MAX_AGE_HOURS = _int("AUTO_DETECT_MAX_AGE_HOURS", 6)

# --- Ranking ---------------------------------------------------------------
CURRENT_SEASON = _int("CURRENT_SEASON", 1)
STARTING_LP = _int("STARTING_LP", 1000)
STARTING_MMR = _float("STARTING_MMR", 1500.0)
STARTING_RD = _float("STARTING_RD", 350.0)
PLACEMENT_GAMES = _int("PLACEMENT_GAMES", 5)
MIN_GAME_MINUTES = _float("MIN_GAME_MINUTES", 15.0)   # shorter games are remakes: no LP
INACTIVITY_DECAY_DAYS = _int("INACTIVITY_DECAY_DAYS", 14)

# --- Anti-smurf ------------------------------------------------------------
SMURF_ENABLED = _bool("SMURF_ENABLED", True)
SMURF_SIMILARITY_THRESHOLD = _float("SMURF_SIMILARITY_THRESHOLD", 0.72)
SMURF_MIN_GAMES = _int("SMURF_MIN_GAMES", 3)
SMURF_LOW_LEVEL = _int("SMURF_LOW_LEVEL", 60)
