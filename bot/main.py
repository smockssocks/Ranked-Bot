import asyncio
import logging
import discord
from discord.ext import commands
from bot.config import DISCORD_TOKEN, DISCORD_GUILD_ID

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ranked-bot")


class RankedBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.guild_id = DISCORD_GUILD_ID

    async def setup_hook(self):
        await self.load_extension("bot.cogs.lobby")
        await self.load_extension("bot.cogs.ranking")
        await self.load_extension("bot.cogs.admin")
        guild = discord.Object(id=self.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("Slash commands synced to guild %s", self.guild_id)

    async def on_ready(self):
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)


async def main():
    bot = RankedBot()
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
