"""Ready check: accept / decline when a lobby fills, the decline cooldown, timeouts, and the live message."""
import asyncio
import itertools
import random
from datetime import datetime, timedelta, timezone

import pytest

from bot import config
from bot.models.player import Player
from bot.services import lobby_manager as lm
from bot.services import ready_check as rc
from bot.services.lobby_manager import LobbyError
from bot.ui import embeds

NOW = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)


async def _full(session, real=10, fillers=0, channel="c"):
    for i in range(1, real + 1):
        session.add(Player(discord_id=str(i), discord_username=f"u{i}", riot_puuid=f"puuid-{i}"))
    await session.commit()
    lobby = await lm.create_lobby(session, "42", channel, "1")
    for i in range(1, real + 1):
        await lm.queue_player(session, lobby, str(i))
    if fillers:
        await lm.test_fill(session, lobby, count=fillers, rng=random.Random(1))
    return lobby


# --------------------------------------------------------------------------- #
# The service                                                                  #
# --------------------------------------------------------------------------- #

async def test_begin_asks_real_players_and_fillers_accept_themselves(db):
    async with db() as s:
        lobby = await _full(s, real=3, fillers=7)
        await rc.begin(s, lobby, now=NOW)
        assert rc.running(lobby) and rc._aware(lobby.ready_deadline) == NOW + timedelta(seconds=config.READY_CHECK_SECONDS)
        rows = await rc.rows(s, lobby)
        assert sorted(r.player.discord_id for r in rows if not r.accepted) == ["1", "2", "3"]
        assert not await rc.everyone_accepted(s, lobby)


async def test_only_a_full_waiting_lobby_gets_a_ready_check(db):
    async with db() as s:
        lobby = await _full(s, real=9)
        with pytest.raises(rc.ReadyCheckError):
            await rc.begin(s, lobby)


async def test_accepting(db):
    async with db() as s:
        lobby = await _full(s)
        await rc.begin(s, lobby, now=NOW)
        assert await rc.accept(s, lobby, "1") is True
        assert await rc.accept(s, lobby, "1") is False               # pressing twice is harmless
        with pytest.raises(rc.ReadyCheckError, match="not in this lobby"):
            await rc.accept(s, lobby, "999")
        for i in range(2, 11):
            await rc.accept(s, lobby, str(i))
        assert await rc.everyone_accepted(s, lobby)


async def test_decline_removes_them_with_a_3_minute_cooldown_and_keeps_everyone_else(db, monkeypatch):
    monkeypatch.setattr(config, "READY_CHECK_COOLDOWN_MINUTES", 3)
    async with db() as s:
        lobby = await _full(s)
        await rc.begin(s, lobby, now=NOW)
        await rc.accept(s, lobby, "1")
        p = await rc.decline(s, lobby, "4", now=NOW)
        assert p.discord_id == "4" and not rc.running(lobby)
        assert len(lobby.players) == 9                               # the other nine keep their spots
        assert rc._aware(p.queue_cooldown_until) == NOW + timedelta(minutes=3)
        with pytest.raises(rc.ReadyCheckError):
            await rc.accept(s, lobby, "2")                           # that check is over
        # can't requeue during the cooldown...
        assert rc.cooldown_left(p, NOW + timedelta(minutes=2)) is not None
        assert rc.cooldown_left(p, NOW + timedelta(minutes=3, seconds=1)) is None


async def test_cooldown_blocks_queueing_but_not_a_host_force_queue(db, monkeypatch):
    monkeypatch.setattr(config, "READY_CHECK_COOLDOWN_MINUTES", 3)
    async with db() as s:
        lobby = await _full(s)
        await rc.begin(s, lobby)
        await rc.decline(s, lobby, "4")
        with pytest.raises(LobbyError, match="can't queue until"):
            await lm.queue_player(s, lobby, "4")
        await lm.force_queue(s, lobby, "1", "4")                     # the host's call wins
        assert len(lobby.players) == 10


async def test_no_cooldown_when_set_to_zero_and_never_for_fillers(db, monkeypatch):
    monkeypatch.setattr(config, "READY_CHECK_COOLDOWN_MINUTES", 0)
    async with db() as s:
        lobby = await _full(s)
        await rc.begin(s, lobby)
        p = await rc.decline(s, lobby, "4")
        assert p.queue_cooldown_until is None
        await lm.queue_player(s, lobby, "4")


async def test_timeout_removes_whoever_did_not_accept(db, monkeypatch):
    monkeypatch.setattr(config, "READY_CHECK_COOLDOWN_MINUTES", 3)
    async with db() as s:
        lobby = await _full(s, real=8, fillers=2)
        await rc.begin(s, lobby, now=NOW)
        for i in range(1, 7):
            await rc.accept(s, lobby, str(i))
        assert await rc.due(s, now=NOW + timedelta(seconds=10)) == []
        late = NOW + timedelta(seconds=config.READY_CHECK_SECONDS + 1)
        assert [l.id for l in await rc.due(s, now=late)] == [lobby.id]
        removed = await rc.expire(s, lobby, now=late)
        assert sorted(p.discord_id for p in removed) == ["7", "8"]
        assert all(p.queue_cooldown_until is not None for p in removed)
        assert len(lobby.players) == 8 and not rc.running(lobby)    # six who accepted + two fillers
        assert await rc.expire(s, lobby, now=late) is None           # already over
        assert await rc.due(s, now=late) == []


