#!/usr/bin/env bash
# Linux / macOS launcher. Mirrors START-BOT.bat.
set -u
cd "$(dirname "$0")"

PY=""
for c in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,11) else 1)' 2>/dev/null; then
        PY="$c"; break
    fi
done
if [ -z "$PY" ]; then
    echo "Python 3.11 or newer is required but was not found."
    echo "Install it, then run ./start.sh again."
    exit 1
fi
echo "Using $($PY --version)"

if [ ! -x ".venv/bin/python" ]; then
    echo "First run: creating the Python environment..."
    "$PY" -m venv .venv || { echo "Could not create the virtual environment."; exit 1; }
fi
VPY=".venv/bin/python"

if ! "$VPY" -c "import discord, sqlalchemy, aiohttp, aiosqlite, dotenv" >/dev/null 2>&1; then
    echo "Installing dependencies. This takes a minute..."
    "$VPY" -m pip install --upgrade pip --quiet --disable-pip-version-check
    "$VPY" -m pip install -r requirements.txt --disable-pip-version-check || { echo "Dependency install failed."; exit 1; }
fi

if [ ! -f ".env" ]; then
    cp .env.example .env
    echo
    echo "-------------------------------------------------------"
    echo " Created your settings file: .env"
    echo " Open it in a text editor and fill in:"
    echo "     DISCORD_TOKEN, DISCORD_GUILD_ID, RIOT_API_KEY"
    echo " Then run ./start.sh again."
    echo "-------------------------------------------------------"
    exit 1
fi

"$VPY" tools/preflight.py || exit 1

echo "Starting the bot. Press Ctrl+C to stop."
exec "$VPY" -m bot.main
