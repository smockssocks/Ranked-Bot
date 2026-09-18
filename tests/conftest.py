import os
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

# Point the bot at a throwaway sqlite DB before any bot module is imported.
_tmp = tempfile.mkdtemp(prefix="ranked-bot-test-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{os.path.join(_tmp, 'test.db')}"
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("RIOT_API_KEY", "")
os.environ.setdefault("OPENROUTER_API_KEY", "")
os.environ.setdefault("CHAT_CHANNEL_IDS", "555")

import pytest  # noqa: E402

from bot.db.database import Base, engine  # noqa: E402
import bot.models  # noqa: E402,F401


@pytest.fixture
async def db():
    """Fresh schema for every test that asks for it."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    from bot.db.database import SessionLocal
    yield SessionLocal
