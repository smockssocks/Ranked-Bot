"""
/admin setup: builds the Discord server layout for inhouses.

Creates categories, text and voice channels and an "Inhouse Mod" role, sets
channel permissions, points the bot's settings at the new channels, and posts
and pins the how-to-play guide.

Safety rules:
  * Additive only. Never deletes, renames, moves, or changes the permissions of a
    channel or role that already exists.
  * Idempotent. What it creates is remembered by ID, so renaming a channel later
    is fine; running setup again only adds what is missing. A channel you already
    had with the same name is reused as-is.
  * The bot always keeps access to channels it locks down, so it can still post.
  * Permission overwrites only ever mention permissions the bot itself holds,
    because Discord refuses to let anyone grant or deny permissions they lack.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import discord
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import settings
from bot.ui.guide import how_to_play

log = logging.getLogger("ranked-bot.setup")

REASON = "Ranked Inhouse Bot: /admin setup"
MOD_ROLE_NAME = "Inhouse Mod"
REMEMBER_PREFIX = "setup:"

# Permissions the bot needs on top of the basic invite to build the server.
REQUIRED_PERMISSIONS = ("manage_channels", "manage_roles", "manage_messages")
BASE_INVITE_PERMISSIONS = 117824   # view, send, embed, attach, read history, react


@dataclass(frozen=True)
class Spec:
    key: str           # stable identifier, used to remember the created channel's ID
    name: str
    kind: str          # "category" | "text" | "voice"
    access: str        # "public" | "readonly" | "commands" | "staff"
    parent: str | None
    purpose: str
    topic: str | None = None
    user_limit: int | None = None


LAYOUT: tuple[Spec, ...] = (
    Spec("cat_info", "INFORMATION", "category", "public", None, "Start-here channels"),
    Spec("how_to_play", "how-to-play", "text", "readonly", "cat_info",
         "The player guide, posted and pinned by the bot",
         topic="Start here: how inhouses work on this server."),
    Spec("announcements", "announcements", "text", "readonly", "cat_info",
         "News. Only staff can post", topic="News and updates."),

    Spec("cat_inhouse", "INHOUSES", "category", "public", None, "Where games happen"),
    Spec("inhouse_queue", "inhouse-queue", "text", "commands", "cat_inhouse",
         "Lobbies and queue commands. No chatting, so the lobby post stays visible",
         topic="Lobbies live here. Press Join to play. Commands only, chat in #inhouse-chat."),
    Spec("inhouse_chat", "inhouse-chat", "text", "public", "cat_inhouse", "Talk about games"),
    Spec("match_results", "match-results", "text", "readonly", "cat_inhouse",
         "Results and LP changes, posted automatically",
         topic="Results and LP changes, posted automatically after every game."),
    Spec("vc_lobby", "Lobby", "voice", "public", "cat_inhouse", "Voice while the lobby fills"),
    Spec("vc_blue", "Blue Side", "voice", "public", "cat_inhouse", "Blue team voice, 5 max", user_limit=5),
    Spec("vc_red", "Red Side", "voice", "public", "cat_inhouse", "Red team voice, 5 max", user_limit=5),

    Spec("cat_staff", "STAFF", "category", "staff", None, "Hidden from players"),
    Spec("mod_flags", "mod-flags", "text", "staff", "cat_staff", "Anti-smurf flags for mods",
         topic="Anti-smurf flags. Review with /flags show, resolve with /flags resolve."),
    Spec("bot_admin", "bot-admin", "text", "staff", "cat_staff", "Run /admin commands out of sight",
         topic="Run /admin commands here to keep them out of public channels."),
)

# Which bot setting each channel is wired to.
SETTINGS_WIRING: dict[str, tuple[str, str]] = {
    "inhouse_queue": (settings.KEY_QUEUE_CHANNEL, "Queue channel"),
    "match_results": (settings.KEY_RESULTS_CHANNEL, "Results channel"),
    "mod_flags": (settings.KEY_MOD_CHANNEL, "Mod alerts channel"),
}


# --------------------------------------------------------------------------- #
# Permissions                                                                 #
# --------------------------------------------------------------------------- #

def missing_permissions(perms: discord.Permissions) -> list[str]:
    if perms.administrator:
        return []
    return [p for p in REQUIRED_PERMISSIONS if not getattr(perms, p)]


def invite_permissions() -> discord.Permissions:
    p = discord.Permissions(BASE_INVITE_PERMISSIONS)
    for name in REQUIRED_PERMISSIONS:
        setattr(p, name, True)
    return p


def invite_url(application_id: int | None) -> str:
    cid = application_id or "YOUR_CLIENT_ID"
    return (f"https://discord.com/oauth2/authorize?client_id={cid}"
            f"&scope=bot%20applications.commands&permissions={invite_permissions().value}")


def pretty_permission(name: str) -> str:
    return name.replace("_", " ").title()


def _only_what_bot_has(ow: discord.PermissionOverwrite, have: discord.Permissions) -> discord.PermissionOverwrite:
    """Discord rejects overwrites for permissions the bot lacks; drop those."""
    if have.administrator:
        return ow
    return discord.PermissionOverwrite(**{n: v for n, v in ow if v is not None and getattr(have, n, False)})


BOT_ACCESS = dict(view_channel=True, send_messages=True, embed_links=True, read_message_history=True)


def overwrites_for(access: str, guild: Any, mod_role: Any | None, have: discord.Permissions) -> dict:
    everyone, me = guild.default_role, guild.me
    if access == "public":
        return {}
    if access in ("readonly", "commands"):
        # Players can read and use slash commands and buttons, but not type. The bot can post.
        raw = {everyone: discord.PermissionOverwrite(send_messages=False, create_public_threads=False,
                                                     create_private_threads=False),
               me: discord.PermissionOverwrite(**BOT_ACCESS)}
    elif access == "staff":
        raw = {everyone: discord.PermissionOverwrite(view_channel=False),
               me: discord.PermissionOverwrite(**BOT_ACCESS)}
        if mod_role is not None:
            raw[mod_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                        read_message_history=True)
    else:
        raise ValueError(f"unknown access {access!r}")
    out = {}
    for target, ow in raw.items():
        kept = _only_what_bot_has(ow, have)
        if not kept.is_empty():
            out[target] = kept
    return out


# --------------------------------------------------------------------------- #
# Planning                                                                    #
# --------------------------------------------------------------------------- #

@dataclass
class PlanItem:
    spec: Spec
    existing: Any | None
    how: str    # "remembered" | "same name" | "create"


@dataclass
class Plan:
    items: list[PlanItem]
    role: Any | None
    role_how: str

    @property
    def to_create(self) -> list[PlanItem]:
        return [i for i in self.items if i.existing is None]

    @property
    def to_reuse(self) -> list[PlanItem]:
        return [i for i in self.items if i.existing is not None]


def label(spec: Spec, channel: Any | None = None) -> str:
    name = channel.name if channel is not None else spec.name
    if spec.kind == "category":
        return f"Category {name}"
    if spec.kind == "voice":
        return f"Voice: {name}"
    return f"#{name}"


def _kind(channel: Any) -> str:
    return str(getattr(channel, "type", ""))


def _find_by_name(guild: Any, spec: Spec, parent_name: str | None) -> Any | None:
    if spec.kind == "category":
        pool = list(guild.categories)
    elif spec.kind == "text":
        pool = list(guild.text_channels)
    else:
        pool = list(guild.voice_channels)
    matches = [c for c in pool if c.name.lower() == spec.name.lower()]
    if parent_name:
        under = [c for c in matches if getattr(c, "category", None) and c.category.name.lower() == parent_name.lower()]
        if under:
            return under[0]
    return matches[0] if matches else None


async def _remembered(session: AsyncSession, gid: str, key: str) -> int | None:
    v = await settings.get_setting(session, gid, REMEMBER_PREFIX + key)
    return int(v) if v and v.isdigit() else None


async def _remember(session: AsyncSession, gid: str, key: str, obj_id: int) -> None:
    await settings.set_setting(session, gid, REMEMBER_PREFIX + key, str(obj_id))


async def plan(guild: Any, session: AsyncSession) -> Plan:
    gid = str(guild.id)
    names = {s.key: s.name for s in LAYOUT}
    items: list[PlanItem] = []
    for spec in LAYOUT:
        found, how = None, "create"
        rid = await _remembered(session, gid, spec.key)
        if rid:
            ch = guild.get_channel(rid)
            if ch is not None and _kind(ch) == spec.kind:
                found, how = ch, "remembered"
        if found is None:
            ch = _find_by_name(guild, spec, names.get(spec.parent) if spec.parent else None)
            if ch is not None:
                found, how = ch, "same name"
        items.append(PlanItem(spec, found, how))

    role, role_how = None, "create"
    rid = await _remembered(session, gid, "mod_role")
    if rid and guild.get_role(rid) is not None:
        role, role_how = guild.get_role(rid), "remembered"
    else:
        named = [r for r in guild.roles if r.name.lower() == MOD_ROLE_NAME.lower()]
        if named:
            role, role_how = named[0], "same name"
    return Plan(items, role, role_how)


# --------------------------------------------------------------------------- #
# Building                                                                    #
# --------------------------------------------------------------------------- #

@dataclass
class Report:
    created: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    settings: list[str] = field(default_factory=list)
    guide: str | None = None
    role_mention: str | None = None


def _why(e: Exception) -> str:
    if isinstance(e, discord.Forbidden):
        return "Discord refused: the bot is missing a permission here"
    if isinstance(e, discord.HTTPException):
        return f"Discord error {e.status}: {e.text or 'unknown'}"
    return str(e)


async def build(guild: Any, session: AsyncSession) -> Report:
    gid = str(guild.id)
    have = guild.me.guild_permissions
    p = await plan(guild, session)
    report = Report()

    # 1. Mod role (needed first so staff channels can grant it access)
    role = p.role
    if role is None:
        try:
            perms = discord.Permissions(manage_messages=bool(have.manage_messages or have.administrator))
            role = await guild.create_role(name=MOD_ROLE_NAME, permissions=perms, mentionable=False, reason=REASON)
            report.created.append(f"@{MOD_ROLE_NAME} role")
        except discord.HTTPException as e:
            report.failed.append((f"@{MOD_ROLE_NAME} role", _why(e)))
    else:
        report.reused.append(f"@{role.name} role")
    if role is not None:
        await _remember(session, gid, "mod_role", role.id)
        report.role_mention = getattr(role, "mention", f"@{role.name}")

    # 2. Categories and channels, in layout order (each category precedes its channels)
    made: dict[str, Any] = {}
    for item in p.items:
        spec = item.spec
        if item.existing is not None:
            made[spec.key] = item.existing
            report.reused.append(label(spec, item.existing))
            await _remember(session, gid, spec.key, item.existing.id)
            continue
        ow = overwrites_for(spec.access, guild, role, have)
        parent = made.get(spec.parent) if spec.parent else None
        try:
            if spec.kind == "category":
                ch = await guild.create_category(spec.name, overwrites=ow, reason=REASON)
            elif spec.kind == "text":
                kw: dict[str, Any] = {"category": parent, "overwrites": ow, "reason": REASON}
                if spec.topic:
                    kw["topic"] = spec.topic
                ch = await guild.create_text_channel(spec.name, **kw)
            else:
                kw = {"category": parent, "overwrites": ow, "reason": REASON}
                if spec.user_limit:
                    kw["user_limit"] = spec.user_limit
                ch = await guild.create_voice_channel(spec.name, **kw)
        except discord.HTTPException as e:
            report.failed.append((label(spec), _why(e)))
            continue
        made[spec.key] = ch
        report.created.append(label(spec, ch))
        await _remember(session, gid, spec.key, ch.id)

    # 3. Point the bot's settings at the channels
    for key, (skey, what) in SETTINGS_WIRING.items():
        ch = made.get(key)
        if ch is None:
            continue
        before = await settings.get_setting(session, gid, skey)
        if before != str(ch.id):
            await settings.set_setting(session, gid, skey, str(ch.id))
            report.settings.append(f"{what} set to #{ch.name}")
        else:
            report.settings.append(f"{what} already #{ch.name}")

    # 4. Post (or refresh) and pin the how-to-play guide
    htp = made.get("how_to_play")
    if htp is not None:
        queue, chat = made.get("inhouse_queue"), made.get("inhouse_chat")
        text = how_to_play(queue.id if queue else None, chat.id if chat else None)
        msg = None
        mid = await _remembered(session, gid, "guide_message")
        if mid:
            try:
                msg = await htp.fetch_message(mid)
                await msg.edit(content=text)
                report.guide = f"Guide in #{htp.name} updated"
            except discord.HTTPException:
                msg = None
        if msg is None:
            try:
                msg = await htp.send(text)
                await _remember(session, gid, "guide_message", msg.id)
                try:
                    await msg.pin(reason=REASON)
                    report.guide = f"Guide posted and pinned in #{htp.name}"
                except discord.HTTPException as e:
                    report.guide = f"Guide posted in #{htp.name} but not pinned ({_why(e)})"
            except discord.HTTPException as e:
                report.failed.append((f"Guide in #{htp.name}", _why(e)))

    log.info("setup in guild %s: %d created, %d reused, %d failed",
             gid, len(report.created), len(report.reused), len(report.failed))
    return report
