"""Run once to populate the recipe database from TheMealDB (free, no API key).

Usage:
    python setup.py

Downloads ~300 diverse real recipes and stores them in pantry.db.
After this, the bot runs entirely offline with no ongoing API costs.
"""
import requests
import sqlite3
import json
import time
from pathlib import Path

DB_PATH = Path('pantry.db')
BASE_URL = 'https://www.themealdb.com/api/json/v1/1'


def fetch_all_meals() -> list:
    meals = []
    print('Fetching recipes from TheMealDB (free, one-time)...')
    for letter in 'abcdefghijklmnopqrstuvwxyz':
        try:
            resp = requests.get(f'{BASE_URL}/search.php?f={letter}', timeout=15)
            resp.raise_for_status()
            data = resp.json()
            batch = data.get('meals') or []
            meals.extend(batch)
            print(f'  {letter.upper()}: {len(batch)} recipe(s)')
        except Exception as e:
            print(f'  {letter.upper()}: failed - {e}')
        time.sleep(0.25)
    return meals


def extract_ingredients(meal: dict):
    ingredients, measures = [], []
    for i in range(1, 21):
        ing = (meal.get(f'strIngredient{i}') or '').strip()
        msr = (meal.get(f'strMeasure{i}') or '').strip()
        if ing:
            ingredients.append(ing)
            measures.append(msr)
    return ingredients, measures


def populate_db(meals: list):
    with sqlite3.connect(DB_PATH) as conn:
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
        inserted = 0
        for meal in meals:
            ingredients, measures = extract_ingredients(meal)
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO recipes
                        (meal_id, name, category, area, instructions, thumbnail, ingredients, measures)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    meal['idMeal'],
                    meal['strMeal'],
                    meal.get('strCategory'),
                    meal.get('strArea'),
                    meal.get('strInstructions'),
                    meal.get('strMealThumb'),
                    json.dumps(ingredients),
                    json.dumps(measures),
                ))
                inserted += 1
            except Exception as e:
                print(f'  Error saving "{meal.get("strMeal")}": {e}')

    print(f'\nDone - {inserted} recipes saved to pantry.db')
    print('Now run: python bot.py')


if __name__ == '__main__':
    meals = fetch_all_meals()
    print(f'\nTotal fetched: {len(meals)} recipes')
    populate_db(meals)
