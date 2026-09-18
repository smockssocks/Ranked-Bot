"""
Conversational layer via OpenRouter.

When does the bot answer?  decide_trigger() (pure, testable) says yes when:
  * the bot is @mentioned, or the message replies to one of the bot's messages,
  * the message is a DM,
  * the channel is a configured "bot channel" (CHAT_CHANNEL_IDS),
  * the message starts with the bot's name ("ranked bot, ..."),
  * or the same user talked to the bot in this channel within the follow-up
    window (CHAT_FOLLOWUP_WINDOW_SECS) - this is how "people keep talking to it
    without an @" is handled. The window is per (channel, user), so unrelated
    chatter in the channel is ignored. Saying "bye"/"thanks" or "stop" closes the window.

Memory (cheap by design):
  * short-term: the last CHAT_HISTORY_TURNS turns per (channel, user) from the DB
  * long-term: one rolling summary per user, refreshed every CHAT_SUMMARY_EVERY turns
    by a single extra request to the (cheap) summary model.
  * a daily token budget (CHAT_DAILY_TOKEN_BUDGET) hard-stops spending.

Server context: the caller passes a `context` dict (the user's rank, the lobby
state, recent results) that is injected into the system prompt so the bot really
"runs the server" instead of guessing.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from bot import config
from bot.models.chat import ChatMessage, ChatMemory, ChatUsage

log = logging.getLogger("ranked-bot.chat")

_CLOSERS = re.compile(r"^\s*(bye|goodbye|thanks?|thank you|ty|stop|nvm|never ?mind|ok(ay)?( thanks)?|gg)\W*$", re.I)


def is_configured() -> bool:
    return bool(config.OPENROUTER_API_KEY)


@dataclass
class TriggerInput:
    content: str
    mentions_bot: bool
    is_reply_to_bot: bool
    is_dm: bool
    channel_id: int
    author_id: int
    bot_name: str
    last_interaction: float | None   # epoch secs of the last bot<->user exchange in this channel
    now: float
    is_bot_author: bool = False


def decide_trigger(t: TriggerInput) -> tuple[bool, str]:
    """Return (respond?, reason)."""
    if t.is_bot_author:
        return False, "bot"
    text = t.content.strip()
    if not text:
        return False, "empty"
    if text.startswith(("/", "!", ".", "$", "-")):
        return False, "command"
    if t.mentions_bot:
        return True, "mention"
    if t.is_reply_to_bot:
        return True, "reply"
    if t.is_dm:
        return True, "dm"
    if t.channel_id in config.CHAT_CHANNEL_IDS:
        return True, "bot_channel"
    name = t.bot_name.lower()
    low = text.lower()
    first_word = name.split()[0]
    if low.startswith(name) or re.match(rf"^(hey|yo|ok|hi|hello)?[\s,]*{re.escape(first_word)}\b", low):
        return True, "name"
    if t.last_interaction is not None and (t.now - t.last_interaction) <= config.CHAT_FOLLOWUP_WINDOW_SECS:
        if _CLOSERS.match(text):
            return True, "closer"   # answer once, caller then closes the window
        return True, "followup"
    return False, "none"


def closes_window(content: str) -> bool:
    return bool(_CLOSERS.match(content.strip()))


# --------------------------------------------------------------------------- #
# Budget                                                                       #
# --------------------------------------------------------------------------- #

async def _usage_today(session: AsyncSession) -> ChatUsage:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = await session.scalar(select(ChatUsage).where(ChatUsage.day == day))
    if row is None:
        row = ChatUsage(day=day)
        session.add(row)
        await session.flush()
    return row


async def budget_remaining(session: AsyncSession) -> int:
    u = await _usage_today(session)
    return config.CHAT_DAILY_TOKEN_BUDGET - (u.prompt_tokens + u.completion_tokens)


# --------------------------------------------------------------------------- #
# OpenRouter                                                                   #
# --------------------------------------------------------------------------- #

class OpenRouterError(Exception):
    pass


async def _complete(messages: list[dict[str, str]], model: str, max_tokens: int, temperature: float = 0.7) -> tuple[str, int, int]:
    if not config.OPENROUTER_API_KEY:
        raise OpenRouterError("OPENROUTER_API_KEY is not configured.")
    url = f"{config.OPENROUTER_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/smockssocks/ranked-bot",
        "X-Title": "Ranked Inhouse Bot",
    }
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise OpenRouterError(f"OpenRouter {resp.status}: {str(data)[:300]}")
    try:
        text = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as e:
        raise OpenRouterError(f"Unexpected OpenRouter response: {str(data)[:300]}") from e
    usage = data.get("usage") or {}
    return text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))


# --------------------------------------------------------------------------- #
# Memory                                                                       #
# --------------------------------------------------------------------------- #

async def _memory(session: AsyncSession, guild_id: str, discord_id: str) -> ChatMemory:
    m = await session.scalar(select(ChatMemory).where(ChatMemory.guild_id == guild_id, ChatMemory.discord_id == discord_id))
    if m is None:
        m = ChatMemory(guild_id=guild_id, discord_id=discord_id, summary="")
        session.add(m)
        await session.flush()
    return m


async def _recent(session: AsyncSession, guild_id: str, channel_id: str, discord_id: str, limit: int) -> list[ChatMessage]:
    rows = (await session.execute(
        select(ChatMessage).where(ChatMessage.guild_id == guild_id, ChatMessage.channel_id == channel_id,
                                  ChatMessage.discord_id == discord_id)
        .order_by(desc(ChatMessage.id)).limit(limit)
    )).scalars().all()
    return list(reversed(rows))


async def forget(session: AsyncSession, guild_id: str, discord_id: str) -> None:
    for row in (await session.execute(select(ChatMessage).where(
            ChatMessage.guild_id == guild_id, ChatMessage.discord_id == discord_id))).scalars():
        await session.delete(row)
    m = await session.scalar(select(ChatMemory).where(ChatMemory.guild_id == guild_id, ChatMemory.discord_id == discord_id))
    if m:
        await session.delete(m)
    await session.commit()


def build_system_prompt(user_name: str, memory_summary: str, context: dict[str, Any]) -> str:
    parts = [config.CHAT_PERSONA, "",
             "How the ranking works (explain when asked): every custom game is pulled from the Riot API. "
             "Winning or losing is the biggest factor, but each player also gets a performance score (PS) "
             "that compares them to their lane opponent and to role averages on this server: laning gold/XP/CS "
             "leads, kill participation, deaths, damage share, objective participation, vision, healing/CC for "
             "supports. A great performance in a loss can lose almost nothing or even gain a little LP; a bad "
             "performance in a win gains less. New players move faster during placements. Champions do not matter, "
             "only what you did with them. Tiers come from LP."]
    if memory_summary:
        parts += ["", f"What you remember about {user_name}: {memory_summary}"]
    if context:
        parts += ["", "Live server data (authoritative, use it):"]
        for k, v in context.items():
            parts.append(f"- {k}: {v}")
    parts += ["", f"You are talking to {user_name}. Keep replies under 120 words unless asked for detail. "
              "Use plain text, no markdown headers."]
    return "\n".join(parts)


async def reply(
    session: AsyncSession, guild_id: str, channel_id: str, discord_id: str, user_name: str,
    content: str, context: dict[str, Any] | None = None,
) -> str:
    """Generate a reply, persist both turns, refresh memory when due. Raises OpenRouterError."""
    if await budget_remaining(session) <= 0:
        return "I've hit my daily chat budget. I'll be back tomorrow (rankings still work as usual)."

    mem = await _memory(session, guild_id, discord_id)
    history = await _recent(session, guild_id, channel_id, discord_id, config.CHAT_HISTORY_TURNS * 2)
    messages = [{"role": "system", "content": build_system_prompt(user_name, mem.summary, context or {})}]
    for h in history:
        messages.append({"role": h.role, "content": h.content})
    messages.append({"role": "user", "content": content[:2000]})

    text, p_tok, c_tok = await _complete(messages, config.OPENROUTER_MODEL, config.CHAT_MAX_REPLY_TOKENS)
    text = text[:1900] or "..."

    session.add(ChatMessage(guild_id=guild_id, channel_id=channel_id, discord_id=discord_id, role="user", content=content[:2000]))
    session.add(ChatMessage(guild_id=guild_id, channel_id=channel_id, discord_id=discord_id, role="assistant", content=text, tokens=c_tok))
    usage = await _usage_today(session)
    usage.prompt_tokens += p_tok
    usage.completion_tokens += c_tok
    usage.requests += 1
    mem.turns_since_summary += 1
    mem.total_turns += 1
    await session.commit()

    if mem.turns_since_summary >= config.CHAT_SUMMARY_EVERY:
        try:
            await _refresh_summary(session, mem, guild_id, discord_id, user_name)
        except OpenRouterError as e:
            log.warning("summary refresh failed: %s", e)
    await _prune(session, guild_id, channel_id, discord_id)
    return text


async def _refresh_summary(session: AsyncSession, mem: ChatMemory, guild_id: str, discord_id: str, user_name: str) -> None:
    rows = (await session.execute(
        select(ChatMessage).where(ChatMessage.guild_id == guild_id, ChatMessage.discord_id == discord_id)
        .order_by(desc(ChatMessage.id)).limit(config.CHAT_SUMMARY_EVERY * 2)
    )).scalars().all()
    transcript = "\n".join(f"{'User' if r.role == 'user' else 'Bot'}: {r.content}" for r in reversed(rows))
    prompt = [
        {"role": "system", "content": "You maintain a compact memory about a Discord user for a League of Legends inhouse bot. "
                                      "Merge the existing memory with the new conversation. Keep only durable facts: preferences, "
                                      "roles/champions they like, ongoing topics, how they like to be talked to. Max 120 words. Plain text."},
        {"role": "user", "content": f"Existing memory about {user_name}:\n{mem.summary or '(none)'}\n\nNew conversation:\n{transcript}"},
    ]
    text, p_tok, c_tok = await _complete(prompt, config.OPENROUTER_SUMMARY_MODEL, 220, temperature=0.2)
    mem.summary = text[:1200]
    mem.turns_since_summary = 0
    usage = await _usage_today(session)
    usage.prompt_tokens += p_tok
    usage.completion_tokens += c_tok
    usage.requests += 1
    await session.commit()


async def _prune(session: AsyncSession, guild_id: str, channel_id: str, discord_id: str, keep: int = 40) -> None:
    rows = (await session.execute(
        select(ChatMessage).where(ChatMessage.guild_id == guild_id, ChatMessage.channel_id == channel_id,
                                  ChatMessage.discord_id == discord_id).order_by(desc(ChatMessage.id)).offset(keep)
    )).scalars().all()
    for r in rows:
        await session.delete(r)
    if rows:
        await session.commit()


class FollowupTracker:
    """In-memory (channel, user) -> last exchange time. Cheap and good enough across restarts."""

    def __init__(self):
        self._last: dict[tuple[int, int], float] = {}

    def get(self, channel_id: int, user_id: int) -> float | None:
        return self._last.get((channel_id, user_id))

    def touch(self, channel_id: int, user_id: int) -> None:
        self._last[(channel_id, user_id)] = time.time()
        if len(self._last) > 5000:
            cutoff = time.time() - config.CHAT_FOLLOWUP_WINDOW_SECS
            self._last = {k: v for k, v in self._last.items() if v >= cutoff}

    def close(self, channel_id: int, user_id: int) -> None:
        self._last.pop((channel_id, user_id), None)
