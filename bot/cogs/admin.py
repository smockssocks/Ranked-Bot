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
from bot.services import account_link, lobby_manager, server_setup, settings, smurf_detector
from bot.services.game_processor import get_or_create_rating, process_match, reprocess_match, rollback_match
from bot.services.riot_api import RiotAPIError, RiotClient, RiotUnavailable, friendly_error, normalize_match_id
from bot.ui import embeds

import logging

log = logging.getLogger("ranked-bot.cogs.admin")


async def link_account(session, member: discord.abc.User, riot_id: str) -> tuple[Player, object | None]:
    """Admin override used by /admin link: links without the ownership check. Returns (player, flag)."""
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
    p.link_verified = False          # an admin vouched for it; the player never proved ownership
    p.linked_at = datetime.now(timezone.utc)
    p.summoner_level = int(summoner.get("summonerLevel", 0)) if summoner else None
    p.riot_rank_snapshot = {"entries": entries or [], "summoner_level": p.summoner_level}
    await get_or_create_rating(session, p.id, "OVERALL", config.CURRENT_SEASON)
    await session.commit()
    flag = await smurf_detector.evaluate_link(session, p, summoner, entries)
    return p, flag


def _cap(lines: list[str], limit: int = 1000) -> str:
    out, used = [], 0
    for ln in lines:
        if used + len(ln) + 1 > limit:
            out.append(f"...and {len(lines) - len(out)} more")
            break
        out.append(ln)
        used += len(ln) + 1
    return "\n".join(out) or "None"


def setup_report_embed(r: server_setup.Report) -> discord.Embed:
    ok = not r.failed
    e = discord.Embed(title="Server setup complete" if ok else "Server setup finished with problems",
                      color=discord.Color.green() if ok else discord.Color.orange())
    e.add_field(name=f"Created ({len(r.created)})", value=_cap(r.created), inline=True)
    e.add_field(name=f"Already there, left as is ({len(r.reused)})", value=_cap(r.reused), inline=True)
    if r.settings:
        e.add_field(name="Bot settings", value=_cap(r.settings), inline=False)
    if r.guide:
        e.add_field(name="Player guide", value=r.guide, inline=False)
    if r.failed:
        e.add_field(name="Could not do", value=_cap([f"{what}: {why}" for what, why in r.failed]), inline=False)
    steps = [
        f"Give your moderators the {r.role_mention or '@Inhouse Mod'} role so they can see the STAFF channels and use /flags.",
        "Drag the categories into whatever order you like. Renaming channels is fine.",
        "Run /admin modes to choose which lobby modes hosts can open.",
        "Running /admin setup again only adds what is missing. A channel you delete will come back.",
    ]
    e.add_field(name="Next", value="\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)), inline=False)
    return e


class SetupConfirmView(discord.ui.View):
    """Build nothing until the admin who asked presses Build it."""

    def __init__(self, invoker_id: int):
        super().__init__(timeout=300)
        self.invoker_id = invoker_id

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.invoker_id:
            await inter.response.send_message("Only the admin who ran /admin setup can confirm it.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Build it", style=discord.ButtonStyle.success)
    async def confirm(self, inter: discord.Interaction, _b: discord.ui.Button):
        self.stop()
        await inter.response.edit_message(content="Building your server. This takes a few seconds...",
                                          embed=None, view=None)
        try:
            async with SessionLocal() as session:
                report = await server_setup.build(inter.guild, session)
        except Exception:
            log.exception("server setup failed")
            await inter.edit_original_response(
                content="Setup stopped because of an unexpected error. It is logged in the bot's window. "
                        "Anything already created was kept, and running /admin setup again will carry on.")
            return
        await inter.edit_original_response(content=None, embed=setup_report_embed(report))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, inter: discord.Interaction, _b: discord.ui.Button):
        self.stop()
        await inter.response.edit_message(content="Cancelled. Nothing was changed.", embed=None, view=None)


class LinkVerifyView(discord.ui.View):
    """Buttons under a /link challenge. Persistent, so they still work after a restart."""

    def __init__(self, cog: "AdminCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, custom_id="link:verify")
    async def verify(self, inter: discord.Interaction, _b: discord.ui.Button):
        await self.cog.handle_verify(inter)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="link:cancel")
    async def cancel(self, inter: discord.Interaction, _b: discord.ui.Button):
        async with SessionLocal() as session:
            await account_link.cancel(session, str(inter.user.id))
        await inter.response.edit_message(content="Link cancelled. Nothing was changed.", embed=None, view=None)


