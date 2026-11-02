from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import desc, select

from bot import config
from bot.db.database import SessionLocal
from bot.models.game import Game, GameParticipant
from bot.models.player import Player
from bot.models.rating import PlayerRating
import logging

from bot.services import bans, ladder, settings
from bot.services.ranks import division_for_lp, tier_for_lp
from bot.services.rating_engine import ROLE_DISPLAY
from bot.ui import embeds


async def _player(session, member: discord.abc.User) -> Player | None:
    return await session.scalar(select(Player).where(Player.discord_id == str(member.id)))


log = logging.getLogger("ranked-bot.cogs.ranking")


class BoardView(discord.ui.View):
    """Buttons under the #leaderboard post and the how-to-play guide. Persistent."""

    def __init__(self, cog: "RankingCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="My rank", emoji="📊", style=discord.ButtonStyle.primary, custom_id="board:me")
    async def me(self, inter: discord.Interaction, _b: discord.ui.Button):
        await inter.response.defer(ephemeral=True)
        embed, msg = await self.cog.rank_for(inter.user)
        await inter.followup.send(msg, embed=embed, ephemeral=True) if embed else \
            await inter.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="My last game", emoji="🔎", style=discord.ButtonStyle.secondary, custom_id="board:last")
    async def last(self, inter: discord.Interaction, _b: discord.ui.Button):
        await inter.response.defer(ephemeral=True)
        embed, msg = await self.cog.explain_for(inter.user, 0)
        await inter.followup.send(msg, embed=embed, ephemeral=True) if embed else \
            await inter.followup.send(msg, ephemeral=True)


