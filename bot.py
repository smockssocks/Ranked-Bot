import discord
from discord import app_commands
from discord.ext import commands
import os
from dotenv import load_dotenv
from pantry import PantryDB
from recipes import RecipeDB

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)
pantry_db = PantryDB()
recipe_db = RecipeDB()


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f'Logged in as {bot.user} - slash commands synced')
    count = recipe_db.count()
    if count == 0:
        print('WARNING: Recipe database is empty. Run: python setup.py')
    else:
        print(f'Recipe database loaded: {count} recipes')


@bot.tree.command(name='add', description='Add ingredients to your pantry')
@app_commands.describe(ingredients='Comma-separated ingredients, e.g.: eggs, butter, garlic')
async def add_cmd(interaction: discord.Interaction, ingredients: str):
    user_id = str(interaction.user.id)
    items = [i.strip().lower() for i in ingredients.split(',') if i.strip()]
    if not items:
        await interaction.response.send_message('Please provide at least one ingredient.', ephemeral=True)
        return
    added, skipped = pantry_db.add_ingredients(user_id, items)
    parts = []
    if added:
        parts.append(f'Added: **{", ".join(added)}**')
    if skipped:
        parts.append(f'Already had: {", ".join(skipped)}')
    await interaction.response.send_message('\n'.join(parts))


@bot.tree.command(name='remove', description='Remove an ingredient from your pantry')
@app_commands.describe(ingredient='Ingredient to remove')
async def remove_cmd(interaction: discord.Interaction, ingredient: str):
    user_id = str(interaction.user.id)
    removed = pantry_db.remove_ingredient(user_id, ingredient.strip().lower())
    if removed:
        await interaction.response.send_message(f'Removed **{ingredient}** from your pantry.')
    else:
        await interaction.response.send_message(f'**{ingredient}** was not in your pantry.', ephemeral=True)


@bot.tree.command(name='pantry', description='Show everything in your pantry')
async def pantry_cmd(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    ingredients = pantry_db.get_ingredients(user_id)
    if not ingredients:
        await interaction.response.send_message(
            'Your pantry is empty. Use `/add` to add ingredients!', ephemeral=True
        )
        return
    lines = '\n'.join(f'- {i}' for i in ingredients)
    embed = discord.Embed(title='Your Pantry', description=lines, color=0x00b894)
    embed.set_footer(text=f'{len(ingredients)} ingredient(s)')
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name='ideas', description='Get recipe ideas from your current pantry')
async def ideas_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    user_id = str(interaction.user.id)
    pantry = pantry_db.get_ingredients(user_id)
    if not pantry:
        await interaction.followup.send('Your pantry is empty. Use `/add` to get started!')
        return
    matches = recipe_db.find_recipes(pantry, limit=8)
    if not matches:
        await interaction.followup.send('No matching recipes found. Try adding more ingredients!')
        return
    embed = _build_results_embed('Recipe Ideas from Your Pantry', matches, pantry)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name='use', description='Find recipes that use a specific ingredient')
@app_commands.describe(ingredient='Ingredient you want to use up, e.g.: dark brown sugar')
async def use_cmd(interaction: discord.Interaction, ingredient: str):
    await interaction.response.defer()
    user_id = str(interaction.user.id)
    pantry = pantry_db.get_ingredients(user_id)
    ing = ingredient.strip().lower()
    matches = recipe_db.find_recipes(pantry, priority=ing, limit=8)
    if not matches:
        await interaction.followup.send(f'No recipes found that use **{ingredient}**.')
        return
    embed = _build_results_embed(f'Recipes Using "{ingredient.title()}"', matches, pantry)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name='clear', description='Clear all ingredients from your pantry')
async def clear_cmd(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    count = pantry_db.clear(user_id)
    await interaction.response.send_message(f'Cleared {count} ingredient(s) from your pantry.')


def _build_results_embed(title: str, matches: list, pantry: list) -> discord.Embed:
    embed = discord.Embed(title=title, color=0xfdcb6e)
    for r in matches[:5]:
        pct = r['match_pct']
        bar = _pct_bar(pct)
        if r['missing']:
            missing_preview = ', '.join(r['missing'][:4])
            if len(r['missing']) > 4:
                missing_preview += f" +{len(r['missing']) - 4} more"
            detail = f"{bar} {pct}%\nNeeds: {missing_preview}"
        else:
            detail = f"{bar} {pct}%\n[+] You have everything!"
        label = r['name']
        meta = ' | '.join(filter(None, [r['category'], r['area']]))
        if meta:
            label += f' ({meta})'
        embed.add_field(name=label, value=detail, inline=False)
    embed.set_footer(text=f'Pantry: {len(pantry)} item(s) | Top {min(5, len(matches))} of {len(matches)} matches')
    return embed


def _pct_bar(pct: int) -> str:
    filled = round(pct / 10)
    return '#' * filled + '-' * (10 - filled)


if __name__ == '__main__':
    if not TOKEN:
        raise ValueError('DISCORD_TOKEN not set in .env')
    bot.run(TOKEN)
