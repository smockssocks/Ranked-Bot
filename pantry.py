import sqlite3
from pathlib import Path
from typing import List, Tuple

DB_PATH = Path('pantry.db')


class PantryDB:
    def __init__(self):
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(DB_PATH)

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pantry (
                    id         INTEGER PRIMARY KEY,
                    user_id    TEXT NOT NULL,
                    ingredient TEXT NOT NULL COLLATE NOCASE,
                    added_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, ingredient)
                )
            """)

    def add_ingredients(self, user_id: str, ingredients: List[str]) -> Tuple[List[str], List[str]]:
        added, skipped = [], []
        with self._conn() as conn:
            for ing in ingredients:
                try:
                    conn.execute(
                        'INSERT INTO pantry (user_id, ingredient) VALUES (?, ?)',
                        (user_id, ing)
                    )
                    added.append(ing)
                except sqlite3.IntegrityError:
                    skipped.append(ing)
        return added, skipped

    def remove_ingredient(self, user_id: str, ingredient: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                'DELETE FROM pantry WHERE user_id = ? AND LOWER(ingredient) = LOWER(?)',
                (user_id, ingredient)
            )
            return cur.rowcount > 0

    def get_ingredients(self, user_id: str) -> List[str]:
        with self._conn() as conn:
            cur = conn.execute(
                'SELECT ingredient FROM pantry WHERE user_id = ? ORDER BY ingredient COLLATE NOCASE',
                (user_id,)
            )
            return [row[0] for row in cur.fetchall()]

    def clear(self, user_id: str) -> int:
        with self._conn() as conn:
            cur = conn.execute('DELETE FROM pantry WHERE user_id = ?', (user_id,))
            return cur.rowcount