class RankingCog(commands.Cog, name="Ranking"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._board_cache: dict[str, str] = {}

    async def cog_load(self):
        self.bot.add_view(BoardView(self))
        self.board_loop.start()

    async def cog_unload(self):
        self.board_loop.cancel()

    # ------------------------------------------------------------------ #
    # The #leaderboard channel                                             #
    # ------------------------------------------------------------------ #

    async def _board_channel(self, guild_id: str):
        async with SessionLocal() as session:
            cid = await settings.get_setting(session, guild_id, settings.KEY_LEADERBOARD_CHANNEL)
        if not cid or not cid.isdigit():
            return None
        ch = self.bot.get_channel(int(cid))
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(int(cid))
            except discord.HTTPException:
                return None
        return ch

    async def refresh_board(self, guild_id: str, force: bool = False) -> str:
        """Bring the live top-N post up to date (or post it). Returns what happened."""
        ch = await self._board_channel(guild_id)
        if ch is None:
            return "No leaderboard channel is set. Run `/admin setup`, or `/admin leaderboardchannel`."
        async with SessionLocal() as session:
            rows = await ladder.standings(session, limit=config.LEADERBOARD_SIZE)
            embed = embeds.leaderboard_embed(rows, config.CURRENT_SEASON)
            mid = await settings.get_setting(session, guild_id, settings.KEY_LEADERBOARD_MESSAGE)
            key = f"{ch.id}:{mid}:{embed.title}:{embed.description}"
            if not force and self._board_cache.get(guild_id) == key:
                return "Already up to date."
            try:
                if mid and mid.isdigit():
                    try:
                        await ch.get_partial_message(int(mid)).edit(embed=embed, view=BoardView(self))
                        self._board_cache[guild_id] = key
                        return f"Updated the leaderboard in {ch.mention}."
                    except discord.NotFound:
                        pass                                   # deleted: post a new one
                msg = await ch.send(embed=embed, view=BoardView(self))
                await settings.set_setting(session, guild_id, settings.KEY_LEADERBOARD_MESSAGE, str(msg.id))
                self._board_cache[guild_id] = f"{ch.id}:{msg.id}:{embed.title}:{embed.description}"
                return f"Posted the leaderboard in {ch.mention}."
            except discord.HTTPException as e:
                log.warning("could not update leaderboard in guild %s: %s", guild_id, e)
                return f"Couldn't post in {ch.mention}: the bot needs Send Messages and Embed Links there."

    async def close_season_board(self, guild_id: str, old_season: int) -> None:
        """Freeze the live post as last season's final standings; a fresh post follows."""
        ch = await self._board_channel(guild_id)
        if ch is None:
            return
        async with SessionLocal() as session:
            rows = await ladder.standings(session, season=old_season, limit=config.LEADERBOARD_SIZE)
            mid = await settings.get_setting(session, guild_id, settings.KEY_LEADERBOARD_MESSAGE)
            final = embeds.leaderboard_embed(rows, old_season, final=True)
            try:
                if mid and mid.isdigit():
                    try:
                        await ch.get_partial_message(int(mid)).edit(embed=final, view=None)
                    except discord.NotFound:
                        await ch.send(embed=final)
                else:
                    await ch.send(embed=final)
            except discord.HTTPException as e:
                log.warning("could not post final standings: %s", e)
            await settings.set_setting(session, guild_id, settings.KEY_LEADERBOARD_MESSAGE, "")
        self._board_cache.pop(guild_id, None)
        await self.refresh_board(guild_id, force=True)

    @tasks.loop(minutes=10)
    async def board_loop(self):
        """Safety net: the post is refreshed after every game; this catches anything else."""
        for guild in self.bot.guilds:
            try:
                await self.refresh_board(str(guild.id))
            except Exception:
                log.exception("leaderboard refresh failed")

    @board_loop.before_loop
    async def _before_board(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    # Rank and last game, shared by the commands and the buttons           #
    # ------------------------------------------------------------------ #

    async def rank_for(self, member: discord.abc.User):
        """(embed, None) or (None, message)."""
        async with SessionLocal() as session:
            p = await _player(session, member)
            if p is None or p.riot_puuid is None:
                return None, (f"{member.display_name} hasn't linked a Riot account yet. "
                              f"Press **Link my account** in #how-to-play, or run `/link`.")
            rows = (await session.execute(select(PlayerRating).where(
                PlayerRating.player_id == p.id, PlayerRating.season == config.CURRENT_SEASON))).scalars().all()
            overall = next((r for r in rows if r.role == "OVERALL"), None)
            roles = [r for r in rows if r.role != "OVERALL"]
            recent = (await session.execute(
                select(GameParticipant).join(Game, Game.id == GameParticipant.game_id)
                .where(GameParticipant.player_id == p.id, Game.status == "processed")
                .order_by(desc(Game.played_at)).limit(10))).scalars().all()
            place = (await ladder.positions(session)).get(p.id)
        e = embeds.rank_embed(p.discord_username, overall, roles, p.summoner_name, recent,
                              verified=bool(p.link_verified))
        if place:
            e.description = f"**#{place} on the Season {config.CURRENT_SEASON} leaderboard**\n" + (e.description or "")
        return e, None

    @app_commands.command(name="rank", description="Show a player's rank, LP and performance profile.")
    async def rank(self, inter: discord.Interaction, player: discord.Member | None = None):
        await inter.response.defer()
        embed, msg = await self.rank_for(player or inter.user)
        if embed is None:
            await inter.followup.send(msg)
        else:
            await inter.followup.send(embed=embed)

    @app_commands.command(name="leaderboard", description="Top players this season, by LP or by versatility.")
    @app_commands.describe(role="Show the ladder for one role",
                           sort="Rank by LP (default) or by how good players are across every role")
    @app_commands.choices(role=[app_commands.Choice(name=v, value=k) for k, v in ROLE_DISPLAY.items()],
                          sort=[app_commands.Choice(name="LP", value="lp"),
                                app_commands.Choice(name="Versatility (all-round skill)", value="versatility")])
    async def leaderboard(self, inter: discord.Interaction, role: app_commands.Choice[str] | None = None,
                          sort: app_commands.Choice[str] | None = None):
        await inter.response.defer()
        if sort and sort.value == "versatility":
            await self._versatility_board(inter)
            return
        key = role.value if role else "OVERALL"
        async with SessionLocal() as session:
            if key == "OVERALL":
                rows = await ladder.standings(session, limit=15)
            else:
                rows = (await session.execute(
                    select(PlayerRating, Player).join(Player, Player.id == PlayerRating.player_id)
                    .where(PlayerRating.role == key, PlayerRating.season == config.CURRENT_SEASON,
                           PlayerRating.games_played > 0, Player.is_test.is_(False))
                    .order_by(desc(PlayerRating.lp), desc(PlayerRating.mmr))
                    .limit(15))).all()
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
        e.set_footer(text="⏳ = still in placements • /leaderboard sort:Versatility for all-round skill")
        await inter.followup.send(embed=e)

    async def _versatility_board(self, inter: discord.Interaction):
        async with SessionLocal() as session:
            rows = (await session.execute(
                select(PlayerRating, Player).join(Player, Player.id == PlayerRating.player_id)
                .where(PlayerRating.season == config.CURRENT_SEASON))).all()
        by_player: dict[int, tuple[Player, list]] = {}
        for r, p in rows:
            by_player.setdefault(p.id, (p, []))[1].append(r)
        scored = []
        for p, rrows in by_player.values():
            overall = next((r for r in rrows if r.role == "OVERALL"), None)
            if not overall or overall.games_played == 0:
                continue
            scored.append((embeds.versatility_of(rrows), p, overall))
        if not scored:
            await inter.followup.send("No ranked games yet this season.")
            return
        scored.sort(key=lambda t: t[0].score, reverse=True)
        e = discord.Embed(title=f"Versatility (Season {config.CURRENT_SEASON})", color=discord.Color.purple())
        lines = [f"`{i:>2}.` **{p.discord_username}** — **{v.score:+.2f}** • {v.proven}/5 roles proven • {o.lp} LP"
                 for i, (v, p, o) in enumerate(scored[:15], 1)]
        e.description = "\n".join(lines)
        e.set_footer(text="How good you are across every role. Weak or untested roles pull the score down.")
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
        embed, msg = await self.explain_for(player or inter.user, games_ago)
        if embed is None:
            await inter.followup.send(msg)
        else:
            await inter.followup.send(embed=embed)

    async def explain_for(self, target: discord.abc.User, games_ago: int = 0):
        """(embed, None) or (None, message): why a recent game gained or lost LP."""
        async with SessionLocal() as session:
            p = await _player(session, target)
            if p is None:
                return None, f"{target.display_name} hasn't linked a Riot account yet."
            row = (await session.execute(
                select(GameParticipant, Game).join(Game, Game.id == GameParticipant.game_id)
                .where(GameParticipant.player_id == p.id, Game.status == "processed")
                .order_by(desc(Game.played_at)).offset(max(0, games_ago)).limit(1))).first()
        if not row:
            return None, "No recorded games yet." if games_ago == 0 else "No such game."
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
        return e, None

    @app_commands.command(name="help", description="How to play inhouses here: setup, joining a queue, and every command.")
    async def help_cmd(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)
        where = "this server"
        async with SessionLocal() as session:
            p = await _player(session, inter.user)
            if inter.guild_id:
                qc = await settings.queue_channel_id(session, str(inter.guild_id))
                if qc:
                    where = f"<#{qc}>"

        banned = None
        if inter.guild_id:
            async with SessionLocal() as session:
                banned = await bans.active_ban(session, str(inter.guild_id), str(inter.user.id))
        linked = bool(p and p.riot_puuid)
        step1 = ("~~**1. Link your Riot account**~~ ✅ done" if linked else
                 "**1. Link your Riot account**\n"
                 "Press **Link my account** in the how-to-play channel, or run `/link` with your full Riot ID, "
                 "for example `/link Danman#NA1`.\n"
                 "The bot will ask you to switch your League profile icon once, to prove the account is "
                 "yours. You only do this once, and you cannot join a queue until you have.")

        e = discord.Embed(
            title="How to play inhouses",
            description=f"Queues happen in {where}.",
            color=discord.Color.blurple(),
        )
        if banned is not None:
            e.add_field(name="You're currently banned", value=bans.player_message(banned), inline=False)
        e.add_field(name="Getting started", value=step1, inline=False)
        e.add_field(
            name="2. Join the queue",
            value=(f"When someone opens a lobby in {where}, press the green **Join** button and pick your main "
                   "and second role (remembered for next time).\n"
                   "Changed your mind? Press **Leave** or run `/dequeue`."),
            inline=False)
        e.add_field(
            name="3. Accept, then get drafted",
            value=("At 10 players everyone is pinged to press **Accept**. Decline or miss it and you're "
                   "out, with a short queue timeout. Once all 10 accept, the two players highest on the "
                   "leaderboard become captains and pick teams from a dropdown, snake style "
                   "(the lower-ranked captain picks first). You get a **private thread for your game** with "
                   "how to get in, and your team gets its own voice channel."),
            inline=False)
        e.add_field(
            name="4. Play",
            value=("Join the custom game and play it out. You do not report anything: the bot finds "
                   "the game and posts everyone's LP within a couple of minutes."),
            inline=False)
        e.add_field(
            name="Your rank",
            value=("`/rank` your tier, LP, record and per-role stats\n"
                   "`/history` your recent games\n"
                   "`/explain` exactly why your last game gained or lost LP\n"
                   "`/leaderboard` the ladder, or `/leaderboard role:Jungle`\n"
                   "`/howranked` how the whole system works"),
            inline=False)
        e.add_field(
            name="Fair to every role",
            value=("You're always judged against the average for the role you actually played, so a good "
                   "support game counts just as much as a good mid game. `/rank` shows your **versatility**: "
                   "how good you are across every role."),
            inline=False)
        e.set_footer(text="Win or lose matters most, but playing well still counts. Carry a loss and you barely drop.")
        await inter.followup.send(embed=e, ephemeral=True)

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
            "• **Captain draft.** The two players highest on the leaderboard captain and pick teams; the "
            "lower-ranked captain picks first. Captains see everyone's rank and preferred roles.\n"
            "• You're always scored against the role you actually played, so a player who is good at every "
            "role climbs higher than one who is only good at one. `/leaderboard sort:Versatility` ranks exactly that.\n"
            "• **Seasons** reset LP and placements, but part of your hidden rating carries over.\n"
            "• Consistency and games in a role feed a per-role rating (`/leaderboard role`).\n"
            "• Teams are balanced on a hidden MMR that also learns from performance, so smurfs get placed fast.\n"
            "Use `/explain` after any game to see exactly what moved your LP."
        )
        await inter.response.send_message(text)


async def setup(bot: commands.Bot):
    await bot.add_cog(RankingCog(bot))
