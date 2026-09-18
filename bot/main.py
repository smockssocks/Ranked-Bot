import asyncio
import logging
import sys

import discord
from discord.ext import commands, tasks

from bot import config
from bot.db.database import SessionLocal, init_db
from bot.services import auto_detect, drafter_api, lobby_manager, settings
from bot.ui import embeds

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("ranked-bot")

COGS = ["bot.cogs.lobby", "bot.cogs.ranking", "bot.cogs.admin", "bot.cogs.moderation", "bot.cogs.chat"]


class RankedBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True   # needed for the chat layer
        super().__init__(command_prefix=config.COMMAND_PREFIX, intents=intents)

    async def setup_hook(self):
        await init_db()
        for cog in COGS:
            await self.load_extension(cog)
        if config.DISCORD_GUILD_ID:
            guild = discord.Object(id=config.DISCORD_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to guild %s", config.DISCORD_GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Slash commands synced globally (can take up to an hour to appear)")
        if config.AUTO_DETECT_ENABLED and config.RIOT_API_KEY:
            self.auto_detect_loop.change_interval(seconds=config.AUTO_DETECT_POLL_SECS)
            self.auto_detect_loop.start()
        if drafter_api.is_configured():
            self.draft_poll_loop.change_interval(seconds=config.DRAFTER_POLL_SECS)
            self.draft_poll_loop.start()

    async def on_ready(self):
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)
        await self.change_presence(activity=discord.Game(name="/inhouse create • /howranked"))

    # ------------------------------------------------------------------ #
    # background: pick up finished custom games                            #
    # ------------------------------------------------------------------ #
    @tasks.loop(seconds=120)
    async def auto_detect_loop(self):
        try:
            processed = await auto_detect.poll_once(SessionLocal)
        except Exception:
            log.exception("auto-detect pass failed")
            return
        for lobby, result in processed:
            await self._announce_result(lobby, result)

    async def _announce_result(self, lobby, result):
        embed = embeds.results_embed(result)
        targets = {int(lobby.channel_id)}
        async with SessionLocal() as session:
            extra = await settings.results_channel_id(session, lobby.guild_id)
        if extra:
            targets.add(extra)
        for cid in targets:
            ch = self.get_channel(cid)
            if ch is None:
                try:
                    ch = await self.fetch_channel(cid)
                except discord.HTTPException:
                    continue
            try:
                await ch.send(f"Game detected for lobby #{lobby.id}!", embed=embed)
            except discord.HTTPException as e:
                log.warning("could not announce result: %s", e)
        lobby_cog = self.get_cog("Lobby")
        if lobby_cog:
            async with SessionLocal() as session:
                fresh = await session.get(type(lobby), lobby.id)
                if fresh:
                    await lobby_cog.refresh_lobby_message(session, fresh)
        mod = self.get_cog("Moderation")
        if mod and result.smurf_flags:
            await mod.post_flags(lobby.guild_id, result.smurf_flags)

    @auto_detect_loop.before_loop
    async def _before_auto(self):
        await self.wait_until_ready()

    # ------------------------------------------------------------------ #
    # background: post drafter.lol picks/bans once a draft completes       #
    # ------------------------------------------------------------------ #
    @tasks.loop(seconds=20)
    async def draft_poll_loop(self):
        try:
            async with SessionLocal() as session:
                lobbies = [l for l in await lobby_manager.active_lobbies(session)
                           if l.drafter_series_id and not l.drafter_result]
                if not lobbies:
                    return
                async with drafter_api.DrafterClient() as dc:
                    for lobby in lobbies:
                        try:
                            drafts = await dc.completed_drafts(lobby.drafter_series_id)
                        except drafter_api.DrafterError as e:
                            log.debug("draft poll %s: %s", lobby.id, e)
                            continue
                        if not drafts:
                            continue
                        d = drafts[0]
                        lobby.drafter_result = {"blue_picks": d.blue_picks, "red_picks": d.red_picks,
                                                "blue_bans": d.blue_bans, "red_bans": d.red_bans}
                        await session.commit()
                        ch = self.get_channel(int(lobby.channel_id))
                        if ch:
                            e = discord.Embed(title=f"Draft complete — lobby #{lobby.id}", color=discord.Color.purple())
                            e.add_field(name="Blue picks", value=", ".join(d.blue_picks) or "—", inline=True)
                            e.add_field(name="Red picks", value=", ".join(d.red_picks) or "—", inline=True)
                            e.add_field(name="Bans", value=f"Blue: {', '.join(d.blue_bans) or '—'}\nRed: {', '.join(d.red_bans) or '—'}", inline=False)
                            e.set_footer(text="Lock these in the custom lobby. The result is picked up automatically.")
                            await ch.send(embed=e)
        except Exception:
            log.exception("draft poll failed")

    @draft_poll_loop.before_loop
    async def _before_draft(self):
        await self.wait_until_ready()


def _validate() -> None:
    missing = [k for k, v in (("DISCORD_TOKEN", config.DISCORD_TOKEN),) if not v]
    if missing:
        log.error("Missing required settings: %s (see .env.example)", ", ".join(missing))
        sys.exit(1)
    if not config.RIOT_API_KEY:
        log.warning("RIOT_API_KEY not set: linking and match processing will not work.")
    if not config.OPENROUTER_API_KEY:
        log.info("OPENROUTER_API_KEY not set: chat layer disabled.")
    if not config.DRAFTER_API_KEY:
        log.info("DRAFTER_API_KEY not set: drafter.lol drafts disabled.")


async def main():
    _validate()
    bot = RankedBot()
    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