async def test_expiry_when_everyone_had_accepted_removes_nobody(db):
    async with db() as s:
        lobby = await _full(s)
        await rc.begin(s, lobby, now=NOW)
        for i in range(1, 11):
            await rc.accept(s, lobby, str(i))
        assert await rc.expire(s, lobby, now=NOW + timedelta(hours=1)) == []
        assert len(lobby.players) == 10


async def test_only_one_caller_can_end_a_ready_check(db):
    async with db() as s:
        lobby = await _full(s)
        await rc.begin(s, lobby)
    async with db() as a, db() as b:
        la, lb = await lm.get_active_lobby(a, "42"), await lm.get_active_lobby(b, "42")
        results = await asyncio.gather(rc.end(a, la), rc.end(b, lb))
        assert sorted(results) == [False, True]


async def test_accepts_from_other_sessions_are_seen(db):
    """Each button press has its own session; a stale cache must not hide someone's accept."""
    async with db() as s:
        lobby = await _full(s)
        await rc.begin(s, lobby)
    async with db() as watcher:
        seen = await lm.get_active_lobby(watcher, "42")
        assert not await rc.everyone_accepted(watcher, seen)
        async with db() as other:
            ol = await lm.get_active_lobby(other, "42")
            for i in range(1, 11):
                await rc.accept(other, ol, str(i))
        assert await rc.everyone_accepted(watcher, seen)
        assert all(r.accepted for r in await rc.rows(watcher, seen))


async def test_host_removal_ban_and_cancel_call_off_the_check(db):
    async with db() as s:
        lobby = await _full(s)
        await rc.begin(s, lobby)
        with pytest.raises(LobbyError, match="Decline"):
            await lm.dequeue_player(s, lobby, "3")                   # leaving is a decline, handled by the cog
        target = await lm.force_remove(s, lobby, "1", "3")
        assert not rc.running(lobby)
        assert target.queue_cooldown_until is None                   # the host's removal isn't a penalty
        await lm.queue_player(s, lobby, "3")                         # so they can come straight back
        await rc.begin(s, lobby)
        await lm.remove_banned_player(s, "42", "5")
        assert not rc.running(lobby)
        s.add(Player(discord_id="11", discord_username="u11", riot_puuid="puuid-11"))
        await s.commit()
        await lm.queue_player(s, lobby, "11")
        await rc.begin(s, lobby)
        await lm.cancel_lobby(s, lobby, "1")
        assert lobby.ready_deadline is None


async def test_ready_embed_shows_who_has_accepted(db):
    async with db() as s:
        lobby = await _full(s, real=8, fillers=2)
        await rc.begin(s, lobby, now=NOW)
        await rc.accept(s, lobby, "1")
        e = embeds.ready_embed(lobby, await rc.rows(s, lobby)).to_dict()
        assert "3/10 accepted" in e["description"]                   # player 1 and the two fillers
        assert f"<t:{rc.unix(lobby.ready_deadline)}:R>" in e["description"]   # live countdown
        body = " ".join(f["value"] for f in e["fields"])
        assert "✅ <@1>" in body and "⏳ <@2>" in body and "✅ **Filler" in body
        assert "<@" not in e["title"]                                # mentions don't render in titles
        rows = await rc.rows(s, lobby)
        p2 = next(r.player for r in rows if r.player.discord_id == "2")
        done = embeds.ready_embed(lobby, rows, "declined", "u2 declined.", {p2.id}).to_dict()
        assert "❌ <@2>" in " ".join(f["value"] for f in done["fields"]) and "u2 declined." in done["description"]


# --------------------------------------------------------------------------- #
# The buttons and message, through the real cog                                #
# --------------------------------------------------------------------------- #

_ids = itertools.count(5_000_000)


class Msg:
    def __init__(self, channel, content, embed, view):
        self.id, self.channel = next(_ids), channel
        self.content, self.embed, self.view = content, embed, view
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)
        if "embed" in kw:
            self.embed = kw["embed"]
        if "view" in kw:
            self.view = kw["view"]


class Channel:
    def __init__(self):
        self.id, self.sent = next(_ids), []

    async def send(self, content=None, embed=None, view=None, allowed_mentions=None, **kw):
        m = Msg(self, content, embed, view)
        self.sent.append(m)
        return m

    def get_partial_message(self, mid):
        return next(m for m in self.sent if m.id == mid)


class Bot:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, cid):
        return self.channel if cid == self.channel.id else None

    def get_guild(self, gid):
        return None

    def add_view(self, view):
        pass


