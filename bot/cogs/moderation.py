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
from bot.services import settings
from bot.ui import embeds

log = logging.getLogger("ranked-bot.cogs.moderation")


class ModerationCog(commands.Cog, name="Moderation"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

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
                    msg = await channel.send(embed=embeds.flag_embed(flag, p.discord_username if p else "?", m.discord_username if m else None))
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


async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationCog(bot))
