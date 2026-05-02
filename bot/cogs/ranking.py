from __future__ import annotations
import discord
from discord.ext import commands
from discord import app_commands
from sqlalchemy import select, desc

from bot.db.database import SessionLocal
from bot.models.player import Player
from bot.models.rating import PlayerRating
from bot.models.game import GameParticipant, Game


class RankingCog(commands.Cog, name="Ranking"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="rank", description="Show your LP and stats (or another player's).")
    @app_commands.describe(player="The player to look up (leave blank for yourself).")
    async def rank(self, interaction: discord.Interaction, player: discord.Member | None = None):
        await interaction.response.defer()
        target = player or interaction.user
        async with SessionLocal() as session:
            db_player = await session.scalar(
                select(Player).where(Player.discord_id == str(target.id))
            )
            if db_player is None:
                await interaction.followup.send(f"{target.display_name} is not registered.")
                return

            ratings = {r.role: r for r in db_player.ratings}
            overall = ratings.get("OVERALL")

            embed = discord.Embed(
                title=f"{db_player.discord_username}'s Rank",
                color=discord.Color.gold(),
            )
            if overall:
                win_rate = (overall.wins / overall.games_played * 100) if overall.games_played else 0
                embed.add_field(name="LP", value=str(overall.lp), inline=True)
                embed.add_field(name="Games", value=str(overall.games_played), inline=True)
                embed.add_field(name="Win Rate", value=f"{win_rate:.1f}%", inline=True)
            else:
                embed.description = "No games played yet."

            # Per-role breakdown
            role_lines = []
            for role in ["TOP", "JUNGLE", "MID", "BOTTOM", "SUPPORT"]:
                r = ratings.get(role)
                if r and r.games_played > 0:
                    role_lines.append(f"**{role}**: {r.lp} LP ({r.games_played}G)")
            if role_lines:
                embed.add_field(name="By Role", value="\n".join(role_lines), inline=False)

            if db_player.summoner_name:
                embed.set_footer(text=f"Riot: {db_player.summoner_name}")

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="leaderboard", description="Top 10 players by LP.")
    async def leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        async with SessionLocal() as session:
            top_ratings = (await session.execute(
                select(PlayerRating, Player)
                .join(Player, Player.id == PlayerRating.player_id)
                .where(PlayerRating.role == "OVERALL", PlayerRating.games_played > 0)
                .order_by(desc(PlayerRating.lp))
                .limit(10)
            )).all()

            if not top_ratings:
                await interaction.followup.send("No ranked players yet.")
                return

            embed = discord.Embed(title="Leaderboard — Top 10", color=discord.Color.gold())
            lines = []
            medals = ["🥇", "🥈", "🥉"]
            for i, (rating, db_player) in enumerate(top_ratings):
                prefix = medals[i] if i < 3 else f"{i + 1}."
                win_rate = (rating.wins / rating.games_played * 100) if rating.games_played else 0
                lines.append(
                    f"{prefix} **{db_player.discord_username}** — "
                    f"{rating.lp} LP | {rating.games_played}G | {win_rate:.0f}% WR"
                )
            embed.description = "\n".join(lines)

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="history", description="Show recent game history.")
    @app_commands.describe(player="The player to look up (leave blank for yourself).")
    async def history(self, interaction: discord.Interaction, player: discord.Member | None = None):
        await interaction.response.defer()
        target = player or interaction.user
        async with SessionLocal() as session:
            db_player = await session.scalar(
                select(Player).where(Player.discord_id == str(target.id))
            )
            if db_player is None:
                await interaction.followup.send(f"{target.display_name} is not registered.")
                return

            recent = (await session.execute(
                select(GameParticipant, Game)
                .join(Game, Game.id == GameParticipant.game_id)
                .where(GameParticipant.player_id == db_player.id)
                .order_by(desc(Game.played_at))
                .limit(5)
            )).all()

            if not recent:
                await interaction.followup.send(f"{db_player.discord_username} has no game history.")
                return

            embed = discord.Embed(
                title=f"{db_player.discord_username}'s Recent Games",
                color=discord.Color.blue(),
            )
            for part, game in recent:
                result = "Win" if part.win else "Loss"
                lp_str = f"+{part.lp_delta}" if part.lp_delta >= 0 else str(part.lp_delta)
                played = game.played_at.strftime("%b %d") if game.played_at else "?"
                embed.add_field(
                    name=f"{result} — {part.champion_name or '?'} ({part.role}) — {played}",
                    value=(
                        f"LP: {part.lp_before} → {part.lp_after} ({lp_str})\n"
                        f"Impact: {part.impact_score:.2f} | "
                        f"K/D/A: {part.kills}/{part.deaths}/{part.assists}"
                    ),
                    inline=False,
                )

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(RankingCog(bot))
