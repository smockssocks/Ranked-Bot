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


async def test_pick_order_roles_first_come_first_serve(db):
    async with db() as session:
        players = await _players(session)
        lobby = await lm.create_lobby(session, "g", "c", "host-999", mode="pick_order")
        base = datetime.now(timezone.utc)
        for i, p in enumerate(players):
            lp = await lm.queue_player(session, lobby, p.discord_id, "MIDDLE")   # everyone wants mid
            lp.joined_at = base + timedelta(seconds=i)
        await session.commit()
        t1, t2 = await lm.make_teams(session, lobby)
        for team in (t1, t2):
            first = min(team, key=lambda x: lm._aware(x.joined_at))
            assert first.assigned_role == "MIDDLE"
            assert sorted(x.assigned_role for x in team) == sorted(lm.ROLES)


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
