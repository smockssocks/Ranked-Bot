"""/admin setup against a fake Discord server: layout, permissions, idempotency, safety."""
import itertools
from types import SimpleNamespace

import discord
import pytest

from bot.services import server_setup as ss
from bot.services import settings
from bot.ui.guide import how_to_play

_ids = itertools.count(10_000)


class FakeRole:
    def __init__(self, name, permissions=None):
        self.id, self.name = next(_ids), name
        self.permissions = permissions or discord.Permissions.none()
        self.mention = f"<@&{self.id}>"


class FakeMember:
    """Like discord.Member: hashable, so it can key a permission-overwrites dict."""
    def __init__(self, perms, top_role):
        self.id, self.guild_permissions, self.top_role = 1, perms, top_role


class FakeMessage:
    def __init__(self, content):
        self.id, self.content, self.pinned = next(_ids), content, False

    async def edit(self, content=None):
        self.content = content

    async def pin(self, reason=None):
        self.pinned = True


class FakeChannel:
    def __init__(self, name, kind, category=None, overwrites=None, topic=None, user_limit=None):
        self.id, self.name, self.type, self.category = next(_ids), name, kind, category
        self.overwrites, self.topic, self.user_limit = dict(overwrites or {}), topic, user_limit
        self.messages: dict[int, FakeMessage] = {}

    async def send(self, content):
        m = FakeMessage(content)
        self.messages[m.id] = m
        return m

    async def fetch_message(self, mid):
        if mid in self.messages:
            return self.messages[mid]
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")


def _forbidden():
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")


class FakeGuild:
    def __init__(self, perms=None, fail_names=()):
        self.id = 42
        self.default_role = FakeRole("@everyone")
        self.roles = [self.default_role]
        bot_role = FakeRole("Ranked Bot")
        self.me = FakeMember(perms or full_perms(), bot_role)
        self.channels: list[FakeChannel] = []
        self.fail_names = set(fail_names)

    categories = property(lambda self: [c for c in self.channels if c.type == "category"])
    text_channels = property(lambda self: [c for c in self.channels if c.type == "text"])
    voice_channels = property(lambda self: [c for c in self.channels if c.type == "voice"])

    def get_channel(self, cid):
        return next((c for c in self.channels if c.id == cid), None)

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    def _add(self, ch):
        if ch.name in self.fail_names:
            raise _forbidden()
        self.channels.append(ch)
        return ch

    async def create_role(self, name, permissions, mentionable=False, reason=None):
        r = FakeRole(name, permissions)
        self.roles.append(r)
        return r

    async def create_category(self, name, overwrites=None, reason=None):
        return self._add(FakeChannel(name, "category", overwrites=overwrites))

    async def create_text_channel(self, name, category=None, overwrites=None, topic=None, reason=None):
        return self._add(FakeChannel(name, "text", category, overwrites, topic))

    async def create_voice_channel(self, name, category=None, overwrites=None, user_limit=None, reason=None):
        return self._add(FakeChannel(name, "voice", category, overwrites, user_limit=user_limit))


def full_perms():
    p = ss.invite_permissions()
    p.create_public_threads = p.create_private_threads = True
    return p


def by_name(guild, name):
    return next(c for c in guild.channels if c.name == name)


# --------------------------------------------------------------------------- #

async def test_fresh_server_gets_full_layout(db):
    g = FakeGuild()
    async with db() as s:
        r = await ss.build(g, s)
        assert not r.failed
        assert len(r.created) == len(ss.LAYOUT) + 1 and r.reused == []      # every channel + the mod role
        # structure
        assert by_name(g, "how-to-play").category.name == "INFORMATION"
        assert by_name(g, "inhouse-queue").category.name == "INHOUSES"
        assert by_name(g, "mod-flags").category.name == "STAFF"
        assert by_name(g, "Blue Side").user_limit == 5 and by_name(g, "Lobby").user_limit is None
        # permissions
        everyone, me = g.default_role, g.me
        role = next(x for x in g.roles if x.name == ss.MOD_ROLE_NAME)
        assert role.permissions.manage_messages
        for ro in ("how-to-play", "announcements", "match-results", "inhouse-queue"):
            ow = by_name(g, ro).overwrites
            assert ow[everyone].send_messages is False, ro
            assert ow[me].send_messages is True, f"bot must still post in #{ro}"
        staff = by_name(g, "mod-flags").overwrites
        assert staff[everyone].view_channel is False
        assert staff[me].view_channel is True, "bot must not lock itself out of #mod-flags"
        assert staff[role].view_channel is True
        assert by_name(g, "inhouse-chat").overwrites == {}
        # settings wired
        gid = str(g.id)
        assert await settings.queue_channel_id(s, gid) == by_name(g, "inhouse-queue").id
        assert await settings.results_channel_id(s, gid) == by_name(g, "match-results").id
        assert await settings.mod_channel_id(s, gid) == by_name(g, "mod-flags").id
        # guide posted, pinned, and links the real queue channel
        msgs = list(by_name(g, "how-to-play").messages.values())
        assert len(msgs) == 1 and msgs[0].pinned
        assert f"<#{by_name(g, 'inhouse-queue').id}>" in msgs[0].content


