"""
Tests your Riot API key and tells you exactly what is wrong with it.

Run it while the bot is stopped:
    Windows        .venv\\Scripts\\python.exe tools\\check_riot_key.py
    Linux/macOS    .venv/bin/python tools/check_riot_key.py

It never prints your full key.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# A Riot key is "RGAPI-" plus a 36 character UUID.
EXPECTED_LEN = 42
PLATFORM_FOR_REGION = {
    "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas",
    "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe", "me1": "europe",
    "kr": "asia", "jp1": "asia",
    "oc1": "sea", "ph2": "sea", "sg2": "sea", "th2": "sea", "tw2": "sea", "vn2": "sea",
}


def mask(key: str) -> str:
    if not key:
        return "(empty)"
    if len(key) <= 14:
        return key[:4] + "..."
    return f"{key[:11]}...{key[-4:]}"


def hr(title: str = "") -> None:
    print("  " + "-" * 62)
    if title:
        print(f"  {title}")
        print("  " + "-" * 62)


def scan_raw_file() -> list[str]:
    """Look at the bytes of .env for the things that silently corrupt a key."""
    problems: list[str] = []
    path = ROOT / ".env"
    if not path.exists():
        print("  [FIX] There is no .env file. Run START-BOT.bat to create one.")
        return ["no .env file"]

    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        print("  [!]  Your .env starts with a hidden BOM marker from Notepad.")
        print("       Harmless for now, but if settings act strange, re-save the file")
        print("       in Notepad with Encoding set to 'UTF-8' rather than 'UTF-8 with BOM'.")

    text = raw.decode("utf-8", errors="replace")
    hits = [ln for ln in text.splitlines() if ln.strip().startswith("RIOT_API_KEY")]
    if not hits:
        print("  [FIX] No RIOT_API_KEY line found in .env.")
        return ["no RIOT_API_KEY line"]
    if len(hits) > 1:
        print(f"  [FIX] RIOT_API_KEY appears {len(hits)} times in .env.")
        print("       The LAST one wins, which is probably not the one you edited.")
        print("       Delete the duplicates so only one remains.")
        problems.append("duplicate RIOT_API_KEY lines")

    line = hits[-1]
    value = line.partition("=")[2]
    if value != value.strip():
        print("  [!]  There is extra whitespace around the key in .env. Usually harmless.")
    v = value.strip()
    if v.startswith(("'", '"')) and v.endswith(("'", '"')):
        print("  [!]  Your key is wrapped in quotation marks. That is allowed, but")
        print("       plain is safer: RIOT_API_KEY=RGAPI-....")
    if " " in v.strip("'\""):
        print("  [FIX] The key contains a space. It was probably copied incompletely.")
        problems.append("space inside key")
    return problems


async def check_tournament(config, host: str) -> None:
    """
    Ask Riot whether the key may make tournament codes, without creating anything: look up a
    code that can't exist. 403 means no Tournament API access; 400 or 404 mean the key got
    past the access check.
    """
    import aiohttp
    hr("Tournament codes")
    key = config.tournament_api_key()
    label = "RIOT_TOURNAMENT_API_KEY" if config.RIOT_TOURNAMENT_API_KEY else "RIOT_API_KEY"
    if not key:
        print("  Off. Games use a lobby name and password, which works fine.")
        print("  To use real tournament codes, see TOURNAMENT-CODES.md.")
        key, label = config.RIOT_API_KEY, "RIOT_API_KEY"
        print("  Checking anyway whether your main key could make them...")
    url = f"https://{host}/lol/tournament/v5/codes/NA0000-ACCESSCHECK"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers={"X-Riot-Token": key},
                             timeout=aiohttp.ClientTimeout(total=20)) as resp:
                status = resp.status
    except Exception as e:
        print(f"  [!]  Could not ask Riot about tournament access: {e}")
        return
    print(f"  Riot replied: HTTP {status} for {label}")
    if status in (400, 404):
        print(f"  [OK]  {label} has Tournament API access. Tournament codes will work.")
        if not config.tournament_api_key():
            print("        Turn them on with TOURNAMENT_CODES=true in .env, then restart the bot.")
        from bot.services.game_channel import callback_problem
        problem = callback_problem(config.RIOT_TOURNAMENT_CALLBACK_URL)
        if problem:
            print(f"  [FIX] {problem}")
    elif status in (401, 403):
        print(f"  [--]  {label} does NOT have Tournament API access.")
        print("        Development and Personal keys never do. You need a Production key")
        print("        that Riot approved for tournaments. TOURNAMENT-CODES.md explains how.")
        if config.tournament_api_key():
            print("        Until then the bot falls back to lobby passwords, so games still work.")
    elif status == 429:
        print("  [!]  Rate limited. Try again in a minute.")
    else:
        print("  [!]  Unexpected reply. Tournament access is unknown.")


async def main() -> int:
    print()
    hr("RIOT API KEY CHECK")

    problems = scan_raw_file()

    # Read the key the same way the bot does, AFTER checking for shadowing.
    os_level = os.environ.get("RIOT_API_KEY")
    from dotenv import dotenv_values
    file_values = dotenv_values(ROOT / ".env")
    file_key = (file_values.get("RIOT_API_KEY") or "").strip()

    if os_level and file_key and os_level.strip() != file_key:
        print()
        print("  [FIX] IMPORTANT: RIOT_API_KEY is ALSO set as a system environment")
        print("        variable on this computer, and it does NOT match your .env file.")
        print(f"          system says : {mask(os_level.strip())}")
        print(f"          .env says   : {mask(file_key)}")
        print()
        print("        The bot now prefers the .env file, so this is no longer fatal,")
        print("        but you should delete the stray system variable:")
        print("          1. Press the Windows key and type 'environment variables'")
        print("          2. Open 'Edit the system environment variables'")
        print("          3. Click 'Environment Variables...'")
        print("          4. Find RIOT_API_KEY in either list, select it, click Delete")
        print("          5. Click OK, then restart the bot")
        problems.append("system environment variable shadowing .env")

    from bot import config  # loads .env with override=True
    key = config.RIOT_API_KEY.strip()

    print()
    print(f"  Key the bot will use : {mask(key)}")
    print(f"  Length               : {len(key)} characters")

    if not key:
        print("  [FIX] The key is empty. Paste it into .env after RIOT_API_KEY=")
        return 1
    if not key.startswith("RGAPI-"):
        print("  [FIX] A Riot key must start with 'RGAPI-'. Yours does not.")
        print("        You may have copied the wrong thing from the Riot site.")
        print("        Copy the 'DEVELOPMENT API KEY' box on developer.riotgames.com.")
        problems.append("key does not start with RGAPI-")
    elif len(key) != EXPECTED_LEN:
        print(f"  [FIX] A Riot key is normally {EXPECTED_LEN} characters. Yours is {len(key)}.")
        print("        It was probably cut off when copying. Copy it again, making sure")
        print("        you select the whole thing.")
        problems.append(f"key is {len(key)} characters, expected {EXPECTED_LEN}")
    else:
        print("  [OK]  Key format looks correct.")

    region = config.RIOT_REGION
    platform = config.RIOT_PLATFORM
    want = PLATFORM_FOR_REGION.get(region)
    print(f"  Region / platform    : {region} / {platform}")
    if want and want != platform:
        print(f"  [FIX] RIOT_PLATFORM should be '{want}' for region '{region}'.")
        problems.append("region and platform disagree")

    # ---- live test against Riot ------------------------------------------
    print()
    hr("Asking Riot whether it accepts the key")
    import aiohttp
    host = {"americas": "americas.api.riotgames.com", "europe": "europe.api.riotgames.com",
            "asia": "asia.api.riotgames.com", "sea": "sea.api.riotgames.com"}.get(platform, "americas.api.riotgames.com")
    url = f"https://{host}/riot/account/v1/accounts/by-riot-id/Faker/KR1"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers={"X-Riot-Token": key},
                             timeout=aiohttp.ClientTimeout(total=20)) as resp:
                status = resp.status
                body = (await resp.text())[:200]
    except Exception as e:
        from bot.services.riot_api import classify_connection_error
        kind = classify_connection_error(e)
        print(f"  [FIX] Could not reach Riot at all ({kind}). Your key was NOT tested.")
        print(f"        Detail: {e}")
        print()
        if kind == "dns":
            print("  This computer could not look up Riot's server address. Discord may still")
            print("  work, because each site is looked up separately. Try, in this order:")
            print()
            print("    1. Wait a minute and run this check again. It is often temporary.")
            print("    2. Turn off any VPN.")
            print("    3. Pause ad-blocking, parental-control or DNS-filtering software.")
            print("       Some block 'api' addresses.")
            print("    4. Reset Windows' address cache: press the Windows key, type cmd,")
            print("       open Command Prompt, type   ipconfig /flushdns   and press Enter.")
            print("    5. Still failing? Restart your router, or set your PC's DNS to")
            print("       1.1.1.1 in your network adapter settings.")
        elif kind == "timeout":
            print("  Riot did not answer in time. Check your connection and try again.")
        else:
            print("  Check your internet connection, and that a firewall or antivirus is")
            print("  not blocking Python.")
        return 1

    print(f"  Riot replied: HTTP {status}")
    print(f"  Body: {body}")
    print()

    if status in (200, 404):
        # 404 just means that test account name was not found; the KEY worked.
        print("  [OK]  YOUR KEY WORKS. Riot accepted it.")
        print()
        await check_tournament(config, host)
        print()
        if problems:
            print("  Other things worth tidying up:")
            for p in problems:
                print(f"    - {p}")
        else:
            print("  If /link still fails in Discord, you did not restart the bot after")
            print("  editing .env. Close the bot window and run START-BOT.bat again.")
        return 0

    if status in (401, 403):
        print("  [FIX] RIOT REJECTED THIS KEY.")
        print()
        if "unknown" in body.lower():
            print("  'Unknown apikey' means the key string does not exist in Riot's system.")
            print("  Almost always one of these:")
            print()
            print("    1. THE BOT WAS NOT RESTARTED after you pasted the new key.")
            print("       The key is read once when the bot starts. Close the black")
            print("       window completely and run START-BOT.bat again.")
            print()
            print("    2. The key was copied incompletely. It must be exactly 42")
            print("       characters: RGAPI- followed by 36 more.")
            print()
            print("    3. You copied from the wrong box on developer.riotgames.com.")
            print("       You want the big 'DEVELOPMENT API KEY' field.")
        else:
            print("  Your key has most likely EXPIRED. Development keys last 24 hours.")
            print()
            print("    1. Go to https://developer.riotgames.com and sign in")
            print("    2. Click REGENERATE API KEY, then copy the new one")
            print("    3. Paste it into .env after RIOT_API_KEY=")
            print("    4. Restart the bot with START-BOT.bat")
            print()
            print("  To stop this happening daily, click 'Register Product' on that")
            print("  site and apply for a Personal API Key, which does not expire.")
        return 1

    if status == 429:
        print("  [!]  Rate limited. The key is valid. Wait a minute and try again.")
        return 0

    print("  [FIX] Unexpected reply from Riot. The body above is the best clue.")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(1)
