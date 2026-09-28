from datetime import datetime, timedelta, timezone

import pytest

from bot.models.lobby import Lobby, LobbyPlayer
from bot.models.player import Player
from bot.services import lobby_manager as lm
from bot.services.lobby_manager import LobbyError
from bot.services import game_processor
from bot.services import auto_detect
from tests.fixtures import make_match


async def _players(session, n=10):
    out = []
    for i in range(1, n + 1):
        p = Player(discord_id=str(i), discord_username=f"u{i}", riot_puuid=f"puuid-{i}")
        session.add(p); out.append(p)
    await session.commit()
    return out


async def test_queue_and_balanced_teams(db):
    async with db() as session:
        players = await _players(session)
        # give a spread of MMR
        for i, p in enumerate(players):
            r = await game_processor.get_or_create_rating(session, p.id, "OVERALL", 1)
            r.mmr = 1300 + i * 60
        await session.commit()
        lobby = await lm.create_lobby(session, "g", "c", "1", mode="balanced")
        prefs = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"] * 2
        for p, pref in zip(players, prefs):
            await lm.queue_player(session, lobby, p.discord_id, pref)
        with pytest.raises(LobbyError):
            await lm.queue_player(session, lobby, "1", "TOP")     # duplicate
        with pytest.raises(LobbyError):
            await lm.create_lobby(session, "g", "c", "1")          # one per channel
        t1, t2 = await lm.make_teams(session, lobby)
        assert len(t1) == len(t2) == 5 and lobby.status == "active"
        ratings = await lm.lobby_ratings(session, lobby)
        gap = abs(sum(ratings[x.player_id]["mmr"] for x in t1) - sum(ratings[x.player_id]["mmr"] for x in t2)) / 5
        assert gap < 70   # role pairs differ by 300 MMR, so 60 is the optimum with every preference honoured
        for team in (t1, t2):
            assert sorted(x.assigned_role for x in team) == sorted(lm.ROLES)
            assert all(x.assigned_role == x.preferred_role for x in team)   # every preference honoured


async def test_pick_order_is_not_role_queue(db):
    """Pick order ignores role preferences entirely and hands out pick positions 1-5."""
    async with db() as session:
        players = await _players(session)
        for i, p in enumerate(players):
            r = await game_processor.get_or_create_rating(session, p.id, "OVERALL", 1)
            r.mmr = 1300 + i * 60
        await session.commit()
        lobby = await lm.create_lobby(session, "g", "c", "host-999")          # default mode
        assert lobby.mode == "pick_order" and lobby.ranked is True
        for p in players:
            lp = await lm.queue_player(session, lobby, p.discord_id, "MIDDLE", "TOP")
            assert lp.preferred_role is None and lp.secondary_role is None    # no role queue
        t1, t2 = await lm.make_teams(session, lobby)
        for team in (t1, t2):
            assert sorted(x.pick_order for x in team) == [1, 2, 3, 4, 5]
            assert all(x.assigned_role is None for x in team)
        ratings = await lm.lobby_ratings(session, lobby)
        gap = abs(sum(ratings[x.player_id]["mmr"] for x in t1) - sum(ratings[x.player_id]["mmr"] for x in t2)) / 5
        assert gap <= 10 + lm.PICK_ORDER_GAP_TOLERANCE
        lines = lm.team_lines(t1, {x.player_id: "n" for x in t1}, "pick_order")
        assert lines[0].startswith("**Pick 1**")
        someone = await session.get(Player, t1[0].player_id)
        with pytest.raises(LobbyError):
            await lm.set_role(session, lobby, "host-999", someone.discord_id, "TOP", is_admin=True)


def test_pick_priority_rotation():
    import random
    rng = random.Random(0)
    history = {1: [5, 5, 4], 2: [1, 1, 2], 3: [], 4: [3, 3], 5: [4, 5]}
    order = lm.pick_priority([1, 2, 3, 4, 5], history, rng)
    assert order[0] == 1          # most late picks recently -> picks first now
    assert order[-1] == 2         # most early picks recently -> picks last now
    assert order.index(3) in (1, 2, 3)   # newcomer sits in the middle
    # only the most recent PICK_HISTORY_LIMIT games count
    old_luck = {7: [5] * 50 + [1] * lm.PICK_HISTORY_LIMIT, 8: []}
    assert lm.pick_priority([7, 8], old_luck, rng)[0] == 8