class Inter:
    """Just enough of a button press."""
    def __init__(self, user_id, message):
        self.user = type("U", (), {"id": int(user_id), "mention": f"<@{user_id}>"})()
        self.message = message
        self.private = []
        outer = self

        class Response:
            async def defer(self, **kw):
                pass

        class Followup:
            async def send(self, content=None, ephemeral=False, **kw):
                outer.private.append(content)

        self.response, self.followup = Response(), Followup()

    async def edit_original_response(self, embed=None, **kw):
        await self.message.edit(embed=embed)


@pytest.fixture
def cog(monkeypatch):
    from bot.cogs.lobby import LobbyCog
    channel = Channel()
    c = LobbyCog(Bot(channel))
    c.started = []

    async def fake_start(session, lobby, public, random_captains=False):
        c.started.append(lobby.id)
    monkeypatch.setattr(c, "start_lobby", fake_start)
    c.channel = channel
    return c


def _desc(msg):
    return msg.embed.to_dict()["description"]


async def test_full_lobby_pings_everyone_and_updates_as_they_accept(db, cog):
    async with db() as s:
        lobby = await _full(s, real=10, channel=str(cog.channel.id))
        await cog.after_join(s, lobby)
        msg = cog.channel.sent[-1]
        for i in range(1, 11):
            assert f"<@{i}>" in msg.content                         # everyone is pinged once
        assert msg.view is not None and "0/10 accepted" in _desc(msg)
        lobby_id = lobby.id
    for i in range(1, 10):
        await cog.handle_ready_accept(Inter(i, msg))
    assert "9/10 accepted" in _desc(msg) and cog.started == []
    await cog.handle_ready_accept(Inter(10, msg))
    assert cog.started == [lobby_id]                                 # starts by itself
    assert msg.view is None and "Everyone accepted" in msg.embed.title
    note = Inter(3, msg)
    await cog.handle_ready_accept(note)                              # late press on a closed check
    assert note.private == ["This ready check is over."]


async def test_two_last_accepts_at_once_start_the_lobby_once(db, cog):
    async with db() as s:
        lobby = await _full(s, real=10, channel=str(cog.channel.id))
        await cog.after_join(s, lobby)
    msg = cog.channel.sent[-1]
    for i in range(1, 9):
        await cog.handle_ready_accept(Inter(i, msg))
    await asyncio.gather(cog.handle_ready_accept(Inter(9, msg)), cog.handle_ready_accept(Inter(10, msg)))
    assert len(cog.started) == 1


async def test_decline_through_the_button(db, cog, monkeypatch):
    monkeypatch.setattr(config, "READY_CHECK_COOLDOWN_MINUTES", 3)
    async with db() as s:
        lobby = await _full(s, real=10, channel=str(cog.channel.id))
        await cog.after_join(s, lobby)
    msg = cog.channel.sent[-1]
    await cog.handle_ready_accept(Inter(1, msg))
    press = Inter(4, msg)
    await cog.handle_ready_decline(press)
    assert "can't queue until" in press.private[0]
    assert msg.view is None and "failed" in msg.embed.title and "<@4> declined" in _desc(msg)
    assert "back to 9/10" in cog.channel.sent[-1].content
    assert cog.started == []
    async with db() as s:
        lobby = await lm.get_active_lobby(s, "42")
        with pytest.raises(LobbyError, match="can't queue until"):
            await lm.queue_player(s, lobby, "4")
        # someone new fills the spot: a fresh ready check, and everyone accepts again
        s.add(Player(discord_id="11", discord_username="u11", riot_puuid="puuid-11"))
        await s.commit()
        await lm.queue_player(s, lobby, "11")
        await cog.after_join(s, lobby)
    again = cog.channel.sent[-1]
    assert again is not msg and "0/10 accepted" in _desc(again)


async def test_timeout_through_the_background_loop(db, cog):
    async with db() as s:
        lobby = await _full(s, real=10, channel=str(cog.channel.id))
        await cog.after_join(s, lobby)
    msg = cog.channel.sent[-1]
    for i in range(1, 9):
        await cog.handle_ready_accept(Inter(i, msg))
    async with db() as s:
        lobby = await lm.get_active_lobby(s, "42")
        lobby.ready_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        await s.commit()
    await cog.ready_check_timeouts.coro(cog)
    assert msg.view is None and "timed out" in msg.embed.title
    assert "<@9>" in _desc(msg) and "<@10>" in _desc(msg)
    assert "back to 8/10" in cog.channel.sent[-1].content
    async with db() as s:
        assert len((await lm.get_active_lobby(s, "42")).players) == 8


async def test_fillers_only_lobby_starts_straight_away(db, cog):
    async with db() as s:
        lobby = await _full(s, real=0, fillers=10, channel=str(cog.channel.id))
        await cog.after_join(s, lobby)
        assert cog.started == [lobby.id] and cog.channel.sent == []


async def test_ready_check_can_be_turned_off(db, cog, monkeypatch):
    monkeypatch.setattr(config, "READY_CHECK_ENABLED", False)
    async with db() as s:
        lobby = await _full(s, real=10, channel=str(cog.channel.id))
        await cog.after_join(s, lobby)
        assert not rc.running(lobby)
        assert "is full" in cog.channel.sent[-1].content and "<@1>" in cog.channel.sent[-1].content