class AdminCog(commands.Cog, name="Admin"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(LinkVerifyView(self))

    async def _notify_mods(self, guild_id: str, text: str) -> None:
        async with SessionLocal() as session:
            cid = await settings.mod_channel_id(session, guild_id)
        channel = self.bot.get_channel(cid) if cid else None
        if channel is not None:
            try:
                await channel.send(text)
            except discord.HTTPException:
                pass

    # ---- self-serve link, with proof of ownership --------------------------
    @app_commands.command(name="link", description="Link your Riot account (GameName#TAG). You'll prove it's yours.")
    @app_commands.describe(riot_id="Your Riot ID with tag, exactly as in the League client, e.g. Danman#NA1")
    async def link(self, inter: discord.Interaction, riot_id: str):
        await inter.response.defer(ephemeral=True)
        async with SessionLocal() as session:
            try:
                async with RiotClient() as riot:
                    result = await account_link.begin(session, riot, str(inter.user.id), inter.user.name, riot_id)
            except account_link.LinkError as e:
                await inter.followup.send(str(e), ephemeral=True); return
            except (RiotAPIError, RiotUnavailable) as e:
                await inter.followup.send(friendly_error(e), ephemeral=True); return
        if isinstance(result, account_link.Linked):
            await inter.followup.send(f"Your Riot ID is updated to **{result.riot_id}**. You're all set.",
                                      ephemeral=True)
            return
        await inter.followup.send(embed=await self._challenge_embed(result), view=LinkVerifyView(self),
                                  ephemeral=True)

    async def _challenge_embed(self, c: account_link.Challenge) -> discord.Embed:
        mins = account_link.VERIFY_MINUTES
        e = discord.Embed(
            title=f"Prove {c.riot_id} is yours",
            description=(f"So nobody can link an account that isn't theirs, change your League profile icon "
                         f"to **icon #{c.icon_id}**, shown here. Every account has it.\n\n"
                         f"**1.** In the League client, click your profile picture, top right.\n"
                         f"**2.** Choose the icon shown here and save.\n"
                         f"**3.** Wait about a minute, then press **Verify**.\n\n"
                         f"You have {mins} minutes. You can change your icon back once you're verified."),
            color=discord.Color.blurple())
        e.set_thumbnail(url=await account_link.icon_image_url(c.icon_id))
        if c.reclaim_from:
            e.add_field(name="Heads up",
                        value=f"This account is currently linked to **{c.reclaim_from}**. Verifying proves "
                              f"it's yours and moves it to you. The moderators will be told.", inline=False)
        e.set_footer(text=f"Your icon right now: #{c.current_icon}")
        return e

    async def handle_verify(self, inter: discord.Interaction):
        await inter.response.defer()
        async with SessionLocal() as session:
            try:
                async with RiotClient() as riot:
                    linked = await account_link.verify(session, riot, str(inter.user.id), inter.user.name)
            except account_link.LinkError as e:
                await inter.followup.send(str(e), ephemeral=True); return
            except (RiotAPIError, RiotUnavailable) as e:
                await inter.followup.send(friendly_error(e), ephemeral=True); return
        await inter.edit_original_response(
            content=f"**Verified.** {linked.riot_id} is linked to you. You can change your icon back now.\n"
                    f"You can join queues. Your first {config.PLACEMENT_GAMES} games are placements.",
            embed=None, view=None)
        gid = str(inter.guild_id) if inter.guild_id else None
        if gid and linked.transferred_from:
            await self._notify_mods(gid, f"🔁 **{linked.riot_id}** was linked to **{linked.transferred_from}**. "
                                         f"{inter.user.mention} proved they own it, so it moved to them. "
                                         f"{linked.transferred_from}'s ratings stay on their profile.")
        mod = self.bot.get_cog("Moderation")
        if gid and linked.flag and mod:
            await mod.post_flags(gid, [linked.flag])

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
                await inter.followup.send(friendly_error(e)); return
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

    @admin.command(name="submit", description="Process a match by its game ID.")
    @app_commands.describe(match_id="The game ID from the post-game screen, e.g. 5650481942. The region is added for you.")
    async def admin_submit(self, inter: discord.Interaction, match_id: str):
        await inter.response.defer()
        try:
            mid = normalize_match_id(match_id)
        except ValueError as e:
            await inter.followup.send(str(e)); return
        async with SessionLocal() as session:
            try:
                result = await process_match(session, mid, submitted_by=inter.user.name)
            except ValueError as e:
                await inter.followup.send(str(e)); return
            except (RiotAPIError, RiotUnavailable) as e:
                await inter.followup.send(friendly_error(e, context="match", match_id=mid)); return
        await inter.followup.send(embed=embeds.results_embed(result))
        mod = self.bot.get_cog("Moderation")
        if mod and result.smurf_flags:
            await mod.post_flags(str(inter.guild_id), result.smurf_flags)

    @admin.command(name="reprocess", description="Roll back a match and process it again (most recent games only).")
    @app_commands.describe(match_id="The game ID from the post-game screen, e.g. 5650481942. The region is added for you.")
    async def admin_reprocess(self, inter: discord.Interaction, match_id: str):
        await inter.response.defer()
        try:
            mid = normalize_match_id(match_id)
        except ValueError as e:
            await inter.followup.send(str(e)); return
        async with SessionLocal() as session:
            try:
                result = await reprocess_match(session, mid, submitted_by=inter.user.name)
            except ValueError as e:
                await inter.followup.send(str(e)); return
            except (RiotAPIError, RiotUnavailable) as e:
                await inter.followup.send(friendly_error(e, context="match", match_id=mid)); return
        await inter.followup.send(embed=embeds.results_embed(result))

    @admin.command(name="rollback", description="Undo a match's LP changes (most recent games only).")
    @app_commands.describe(match_id="The game ID from the post-game screen, e.g. 5650481942. The region is added for you.")
    async def admin_rollback(self, inter: discord.Interaction, match_id: str):
        await inter.response.defer()
        try:
            mid = normalize_match_id(match_id)
        except ValueError as e:
            await inter.followup.send(str(e)); return
        async with SessionLocal() as session:
            try:
                await rollback_match(session, mid)
            except ValueError as e:
                await inter.followup.send(str(e)); return
        await inter.followup.send(f"Rolled back **{mid}**. Ratings restored to their pre-game values.")

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

    @admin.command(name="setup", description="Build the inhouse channels, voice rooms and mod role, and wire the bot up.")
    async def admin_setup(self, inter: discord.Interaction):
        guild = inter.guild
        missing = server_setup.missing_permissions(guild.me.guild_permissions)
        if missing:
            names = ", ".join(f"**{server_setup.pretty_permission(m)}**" for m in missing)
            await inter.response.send_message(
                f"I can't build channels yet. I'm missing {names}.\n\n"
                f"**Easiest fix:** open this link, pick this server, and approve. It keeps everything and just "
                f"adds the permissions:\n{server_setup.invite_url(self.bot.application_id)}\n\n"
                f"**Or:** Server Settings > Roles > my role (`{guild.me.top_role.name}`) and turn those on.\n"
                f"Then run `/admin setup` again.", ephemeral=True)
            return
        async with SessionLocal() as session:
            p = await server_setup.plan(guild, session)
            current = {key: await settings.get_setting(session, str(guild.id), skey)
                       for key, (skey, _) in server_setup.SETTINGS_WIRING.items()}
            lobby = await lobby_manager.get_active_lobby(session, str(guild.id))
        e = discord.Embed(title="Server setup: preview",
                          description="Nothing changes until you press **Build it**. Setup only adds things. "
                                      "It never deletes, renames or edits channels you already have.",
                          color=discord.Color.blurple())
        create = [f"{server_setup.label(i.spec)}: {i.spec.purpose}" for i in p.to_create]
        if p.role is None:
            create.insert(0, f"@{server_setup.MOD_ROLE_NAME} role: can see STAFF channels and use /flags")
        reuse = [server_setup.label(i.spec, i.existing) for i in p.to_reuse]
        if p.role is not None:
            reuse.insert(0, f"@{p.role.name} role")
        e.add_field(name=f"Will create ({len(create)})", value=_cap(create), inline=False)
        if reuse:
            e.add_field(name=f"Already there, will be used as is ({len(reuse)})", value=_cap(reuse), inline=False)
        wiring = []
        for key, (skey, what) in server_setup.SETTINGS_WIRING.items():
            spec = next(s for s in server_setup.LAYOUT if s.key == key)
            was = f" (currently <#{current[key]}>)" if current[key] else ""
            wiring.append(f"{what}: #{spec.name}{was}")
        e.add_field(name="Bot settings it will point at these channels", value="\n".join(wiring), inline=False)
        e.add_field(name="Also", value="Posts the player guide in #how-to-play and pins it. "
                                       "#inhouse-queue is commands-only so the lobby post never gets buried; "
                                       "players chat in #inhouse-chat.", inline=False)
        queue_item = next(i for i in p.items if i.spec.key == "inhouse_queue")
        if lobby is not None and (queue_item.existing is None or int(lobby.channel_id) != queue_item.existing.id):
            e.add_field(name="Heads up",
                        value=f"There is an open lobby in <#{lobby.channel_id}>. Once queues move to "
                              f"#inhouse-queue, cancel that lobby and open a new one there.", inline=False)
        await inter.response.send_message(embed=e, view=SetupConfirmView(inter.user.id), ephemeral=True)

    @admin.command(name="testfill", description="Testing: fill the open lobby with filler players.")
    @app_commands.describe(count="How many fillers to add. Leave empty to fill the lobby.")
    async def admin_testfill(self, inter: discord.Interaction, count: app_commands.Range[int, 1, 10] | None = None):
        async with SessionLocal() as session:
            lobby = await lobby_manager.get_active_lobby(session, str(inter.guild_id))
            if lobby is None:
                await inter.response.send_message("Open a lobby first with `/inhouse create`.", ephemeral=True)
                return
            try:
                added = await lobby_manager.test_fill(session, lobby, count)
            except lobby_manager.LobbyError as e:
                await inter.response.send_message(str(e), ephemeral=True)
                return
            n = len(lobby.players)
            lobby_cog = self.bot.get_cog("Lobby")
            if lobby_cog:
                await lobby_cog.refresh_lobby_message(session, lobby)
        tips = ["They have a spread of ratings, so balancing and captain choice do something.",
                "They are never sent to Riot, and can't show up on leaderboards.",
                "In a captain draft, the host or an admin picks for a filler captain.",
                "Run `/admin testclear` when you're done. It removes every filler."]
        full = "\nThe lobby is full. Press **Start**, or run `/inhouse captains` to choose captains." \
            if n >= lobby.max_players else ""
        await inter.response.send_message(
            f"Added {len(added)} filler(s): {', '.join(f.discord_username for f in added)}. "
            f"Lobby is {n}/{lobby.max_players}.{full}\n" + "\n".join(f"- {t}" for t in tips), ephemeral=True)

    @admin.command(name="testclear", description="Testing: remove every filler player.")
    async def admin_testclear(self, inter: discord.Interaction):
        async with SessionLocal() as session:
            removed, cancelled = await lobby_manager.test_clear(session)
            lobby = await lobby_manager.get_active_lobby(session, str(inter.guild_id))
            lobby_cog = self.bot.get_cog("Lobby")
            if lobby and lobby_cog:
                await session.refresh(lobby)
                await lobby_cog.refresh_lobby_message(session, lobby)
        if not removed:
            await inter.response.send_message("There are no fillers to remove.", ephemeral=True)
            return
        extra = (f" {cancelled} lobby that already had teams with fillers was cancelled; open a new one."
                 if cancelled == 1 else
                 f" {cancelled} lobbies that already had teams with fillers were cancelled." if cancelled else "")
        await inter.response.send_message(f"Removed {removed} filler(s) and everything they touched.{extra}",
                                          ephemeral=True)

    @admin.command(name="modes", description="Choose which lobby modes hosts can open, the default, and casual lobbies.")
    @app_commands.describe(
        pick_order="Allow pick order lobbies (no role queue).",
        balanced="Allow balanced lobbies (role queue).",
        captain="Allow captain draft lobbies.",
        default="Mode used when a host does not choose one.",
        casual="Allow hosts to open casual lobbies that do not change LP.",
    )
    @app_commands.choices(default=[
        app_commands.Choice(name="Pick order", value="pick_order"),
        app_commands.Choice(name="Balanced", value="balanced"),
        app_commands.Choice(name="Captain draft", value="captain"),
    ])
    async def admin_modes(self, inter: discord.Interaction, pick_order: bool | None = None,
                          balanced: bool | None = None, captain: bool | None = None,
                          default: app_commands.Choice[str] | None = None, casual: bool | None = None):
        gid = str(inter.guild_id)
        async with SessionLocal() as session:
            allowed = await settings.allowed_modes(session, gid)
            dflt = await settings.default_mode(session, gid)
            cas = await settings.casual_allowed(session, gid)
            changed = any(v is not None for v in (pick_order, balanced, captain, default, casual))
            notes: list[str] = []
            if changed:
                want = {"pick_order": pick_order, "balanced": balanced, "captain": captain}
                new_allowed = [m for m in settings.ALL_MODES
                               if (want[m] if want[m] is not None else m in allowed)]
                if not new_allowed:
                    await inter.response.send_message("At least one mode has to stay on.", ephemeral=True)
                    return
                new_default = default.value if default else dflt
                if new_default not in new_allowed:
                    if default:
                        await inter.response.send_message(
                            f"{settings.MODE_NAMES[new_default]} can't be the default while it's turned off.",
                            ephemeral=True)
                        return
                    new_default = new_allowed[0]
                    notes.append(f"The default was turned off, so it is now {settings.MODE_NAMES[new_default]}.")
                new_cas = casual if casual is not None else cas
                await settings.set_setting(session, gid, settings.KEY_ALLOWED_MODES, ",".join(new_allowed))
                await settings.set_setting(session, gid, settings.KEY_DEFAULT_MODE, new_default)
                await settings.set_setting(session, gid, settings.KEY_CASUAL_ALLOWED, "1" if new_cas else "0")
                allowed, dflt, cas = new_allowed, new_default, new_cas
        lines = ["**Lobby modes**" + (" updated." if changed else "")]
        for m in settings.ALL_MODES:
            on = m in allowed
            star = "  ← default" if m == dflt else ""
            lines.append(f"{'✅' if on else '⛔'} {settings.MODE_NAMES[m]}{star}")
        lines.append(f"{'✅' if cas else '⛔'} Casual lobbies (no LP)")
        lines += notes
        if not changed:
            lines.append("\nChange with options, e.g. `/admin modes balanced:False captain:False` "
                         "to make pick order the only way to play.")
        await inter.response.send_message("\n".join(lines), ephemeral=True)

    @admin.command(name="queuechannel", description="Lock inhouse queues to ONE channel (recommended).")
    @app_commands.describe(channel="The channel queues live in. Leave empty to see the current setting.",
                           clear="Set to True to allow queues in any channel again.")
    async def admin_queuechannel(self, inter: discord.Interaction, channel: discord.TextChannel | None = None,
                                 clear: bool = False):
        async with SessionLocal() as session:
            if clear:
                await settings.set_setting(session, str(inter.guild_id), settings.KEY_QUEUE_CHANNEL, "")
                await inter.response.send_message(
                    "Inhouse queues can now be opened in **any** channel.", ephemeral=True)
                return
            if channel is None:
                current = await settings.queue_channel_id(session, str(inter.guild_id))
                msg = (f"Inhouse queues are locked to <#{current}>." if current
                       else "Inhouse queues are not locked to a channel yet.\n"
                            "Run `/admin queuechannel channel:#your-channel` to lock them.")
                await inter.response.send_message(msg, ephemeral=True)
                return
            await settings.set_setting(session, str(inter.guild_id), settings.KEY_QUEUE_CHANNEL, str(channel.id))
        await inter.response.send_message(
            f"Inhouse queues are now locked to {channel.mention}.\n"
            f"Every `/queue`, `/inhouse` and lobby button only works there. Anyone who tries "
            f"elsewhere gets a private nudge pointing at it.\n\n"
            f"Next: go to {channel.mention} and run `/inhouse create` to post the lobby panel.",
            ephemeral=True)

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