async def test_pick_history_feeds_next_lobby(db):
    """Late picks in a completed pick-order lobby earn early picks next time; captain picks are ignored."""
    async with db() as session:
        players = await _players(session)
        done = await lm.create_lobby(session, "g", "c1", "h", mode="pick_order")
        for p in players:
            await lm.queue_player(session, done, p.discord_id)
        await lm.make_teams(session, done)
        unlucky = {lp.player_id for lp in done.players if lp.pick_order == 5}
        done.status = "completed"
        cap = await lm.create_lobby(session, "g", "c2", "h", mode="captain")   # captain pick indexes must not count
        for p in players[:2]:
            session.add(LobbyPlayer(lobby_id=cap.id, player_id=p.id, pick_order=-1))
        cap.status = "completed"
        await session.commit()
        hist = await lm.pick_history(session, [p.id for p in players])
        assert all(-1 not in v for v in hist.values())
        nxt = await lm.create_lobby(session, "g", "c3", "h")
        for p in players:
            await lm.queue_player(session, nxt, p.discord_id)
        await lm.make_teams(session, nxt)
        for lp in nxt.players:
            if lp.player_id in unlucky:
                assert lp.pick_order <= 2, "last pick last time should mean an early pick now"


async def test_captain_draft_flow_and_role_swap(db):
    async with db() as session:
        players = await _players(session)
        lobby = await lm.create_lobby(session, "g", "c", "host-999", mode="captain")
        for p in players:
            await lm.queue_player(session, lobby, p.discord_id)
        cap1, cap2 = await lm.start_captain_draft(session, lobby, random_captains=True)
        assert lobby.status == "drafting"
        with pytest.raises(LobbyError):
            await lm.captain_pick(session, lobby, cap2.discord_id, players[0].discord_id)  # not cap2's turn
        pool = list(lobby.draft_state["pool"])
        order = lobby.draft_state["pick_order"]
        for turn, pid in zip(order, pool):
            cap = cap1 if turn == 1 else cap2
            target = next(p for p in players if p.id == pid)
            await lm.captain_pick(session, lobby, cap.discord_id, target.discord_id)
        assert lobby.status == "active"
        t1 = [lp for lp in lobby.players if lp.team == 1]
        t2 = [lp for lp in lobby.players if lp.team == 2]
        assert len(t1) == len(t2) == 5
        assert lobby.team1_name.startswith("Team ")
        # captain moves someone to TOP; previous top holder swaps
        someone = next(lp for lp in t1 if lp.assigned_role != "TOP")
        old_top = next(lp for lp in t1 if lp.assigned_role == "TOP")
        old_role = someone.assigned_role
        target = await session.get(Player, someone.player_id)
        await lm.set_role(session, lobby, cap1.discord_id, target.discord_id, "TOP")
        assert someone.assigned_role == "TOP" and old_top.assigned_role == old_role
        # non-captain cannot
        outsider = await session.get(Player, t2[0].player_id)
        with pytest.raises(LobbyError):
            await lm.set_role(session, lobby, outsider.discord_id, target.discord_id, "MIDDLE")


async def test_auto_detect_match_belongs(db):
    match, _ = make_match(queue_id=0)
    lobby_puuids = {f"puuid-{i}" for i in range(1, 11)}
    assert auto_detect.match_belongs_to_lobby(match, lobby_puuids, 8)
    assert not auto_detect.match_belongs_to_lobby(match, {"puuid-1", "puuid-2"}, 8)
    ranked, _ = make_match(queue_id=420)
    assert not auto_detect.match_belongs_to_lobby(ranked, lobby_puuids, 8)