async def test_running_again_creates_nothing(db):
    g = FakeGuild()
    async with db() as s:
        await ss.build(g, s)
        n = len(g.channels)
        r = await ss.build(g, s)
        assert r.created == [] and not r.failed
        assert len(r.reused) == len(ss.LAYOUT) + 1
        assert len(g.channels) == n
        htp = by_name(g, "how-to-play")
        assert len(htp.messages) == 1, "guide must be refreshed in place, not reposted"
        assert "updated" in r.guide


async def test_renamed_channels_are_remembered_and_deleted_ones_come_back(db):
    g = FakeGuild()
    async with db() as s:
        await ss.build(g, s)
        by_name(g, "inhouse-chat").name = "general-inhouse"          # admin renamed it
        g.channels.remove(by_name(g, "Red Side"))                     # admin deleted it
        r = await ss.build(g, s)
        assert r.created == ["Voice: Red Side"]
        assert "#general-inhouse" in r.reused
        assert not any(c.name == "inhouse-chat" for c in g.channels), "renamed channel must not be duplicated"


async def test_existing_channels_are_used_but_never_changed(db):
    g = FakeGuild()
    mine = FakeChannel("how-to-play", "text")                         # the owner's own channel, public
    g.channels.append(mine)
    async with db() as s:
        r = await ss.build(g, s)
        assert "#how-to-play" in r.reused
        assert mine.overwrites == {}, "setup must not edit permissions on a channel it did not create"
        assert sum(1 for c in g.channels if c.name == "how-to-play") == 1
        assert len(mine.messages) == 1                                # guide still posted there


async def test_one_failure_does_not_stop_the_rest(db):
    g = FakeGuild(fail_names={"match-results"})
    async with db() as s:
        r = await ss.build(g, s)
        assert [w for w, _ in r.failed] == ["#match-results"]
        assert "missing a permission" in r.failed[0][1]
        assert len(r.created) == len(ss.LAYOUT)                       # everything else, plus the role
        assert await settings.get_setting(s, str(g.id), settings.KEY_RESULTS_CHANNEL) is None


def test_overwrites_only_use_permissions_the_bot_has():
    g = FakeGuild()
    lean = ss.invite_permissions()                                    # no thread permissions
    ow = ss.overwrites_for("readonly", g, None, lean)[g.default_role]
    assert ow.send_messages is False and ow.create_public_threads is None
    rich = ss.overwrites_for("readonly", g, None, full_perms())[g.default_role]
    assert rich.create_public_threads is False
    assert ss.overwrites_for("public", g, None, lean) == {}


def test_permission_check_and_invite_link():
    assert ss.missing_permissions(discord.Permissions(ss.BASE_INVITE_PERMISSIONS)) == list(ss.REQUIRED_PERMISSIONS)
    assert ss.missing_permissions(discord.Permissions(administrator=True)) == []
    assert ss.missing_permissions(ss.invite_permissions()) == []
    url = ss.invite_url(1387571815718719548)
    assert "client_id=1387571815718719548" in url and f"permissions={ss.invite_permissions().value}" in url
    assert discord.Permissions(ss.invite_permissions().value).send_messages   # keeps the original permissions


def test_guide_fits_in_one_discord_message():
    worst = how_to_play(123456789012345678, 123456789012345678)
    assert len(worst) <= 2000
    assert "<#123456789012345678>" in worst
    assert "the queue channel" in how_to_play(None)
