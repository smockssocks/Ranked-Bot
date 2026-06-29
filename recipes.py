import sqlite3
import json
from pathlib import Path
from typing import List, Optional, Dict

DB_PATH = Path('pantry.db')


class RecipeDB:
    def __init__(self):
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(DB_PATH)

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS recipes (
                    id           INTEGER PRIMARY KEY,
                    meal_id      TEXT UNIQUE,
                    name         TEXT NOT NULL,
                    category     TEXT,
                    area         TEXT,
                    instructions TEXT,
                    thumbnail    TEXT,
                    ingredients  TEXT,
                    measures     TEXT
                )
            """)

    def count(self) -> int:
        with self._conn() as conn:
            return conn.execute('SELECT COUNT(*) FROM recipes').fetchone()[0]

    def find_recipes(
        self,
        pantry: List[str],
        priority: Optional[str] = None,
        limit: int = 10
    ) -> List[Dict]:
        pantry_set = {i.lower().strip() for i in pantry}

        with self._conn() as conn:
            if priority:
                cur = conn.execute(
                    'SELECT name, category, area, ingredients FROM recipes '
                    'WHERE LOWER(ingredients) LIKE ?',
                    (f'%{priority.lower()}%',)
                )
            else:
                cur = conn.execute(
                    'SELECT name, category, area, ingredients FROM recipes'
                )
            rows = cur.fetchall()

        results = []
        for name, category, area, ingredients_json in rows:
            try:
                recipe_ings = [i.lower().strip() for i in json.loads(ingredients_json) if i.strip()]
            except (json.JSONDecodeError, TypeError):
                continue
            if not recipe_ings:
                continue

            matched = [ri for ri in recipe_ings if _matches_any(ri, pantry_set)]
            missing = [ri for ri in recipe_ings if not _matches_any(ri, pantry_set)]
            match_pct = int(len(matched) / len(recipe_ings) * 100)

            results.append({
                'name': name,
                'category': category or '',
                'area': area or '',
                'match_pct': match_pct,
                'matched': matched,
                'missing': missing,
            })

        results.sort(key=lambda x: x['match_pct'], reverse=True)
        return results[:limit]


def _matches_any(recipe_ing: str, pantry_set: set) -> bool:
    for pantry_ing in pantry_set:
        if pantry_ing in recipe_ing or recipe_ing in pantry_ing:
            return True
    return False
