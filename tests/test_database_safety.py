"""Linked accounts must survive updates: fixed database location and automatic backups."""
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from bot.config import anchor_sqlite_url
from bot.db.backup import backup_database, linked_player_count, sqlite_file


def test_relative_sqlite_path_is_pinned_to_the_bot_folder(tmp_path):
    root = tmp_path / "bot-folder"
    url = anchor_sqlite_url("sqlite+aiosqlite:///./ranked_bot.db", root=root)
    assert sqlite_file(url) == (root / "ranked_bot.db").resolve()


def test_other_database_urls_are_left_alone():
    for url in ("sqlite+aiosqlite:////abs/ranked_bot.db", "sqlite+aiosqlite:///:memory:",
                "postgresql+asyncpg://user:pw@host:5432/ranked_bot"):
        assert anchor_sqlite_url(url) == url
    assert sqlite_file("postgresql+asyncpg://user:pw@host/db") is None


def _make_db(path: Path, linked: int, unlinked: int = 0) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE players (id INTEGER PRIMARY KEY, riot_puuid TEXT)")
    con.executemany("INSERT INTO players (riot_puuid) VALUES (?)",
                    [(f"p{i}",) for i in range(linked)] + [(None,)] * unlinked)
    con.commit()
    con.close()


def test_linked_player_count(tmp_path):
    db = tmp_path / "ranked_bot.db"
    assert linked_player_count(db) is None                 # no database yet
    _make_db(db, linked=12, unlinked=3)
    assert linked_player_count(db) == 12                   # only players with a Riot account


def test_backups_are_complete_and_rotated(tmp_path):
    db, backups = tmp_path / "ranked_bot.db", tmp_path / "backups"
    _make_db(db, linked=7)
    start = datetime(2026, 9, 1)
    for day in range(20):
        backup_database(db, backups, keep=14, now=start + timedelta(days=day))
    copies = sorted(backups.glob("ranked_bot-*.db"))
    assert len(copies) == 14
    assert copies[0].name == "ranked_bot-20260907-000000.db"   # the six oldest were pruned
    assert all(linked_player_count(c) == 7 for c in copies)


def test_no_backup_of_a_missing_or_empty_database(tmp_path):
    assert backup_database(tmp_path / "nope.db", tmp_path / "b") is None
    (tmp_path / "empty.db").touch()
    assert backup_database(tmp_path / "empty.db", tmp_path / "b") is None
