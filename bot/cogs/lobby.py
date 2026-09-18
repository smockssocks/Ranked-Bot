from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from bot import config
from bot.db.database import SessionLocal
from bot.models.player import Player
from bot.services import drafter_api, lobby_manager
from bot.services.lobby_manager import LobbyError
from bot.services.rating_engine import ROLES, ROLE_DISPLAY
from bot.ui import embeds

log = logging.getLogger("ranked-bot.cogs.lobby")

ROLE_CHOICES = [app_commands.Choice(name=ROLE_DISPLAY[r], value=r) for r in ROLES]


async def _names(session, lobby) -> dict[int, str]:
    out = {}
    for lp in lobby.players:
        p = await session.get(Player, lp.player_id)
        out[lp.player_id] = p.discord_username if p else "?"
    return out


def _is_admin(inter: discord.Interaction) -> bool:
    perms = getattr(inter.user, "guild_permissions", None)
    return bool(perms and perms.manage_guild)


class LobbyView(discord.ui.View):
    """Persistent Join / Leave / Start buttons on the lobby message."""

    def __init__(self, cog: "LobbyCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="lobby:join")
    async def join(self, inter: discord.Interaction, _b: discord.ui.Button):
        await self.cog.handle_join(inter, None, None)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, custom_id="lobby:leave")
    async def leave(self, inter: discord.Interaction, _b: discord.ui.Button):
        await self.cog.handle_leave(inter)

    @discord.ui.button(label="Start", style=discord.ButtonStyle.primary, custom_id="lobby:start")
    async def start(self, inter: discord.Interaction, _b: discord.ui.Button):
        await self.cog.handle_start(inter)


