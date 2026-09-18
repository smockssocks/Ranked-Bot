from __future__ import annotations

from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete, select

from bot import config
from bot.db.database import SessionLocal
from bot.models.player import Player
from bot.models.rating import PlayerRating, RoleBaseline
from bot.services import settings, smurf_detector
from bot.services.game_processor import get_or_create_rating, process_match, reprocess_match, rollback_match
from bot.services.riot_api import RiotAPIError, RiotClient, RiotUnavailable
from bot.ui import embeds


async def link_account(session, member: discord.abc.User, riot_id: str) -> tuple[Player, object | None]:
    """Shared by /link (self) and /admin link. Returns (player, smurf_flag_or_None)."""
    if "#" not in riot_id:
        raise ValueError("Use the format `GameName#TAG` (e.g. `Faker#KR1`).")
    game_name, tag = riot_id.rsplit("#", 1)
    async with RiotClient() as riot:
        account = await riot.get_account_by_riot_id(game_name.strip(), tag.strip())
        puuid = account["puuid"]
        summoner, entries = None, None
        try:
            summoner = await riot.get_summoner_by_puuid(puuid)
            entries = await riot.get_league_entries_by_puuid(puuid)
        except RiotAPIError:
            pass
    summoner_name = f"{account['gameName']}#{account['tagLine']}"
    p = await session.scalar(select(Player).where(Player.discord_id == str(member.id)))
    if p is None:
        p = Player(discord_id=str(member.id), discord_username=member.name)
        session.add(p)
        await session.flush()
    conflict = await session.scalar(select(Player).where(Player.riot_puuid == puuid, Player.id != p.id))
    if conflict:
        raise ValueError(f"That Riot account is already linked to **{conflict.discord_username}**. Ask a mod if that's wrong.")
    p.riot_puuid = puuid
    p.summoner_name = summoner_name
    p.discord_username = member.name
    p.linked_at = datetime.now(timezone.utc)
    p.summoner_level = int(summoner.get("summonerLevel", 0)) if summoner else None
    p.riot_rank_snapshot = {"entries": entries or [], "summoner_level": p.summoner_level}
    await get_or_create_rating(session, p.id, "OVERALL", config.CURRENT_SEASON)
    await session.commit()
    flag = await smurf_detector.evaluate_link(session, p, summoner, entries)
    return p, flag


