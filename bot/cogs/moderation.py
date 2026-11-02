from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import desc, select

from bot.db.database import SessionLocal
from bot.models.player import Player
from bot.models.smurf import SmurfFlag
from bot.services import bans, lobby_manager, settings
from bot.ui import embeds

log = logging.getLogger("ranked-bot.cogs.moderation")


async def _is_mod(inter: discord.Interaction) -> bool:
    from bot.cogs.panel import is_staff
    return await is_staff(inter)


class FlagView(discord.ui.View):
    """Confirmed smurf / False alarm buttons. Persistent on the mod channel posts (flag_id None);
    a one-off copy for a flag opened from the control panel."""

    def __init__(self, cog: "ModerationCog", flag_id: int | None = None):
        super().__init__(timeout=None if flag_id is None else 300)
        self.cog, self.flag_id = cog, flag_id
        suffix = "" if flag_id is None else f":{flag_id}"
        for label, outcome, style in (("Confirmed smurf", "confirmed", discord.ButtonStyle.danger),
                                      ("False alarm", "dismissed", discord.ButtonStyle.secondary)):
            b = discord.ui.Button(label=label, style=style, custom_id=f"flag:{outcome}{suffix}")
            b.callback = self._make(outcome)
            self.add_item(b)

    def _make(self, outcome: str):
        async def callback(inter: discord.Interaction):
            if not await _is_mod(inter):
                await inter.response.send_message("Only staff can resolve flags.", ephemeral=True)
                return
            await self.cog.resolve_flag_button(inter, outcome, self.flag_id)
        return callback


