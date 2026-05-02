from __future__ import annotations
import discord
from discord.ext import commands
from discord import app_commands
from sqlalchemy import select

from bot.db.database import SessionLocal
from bot.models.player import Player
from bot.models.rating import PlayerRating
from bot.services.riot_api import RiotClient, RiotAPIError
from bot.services.game_processor import process_match, reprocess_match


class AdminCog(commands.Cog, name="Admin"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    admin = app_commands.Group(name="admin", description="Admin commands (restricted).")

    @admin.command(name="link", description="Link a Discord user to their Riot account.")
    @app_commands.describe(
        member="The Discord user to link.",
        riot_id="Their Riot ID (e.g. PlayerName#NA1)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def admin_link(self, interaction: discord.Interaction, member: discord.Member, riot_id: str):
        await interaction.response.defer(ephemeral=True)

        if "#" not in riot_id:
            await interaction.followup.send("Invalid Riot ID format. Use `GameName#TAG` (e.g. `Faker#KR1`).")
            return

        game_name, tag = riot_id.rsplit("#", 1)

        try:
            async with RiotClient() as riot:
                account = await riot.get_account_by_riot_id(game_name.strip(), tag.strip())
        except RiotAPIError as e:
            await interaction.followup.send(f"Riot API error: {e}")
            return

        puuid = account["puuid"]
        summoner_name = f"{account['gameName']}#{account['tagLine']}"

        async with SessionLocal() as session:
            db_player = await session.scalar(
                select(Player).where(Player.discord_id == str(member.id))
            )
            if db_player is None:
                db_player = Player(
                    discord_id=str(member.id),
                    discord_username=member.name,
                )
                session.add(db_player)
                await session.flush()

            # Check if PUUID is already used by another account
            conflict = await session.scalar(
                select(Player).where(
                    Player.riot_puuid == puuid,
                    Player.id != db_player.id,
                )
            )
            if conflict:
                await interaction.followup.send(
                    f"That Riot account is already linked to {conflict.discord_username}."
                )
                return

            db_player.riot_puuid = puuid
            db_player.summoner_name = summoner_name
            db_player.discord_username = member.name

            # Ensure OVERALL rating row exists
            existing_rating = await session.scalar(
                select(PlayerRating).where(
                    PlayerRating.player_id == db_player.id,
                    PlayerRating.role == "OVERALL",
                )
            )
            if existing_rating is None:
                session.add(PlayerRating(player_id=db_player.id, role="OVERALL"))

            await session.commit()

        await interaction.followup.send(
            f"Linked **{member.display_name}** to Riot account **{summoner_name}**."
        )

    @admin.command(name="submit", description="Manually submit a match ID for processing.")
    @app_commands.describe(match_id="Riot match ID (e.g. NA1_1234567890)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def admin_submit(self, interaction: discord.Interaction, match_id: str):
        await interaction.response.defer()
        async with SessionLocal() as session:
            try:
                game = await process_match(
                    session, match_id.strip(),
                    submitted_by=interaction.user.name,
                )
            except ValueError as e:
                await interaction.followup.send(str(e))
                return
            except RiotAPIError as e:
                await interaction.followup.send(f"Riot API error: {e}")
                return

        await interaction.followup.send(
            f"Match **{match_id}** processed. "
            f"{len(game.participants)} participants updated."
        )

    @admin.command(name="reprocess", description="Roll back and reprocess a match.")
    @app_commands.describe(match_id="Riot match ID to reprocess")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def admin_reprocess(self, interaction: discord.Interaction, match_id: str):
        await interaction.response.defer()
        async with SessionLocal() as session:
            try:
                game = await reprocess_match(
                    session, match_id.strip(),
                    submitted_by=interaction.user.name,
                )
            except ValueError as e:
                await interaction.followup.send(str(e))
                return
            except RiotAPIError as e:
                await interaction.followup.send(f"Riot API error: {e}")
                return

        await interaction.followup.send(
            f"Match **{match_id}** reprocessed successfully using stored weight config."
        )

    @admin.command(name="reset", description="Reset a player's LP and MMR to defaults.")
    @app_commands.describe(member="The player to reset.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def admin_reset(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        async with SessionLocal() as session:
            db_player = await session.scalar(
                select(Player).where(Player.discord_id == str(member.id))
            )
            if db_player is None:
                await interaction.followup.send(f"{member.display_name} is not registered.")
                return

            from sqlalchemy import delete
            await session.execute(
                delete(PlayerRating).where(PlayerRating.player_id == db_player.id)
            )
            session.add(PlayerRating(player_id=db_player.id, role="OVERALL"))
            await session.commit()

        await interaction.followup.send(
            f"Reset LP and MMR for **{member.display_name}** to defaults."
        )

    @admin.command(name="unlink", description="Remove a player's Riot account link.")
    @app_commands.describe(member="The Discord user to unlink.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def admin_unlink(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        async with SessionLocal() as session:
            db_player = await session.scalar(
                select(Player).where(Player.discord_id == str(member.id))
            )
            if db_player is None:
                await interaction.followup.send(f"{member.display_name} is not registered.")
                return

            db_player.riot_puuid = None
            db_player.summoner_name = None
            await session.commit()

        await interaction.followup.send(f"Unlinked Riot account from **{member.display_name}**.")

    @admin_link.error
    @admin_submit.error
    @admin_reprocess.error
    @admin_reset.error
    @admin_unlink.error
    async def admin_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You need the **Manage Server** permission to use admin commands.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