class AdminCog(commands.Cog, name="Admin"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---- self-serve link -------------------------------------------------
    @app_commands.command(name="link", description="Link your Riot account (GameName#TAG) so games count.")
    async def link(self, inter: discord.Interaction, riot_id: str):
        await inter.response.defer(ephemeral=True)
        async with SessionLocal() as session:
            try:
                p, flag = await link_account(session, inter.user, riot_id)
            except ValueError as e:
                await inter.followup.send(str(e)); return
            except RiotAPIError as e:
                await inter.followup.send(f"Riot API error: {e}"); return
            except RiotUnavailable:
                await inter.followup.send("The bot has no Riot API key configured yet."); return
        await inter.followup.send(f"Linked **{p.summoner_name}**. You can `/queue` now. First {config.PLACEMENT_GAMES} games are placements.")
        mod = self.bot.get_cog("Moderation")
        if flag and mod:
            await mod.post_flags(str(inter.guild_id), [flag])

    admin = app_commands.Group(name="admin", description="Admin commands (Manage Server).", default_permissions=discord.Permissions(manage_guild=True))

    @admin.command(name="link", description="Link another member's Riot account.")
    async def admin_link(self, inter: discord.Interaction, member: discord.Member, riot_id: str):
        await inter.response.defer(ephemeral=True)
        async with SessionLocal() as session:
            try:
                p, flag = await link_account(session, member, riot_id)
            except ValueError as e:
                await inter.followup.send(str(e)); return
            except (RiotAPIError, RiotUnavailable) as e:
                await inter.followup.send(f"Riot API error: {e}"); return
        await inter.followup.send(f"Linked **{member.display_name}** to **{p.summoner_name}**.")
        mod = self.bot.get_cog("Moderation")
        if flag and mod:
            await mod.post_flags(str(inter.guild_id), [flag])

    @admin.command(name="unlink", description="Remove a member's Riot link.")
    async def admin_unlink(self, inter: discord.Interaction, member: discord.Member):
        await inter.response.defer(ephemeral=True)
        async with SessionLocal() as session:
            p = await session.scalar(select(Player).where(Player.discord_id == str(member.id)))
            if p is None:
                await inter.followup.send("Not registered."); return
            p.riot_puuid = None; p.summoner_name = None
            await session.commit()
        await inter.followup.send(f"Unlinked **{member.display_name}**.")

    @admin.command(name="submit", description="Process a match by Riot match ID.")
    async def admin_submit(self, inter: discord.Interaction, match_id: str):
        await inter.response.defer()
        async with SessionLocal() as session:
            try:
                result = await process_match(session, match_id.strip(), submitted_by=inter.user.name)
            except ValueError as e:
                await inter.followup.send(str(e)); return
            except (RiotAPIError, RiotUnavailable) as e:
                await inter.followup.send(f"Riot API error: {e}"); return
        await inter.followup.send(embed=embeds.results_embed(result))
        mod = self.bot.get_cog("Moderation")
        if mod and result.smurf_flags:
            await mod.post_flags(str(inter.guild_id), result.smurf_flags)

    @admin.command(name="reprocess", description="Roll back a match and process it again (most recent games only).")
    async def admin_reprocess(self, inter: discord.Interaction, match_id: str):
        await inter.response.defer()
        async with SessionLocal() as session:
            try:
                result = await reprocess_match(session, match_id.strip(), submitted_by=inter.user.name)
            except ValueError as e:
                await inter.followup.send(str(e)); return
        await inter.followup.send(embed=embeds.results_embed(result))

    @admin.command(name="rollback", description="Undo a match's LP changes (most recent games only).")
    async def admin_rollback(self, inter: discord.Interaction, match_id: str):
        await inter.response.defer()
        async with SessionLocal() as session:
            try:
                await rollback_match(session, match_id.strip())
            except ValueError as e:
                await inter.followup.send(str(e)); return
        await inter.followup.send(f"Rolled back **{match_id}**. Ratings restored to their pre-game values.")

    @admin.command(name="reset", description="Reset a player's ratings this season.")
    async def admin_reset(self, inter: discord.Interaction, member: discord.Member):
        await inter.response.defer(ephemeral=True)
        async with SessionLocal() as session:
            p = await session.scalar(select(Player).where(Player.discord_id == str(member.id)))
            if p is None:
                await inter.followup.send("Not registered."); return
            await session.execute(delete(PlayerRating).where(PlayerRating.player_id == p.id, PlayerRating.season == config.CURRENT_SEASON))
            await session.commit()
            await get_or_create_rating(session, p.id, "OVERALL", config.CURRENT_SEASON)
        await inter.followup.send(f"Reset **{member.display_name}** to {config.STARTING_LP} LP / placements.")

    @admin.command(name="modchannel", description="Where smurf flags and alerts are posted.")
    async def admin_modchannel(self, inter: discord.Interaction, channel: discord.TextChannel):
        async with SessionLocal() as session:
            await settings.set_setting(session, str(inter.guild_id), settings.KEY_MOD_CHANNEL, str(channel.id))
        await inter.response.send_message(f"Mod alerts will go to {channel.mention}.", ephemeral=True)

    @admin.command(name="resultschannel", description="Where auto-detected game results are posted (default: lobby channel).")
    async def admin_resultschannel(self, inter: discord.Interaction, channel: discord.TextChannel):
        async with SessionLocal() as session:
            await settings.set_setting(session, str(inter.guild_id), settings.KEY_RESULTS_CHANNEL, str(channel.id))
        await inter.response.send_message(f"Results will also be posted to {channel.mention}.", ephemeral=True)

    @admin.command(name="baselines", description="Show the learned per-role community averages.")
    async def admin_baselines(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)
        async with SessionLocal() as session:
            rows = (await session.execute(select(RoleBaseline).where(RoleBaseline.season == config.CURRENT_SEASON))).scalars().all()
        if not rows:
            await inter.followup.send("No baselines yet (no games processed)."); return
        lines = []
        for r in rows:
            st = r.stats or {}
            mean = st.get("mean", {})
            lines.append(f"**{r.role}** n={r.sample_size}: CS/min {mean.get('cs_per_min', 0):.1f}, KP {mean.get('kill_participation', 0)*100:.0f}%, "
                         f"vision {mean.get('vision_per_min', 0):.2f}/min, dmg share {mean.get('damage_share', 0)*100:.0f}%, deaths/min {mean.get('deaths_per_min', 0):.2f}")
        await inter.followup.send("\n".join(lines))

    @admin.command(name="season", description="Show the season. New seasons are started by changing CURRENT_SEASON in .env and restarting.")
    async def admin_season(self, inter: discord.Interaction):
        await inter.response.send_message(f"Current season: **{config.CURRENT_SEASON}**. Ratings, baselines and leaderboards are per season; "
                                          "old seasons stay in the database.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
