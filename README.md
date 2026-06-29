# Pantry Chef

A Discord bot that tracks your pantry ingredients and suggests recipes you can make right now - with zero ongoing API costs.

## How it works

- Your ingredients live in a local SQLite database (`pantry.db`)
- ~300 real recipes are downloaded once from [TheMealDB](https://www.themealdb.com/) (free, no key needed) and stored locally
- The bot matches your pantry against every recipe and ranks them by how many ingredients you already have

## Setup

### 1. Create a Discord bot
1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. New Application > Bot > copy the token
3. Invite the bot to your server with the `applications.commands` scope

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure environment
```bash
cp .env.example .env
# Edit .env and paste your Discord bot token
```

### 4. Populate the recipe database (one-time)
```bash
python setup.py
```
This hits TheMealDB once to download ~300 diverse recipes into `pantry.db`. After this, no internet needed.

### 5. Run the bot
```bash
python bot.py
```

## Commands

| Command | Description | Example |
|---|---|---|
| `/add` | Add ingredients to your pantry | `/add eggs, butter, garlic, pasta` |
| `/remove` | Remove an ingredient | `/remove pasta` |
| `/pantry` | Show your current pantry | `/pantry` |
| `/ideas` | Get recipe ideas from your pantry | `/ideas` |
| `/use` | Find recipes using a specific ingredient | `/use dark brown sugar` |
| `/clear` | Clear your entire pantry | `/clear` |

## Example

```
/add dark brown sugar, butter, eggs, flour, vanilla extract, baking soda
/ideas
  Brownies (90%) - You have everything!
  Chocolate Chip Cookies (85%) - Needs: chocolate chips
  Banana Bread (72%) - Needs: bananas, walnuts

/use dark brown sugar
  -> All recipes using dark brown sugar, ranked by pantry match
```

## Notes
- Each Discord user has their own separate pantry
- Ingredient matching is fuzzy: "butter" matches "unsalted butter" in recipes
- Re-run `python setup.py` anytime to refresh the recipe database
