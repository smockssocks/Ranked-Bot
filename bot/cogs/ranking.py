from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import desc, select

from bot import config
from bot.db.database import SessionLocal
from bot.models.game import Game, GameParticipant
from bot.models.player import Player
from bot.models.rating import PlayerRating
from bot.services.ranks import division_for_lp, tier_for_lp
from bot.services.rating_engine import ROLE_DISPLAY
from bot.ui import embeds


async def _player(session, member: discord.abc.User) -> Player | None:
    return await session.scalar(select(Player).where(Player.discord_id == str(member.id)))


class RankingCog(commands.Cog, name="Ranking"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="rank", description="Show a player's rank, LP and performance profile.")
    async def rank(self, inter: discord.Interaction, player: discord.Member | None = None):
        await inter.response.defer()
        target = player or inter.user
        async with SessionLocal() as session:
            p = await _player(session, target)
            if p is None:
                await inter.followup.send(f"{target.display_name} hasn't linked a Riot account yet (`/link`).")
                return
            rows = (await session.execute(select(PlayerRating).where(
                PlayerRating.player_id == p.id, PlayerRating.season == config.CURRENT_SEASON))).scalars().all()
            overall = next((r for r in rows if r.role == "OVERALL"), None)
            roles = [r for r in rows if r.role != "OVERALL"]
            recent = (await session.execute(
                select(GameParticipant).join(Game, Game.id == GameParticipant.game_id)
                .where(GameParticipant.player_id == p.id, Game.status == "processed")
                .order_by(desc(Game.played_at)).limit(10))).scalars().all()
        await inter.followup.send(embed=embeds.rank_embed(p.discord_username, overall, roles, p.summoner_name, recent))

    @app_commands.command(name="leaderboard", description="Top players this season.")
    @app_commands.describe(role="Show the ladder for one role")
    @app_commands.choices(role=[app_commands.Choice(name=v, value=k) for k, v in ROLE_DISPLAY.items()])
    async def leaderboard(self, inter: discord.Interaction, role: app_commands.Choice[str] | None = None):
        await inter.response.defer()
        key = role.value if role else "OVERALL"
        async with SessionLocal() as session:
            rows = (await session.execute(
                select(PlayerRating, Player).join(Player, Player.id == PlayerRating.player_id)
                .where(PlayerRating.role == key, PlayerRating.season == config.CURRENT_SEASON, PlayerRating.games_played > 0)
                .order_by(desc(PlayerRating.lp)).limit(15))).all()
        if not rows:
            await inter.followup.send("No ranked games yet this season.")
            return
        title = "Leaderboard" + (f" — {ROLE_DISPLAY[key]}" if key != "OVERALL" else "")
        e = discord.Embed(title=f"{title} (Season {config.CURRENT_SEASON})", color=discord.Color.gold())
        lines = []
        for i, (r, p) in enumerate(rows, 1):
            _, emoji = tier_for_lp(r.lp)
            wr = r.wins / r.games_played * 100
            plc = " ⏳" if r.games_played < config.PLACEMENT_GAMES else ""
            lines.append(f"`{i:>2}.` {emoji} **{p.discord_username}** — {r.lp} LP ({division_for_lp(r.lp)}) • {r.games_played}G {wr:.0f}% • PS {r.perf_mean:+.2f}{plc}")
        e.description = "\n".join(lines)
        e.set_footer(text="⏳ = still in placements")
        await inter.followup.send(embed=e)

    @app_commands.command(name="history", description="Recent games with LP changes.")
    async def history(self, inter: discord.Interaction, player: discord.Member | None = None):
        await inter.response.defer()
        target = player or inter.user
        async with SessionLocal() as session:
            p = await _player(session, target)
            if p is None:
                await inter.followup.send(f"{target.display_name} is not registered.")
                return
            rows = (await session.execute(
                select(GameParticipant, Game).join(Game, Game.id == GameParticipant.game_id)
                .where(GameParticipant.player_id == p.id, Game.status == "processed")
                .order_by(desc(Game.played_at)).limit(8))).all()
        if not rows:
            await inter.followup.send(f"{p.discord_username} has no games yet.")
            return
        e = discord.Embed(title=f"{p.discord_username} — recent games", color=discord.Color.blue())
        for part, game in rows:
            res = "🟩 Win" if part.win else "🟥 Loss"
            sign = "+" if part.lp_delta >= 0 else ""
            when = game.played_at.strftime("%b %d") if game.played_at else "?"
            e.add_field(name=f"{res} • {part.champion_name or '?'} {ROLE_DISPLAY.get(part.role, part.role)} • {when}",
                        value=f"{part.kills}/{part.deaths}/{part.assists} • PS {part.impact_score or 0:+.2f} • "
                              f"{part.lp_before} → {part.lp_after} (**{sign}{part.lp_delta}**)", inline=False)
        e.set_footer(text="/explain for the full breakdown of your last game")
        await inter.followup.send(embed=e)

    @app_commands.command(name="explain", description="Why did I gain/lose that LP? Breakdown of a recent game.")
    @app_commands.describe(games_ago="0 = last game, 1 = the one before, ...")
    async def explain(self, inter: discord.Interaction, player: discord.Member | None = None, games_ago: int = 0):
        await inter.response.defer()
        target = player or inter.user
        async with SessionLocal() as session:
            p = await _player(session, target)
            if p is None:
                await inter.followup.send("Not registered.")
                return
            row = (await session.execute(
                select(GameParticipant, Game).join(Game, Game.id == GameParticipant.game_id)
                .where(GameParticipant.player_id == p.id, Game.status == "processed")
                .order_by(desc(Game.played_at)).offset(max(0, games_ago)).limit(1))).first()
        if not row:
            await inter.followup.send("No such game.")
            return
        part, game = row
        m = part.metrics or {}
        contrib = m.get("_contrib", {})
        z = m.get("_z", {})
        e = discord.Embed(
            title=f"{'Win' if part.win else 'Loss'} on {part.champion_name} ({ROLE_DISPLAY.get(part.role, part.role)}) — {'+' if part.lp_delta >= 0 else ''}{part.lp_delta} LP",
            description=part.explanation or "", color=discord.Color.green() if part.lp_delta >= 0 else discord.Color.red())
        top = sorted(contrib.items(), key=lambda kv: -kv[1])[:5]
        bot_ = sorted(contrib.items(), key=lambda kv: kv[1])[:5]
        def fmt(items):
            return "\n".join(f"{embeds.label(k)}: z {z.get(k, 0):+.1f} → {v:+.2f}" for k, v in items if abs(v) > 0.02) or "—"
        e.add_field(name="Helped", value=fmt(top), inline=True)
        e.add_field(name="Hurt", value=fmt(bot_), inline=True)
        lane = f"@10: {m.get('gold_diff_10', 0):+.0f}g, {m.get('xp_diff_10', 0):+.0f}xp, {m.get('cs_diff_10', 0):+.0f}cs • @15: {m.get('gold_diff_15', 0):+.0f}g"
        e.add_field(name="Lane vs opponent", value=lane, inline=False)
        e.add_field(name="Game", value=f"KP {m.get('kill_participation', 0)*100:.0f}% • dmg share {m.get('damage_share', 0)*100:.0f}% • "
                                       f"objectives {m.get('objective_participation', 0)*100:.0f}% • vision {m.get('vision_per_min', 0):.2f}/min • "
                                       f"CS {m.get('cs_per_min', 0):.1f}/min", inline=False)
        e.set_footer(text=f"Match {game.riot_match_id} • z = standard deviations vs role average on this server")
        await inter.followup.send(embed=e)

    @app_commands.command(name="howranked", description="How the ranking works.")
    async def howranked(self, inter: discord.Interaction):
        text = (
            "**How LP works here**\n"
            f"• Everyone starts at {config.STARTING_LP} LP. First {config.PLACEMENT_GAMES} games are placements and move you about twice as fast.\n"
            "• Win/loss is the biggest factor (about ±20 LP for an even game, more if you upset, less if you were favoured).\n"
            "• Every game you also get a **performance score (PS)**: how you did versus your lane opponent and versus role averages "
            "on this server: gold/XP/CS leads at 10 and 15, kill participation, deaths, damage share, objectives, vision, healing and CC for supports. "
            "Champion choice is ignored; only what you did with it counts.\n"
            "• Short games weigh laning more; long games weigh teamfights and objectives more.\n"
            "• Carry a loss (PS around +2) and you lose nothing or gain a little. Get carried in a win and you still gain, just less.\n"
            "• Consistency and games in a role feed a per-role rating (`/leaderboard role`).\n"
            "• Teams are balanced on a hidden MMR that also learns from performance, so smurfs get placed fast.\n"
            "Use `/explain` after any game to see exactly what moved your LP."
        )
        await inter.response.send_message(text)


async def setup(bot: commands.Bot):
    await bot.add_cog(RankingCog(bot))