class LobbyCog(commands.Cog, name="Lobby"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(LobbyView(self))

    # ------------------------------------------------------------------ #
    # shared handlers                                                      #
    # ------------------------------------------------------------------ #

    async def refresh_lobby_message(self, session, lobby):
        if not lobby.message_id:
            return
        try:
            channel = self.bot.get_channel(int(lobby.channel_id)) or await self.bot.fetch_channel(int(lobby.channel_id))
            msg = await channel.fetch_message(int(lobby.message_id))
            names = await _names(session, lobby)
            host = f"<@{lobby.host_discord_id}>"
            if lobby.status == "waiting":
                await msg.edit(embed=embeds.lobby_embed(lobby, names, host), view=LobbyView(self))
            else:
                await msg.edit(embed=embeds.lobby_embed(lobby, names, host), view=None)
        except discord.HTTPException as e:
            log.warning("could not refresh lobby message: %s", e)

    async def handle_join(self, inter: discord.Interaction, role: str | None, secondary: str | None):
        await inter.response.defer(ephemeral=True)
        async with SessionLocal() as session:
            lobby = await lobby_manager.get_active_lobby(session, str(inter.guild_id))
            if lobby is None:
                await inter.followup.send("No open lobby. A host can run `/inhouse create`.", ephemeral=True)
                return
            try:
                await lobby_manager.queue_player(session, lobby, str(inter.user.id), role, secondary)
            except LobbyError as e:
                await inter.followup.send(str(e), ephemeral=True)
                return
            n = len(lobby.players)
            await inter.followup.send(f"You're in ({n}/{lobby.max_players}).", ephemeral=True)
            await self.refresh_lobby_message(session, lobby)
            if n >= lobby.max_players:
                channel = self.bot.get_channel(int(lobby.channel_id))
                if channel:
                    await channel.send(f"**Lobby #{lobby.id} is full!** <@{lobby.host_discord_id}> press **Start** or run `/inhouse start`.")

    async def handle_leave(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)
        async with SessionLocal() as session:
            lobby = await lobby_manager.get_active_lobby(session, str(inter.guild_id))
            if lobby is None:
                await inter.followup.send("No open lobby.", ephemeral=True)
                return
            try:
                await lobby_manager.dequeue_player(session, lobby, str(inter.user.id))
            except LobbyError as e:
                await inter.followup.send(str(e), ephemeral=True)
                return
            await inter.followup.send("You left the queue.", ephemeral=True)
            await self.refresh_lobby_message(session, lobby)

    async def handle_start(self, inter: discord.Interaction, random_captains: bool = False):
        await inter.response.defer()
        async with SessionLocal() as session:
            lobby = await lobby_manager.get_active_lobby(session, str(inter.guild_id))
            if lobby is None:
                await inter.followup.send("No open lobby.")
                return
            if lobby.status != "waiting":
                await inter.followup.send("This lobby has already started.")
                return
            if lobby.host_discord_id != str(inter.user.id) and not _is_admin(inter):
                await inter.followup.send("Only the host (or an admin) can start the lobby.", ephemeral=True)
                return
            if len(lobby.players) < lobby.max_players:
                await inter.followup.send(f"Not enough players ({len(lobby.players)}/{lobby.max_players}).")
                return
            names = await _names(session, lobby)
            try:
                if lobby.mode == "captain":
                    cap1, cap2 = await lobby_manager.start_captain_draft(session, lobby, random_captains)
                    await self.refresh_lobby_message(session, lobby)
                    await inter.followup.send(
                        f"**Captain draft!**\nBlue captain: <@{cap1.discord_id}>\nRed captain: <@{cap2.discord_id}>\n\n"
                        f"<@{cap1.discord_id}> picks first with `/inhouse pick @player` (snake order 1-2-2-1-1-2-2-1)."
                    )
                    return
                await lobby_manager.make_teams(session, lobby)
            except LobbyError as e:
                await inter.followup.send(str(e))
                return
            await self.refresh_lobby_message(session, lobby)
            await self.announce_teams(inter.followup, session, lobby, names)

    async def announce_teams(self, dest, session, lobby, names):
        ratings = await lobby_manager.lobby_ratings(session, lobby)
        note = ""
        if drafter_api.is_configured() and not lobby.drafter_links:
            try:
                await self.create_draft(session, lobby, names)
            except drafter_api.DrafterError as e:
                note = f"\n(drafter.lol draft could not be created: {e})"
        await dest.send("**Teams are set!**" + note, embed=embeds.teams_embed(lobby, names, ratings))

    async def create_draft(self, session, lobby, names):
        t1 = lobby.team1_name or "Blue"
        t2 = lobby.team2_name or "Red"
        async with drafter_api.DrafterClient() as dc:
            series = await dc.create_series(t1, t2, game_amount=1)
        lobby.drafter_series_id = series.series_id or None
        lobby.drafter_links = series.links
        await session.commit()
        return series

    # ------------------------------------------------------------------ #
    # /queue /dequeue                                                      #
    # ------------------------------------------------------------------ #

    @app_commands.command(name="queue", description="Join the current inhouse queue.")
    @app_commands.describe(role="Preferred role", secondary="Backup role")
    @app_commands.choices(role=ROLE_CHOICES, secondary=ROLE_CHOICES)
    async def queue(self, inter: discord.Interaction, role: app_commands.Choice[str] | None = None,
                    secondary: app_commands.Choice[str] | None = None):
        await self.handle_join(inter, role.value if role else None, secondary.value if secondary else None)

    @app_commands.command(name="dequeue", description="Leave the current inhouse queue.")
    async def dequeue(self, inter: discord.Interaction):
        await self.handle_leave(inter)

    # ------------------------------------------------------------------ #
    # /inhouse group                                                       #
    # ------------------------------------------------------------------ #

    inhouse = app_commands.Group(name="inhouse", description="Inhouse lobby management.")

    @inhouse.command(name="create", description="Open a new inhouse lobby with Join/Leave buttons.")
    @app_commands.describe(mode="How teams are made")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Captain draft (captains pick, then set roles)", value="captain"),
        app_commands.Choice(name="Balanced (bot balances MMR + roles)", value="balanced"),
        app_commands.Choice(name="Pick order (balanced teams, roles first-come-first-serve)", value="pick_order"),
    ])
    async def inhouse_create(self, inter: discord.Interaction, mode: app_commands.Choice[str] | None = None):
        await inter.response.defer()
        async with SessionLocal() as session:
            try:
                lobby = await lobby_manager.create_lobby(session, str(inter.guild_id), str(inter.channel_id),
                                                         str(inter.user.id), mode.value if mode else "captain")
            except LobbyError as e:
                await inter.followup.send(str(e))
                return
            msg = await inter.followup.send(embed=embeds.lobby_embed(lobby, {}, inter.user.mention), view=LobbyView(self), wait=True)
            lobby.message_id = str(msg.id)
            await session.commit()

    @inhouse.command(name="start", description="Make teams / start the captain draft (host only).")
    @app_commands.describe(random_captains="Captain mode: pick captains at random instead of highest rated")
    async def inhouse_start(self, inter: discord.Interaction, random_captains: bool = False):
        await self.handle_start(inter, random_captains)

    @inhouse.command(name="pick", description="Captain: pick a player for your team.")
    async def inhouse_pick(self, inter: discord.Interaction, player: discord.Member):
        await inter.response.defer()
        async with SessionLocal() as session:
            lobby = await lobby_manager.get_active_lobby(session, str(inter.guild_id))
            if lobby is None:
                await inter.followup.send("No active lobby.")
                return
            try:
                await lobby_manager.captain_pick(session, lobby, str(inter.user.id), str(player.id))
            except LobbyError as e:
                await inter.followup.send(str(e))
                return
            names = await _names(session, lobby)
            if lobby.status == "active":
                await self.refresh_lobby_message(session, lobby)
                await inter.followup.send("**Draft complete!** Roles below are suggestions from preferences; captains can move players with `/inhouse role`.")
                await self.announce_teams(inter.followup, session, lobby, names)
            else:
                nxt = lobby_manager.current_captain_id(lobby)
                cap = await session.get(Player, nxt) if nxt else None
                pool = [names[pid] for pid in lobby.draft_state["pool"]]
                await inter.followup.send(f"Picked **{player.display_name}**. Available: {', '.join(pool)}\n"
                                          f"<@{cap.discord_id if cap else '?'}>, your pick.")

    @inhouse.command(name="role", description="Captain/host: move a teammate to a role (swaps if taken).")
    @app_commands.choices(role=ROLE_CHOICES)
    async def inhouse_role(self, inter: discord.Interaction, player: discord.Member, role: app_commands.Choice[str]):
        await inter.response.defer()
        async with SessionLocal() as session:
            lobby = await lobby_manager.get_active_lobby(session, str(inter.guild_id))
            if lobby is None:
                await inter.followup.send("No active lobby.")
                return
            try:
                await lobby_manager.set_role(session, lobby, str(inter.user.id), str(player.id), role.value, _is_admin(inter))
            except LobbyError as e:
                await inter.followup.send(str(e))
                return
            names = await _names(session, lobby)
            await inter.followup.send(embed=embeds.teams_embed(lobby, names))

    @inhouse.command(name="teams", description="Show the current teams.")
    async def inhouse_teams(self, inter: discord.Interaction):
        await inter.response.defer()
        async with SessionLocal() as session:
            lobby = await lobby_manager.get_active_lobby(session, str(inter.guild_id))
            if lobby is None or lobby.status not in ("active", "drafting"):
                await inter.followup.send("No teams yet.")
                return
            names = await _names(session, lobby)
            await inter.followup.send(embed=embeds.teams_embed(lobby, names, await lobby_manager.lobby_ratings(session, lobby)))

    @inhouse.command(name="draft", description="Create (or re-create) a drafter.lol draft for the current teams.")
    @app_commands.describe(fearless="Fearless draft", games="Number of games in the series (1-5)")
    async def inhouse_draft(self, inter: discord.Interaction, fearless: bool = False, games: int = 1):
        await inter.response.defer()
        if not drafter_api.is_configured():
            await inter.followup.send("drafter.lol is not configured (set DRAFTER_API_KEY).")
            return
        async with SessionLocal() as session:
            lobby = await lobby_manager.get_active_lobby(session, str(inter.guild_id))
            if lobby is None or lobby.status != "active":
                await inter.followup.send("Teams need to be set first.")
                return
            try:
                async with drafter_api.DrafterClient() as dc:
                    series = await dc.create_series(lobby.team1_name or "Blue", lobby.team2_name or "Red",
                                                    fearless=fearless, game_amount=games)
            except drafter_api.DrafterError as e:
                await inter.followup.send(f"drafter.lol error: {e}")
                return
            lobby.drafter_series_id = series.series_id or None
            lobby.drafter_links = series.links
            lobby.drafter_result = None
            await session.commit()
            names = await _names(session, lobby)
            await inter.followup.send("Draft room ready:", embed=embeds.teams_embed(lobby, names))

    @inhouse.command(name="status", description="Show the current lobby.")
    async def inhouse_status(self, inter: discord.Interaction):
        await inter.response.defer()
        async with SessionLocal() as session:
            lobby = await lobby_manager.get_active_lobby(session, str(inter.guild_id))
            if lobby is None:
                await inter.followup.send("No active lobby.")
                return
            names = await _names(session, lobby)
            await inter.followup.send(embed=embeds.lobby_embed(lobby, names, f"<@{lobby.host_discord_id}>"))

    @inhouse.command(name="cancel", description="Cancel the current lobby (host/admin).")
    async def inhouse_cancel(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)
        async with SessionLocal() as session:
            lobby = await lobby_manager.get_active_lobby(session, str(inter.guild_id))
            if lobby is None:
                await inter.followup.send("No active lobby.")
                return
            try:
                await lobby_manager.cancel_lobby(session, lobby, str(inter.user.id), _is_admin(inter))
            except LobbyError as e:
                await inter.followup.send(str(e))
                return
            await self.refresh_lobby_message(session, lobby)
            await inter.followup.send("Lobby cancelled.")

    @inhouse.command(name="submit", description="Submit a finished match by Riot match ID (if auto-detect missed it).")
    @app_commands.describe(match_id="e.g. NA1_1234567890")
    async def inhouse_submit(self, inter: discord.Interaction, match_id: str):
        await inter.response.defer()
        from bot.services.game_processor import process_match
        from bot.services.riot_api import RiotAPIError, RiotUnavailable
        async with SessionLocal() as session:
            lobby = await lobby_manager.get_active_lobby(session, str(inter.guild_id))
            try:
                result = await process_match(session, match_id.strip(), submitted_by=inter.user.name,
                                             lobby_id=lobby.id if lobby and lobby.status == "active" else None)
            except ValueError as e:
                await inter.followup.send(str(e))
                return
            except (RiotAPIError, RiotUnavailable) as e:
                await inter.followup.send(f"Riot API error: {e}")
                return
            if lobby and lobby.status == "active" and not result.remake:
                await lobby_manager.complete_lobby(session, lobby)
                await self.refresh_lobby_message(session, lobby)
        await inter.followup.send(embed=embeds.results_embed(result))
        mod = self.bot.get_cog("Moderation")
        if mod and result.smurf_flags:
            await mod.post_flags(str(inter.guild_id), result.smurf_flags)


async def setup(bot: commands.Bot):
    await bot.add_cog(LobbyCog(bot))
