"""
Automatic copies of the SQLite database, taken every time the bot starts.

The database (ranked_bot.db) holds everything: linked Riot accounts, ratings, game
history, lobbies and server settings. A copy goes into the backups folder next to
it on each start, and only the newest BACKUP_KEEP copies are kept.

Uses SQLite's own backup API rather than a file copy, so the copy is consistent
even if something else has the database open.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

log = logging.getLogger("ranked-bot.backup")


def sqlite_file(url: str) -> Path | None:
    """The database file for a sqlite URL, or None for other databases."""
    if not url.startswith("sqlite") or ":///" not in url:
        return None
    path = url.partition(":///")[2]
    if not path or path.startswith(":memory:"):
        return None
    return Path(path)


def linked_player_count(db_file: Path) -> int | None:
    """How many players have a linked Riot account. None if the file or table is missing."""
    if not db_file.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db_file.as_posix()}?mode=ro", uri=True)
        try:
            return int(con.execute("SELECT COUNT(*) FROM players WHERE riot_puuid IS NOT NULL").fetchone()[0])
        finally:
            con.close()
    except sqlite3.Error:
        return None


def backup_database(db_file: Path, backup_dir: Path, keep: int = 14, now: datetime | None = None) -> Path | None:
    """Copy the database into backup_dir and prune old copies. Returns the new copy's path."""
    if not db_file.exists() or db_file.stat().st_size == 0:
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"{db_file.stem}-{stamp}.db"
    src = sqlite3.connect(db_file.as_posix())
    dst = sqlite3.connect(dest.as_posix())
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    copies = sorted(backup_dir.glob(f"{db_file.stem}-*.db"))
    for old in copies[:-keep] if keep > 0 else []:
        try:
            old.unlink()
        except OSError:
            log.warning("could not delete old backup %s", old)
    return dest