async def test_auto_detect_poll_processes_lobby_game(db, monkeypatch):
    """Fake Riot client: the active lobby's game shows up and is processed automatically."""
    match, tl = make_match("NA1_auto", queue_id=0)

    class FakeRiot:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get_recent_match_ids(self, puuid, queue=None, count=1, match_type=None, start_time=None):
            return ["NA1_auto"]
        async def get_match(self, mid): return match
        async def get_match_timeline(self, mid): return tl

    monkeypatch.setattr(auto_detect, "RiotClient", FakeRiot)
    async with db() as session:
        players = await _players(session)
        lobby = await lm.create_lobby(session, "g", "c", "1", mode="balanced")
        for p in players:
            await lm.queue_player(session, lobby, p.discord_id)
        await lm.make_teams(session, lobby)
    processed = await auto_detect.poll_once(db)
    assert len(processed) == 1
    lobby, result = processed[0]
    assert result.game.riot_match_id == "NA1_auto" and result.game.auto_detected
    async with db() as session:
        fresh = await session.get(Lobby, lobby.id)
        assert fresh.status == "completed"
    assert await auto_detect.poll_once(db) == []   # nothing active anymore


# --- queue channel restriction -------------------------------------------

def test_wrong_channel_message_pure():
    from bot.services.settings import wrong_channel_message as w
    assert w(0, 999) is None              # not configured: allowed anywhere
    assert w(123, 123) is None            # right channel
    msg = w(123, 999)                     # wrong channel
    assert msg is not None and "<#123>" in msg
    assert "/rank" in msg                 # tells them what still works elsewhere


async def test_queue_channel_setting_roundtrip(db):
    from bot.services import settings
    async with db() as session:
        assert await settings.queue_channel_id(session, "g") == 0          # default: anywhere
        await settings.set_setting(session, "g", settings.KEY_QUEUE_CHANNEL, "555")
        assert await settings.queue_channel_id(session, "g") == 555
        assert settings.wrong_channel_message(555, 555) is None
        assert settings.wrong_channel_message(555, 42) is not None
        await settings.set_setting(session, "g", settings.KEY_QUEUE_CHANNEL, "")   # cleared
        assert await settings.queue_channel_id(session, "g") == 0


# --- force queue / force remove --------------------------------------------

async def test_host_can_force_queue_and_remove(db):
    async with db() as session:
        players = await _players(session)
        lobby = await lm.create_lobby(session, "g", "c", players[0].discord_id)          # player 1 hosts
        await lm.force_queue(session, lobby, players[0].discord_id, players[4].discord_id)
        assert any(lp.player_id == players[4].id for lp in lobby.players)
        with pytest.raises(LobbyError, match="already in the queue"):
            await lm.force_queue(session, lobby, players[0].discord_id, players[4].discord_id)
        with pytest.raises(LobbyError, match="host or an admin"):                         # not the host
            await lm.force_queue(session, lobby, players[2].discord_id, players[5].discord_id)
        await lm.force_queue(session, lobby, players[2].discord_id, players[5].discord_id, is_admin=True)
        removed = await lm.force_remove(session, lobby, players[0].discord_id, players[4].discord_id)
        assert removed.id == players[4].id and not any(lp.player_id == players[4].id for lp in lobby.players)
        with pytest.raises(LobbyError, match="not in this lobby"):
            await lm.force_remove(session, lobby, players[0].discord_id, players[4].discord_id)


async def test_force_queue_needs_a_linked_account_and_room(db):
    async with db() as session:
        players = await _players(session)
        session.add(Player(discord_id="999", discord_username="nolink"))                  # never ran /link
        await session.commit()
        lobby = await lm.create_lobby(session, "g", "c", "host")
        with pytest.raises(LobbyError, match="hasn't linked"):
            await lm.force_queue(session, lobby, "host", "999")
        with pytest.raises(LobbyError, match="hasn't linked"):
            await lm.force_queue(session, lobby, "host", "12345")                         # not even registered
        for p in players:
            await lm.force_queue(session, lobby, "host", p.discord_id)
        session.add(Player(discord_id="11", discord_username="u11", riot_puuid="puuid-11"))
        await session.commit()
        with pytest.raises(LobbyError, match="full"):
            await lm.force_queue(session, lobby, "host", "11")
        await lm.make_teams(session, lobby)
        with pytest.raises(LobbyError, match="Teams are already made"):
            await lm.force_remove(session, lobby, "host", players[0].discord_id)
