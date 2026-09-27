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


def add_missing_columns(sync_conn) -> list[str]:
    """
    create_all() makes missing TABLES but never adds new COLUMNS to tables that
    already exist. Anyone who ran an earlier version on SQLite would then crash
    the moment a new column is queried. This adds any column the models define
    that the database lacks, using the same column DDL create_all would emit.

    Only additive changes are handled. A column that is NOT NULL with no server
    default is added as nullable, since existing rows would have no value for it.
    Returns the "table.column" names it added.
    """
    from sqlalchemy import inspect
    from sqlalchemy.schema import CreateColumn

    insp = inspect(sync_conn)
    existing = set(insp.get_table_names())
    added: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            spec = str(CreateColumn(col).compile(dialect=sync_conn.dialect))
            if col.server_default is None:
                spec = spec.replace(" NOT NULL", "")
            sync_conn.exec_driver_sql(f"ALTER TABLE {table.name} ADD COLUMN {spec}")
            added.append(f"{table.name}.{col.name}")
    return added


async def init_db() -> list[str]:
    """Create missing tables, then add any missing columns. Safe to run on every start."""
    import logging
    import bot.models  # noqa: F401  ensure all models are registered
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        added = await conn.run_sync(add_missing_columns)
    if added:
        logging.getLogger("ranked-bot.db").info("Upgraded database, added columns: %s", ", ".join(added))
    return added
