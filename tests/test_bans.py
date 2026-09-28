"""Queue bans: parsing, every way into a game is blocked, expiry, lifting, history."""
from datetime import datetime, timedelta, timezone

import pytest

from bot.models.ban import QueueBan
from bot.models.player import Player
from bot.services import bans
from bot.services import lobby_manager as lm
from bot.services.lobby_manager import LobbyError


@pytest.mark.parametrize("text, expected", [
    ("30m", timedelta(minutes=30)), ("12h", timedelta(hours=12)), ("3d", timedelta(days=3)),
    ("1w", timedelta(weeks=1)), ("1w 2d", timedelta(days=9)), ("1 week and 2 days", timedelta(days=9)),
    ("90 minutes", timedelta(minutes=90)), ("2 Weeks", timedelta(weeks=2)),
    ("permanent", None), ("Perm", None), ("forever", None),
])
def test_parse_duration(text, expected):
    assert bans.parse_duration(text) == expected


@pytest.mark.parametrize("bad", ["", "soon", "0d", "3x", "400d", "3d banana", "-2d"])
def test_parse_duration_rejects(bad):
    with pytest.raises(ValueError):
        bans.parse_duration(bad)


def test_format_duration():
    assert bans.format_duration(timedelta(days=9)) == "1 week 2 days"
    assert bans.format_duration(timedelta(minutes=90)) == "1 hour 30 minutes"
    assert bans.format_duration(None) == "permanently"


async def _setup(session, n=10):
    players = []
    for i in range(1, n + 1):
        p = Player(discord_id=str(i), discord_username=f"u{i}", riot_puuid=f"puuid-{i}")
        session.add(p)
        players.append(p)
    await session.commit()
    return players


async def test_banned_player_cannot_get_into_a_game_any_way(db):
    async with db() as s:
        players = await _setup(s)
        await bans.ban(s, "g", "5", "u5", timedelta(days=3), "toxic in chat", "mod")
        lobby = await lm.create_lobby(s, "g", "c", "1")
        with pytest.raises(LobbyError, match="You're banned from inhouses for another 3 days"):
            await lm.queue_player(s, lobby, "5")                            # Join button and /queue
        with pytest.raises(LobbyError, match="u5 is banned.*queueban lift"):
            await lm.force_queue(s, lobby, "1", "5")                        # host force-queue
        with pytest.raises(LobbyError, match="can't host"):
            await lm.create_lobby(s, "g", "c2", "5")                        # hosting
        with pytest.raises(LobbyError, match="toxic in chat"):              # the reason is shown
            await lm.queue_player(s, lobby, "5")
        await lm.queue_player(s, lobby, "6")                                # others unaffected


async def test_unlinked_people_can_be_banned_too(db):
    async with db() as s:
        await bans.ban(s, "g", "999", "never-linked", None, "ban evasion", "mod")
        lobby = await lm.create_lobby(s, "g", "c", "host")
        with pytest.raises(LobbyError, match="permanently banned"):
            await lm.queue_player(s, lobby, "999")                          # banned message wins over "link first"


async def test_ban_expires_on_its_own(db):
    async with db() as s:
        await _setup(s)
        past = datetime.now(timezone.utc) - timedelta(days=2)
        await bans.ban(s, "g", "5", "u5", timedelta(days=1), "was late", "mod", now=past)   # ended yesterday
        assert await bans.active_ban(s, "g", "5") is None
        lobby = await lm.create_lobby(s, "g", "c", "1")
        await lm.queue_player(s, lobby, "5")


async def test_lift_and_replace_and_history(db):
    async with db() as s:
        await _setup(s)
        first, replaced = await bans.ban(s, "g", "5", "u5", timedelta(days=1), "first", "modA")
        assert replaced is None
        second, replaced = await bans.ban(s, "g", "5", "u5", timedelta(days=7), "second", "modB")
        assert replaced.id == first.id and replaced.lift_reason == "Replaced by a new ban"
        assert (await bans.active_ban(s, "g", "5")).id == second.id         # only one active ban
        assert [b.reason for b in await bans.list_active(s, "g")] == ["second"]
        lifted = await bans.lift(s, "g", "5", "modC", "appeal accepted")
        assert lifted.id == second.id and lifted.lifted_by == "modC"
        assert await bans.active_ban(s, "g", "5") is None
        assert await bans.lift(s, "g", "5", "modC") is None                 # nothing left to lift
        assert [b.reason for b in await bans.history(s, "g", "5")] == ["second", "first"]
        lobby = await lm.create_lobby(s, "g", "c", "1")
        await lm.queue_player(s, lobby, "5")                                # free to play again


async def test_bans_are_per_server(db):
    async with db() as s:
        await _setup(s)
        await bans.ban(s, "guild-A", "5", "u5", None, "x", "mod")
        lobby = await lm.create_lobby(s, "guild-B", "c", "1")
        await lm.queue_player(s, lobby, "5")                                # banned in A, fine in B


async def test_ban_removes_player_from_a_filling_lobby_but_not_a_started_game(db):
    async with db() as s:
        players = await _setup(s)
        filling = await lm.create_lobby(s, "g", "c1", "1")
        await lm.queue_player(s, filling, "5")
        await bans.ban(s, "g", "5", "u5", timedelta(hours=1), "afk", "mod")
        removed, started = await lm.remove_banned_player(s, "g", "5")
        assert removed.id == filling.id and started is None
        assert not any(lp.player_id == players[4].id for lp in filling.players)

        await bans.lift(s, "g", "5", "mod")
        game = await lm.create_lobby(s, "g", "c2", "1")
        for p in players:
            await lm.queue_player(s, game, p.discord_id)
        await lm.make_teams(s, game)
        await bans.ban(s, "g", "5", "u5", timedelta(hours=1), "afk again", "mod")
        removed, started = await lm.remove_banned_player(s, "g", "5")
        assert removed is None and started.id == game.id                    # a game in progress is left alone
        assert any(lp.player_id == players[4].id for lp in game.players)


def test_player_message_and_timestamps():
    now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    b = QueueBan(guild_id="g", discord_id="5", username="u5", reason="toxic", banned_by="mod",
                 created_at=now, expires_at=now + timedelta(days=3))
    msg = bans.player_message(b, now)
    assert "another 3 days" in msg and "<t:" in msg and "toxic" in msg
    perm = QueueBan(guild_id="g", discord_id="5", username="u5", reason="cheating", banned_by="mod",
                    created_at=now, expires_at=None)
    assert "permanently banned" in bans.player_message(perm, now)
    assert "u5 is" in bans.player_message(perm, now, you=False)
