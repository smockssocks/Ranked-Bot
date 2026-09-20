"""
Checks everything the bot needs BEFORE it tries to start, and explains in plain
language how to fix whatever is missing. Run by start.bat; you can also run it
yourself:  .venv\Scripts\python.exe tools\preflight.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OK = "  [OK]   "
BAD = "  [FIX]  "

PLACEHOLDERS = {
    "your_discord_bot_token_here", "your_guild_id_here", "your_riot_api_key_here",
    "your_discord_token_here", "paste_it_here", "changeme", "xxx",
}

problems: list[str] = []


def say(good: bool, label: str, detail: str = "") -> None:
    print(f"{OK if good else BAD}{label}" + (f"  {detail}" if detail else ""))


def check_python() -> None:
    v = sys.version_info
    good = (v.major, v.minor) >= (3, 11)
    say(good, f"Python {v.major}.{v.minor}.{v.micro}")
    if not good:
        problems.append(
            "Your Python is too old. The bot needs Python 3.11 or newer.\n"
            "   Download it from https://www.python.org/downloads/ and during\n"
            "   the install TICK THE BOX that says 'Add python.exe to PATH'.\n"
            "   Then delete the .venv folder in this directory and run start.bat again."
        )


def check_packages() -> None:
    missing = []
    for mod, name in (("discord", "discord.py"), ("sqlalchemy", "SQLAlchemy"),
                      ("aiohttp", "aiohttp"), ("aiosqlite", "aiosqlite"), ("dotenv", "python-dotenv")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(name)
    say(not missing, "Bot dependencies installed", "" if not missing else f"missing: {', '.join(missing)}")
    if missing:
        problems.append(
            "Some dependencies did not install.\n"
            "   Delete the .venv folder in this directory, then run start.bat again.\n"
            "   If it keeps failing, check that your internet connection works and\n"
            "   that antivirus is not blocking pip."
        )


def read_env() -> dict[str, str]:
    """Read .env exactly the way the bot does, so this check cannot disagree with it."""
    try:
        from dotenv import dotenv_values
    except ImportError:
        return {}
    return {k: (v or "") for k, v in dotenv_values(ROOT / ".env").items()}


def check_env() -> None:
    path = ROOT / ".env"
    if not path.exists():
        say(False, "Settings file (.env)", "not found")
        problems.append(
            "There is no .env settings file.\n"
            "   Run start.bat again and it will create one for you."
        )
        return
    say(True, "Settings file (.env) found")
    env = read_env()

    token = env.get("DISCORD_TOKEN", "")
    good = bool(token) and token.lower() not in PLACEHOLDERS and len(token) > 40
    say(good, "DISCORD_TOKEN", "" if good else "empty, still a placeholder, or too short")
    if not good:
        problems.append(
            "DISCORD_TOKEN is not filled in.\n"
            "   1. Go to https://discord.com/developers/applications\n"
            "   2. Click your application, then the 'Bot' tab on the left\n"
            "   3. Click 'Reset Token', then 'Copy'\n"
            "   4. Open the .env file in this folder with Notepad and paste it after\n"
            "      DISCORD_TOKEN=   (no spaces, no quotes)"
        )

    guild = env.get("DISCORD_GUILD_ID", "")
    good = guild.isdigit() and len(guild) >= 17
    say(good, "DISCORD_GUILD_ID", "" if good else "should be a long number like 123456789012345678")
    if not good:
        problems.append(
            "DISCORD_GUILD_ID is not filled in.\n"
            "   1. In Discord: Settings > Advanced > turn ON Developer Mode\n"
            "   2. Right-click your SERVER name in the left sidebar > 'Copy Server ID'\n"
            "   3. Paste it into .env after DISCORD_GUILD_ID="
        )

    riot = env.get("RIOT_API_KEY", "")
    good = riot.startswith("RGAPI-") or (bool(riot) and riot.lower() not in PLACEHOLDERS and len(riot) > 20)
    say(good, "RIOT_API_KEY", "" if good else "empty or still a placeholder")
    if not good:
        problems.append(
            "RIOT_API_KEY is not filled in. Without it nobody can link an account\n"
            "   and no games can be scored.\n"
            "   1. Go to https://developer.riotgames.com and sign in with a Riot account\n"
            "   2. Copy the 'DEVELOPMENT API KEY' on the front page. It starts with RGAPI-\n"
            "      NOTE: development keys stop working after 24 hours. For permanent use,\n"
            "      click 'Register Product' and apply for a Personal API Key.\n"
            "   3. Paste it into .env after RIOT_API_KEY="
        )
    elif riot.startswith("RGAPI-"):
        print("         note: development keys expire every 24 hours. Apply for a")
        print("         Personal API Key at https://developer.riotgames.com for permanent use.")

    region = env.get("RIOT_REGION", "na1")
    platform = env.get("RIOT_PLATFORM", "americas")
    expected = {
        "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas",
        "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe", "me1": "europe",
        "kr": "asia", "jp1": "asia",
        "oc1": "sea", "ph2": "sea", "sg2": "sea", "th2": "sea", "tw2": "sea", "vn2": "sea",
    }
    want = expected.get(region)
    good = want is not None and want == platform
    say(good, f"Region {region} / platform {platform}")
    if not good:
        if want is None:
            problems.append(
                f"RIOT_REGION '{region}' is not a region I recognise.\n"
                "   Use one of: na1 euw1 eun1 kr br1 la1 la2 oc1 tr1 ru jp1 ph2 sg2 th2 tw2 vn2 me1"
            )
        else:
            problems.append(
                f"RIOT_PLATFORM should be '{want}' when RIOT_REGION is '{region}'.\n"
                f"   Open .env and change the RIOT_PLATFORM line to:  RIOT_PLATFORM={want}"
            )

    # Optional extras: report only, never block startup.
    print()
    print("  Optional features:")
    for key, label, note in (
        ("OPENROUTER_API_KEY", "Chat (bot talks to people)", "get a key at https://openrouter.ai/keys"),
        ("DRAFTER_API_KEY", "drafter.lol draft links", "needs a drafter.lol subscription"),
        ("MOD_CHANNEL_ID", "Smurf alerts channel", "or set it later in Discord with /admin modchannel"),
    ):
        val = env.get(key, "")
        on = bool(val) and val.lower() not in PLACEHOLDERS
        print(f"    {'ON ' if on else 'off'}  {label}" + ("" if on else f"   ({note})"))


def main() -> int:
    print()
    print("  Checking your setup")
    print("  " + "-" * 44)
    check_python()
    check_packages()
    check_env()
    print()
    if not problems:
        print("  " + "-" * 44)
        print("  Everything looks good. Starting the bot...")
        print()
        return 0
    print("  " + "=" * 44)
    print(f"  {len(problems)} thing(s) need fixing before the bot can start:")
    print("  " + "=" * 44)
    for i, p in enumerate(problems, 1):
        print(f"\n  {i}. {p}")
    print()
    print("  Fix the above, save the file, then run start.bat again.")
    print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
