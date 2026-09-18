from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from bot.config import DATABASE_URL


class Base(DeclarativeBase):
    pass


_engine_kwargs = {"echo": False}
if not DATABASE_URL.startswith("sqlite"):
    _engine_kwargs["pool_pre_ping"] = True

engine = create_async_engine(DATABASE_URL, **_engine_kwargs)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    """Create any missing tables (dev / sqlite). Production uses alembic migrations."""
    import bot.models  # noqa: F401  ensure all models are registered
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