class ModerationCog(commands.Cog, name="Moderation"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(FlagView(self))

    async def flag_details(self, flag_id: int) -> discord.Embed:
        async with SessionLocal() as session:
            f = await session.get(SmurfFlag, flag_id)
            p = await session.get(Player, f.player_id)
            m = await session.get(Player, f.matched_player_id) if f.matched_player_id else None
            return embeds.flag_embed(f, p.discord_username if p else "?", m.discord_username if m else None)

    async def resolve_flag_button(self, inter: discord.Interaction, outcome: str, flag_id: int | None):
        async with SessionLocal() as session:
            if flag_id is not None:
                f = await session.get(SmurfFlag, flag_id)
            else:
                f = await session.scalar(select(SmurfFlag).where(SmurfFlag.mod_message_id == str(inter.message.id)))
            if f is None:
                await inter.response.send_message("That flag is gone.", ephemeral=True)
                return
            if f.status != "open":
                await inter.response.send_message(f"Already marked **{f.status}** by {f.resolved_by}.", ephemeral=True)
                return
            f.status = outcome
            f.resolved_by = inter.user.name
            f.resolved_at = datetime.now(timezone.utc)
            await session.commit()
            fid, mod_mid = f.id, f.mod_message_id
        embed = await self.flag_details(fid)
        await inter.response.edit_message(embed=embed, view=None)
        if flag_id is not None and mod_mid:
            # Opened from the panel: also close the buttons on the original alert.
            async with SessionLocal() as session:
                cid = await settings.mod_channel_id(session, str(inter.guild_id))
            ch = self.bot.get_channel(cid) if cid else None
            if ch is not None:
                try:
                    await ch.get_partial_message(int(mod_mid)).edit(embed=embed, view=None)
                except discord.HTTPException:
                    pass

    async def post_flags(self, guild_id: str, flags: list[SmurfFlag]) -> None:
        """Send new smurf flags to the mod channel (if configured)."""
        if not flags:
            return
        async with SessionLocal() as session:
            channel_id = await settings.mod_channel_id(session, guild_id)
            if not channel_id:
                log.info("%d smurf flag(s) raised but no mod channel configured (/admin modchannel)", len(flags))
                return
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except discord.HTTPException:
                    return
            for flag in flags:
                flag = await session.get(SmurfFlag, flag.id) or flag
                p = await session.get(Player, flag.player_id)
                m = await session.get(Player, flag.matched_player_id) if flag.matched_player_id else None
                try:
                    msg = await channel.send(embed=embeds.flag_embed(flag, p.discord_username if p else "?", m.discord_username if m else None),
                                             view=FlagView(self))
                    flag.mod_message_id = str(msg.id)
                except discord.HTTPException as e:
                    log.warning("could not post flag: %s", e)
            await session.commit()

    flags = app_commands.Group(name="flags", description="Anti-smurf flags (mods).", default_permissions=discord.Permissions(manage_messages=True))

    @flags.command(name="list", description="Open smurf flags.")
    async def flags_list(self, inter: discord.Interaction, include_resolved: bool = False):
        await inter.response.defer(ephemeral=True)
        async with SessionLocal() as session:
            q = select(SmurfFlag)
            if not include_resolved:
                q = q.where(SmurfFlag.status == "open")
            rows = (await session.execute(q.order_by(desc(SmurfFlag.id)).limit(15))).scalars().all()
            if not rows:
                await inter.followup.send("No flags. 🎉"); return
            lines = []
            for f in rows:
                p = await session.get(Player, f.player_id)
                lines.append(f"`#{f.id}` [{f.status}] **{p.discord_username if p else f.player_id}** — {f.kind} ({f.score*100:.0f}%): {f.summary}")
        await inter.followup.send("\n".join(lines)[:1900])

    @flags.command(name="show", description="Details of one flag.")
    async def flags_show(self, inter: discord.Interaction, flag_id: int):
        await inter.response.defer(ephemeral=True)
        async with SessionLocal() as session:
            f = await session.get(SmurfFlag, flag_id)
            if f is None:
                await inter.followup.send("No such flag."); return
            p = await session.get(Player, f.player_id)
            m = await session.get(Player, f.matched_player_id) if f.matched_player_id else None
        await inter.followup.send(embed=embeds.flag_embed(f, p.discord_username if p else "?", m.discord_username if m else None))

    @flags.command(name="resolve", description="Confirm or dismiss a flag.")
    @app_commands.choices(outcome=[app_commands.Choice(name="confirmed (it is a smurf)", value="confirmed"),
                                   app_commands.Choice(name="dismissed (false alarm)", value="dismissed")])
    async def flags_resolve(self, inter: discord.Interaction, flag_id: int, outcome: app_commands.Choice[str]):
        await inter.response.defer(ephemeral=True)
        async with SessionLocal() as session:
            f = await session.get(SmurfFlag, flag_id)
            if f is None:
                await inter.followup.send("No such flag."); return
            f.status = outcome.value
            f.resolved_by = inter.user.name
            f.resolved_at = datetime.now(timezone.utc)
            await session.commit()
        await inter.followup.send(f"Flag #{flag_id} marked **{outcome.value}**.")


    # ------------------------------------------------------------------ #
    # Queue bans                                                          #
    # ------------------------------------------------------------------ #

    async def _mod_log(self, guild_id: str, text: str) -> None:
        async with SessionLocal() as session:
            cid = await settings.mod_channel_id(session, guild_id)
        channel = self.bot.get_channel(cid) if cid else None
        if channel is not None:
            try:
                await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

    queueban = app_commands.Group(name="queueban", description="Ban players from inhouses for a while (mods).",
                                  default_permissions=discord.Permissions(manage_messages=True))

    async def _duration_autocomplete(self, inter: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        out: list[app_commands.Choice[str]] = []
        cur = current.strip()
        if cur:
            try:
                out.append(app_commands.Choice(name=f"{cur}  ({bans.format_duration(bans.parse_duration(cur))})"[:100],
                                               value=cur[:100]))
            except ValueError:
                pass
        out += [app_commands.Choice(name=s, value=s) for s in bans.SUGGESTIONS if not cur or cur.lower() in s]
        return out[:25]

    @queueban.command(name="add", description="Ban a player from playing inhouses for a set time.")
    @app_commands.describe(player="Who to ban", duration="How long, e.g. 30m, 12h, 3d, 1w, or permanent",
                           reason="Why. The player is told this, and it is logged for the mods.")
    async def queueban_add(self, inter: discord.Interaction, player: discord.User, duration: str, reason: str):
        await self.do_queueban(inter, player, duration, reason)

    async def do_queueban(self, inter: discord.Interaction, player: discord.abc.User, duration: str, reason: str):
        """Ban someone from inhouses. Used by /queueban add and the control panel."""
        if getattr(player, "bot", False):
            await inter.response.send_message("Bots don't play inhouses.", ephemeral=True)
            return
        try:
            td = bans.parse_duration(duration)
        except ValueError as e:
            await inter.response.send_message(str(e), ephemeral=True)
            return
        await inter.response.defer(ephemeral=True)
        gid = str(inter.guild_id)
        async with SessionLocal() as session:
            new, old = await bans.ban(session, gid, str(player.id), player.name, td, reason, inter.user.name)
            removed, started = await lobby_manager.remove_banned_player(session, gid, str(player.id))
            lobby_cog = self.bot.get_cog("Lobby")
            if removed is not None and lobby_cog:
                await lobby_cog.refresh_lobby_message(session, removed)
        length = "permanently" if td is None else f"for {bans.format_duration(td)}, until {bans.when(new)}"
        try:
            guild_name = inter.guild.name if inter.guild else "this server"
            await player.send(f"You've been banned from inhouse games in **{guild_name}** {length}.\n"
                              f"Reason: {new.reason}\n"
                              f"You can still use the rest of the server. Questions? Ask a moderator.")
            dm = True
        except (discord.HTTPException, AttributeError):
            dm = False
        lines = [f"Banned {player.mention} from inhouses {length}.", f"Reason: {new.reason}"]
        if old is not None:
            lines.append("This replaces the ban they already had.")
        if removed is not None:
            lines.append(f"They were taken out of lobby #{removed.id}.")
        if started is not None:
            lines.append(f"They're in lobby #{started.id}, which already has teams. That game isn't affected; "
                         f"the ban applies from their next queue.")
        if not dm:
            lines.append("I couldn't DM them (their DMs are closed), so they'll find out when they try to queue.")
        await inter.followup.send("\n".join(lines), ephemeral=True)
        await self._mod_log(gid, f"⛔ {inter.user.mention} banned {player.mention} (**{player.name}**) from inhouses "
                                 f"{length}. Reason: {new.reason}")

    queueban_add.autocomplete("duration")(_duration_autocomplete)

    @queueban.command(name="lift", description="End a player's inhouse ban early.")
    @app_commands.describe(player="Whose ban to lift", reason="Optional note for the log")
    async def queueban_lift(self, inter: discord.Interaction, player: discord.User, reason: str | None = None):
        await inter.response.send_message(await self.lift_ban(str(inter.guild_id), str(player.id), inter.user,
                                                              reason or ""), ephemeral=True)

    async def lift_ban(self, guild_id: str, discord_id: str, actor: discord.abc.User, reason: str) -> str:
        """End a ban early. Used by /queueban lift and the control panel. Returns the reply."""
        async with SessionLocal() as session:
            b = await bans.lift(session, guild_id, discord_id, actor.name, reason)
        if b is None:
            return f"<@{discord_id}> isn't banned."
        note = f" Note: {reason}" if reason else ""
        await self._mod_log(guild_id, f"✅ {actor.mention} lifted <@{discord_id}>'s inhouse ban "
                                      f"(originally: {b.reason}).{note}")
        user = self.bot.get_user(int(discord_id)) if discord_id.isdigit() else None
        if user is not None:
            try:
                await user.send("Your inhouse ban has been lifted. You can queue again.")
            except discord.HTTPException:
                pass
        return f"Lifted <@{discord_id}>'s ban. They can queue again."

    @queueban.command(name="list", description="Everyone currently banned from inhouses.")
    async def queueban_list(self, inter: discord.Interaction):
        async with SessionLocal() as session:
            rows = await bans.list_active(session, str(inter.guild_id))
        if not rows:
            await inter.response.send_message("Nobody is banned right now.", ephemeral=True)
            return
        lines = [f"<@{b.discord_id}> (**{b.username}**): until {bans.when(b)}. {b.reason} (by {b.banned_by})"
                 for b in rows[:20]]
        more = f"\n...and {len(rows) - 20} more." if len(rows) > 20 else ""
        e = discord.Embed(title=f"Banned from inhouses ({len(rows)})", description="\n".join(lines) + more,
                          color=discord.Color.red())
        await inter.response.send_message(embed=e, ephemeral=True)

    @queueban.command(name="history", description="A player's current and past inhouse bans.")
    @app_commands.describe(player="Whose history to show")
    async def queueban_history(self, inter: discord.Interaction, player: discord.User):
        async with SessionLocal() as session:
            rows = await bans.history(session, str(inter.guild_id), str(player.id))
        if not rows:
            await inter.response.send_message(f"{player.mention} has never been banned.", ephemeral=True)
            return
        lines = []
        for b in rows[:15]:
            start = f"<t:{int(bans._aware(b.created_at).timestamp())}:d>"
            if bans.is_active(b):
                status = f"**active**, until {bans.when(b)}"
            elif b.lifted_at is not None:
                status = f"lifted by {b.lifted_by}" + (f" ({b.lift_reason})" if b.lift_reason else "")
            else:
                status = "expired"
            length = "permanent" if b.expires_at is None else bans.format_duration(
                bans._aware(b.expires_at) - bans._aware(b.created_at))
            lines.append(f"{start}: {length}, {status}. {b.reason} (by {b.banned_by})")
        e = discord.Embed(title=f"Inhouse bans for {player.name} ({len(rows)} total)", description="\n".join(lines),
                          color=discord.Color.dark_red())
        await inter.response.send_message(embed=e, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationCog(bot))
