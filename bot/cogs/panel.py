"""
The mod control panel: one message in a staff channel with buttons for everything a mod does
on a game night, so nobody has to remember or type commands.

  Lobby       Open lobby · Open casual lobby · Start now · Cancel lobby
  Players     Add player · Remove player · Set captains
  Games       Submit game · Undo a game
  Moderation  Queue ban · Lift ban · Smurf flags
  Season      Refresh leaderboard · New season

Every button checks the person pressing it is staff (Manage Messages, Manage Server, or the
Inhouse Mod role), because a message can be forwarded or the channel opened up by mistake.
Answers are private. The buttons keep working after a restart.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import desc, select

from bot.db.database import SessionLocal
from bot.models.game import Game
from bot.models.player import Player
from bot.models.smurf import SmurfFlag
from bot.services import bans, ladder, lobby_manager, ready_check, seasons, settings
from bot.services.lobby_manager import LobbyError
from bot.services.server_setup import REMEMBER_PREFIX
from bot.ui import embeds

log = logging.getLogger("ranked-bot.cogs.panel")

KEY_PANEL_MESSAGE = REMEMBER_PREFIX + "panel_message"


async def is_staff(inter: discord.Interaction) -> bool:
    perms = getattr(inter.user, "guild_permissions", None)
    if perms is not None and (perms.administrator or perms.manage_guild or perms.manage_messages):
        return True
    async with SessionLocal() as session:
        rid = await settings.get_setting(session, str(inter.guild_id), REMEMBER_PREFIX + "mod_role")
    return bool(rid and any(str(r.id) == rid for r in getattr(inter.user, "roles", [])))


def panel_embed() -> discord.Embed:
    e = discord.Embed(title="Inhouse control panel", color=discord.Color.dark_teal(),
                      description="Everything a mod needs on a game night. Only staff can use these "
                                  "buttons, and every answer is private to you.")
    e.add_field(name="🎮 Lobby", value="**Open lobby** posts a new lobby in the queue channel with you as host. "
                                       "**Start now** starts a full lobby (after the ready check). "
                                       "**Cancel lobby** ends it.", inline=False)
    e.add_field(name="👥 Players", value="**Add** or **Remove** someone from the queue (no cooldown for them). "
                                         "**Set captains** overrides the leaderboard's choice.", inline=False)
    e.add_field(name="🏆 Games", value="**Submit game** records a game the bot missed, by its game ID. "
                                       "**Undo a game** takes back its LP.", inline=False)
    e.add_field(name="🛡️ Moderation", value="**Queue ban** for a set time, **Lift ban**, and **Smurf flags** "
                                            "to review.", inline=False)
    e.add_field(name="📅 Season", value="**Refresh leaderboard**, and **New season** (asks first).", inline=False)
    return e


# --------------------------------------------------------------------------- #
# Small reusable pieces                                                        #
# --------------------------------------------------------------------------- #

Handler = Callable[[discord.Interaction, list[str]], Awaitable[None]]


class ChoiceView(discord.ui.View):
    """A private dropdown; the handler gets the chosen values."""

    def __init__(self, options: list[discord.SelectOption], handler: Handler, placeholder: str,
                 min_values: int = 1, max_values: int = 1):
        super().__init__(timeout=300)
        sel = discord.ui.Select(placeholder=placeholder, options=options[:25],
                                min_values=min_values, max_values=max_values)

        async def cb(inter: discord.Interaction):
            await handler(inter, list(sel.values))
        sel.callback = cb
        self.add_item(sel)


class MemberView(discord.ui.View):
    """A private member picker."""

    def __init__(self, handler: Callable[[discord.Interaction, discord.abc.User], Awaitable[None]],
                 placeholder: str):
        super().__init__(timeout=300)
        sel = discord.ui.UserSelect(placeholder=placeholder)

        async def cb(inter: discord.Interaction):
            await handler(inter, sel.values[0])
        sel.callback = cb
        self.add_item(sel)


class ConfirmView(discord.ui.View):
    def __init__(self, label: str, handler: Callable[[discord.Interaction], Awaitable[None]]):
        super().__init__(timeout=120)
        yes = discord.ui.Button(label=label, style=discord.ButtonStyle.danger)
        no = discord.ui.Button(label="Never mind", style=discord.ButtonStyle.secondary)

        async def on_yes(inter: discord.Interaction):
            await handler(inter)

        async def on_no(inter: discord.Interaction):
            await inter.response.edit_message(content="Nothing changed.", embed=None, view=None)
        yes.callback, no.callback = on_yes, on_no
        self.add_item(yes)
        self.add_item(no)


class SubmitGameModal(discord.ui.Modal, title="Submit a game"):
    game_id = discord.ui.TextInput(label="Game ID", placeholder="5650481942 (from the post-game screen)",
                                   max_length=40)

    def __init__(self, cog: "PanelCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, inter: discord.Interaction):
        await self.cog.submit_game(inter, str(self.game_id))


class BanModal(discord.ui.Modal, title="Queue ban"):
    duration = discord.ui.TextInput(label="How long", placeholder="30m, 12h, 3d, 1w, or permanent", max_length=20)
    reason = discord.ui.TextInput(label="Reason (the player is told this)", style=discord.TextStyle.paragraph,
                                  max_length=300)

    def __init__(self, cog: "PanelCog", target: discord.abc.User):
        super().__init__()
        self.cog, self.target = cog, target

    async def on_submit(self, inter: discord.Interaction):
        await self.cog.ban(inter, self.target, str(self.duration), str(self.reason))


# --------------------------------------------------------------------------- #
# The panel                                                                    #
# --------------------------------------------------------------------------- #

def _button(label: str, emoji: str, cid: str, row: int, style=discord.ButtonStyle.secondary):
    return discord.ui.Button(label=label, emoji=emoji, custom_id=f"panel:{cid}", row=row, style=style)


class PanelView(discord.ui.View):
    LAYOUT = (
        ("Open lobby", "🎮", "open", 0, discord.ButtonStyle.success),
        ("Open casual lobby", "🎲", "open_casual", 0, discord.ButtonStyle.secondary),
        ("Start now", "▶️", "start", 0, discord.ButtonStyle.primary),
        ("Cancel lobby", "✖️", "cancel", 0, discord.ButtonStyle.danger),
        ("Add player", "➕", "add", 1, discord.ButtonStyle.secondary),
        ("Remove player", "➖", "remove", 1, discord.ButtonStyle.secondary),
        ("Set captains", "👑", "captains", 1, discord.ButtonStyle.secondary),
        ("Submit game", "📥", "submit", 2, discord.ButtonStyle.secondary),
        ("Undo a game", "↩️", "undo", 2, discord.ButtonStyle.secondary),
        ("Queue ban", "⛔", "ban", 3, discord.ButtonStyle.secondary),
        ("Lift ban", "✅", "unban", 3, discord.ButtonStyle.secondary),
        ("Smurf flags", "🚩", "flags", 3, discord.ButtonStyle.secondary),
        ("Refresh leaderboard", "🏆", "board", 4, discord.ButtonStyle.secondary),
        ("New season", "📅", "season", 4, discord.ButtonStyle.secondary),
    )

    def __init__(self, cog: "PanelCog"):
        super().__init__(timeout=None)
        self.cog = cog
        for label, emoji, cid, row, style in self.LAYOUT:
            b = _button(label, emoji, cid, row, style)
            b.callback = self._make(cid)
            self.add_item(b)

    def _make(self, cid: str):
        async def callback(inter: discord.Interaction):
            if not await is_staff(inter):
                await inter.response.send_message("Only staff can use the control panel.", ephemeral=True)
                return
            try:
                await getattr(self.cog, f"do_{cid}")(inter)
            except Exception:
                log.exception("panel button %s failed", cid)
                msg = "Something went wrong. The bot log has the details."
                if inter.response.is_done():
                    await inter.followup.send(msg, ephemeral=True)
                else:
                    await inter.response.send_message(msg, ephemeral=True)
        return callback


class PanelCog(commands.Cog, name="Panel"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(PanelView(self))

    @property
    def lobby_cog(self):
        return self.bot.get_cog("Lobby")

    async def post_panel(self, guild_id: str, channel) -> str:
        """Post the panel in `channel`, or refresh it if it's already there."""
        async with SessionLocal() as session:
            mid = await settings.get_setting(session, guild_id, KEY_PANEL_MESSAGE)
            if mid and mid.isdigit():
                try:
                    await channel.get_partial_message(int(mid)).edit(embed=panel_embed(), view=PanelView(self))
                    return f"Control panel in {channel.mention} refreshed."
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    pass                                        # moved to another channel: post a new one
            msg = await channel.send(embed=panel_embed(), view=PanelView(self))
            await settings.set_setting(session, guild_id, KEY_PANEL_MESSAGE, str(msg.id))
            try:
                await msg.pin(reason="Inhouse control panel")
            except discord.HTTPException:
                pass
        return f"Control panel posted in {channel.mention}."

    # ------------------------------------------------------------------ #
    # helpers                                                              #
    # ------------------------------------------------------------------ #

    async def _queue_channel(self, guild_id: str):
        async with SessionLocal() as session:
            cid = await settings.queue_channel_id(session, guild_id)
            if not cid:
                remembered = await settings.get_setting(session, guild_id, REMEMBER_PREFIX + "inhouse_queue")
                cid = int(remembered) if remembered and remembered.isdigit() else 0
        if not cid:
            return None
        ch = self.bot.get_channel(cid)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(cid)
            except discord.HTTPException:
                return None
        return ch

    async def _lobby(self, session, guild_id: str):
        return await lobby_manager.get_active_lobby(session, guild_id)

    async def _lobby_options(self, session, lobby) -> list[discord.SelectOption]:
        place = await ladder.positions(session)
        out = []
        for lp in lobby.players:
            p = await session.get(Player, lp.player_id)
            if p is None:
                continue
            where = f"#{place[p.id]} on the ladder" if p.id in place else "Not ranked yet"
            out.append(discord.SelectOption(label=p.discord_username[:100], value=str(p.id), description=where))
        return out

    @staticmethod
    async def _reply(inter: discord.Interaction, text: str, **kw):
        if inter.response.is_done():
            await inter.followup.send(text, ephemeral=True, **kw)
        else:
            await inter.response.send_message(text, ephemeral=True, **kw)

    # ------------------------------------------------------------------ #
    # Lobby                                                                #
    # ------------------------------------------------------------------ #

    async def do_open(self, inter: discord.Interaction, casual: bool = False):
        await inter.response.defer(ephemeral=True)
        gid = str(inter.guild_id)
        channel = await self._queue_channel(gid)
        if channel is None:
            await self._reply(inter, "There's no queue channel yet. Run `/admin setup` or `/admin queuechannel`.")
            return
        async with SessionLocal() as session:
            try:
                lobby = await self.lobby_cog.open_lobby(session, gid, channel, inter.user, casual)
            except LobbyError as e:
                await self._reply(inter, str(e))
                return
        kind = "Casual lobby" if casual else "Lobby"
        await self._reply(inter, f"{kind} #{lobby.id} is open in {channel.mention}, with you as host.")

    async def do_open_casual(self, inter: discord.Interaction):
        await self.do_open(inter, casual=True)

    async def do_start(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)
        async with SessionLocal() as session:
            lobby = await self._lobby(session, str(inter.guild_id))
            if lobby is None or lobby.status != "waiting":
                await self._reply(inter, "There's no lobby waiting to start.")
                return
            if len(lobby.players) < lobby.max_players:
                await self._reply(inter, f"Lobby #{lobby.id} isn't full yet ({len(lobby.players)}/{lobby.max_players}).")
                return
            channel = await self.lobby_cog._queue_channel(lobby)
            note = await self.lobby_cog.start_or_check(session, lobby, channel)
        await self._reply(inter, note or f"Lobby #{lobby.id} is starting.")

    async def do_cancel(self, inter: discord.Interaction):
        async with SessionLocal() as session:
            lobby = await self._lobby(session, str(inter.guild_id))
        if lobby is None:
            await self._reply(inter, "There's no open lobby.")
            return

        async def confirmed(i: discord.Interaction):
            async with SessionLocal() as session:
                fresh = await session.get(lobby_manager.Lobby, lobby.id)
                if fresh is None or fresh.status not in lobby_manager.ACTIVE_STATUSES:
                    await i.response.edit_message(content="That lobby already ended.", view=None)
                    return
                await lobby_manager.cancel_lobby(session, fresh, str(i.user.id), is_admin=True)
                await self.lobby_cog.refresh_lobby_message(session, fresh)
                from bot.cogs.lobby import cleanup_note
                await self.lobby_cog.post_to_game(fresh, f"**Lobby cancelled** by {i.user.mention}. {cleanup_note()}")
            await i.response.edit_message(content=f"Lobby #{lobby.id} cancelled.", view=None)
        await self._reply(inter, f"Cancel lobby #{lobby.id} ({lobby.status}, {len(lobby.players)} players)?",
                          view=ConfirmView("Cancel the lobby", confirmed))

    # ------------------------------------------------------------------ #
    # Players                                                              #
    # ------------------------------------------------------------------ #

    async def do_add(self, inter: discord.Interaction):
        async def chosen(i: discord.Interaction, user: discord.abc.User):
            await i.response.defer(ephemeral=True)
            async with SessionLocal() as session:
                lobby = await self._lobby(session, str(i.guild_id))
                if lobby is None:
                    await self._reply(i, "There's no open lobby.")
                    return
                try:
                    await lobby_manager.force_queue(session, lobby, str(i.user.id), str(user.id), is_admin=True)
                except LobbyError as e:
                    await self._reply(i, str(e))
                    return
                await self.lobby_cog.refresh_lobby_message(session, lobby)
                await self.lobby_cog.after_join(session, lobby)
                await self._reply(i, f"Added {user.mention} ({len(lobby.players)}/{lobby.max_players}).")
        await self._reply(inter, "Who should go in the queue?", view=MemberView(chosen, "Choose a member"))

    async def do_remove(self, inter: discord.Interaction):
        async with SessionLocal() as session:
            lobby = await self._lobby(session, str(inter.guild_id))
            if lobby is None or lobby.status != "waiting" or not lobby.players:
                await self._reply(inter, "There's no filling lobby with anyone in it.")
                return
            options = await self._lobby_options(session, lobby)

        async def chosen(i: discord.Interaction, values: list[str]):
            async with SessionLocal() as session:
                fresh = await session.get(lobby_manager.Lobby, lobby.id)
                target = await session.get(Player, int(values[0]))
                try:
                    await lobby_manager.force_remove(session, fresh, str(i.user.id), target.discord_id, is_admin=True)
                except LobbyError as e:
                    await i.response.edit_message(content=str(e), view=None)
                    return
                await self.lobby_cog.refresh_lobby_message(session, fresh)
            await i.response.edit_message(content=f"Removed **{target.discord_username}** from the queue. "
                                                  f"No cooldown for them.", view=None)
        await self._reply(inter, "Who should come out of the queue?",
                          view=ChoiceView(options, chosen, "Choose a player"))

    async def do_captains(self, inter: discord.Interaction):
        async with SessionLocal() as session:
            lobby = await self._lobby(session, str(inter.guild_id))
            if lobby is None or lobby.mode != "captain" or lobby.status not in ("waiting", "drafting"):
                await self._reply(inter, "There's no captain lobby waiting for a draft.")
                return
            if len(lobby.players) < lobby.max_players:
                await self._reply(inter, f"The lobby isn't full yet ({len(lobby.players)}/{lobby.max_players}).")
                return
            if ready_check.running(lobby):
                await self._reply(inter, "Wait for the ready check to finish first.")
                return
            options = await self._lobby_options(session, lobby)

        async def chosen(i: discord.Interaction, values: list[str]):
            await i.response.defer(ephemeral=True)
            async with SessionLocal() as session:
                fresh = await session.get(lobby_manager.Lobby, lobby.id)
                order = await ladder.ladder_order(session, [int(v) for v in values])
                from bot import config
                blue, red = (order[1], order[0]) if config.CAPTAIN_FIRST_PICK != "higher" else (order[0], order[1])
                try:
                    cap1, cap2 = await lobby_manager.force_captains(session, fresh, str(i.user.id), True, blue, red)
                except LobbyError as e:
                    await self._reply(i, str(e))
                    return
                await self.lobby_cog.refresh_lobby_message(session, fresh)
                channel = await self.lobby_cog._queue_channel(fresh)
                await self.lobby_cog.start_captain_space(session, fresh, cap1, cap2, channel,
                                                         header=f"Captains chosen by {i.user.display_name}.")
            await self._reply(i, f"Draft started: **{cap1.discord_username}** (blue, picks first) and "
                                 f"**{cap2.discord_username}** (red).")
        await self._reply(inter, "Choose the two captains. The lower-ranked one picks first.",
                          view=ChoiceView(options, chosen, "Choose two captains", min_values=2, max_values=2))

    # ------------------------------------------------------------------ #
    # Games                                                                #
    # ------------------------------------------------------------------ #

    async def do_submit(self, inter: discord.Interaction):
        await inter.response.send_modal(SubmitGameModal(self))

    async def submit_game(self, inter: discord.Interaction, raw: str):
        await inter.response.defer(ephemeral=True)
        async with SessionLocal() as session:
            result, err = await self.lobby_cog.record_game(session, str(inter.guild_id), raw, inter.user.name)
        if err:
            await self._reply(inter, err)
            return
        await self._reply(inter, "Recorded.", embed=embeds.results_embed(result))

    async def do_undo(self, inter: discord.Interaction):
        async with SessionLocal() as session:
            games = (await session.execute(select(Game).where(Game.status == "processed")
                                           .order_by(desc(Game.played_at)).limit(10))).scalars().all()
        if not games:
            await self._reply(inter, "No recorded games to undo.")
            return
        options = [discord.SelectOption(
            label=g.riot_match_id, value=g.riot_match_id,
            description=(g.played_at.strftime("%b %d, %H:%M UTC") if g.played_at else "date unknown"))
            for g in games]

        async def chosen(i: discord.Interaction, values: list[str]):
            mid = values[0]

            async def confirmed(j: discord.Interaction):
                from bot.services.game_processor import rollback_match
                async with SessionLocal() as session:
                    try:
                        await rollback_match(session, mid)
                    except ValueError as e:
                        await j.response.edit_message(content=str(e), view=None)
                        return
                await j.response.edit_message(content=f"Undid **{mid}**. Everyone's LP is back to before it.",
                                              view=None)
                ranking = self.bot.get_cog("Ranking")
                if ranking:
                    await ranking.refresh_board(str(j.guild_id))
            await i.response.edit_message(content=f"Undo **{mid}**? Everyone's LP goes back to before that game. "
                                                  f"Undo newer games first.",
                                          view=ConfirmView("Undo it", confirmed))
        await self._reply(inter, "Which game? Most recent first.", view=ChoiceView(options, chosen, "Choose a game"))

    # ------------------------------------------------------------------ #
    # Moderation                                                           #
    # ------------------------------------------------------------------ #

    async def do_ban(self, inter: discord.Interaction):
        async def chosen(i: discord.Interaction, user: discord.abc.User):
            if getattr(user, "bot", False):
                await i.response.send_message("Bots don't play inhouses.", ephemeral=True)
                return
            await i.response.send_modal(BanModal(self, user))
        await self._reply(inter, "Who are you banning from inhouses?", view=MemberView(chosen, "Choose a member"))

    async def ban(self, inter: discord.Interaction, user: discord.abc.User, duration: str, reason: str):
        mod = self.bot.get_cog("Moderation")
        await mod.do_queueban(inter, user, duration, reason)

    async def do_unban(self, inter: discord.Interaction):
        async with SessionLocal() as session:
            rows = await bans.list_active(session, str(inter.guild_id))
        if not rows:
            await self._reply(inter, "Nobody is banned right now.")
            return
        options = [discord.SelectOption(label=b.username[:100], value=b.discord_id,
                                        description=f"{b.reason}"[:100]) for b in rows[:25]]

        async def chosen(i: discord.Interaction, values: list[str]):
            mod = self.bot.get_cog("Moderation")
            text = await mod.lift_ban(str(i.guild_id), values[0], i.user, "")
            await i.response.edit_message(content=text, view=None)
        await self._reply(inter, "Whose ban should end now?", view=ChoiceView(options, chosen, "Choose a player"))

    async def do_flags(self, inter: discord.Interaction):
        async with SessionLocal() as session:
            rows = (await session.execute(select(SmurfFlag).where(SmurfFlag.status == "open")
                                          .order_by(desc(SmurfFlag.id)).limit(25))).scalars().all()
            options = []
            for f in rows:
                p = await session.get(Player, f.player_id)
                options.append(discord.SelectOption(
                    label=f"#{f.id} {p.discord_username if p else f.player_id}"[:100], value=str(f.id),
                    description=f"{f.kind} ({f.score * 100:.0f}%): {f.summary}"[:100]))
        if not options:
            await self._reply(inter, "No open smurf flags. 🎉")
            return

        async def chosen(i: discord.Interaction, values: list[str]):
            mod = self.bot.get_cog("Moderation")
            embed = await mod.flag_details(int(values[0]))
            from bot.cogs.moderation import FlagView
            await i.response.edit_message(content=None, embed=embed, view=FlagView(mod, int(values[0])))
        await self._reply(inter, f"{len(options)} open flag(s). Choose one to review.",
                          view=ChoiceView(options, chosen, "Choose a flag"))

    # ------------------------------------------------------------------ #
    # Season                                                               #
    # ------------------------------------------------------------------ #

    async def do_board(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)
        ranking = self.bot.get_cog("Ranking")
        await self._reply(inter, await ranking.refresh_board(str(inter.guild_id), force=True))

    async def do_season(self, inter: discord.Interaction):
        from bot import config
        old, new = config.CURRENT_SEASON, config.CURRENT_SEASON + 1

        async def confirmed(i: discord.Interaction):
            await i.response.defer(ephemeral=True)
            async with SessionLocal() as session:
                try:
                    old_s, new_s = await seasons.start_new(session)
                except seasons.SeasonError as e:
                    await i.edit_original_response(content=str(e), view=None)
                    return
            ranking = self.bot.get_cog("Ranking")
            if ranking:
                await ranking.close_season_board(str(i.guild_id), old_s)
            await i.edit_original_response(
                content=f"**Season {new_s} has started.** Season {old_s}'s final standings are kept in the "
                        f"leaderboard channel. Everyone is back to {config.STARTING_LP} LP with "
                        f"{config.PLACEMENT_GAMES} placement games; part of their hidden rating carried over, "
                        f"so teams stay fair.", view=None)
        await self._reply(
            inter, f"End **Season {old}** and start **Season {new}**?\n"
                   f"- Everyone goes back to {config.STARTING_LP} LP and plays placements again.\n"
                   f"- {int(config.SEASON_CARRYOVER * 100)}% of each player's hidden rating carries over, so "
                   f"teams and captains stay fair.\n"
                   f"- Season {old}'s final top {config.LEADERBOARD_SIZE} stays posted in the leaderboard channel, "
                   f"and its games stay in the database.\n"
                   f"- A lobby that's mid-game has to finish first.",
            view=ConfirmView(f"Start Season {new}", confirmed))

    # ------------------------------------------------------------------ #
    # /admin panel                                                         #
    # ------------------------------------------------------------------ #

    @app_commands.command(name="panel", description="Post the mod control panel (buttons for lobbies, bans, games) here.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def panel(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)
        await inter.followup.send(await self.post_panel(str(inter.guild_id), inter.channel), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PanelCog(bot))
