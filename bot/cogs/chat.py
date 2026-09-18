"""Conversational layer: listens to messages and answers via OpenRouter when addressed."""
from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from bot import config
from bot.db.database import SessionLocal
from bot.models.player import Player
from bot.models.rating import PlayerRating
from bot.services import chat_service, lobby_manager
from bot.services.chat_service import FollowupTracker, TriggerInput, OpenRouterError
from bot.services.ranks import division_for_lp

log = logging.getLogger("ranked-bot.cogs.chat")


class ChatCog(commands.Cog, name="Chat"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tracker = FollowupTracker()

    async def _context(self, session, guild_id: str, user_id: str) -> dict:
        ctx: dict = {}
        p = await session.scalar(select(Player).where(Player.discord_id == user_id))
        if p:
            r = await session.scalar(select(PlayerRating).where(
                PlayerRating.player_id == p.id, PlayerRating.role == "OVERALL", PlayerRating.season == config.CURRENT_SEASON))
            if r and r.games_played:
                ctx["this user's rank"] = (f"{division_for_lp(r.lp)} {r.lp} LP, {r.wins}W {r.games_played - r.wins}L, "
                                           f"avg PS {r.perf_mean:+.2f}, streak {r.streak}, Riot {p.summoner_name}")
            else:
                ctx["this user's rank"] = "linked but no games yet" if p.riot_puuid else "not linked (tell them to use /link)"
        else:
            ctx["this user's rank"] = "not registered (tell them to use /link GameName#TAG)"
        lobby = await lobby_manager.get_active_lobby(session, guild_id)
        if lobby:
            ctx["current lobby"] = f"#{lobby.id} status {lobby.status}, mode {lobby.mode}, {len(lobby.players)}/{lobby.max_players} players"
        else:
            ctx["current lobby"] = "none open (a host can /inhouse create)"
        ctx["commands"] = "/link, /queue, /dequeue, /inhouse create|start|pick|role|teams|draft|status|cancel|submit, /rank, /leaderboard, /history, /explain, /howranked, /chat forget"
        return ctx

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not chat_service.is_configured() or self.bot.user is None:
            return
        is_reply_to_bot = False
        if message.reference and message.reference.resolved and isinstance(message.reference.resolved, discord.Message):
            is_reply_to_bot = message.reference.resolved.author.id == self.bot.user.id
        elif message.reference and message.reference.message_id and message.channel:
            try:
                ref = await message.channel.fetch_message(message.reference.message_id)
                is_reply_to_bot = ref.author.id == self.bot.user.id
            except discord.HTTPException:
                pass
        t = TriggerInput(
            content=message.content, mentions_bot=self.bot.user in message.mentions,
            is_reply_to_bot=is_reply_to_bot, is_dm=message.guild is None,
            channel_id=message.channel.id, author_id=message.author.id, bot_name=config.CHAT_BOT_NAME,
            last_interaction=self.tracker.get(message.channel.id, message.author.id), now=time.time(),
            is_bot_author=message.author.bot,
        )
        ok, reason = chat_service.decide_trigger(t)
        if not ok:
            return
        content = message.content.replace(f"<@{self.bot.user.id}>", "").replace(f"<@!{self.bot.user.id}>", "").strip() or "hi"
        guild_id = str(message.guild.id) if message.guild else "dm"
        async with message.channel.typing():
            async with SessionLocal() as session:
                ctx = await self._context(session, guild_id, str(message.author.id)) if message.guild else {}
                try:
                    text = await chat_service.reply(session, guild_id, str(message.channel.id), str(message.author.id),
                                                    message.author.display_name, content, ctx)
                except OpenRouterError as e:
                    log.warning("chat failed: %s", e)
                    return
        await message.reply(text, mention_author=False)
        if reason == "closer" or chat_service.closes_window(content):
            self.tracker.close(message.channel.id, message.author.id)
        else:
            self.tracker.touch(message.channel.id, message.author.id)

    chat = app_commands.Group(name="chat", description="Chat with the bot / manage its memory of you.")

    @chat.command(name="forget", description="Delete everything the bot remembers about you.")
    async def chat_forget(self, inter: discord.Interaction):
        async with SessionLocal() as session:
            await chat_service.forget(session, str(inter.guild_id), str(inter.user.id))
        await inter.response.send_message("Forgotten. Clean slate.", ephemeral=True)

    @chat.command(name="status", description="Chat availability and today's budget.")
    async def chat_status(self, inter: discord.Interaction):
        if not chat_service.is_configured():
            await inter.response.send_message("Chat is off (no OPENROUTER_API_KEY).", ephemeral=True); return
        async with SessionLocal() as session:
            left = await chat_service.budget_remaining(session)
        await inter.response.send_message(
            f"Model: `{config.OPENROUTER_MODEL}` • tokens left today: {max(0, left):,} / {config.CHAT_DAILY_TOKEN_BUDGET:,}\n"
            f"Talk to me by @mentioning, replying, saying my name, or just keep talking within {config.CHAT_FOLLOWUP_WINDOW_SECS}s of my last reply.",
            ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ChatCog(bot))
