from __future__ import annotations
import discord
from discord.ext import commands
from discord import app_commands

from bot.db.database import SessionLocal
from bot.services import lobby_manager
from bot.services.lobby_manager import LobbyError, ROLES
from bot.config import LOBBY_SIZE


class LobbyCog(commands.Cog, name="Lobby"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------ #
    # /queue and /dequeue                                                  #
    # ------------------------------------------------------------------ #

    @app_commands.command(name="queue", description="Join the current inhouse queue.")
    @app_commands.describe(role="Your preferred role (optional)")
    @app_commands.choices(role=[
        app_commands.Choice(name=r, value=r) for r in ROLES
    ])
    async def queue(self, interaction: discord.Interaction, role: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        async with SessionLocal() as session:
            lobby = await lobby_manager.get_active_lobby_for_guild(session, str(interaction.guild_id))
            if lobby is None:
                await interaction.followup.send("No active lobby. A host needs to run `/inhouse create` first.")
                return
            if lobby.status != "waiting":
                await interaction.followup.send("The lobby is no longer accepting players.")
                return
            try:
                preferred = role.value if role else None
                await lobby_manager.queue_player(
                    session, lobby, str(interaction.user.id), preferred_role=preferred
                )
            except LobbyError as e:
                await interaction.followup.send(str(e))
                return

            await session.refresh(lobby)
            count = len(lobby.players)
            await interaction.followup.send(
                f"You joined the queue! ({count}/{LOBBY_SIZE} players)"
            )

            # Announce to channel if lobby is full
            if count >= LOBBY_SIZE:
                channel = interaction.channel
                await channel.send(
                    f"**Lobby is full! ({LOBBY_SIZE}/{LOBBY_SIZE})** "
                    f"The host can run `/inhouse start` to begin."
                )

    @app_commands.command(name="dequeue", description="Leave the current inhouse queue.")
    async def dequeue(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with SessionLocal() as session:
            lobby = await lobby_manager.get_active_lobby_for_guild(session, str(interaction.guild_id))
            if lobby is None:
                await interaction.followup.send("No active lobby.")
                return
            try:
                await lobby_manager.dequeue_player(session, lobby, str(interaction.user.id))
            except LobbyError as e:
                await interaction.followup.send(str(e))
                return
            await interaction.followup.send("You left the queue.")

    # ------------------------------------------------------------------ #
    # /inhouse group                                                       #
    # ------------------------------------------------------------------ #

    inhouse = app_commands.Group(name="inhouse", description="Inhouse lobby management.")

    @inhouse.command(name="create", description="Open a new inhouse lobby.")
    @app_commands.describe(mode="Draft mode: captain (default) or pick_order")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Captain Draft", value="captain"),
        app_commands.Choice(name="Pick Order (auto-balance)", value="pick_order"),
    ])
    async def inhouse_create(self, interaction: discord.Interaction, mode: app_commands.Choice[str] | None = None):
        await interaction.response.defer()
        async with SessionLocal() as session:
            try:
                lobby = await lobby_manager.create_lobby(
                    session,
                    guild_id=str(interaction.guild_id),
                    channel_id=str(interaction.channel_id),
                    host_discord_id=str(interaction.user.id),
                    mode=(mode.value if mode else "captain"),
                )
            except LobbyError as e:
                await interaction.followup.send(str(e))
                return

        embed = discord.Embed(
            title="Inhouse Lobby Created",
            description=(
                f"Mode: **{lobby.mode}**\n"
                f"Players: **0/{LOBBY_SIZE}**\n\n"
                f"Use `/queue` to join!\n"
                f"Host: {interaction.user.mention}"
            ),
            color=discord.Color.blue(),
        )
        await interaction.followup.send(embed=embed)

    @inhouse.command(name="start", description="Start team selection (host only).")
    async def inhouse_start(self, interaction: discord.Interaction):
        await interaction.response.defer()
        async with SessionLocal() as session:
            lobby = await lobby_manager.get_active_lobby_for_guild(session, str(interaction.guild_id))
            if lobby is None:
                await interaction.followup.send("No active lobby.")
                return
            if lobby.host_discord_id != str(interaction.user.id):
                await interaction.followup.send("Only the host can start the lobby.")
                return
            if len(lobby.players) < LOBBY_SIZE:
                await interaction.followup.send(
                    f"Not enough players ({len(lobby.players)}/{LOBBY_SIZE})."
                )
                return

            if lobby.mode == "captain":
                cap1, cap2 = await lobby_manager.start_captain_draft(session, lobby)
                await interaction.followup.send(
                    f"**Captain Draft started!**\n"
                    f"Captain 1: {cap1.discord_username}\n"
                    f"Captain 2: {cap2.discord_username}\n\n"
                    f"Captain 1, use `/inhouse pick @player` to make your first pick."
                )
            else:
                team1, team2 = await lobby_manager.balance_teams(session, lobby)
                embed = _build_teams_embed(team1, team2, lobby)
                await interaction.followup.send(
                    "**Teams have been balanced!**\n"
                    "Create a custom game with the tournament code (if provided) or a standard custom game.",
                    embed=embed,
                )

    @inhouse.command(name="pick", description="Captain: pick a player for your team.")
    @app_commands.describe(player="The player to pick.")
    async def inhouse_pick(self, interaction: discord.Interaction, player: discord.Member):
        await interaction.response.defer()
        async with SessionLocal() as session:
            lobby = await lobby_manager.get_active_lobby_for_guild(session, str(interaction.guild_id))
            if lobby is None:
                await interaction.followup.send("No active lobby.")
                return
            try:
                state = await lobby_manager.captain_pick(
                    session, lobby,
                    captain_discord_id=str(interaction.user.id),
                    pick_discord_id=str(player.id),
                )
            except LobbyError as e:
                await interaction.followup.send(str(e))
                return

            picks_made = state["picks_made"]
            remaining = len(state["pool"])

            if lobby.status == "active":
                # Draft complete
                await session.refresh(lobby)
                team1 = [lp for lp in lobby.players if lp.team == 1]
                team2 = [lp for lp in lobby.players if lp.team == 2]
                embed = _build_teams_embed(team1, team2, lobby)
                await interaction.followup.send("**Draft complete! Here are the teams:**", embed=embed)
            else:
                next_turn = state["pick_order"][picks_made]
                cap_id = lobby.captain1_id if next_turn == 1 else lobby.captain2_id
                from bot.models.player import Player
                from sqlalchemy import select
                cap = await session.get(Player, cap_id)
                await interaction.followup.send(
                    f"Picked **{player.display_name}**. "
                    f"({remaining} players remaining)\n"
                    f"Captain {next_turn} (**{cap.discord_username if cap else '?'}**), your pick."
                )

    @inhouse.command(name="status", description="Show current lobby status.")
    async def inhouse_status(self, interaction: discord.Interaction):
        await interaction.response.defer()
        async with SessionLocal() as session:
            lobby = await lobby_manager.get_active_lobby_for_guild(session, str(interaction.guild_id))
            if lobby is None:
                await interaction.followup.send("No active lobby in this channel.")
                return

            await session.refresh(lobby)
            lines = [f"**Status:** {lobby.status}", f"**Mode:** {lobby.mode}", ""]
            if lobby.players:
                lines.append("**Queue:**")
                for i, lp in enumerate(lobby.players, 1):
                    from bot.models.player import Player
                    p = await session.get(Player, lp.player_id)
                    role_str = f" (pref: {lp.preferred_role})" if lp.preferred_role else ""
                    lines.append(f"{i}. {p.discord_username if p else '?'}{role_str}")
            else:
                lines.append("Queue is empty.")

            await interaction.followup.send("\n".join(lines))

    @inhouse.command(name="cancel", description="Cancel the current lobby (host/admin only).")
    async def inhouse_cancel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with SessionLocal() as session:
            lobby = await lobby_manager.get_active_lobby_for_guild(session, str(interaction.guild_id))
            if lobby is None:
                await interaction.followup.send("No active lobby.")
                return
            try:
                await lobby_manager.cancel_lobby(session, lobby.id, str(interaction.user.id))
            except LobbyError as e:
                await interaction.followup.send(str(e))
                return
            await interaction.followup.send("Lobby cancelled.")

    @inhouse.command(name="submit", description="Submit a completed match for processing.")
    @app_commands.describe(match_id="The Riot match ID (e.g. NA1_1234567890)")
    async def inhouse_submit(self, interaction: discord.Interaction, match_id: str):
        await interaction.response.defer()
        from bot.services.game_processor import process_match
        from bot.services.riot_api import RiotAPIError

        async with SessionLocal() as session:
            lobby = await lobby_manager.get_active_lobby_for_guild(session, str(interaction.guild_id))
            lobby_id = lobby.id if lobby else None

            try:
                game = await process_match(
                    session, match_id,
                    submitted_by=interaction.user.name,
                    lobby_id=lobby_id,
                )
            except ValueError as e:
                await interaction.followup.send(str(e))
                return
            except RiotAPIError as e:
                await interaction.followup.send(f"Riot API error: {e}")
                return

            if lobby:
                lobby.status = "completed"
                await session.commit()

        await interaction.followup.send(
            f"Match **{match_id}** processed successfully! "
            f"LP has been updated for all linked players. "
            f"Use `/rank` to see your new LP."
        )


def _build_teams_embed(team1, team2, lobby) -> discord.Embed:
    embed = discord.Embed(title="Inhouse Teams", color=discord.Color.green())
    t1_lines = [f"{lp.assigned_role or '?'}" for lp in sorted(team1, key=lambda x: ROLES.index(x.assigned_role) if x.assigned_role in ROLES else 99)]
    t2_lines = [f"{lp.assigned_role or '?'}" for lp in sorted(team2, key=lambda x: ROLES.index(x.assigned_role) if x.assigned_role in ROLES else 99)]
    embed.add_field(name="Team 1 (Blue)", value="\n".join(t1_lines) or "—", inline=True)
    embed.add_field(name="Team 2 (Red)", value="\n".join(t2_lines) or "—", inline=True)
    return embed


async def setup(bot: commands.Bot):
    await bot.add_cog(LobbyCog(bot))
