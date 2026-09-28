"""Private #game-N channels: who can see them, join details, privacy, and cleanup."""
import random
from datetime import datetime, timedelta, timezone

import discord
import pytest
from sqlalchemy import select

from bot import config
from bot.models.lobby import Lobby
from bot.models.player import Player
from bot.services import game_channel as gc
from bot.services import lobby_manager as lm
from bot.services import settings
from bot.services.server_setup import REMEMBER_PREFIX
from bot.ui import embeds
from tests.test_server_setup import FakeChannel, FakeGuild, FakeRole


class Member:
    def __init__(self, uid):
        self.id = uid


class GameGuild(FakeGuild):
    """FakeGuild plus members, so channel permissions can name real people."""
    def __init__(self, member_ids=(), **kw):
        super().__init__(**kw)
        self.members = {int(m): Member(int(m)) for m in member_ids}

    def get_member(self, uid):
        return self.members.get(uid)


async def _lobby(session, mode="pick_order", host="1", real=10, fillers=0):
    for i in range(1, real + 1):
        session.add(Player(discord_id=str(i), discord_username=f"u{i}", riot_puuid=f"puuid-{i}"))
    await session.commit()
    lobby = await lm.create_lobby(session, "42", "c", host, mode=mode)
    for i in range(1, real + 1):
        await lm.queue_player(session, lobby, str(i))
    if fillers:
        await lm.test_fill(session, lobby, count=fillers, rng=random.Random(1))
    return lobby


# --------------------------------------------------------------------------- #

async def test_channel_is_private_to_the_lobby_and_staff(db):
    guild = GameGuild(member_ids=[str(i) for i in range(1, 12)])
    mod = FakeRole("Inhouse Mod")
    staff = FakeRole("Staff", discord.Permissions(manage_guild=True))
    random_role = FakeRole("Members")
    guild.roles += [mod, staff, random_role]
    cat = FakeChannel("INHOUSES", "category")
    guild.channels.append(cat)
    async with db() as s:
        await settings.set_setting(s, "42", REMEMBER_PREFIX + "mod_role", str(mod.id))
        await settings.set_setting(s, "42", REMEMBER_PREFIX + "cat_inhouse", str(cat.id))
        lobby = await _lobby(s, host="11")                       # host 11 is organising, not playing
        await lm.make_teams(s, lobby)
        ch = await gc.create(guild, s, lobby)
        assert ch.name == f"game-{lobby.id}" and ch.category is cat
        assert lobby.game_channel_id == str(ch.id)
        ow = ch.overwrites
        assert ow[guild.default_role].view_channel is False      # hidden from everyone else
        assert ow[guild.me].view_channel is True                 # the bot keeps access
        for uid in range(1, 12):                                 # all ten players and the host
            assert ow[guild.members[uid]].view_channel is True
        assert ow[mod].view_channel and ow[staff].view_channel   # moderators and staff
        assert random_role not in ow
        assert await gc.create(guild, s, lobby) is ch            # never makes a second one


async def test_fillers_and_missing_permissions(db):
    async with db() as s:
        lobby = await _lobby(s, real=3, fillers=7)
        await lm.make_teams(s, lobby)
        guild = GameGuild(member_ids=["1", "2", "3"])
        ch = await gc.create(guild, s, lobby)
        people = [t for t in ch.overwrites if isinstance(t, Member)]
        assert sorted(m.id for m in people) == [1, 2, 3]          # fillers have no Discord account
        lobby.game_channel_id = None
        no_perms = GameGuild(perms=discord.Permissions(117824))   # the basic invite: can't make channels
        assert await gc.create(no_perms, s, lobby) is None


async def test_game_channels_can_be_turned_off(db, monkeypatch):
    monkeypatch.setattr(config, "GAME_CHANNELS_ENABLED", False)
    async with db() as s:
        lobby = await _lobby(s)
        assert await gc.create(GameGuild(), s, lobby) is None


# --------------------------------------------------------------------------- #

async def test_lobby_name_and_password_without_a_tournament_key(db, monkeypatch):
    monkeypatch.setattr(config, "RIOT_TOURNAMENT_API_KEY", "")
    async with db() as s:
        lobby = await _lobby(s, host="3")
        await lm.make_teams(s, lobby)
        info = await gc.prepare_join(s, lobby, riot=None, rng=random.Random(5))
        assert info.code is None and info.creator.discord_id == "3"          # the host makes the lobby
        assert info.name.startswith(f"Inhouse {lobby.id}-")
        assert len(info.password) == 6 and set(info.password) <= set(gc.PASSWORD_CHARS)
        again = await gc.prepare_join(s, lobby, riot=None, rng=random.Random(99))
        assert (again.name, again.password) == (info.name, info.password)   # stable once posted


async def test_creator_is_blue_first_pick_when_host_is_not_playing(db, monkeypatch):
    monkeypatch.setattr(config, "RIOT_TOURNAMENT_API_KEY", "")
    async with db() as s:
        lobby = await _lobby(s, host="organiser")
        await lm.make_teams(s, lobby)
        info = await gc.prepare_join(s, lobby)
        blue_pick1 = next(lp for lp in lobby.players if lp.team == 1 and lp.pick_order == 1)
        assert info.creator.id == blue_pick1.player_id


class FakeTournamentRiot:
    def __init__(self, fail=False):
        self.fail, self.calls, self.codes = fail, [], []

    async def create_tournament_provider(self, region, url):
        self.calls.append(("provider", region)); return 11

    async def create_tournament(self, provider_id, name):
        self.calls.append(("tournament", provider_id)); return 22

    async def create_tournament_code(self, tournament_id, allowed_participants=None, metadata=""):
        if self.fail:
            raise RuntimeError("403 Forbidden")
        self.codes.append((tournament_id, list(allowed_participants), metadata))
        return f"NA04-CODE-{len(self.codes)}"


async def test_tournament_code_when_a_tournament_key_is_set(db, monkeypatch):
    monkeypatch.setattr(config, "RIOT_TOURNAMENT_API_KEY", "RGAPI-tournament")
    riot = FakeTournamentRiot()
    async with db() as s:
        lobby = await _lobby(s, real=4, fillers=6)
        await lm.make_teams(s, lobby)
        info = await gc.prepare_join(s, lobby, riot)
        assert info.code == "NA04-CODE-1" and lobby.tournament_code == "NA04-CODE-1"
        tid, allowed, meta = riot.codes[0]
        assert tid == 22 and sorted(allowed) == [f"puuid-{i}" for i in range(1, 5)]   # no fillers
        assert meta == f"lobby:{lobby.id}"
        # provider and tournament are made once per server and reused
        lobby.status = "completed"
        await s.commit()
        nxt = await lm.create_lobby(s, "42", "c2", "1")
        for i in range(1, 11):
            await lm.queue_player(s, nxt, str(i)) if i <= 4 else None
        await gc.prepare_join(s, nxt, riot)
        assert [c for c in riot.calls if c[0] == "provider"] == [("provider", "NA")]
        assert len([c for c in riot.calls if c[0] == "tournament"]) == 1


async def test_falls_back_to_password_if_the_tournament_api_refuses(db, monkeypatch):
    monkeypatch.setattr(config, "RIOT_TOURNAMENT_API_KEY", "RGAPI-no-access")
    async with db() as s:
        lobby = await _lobby(s)
        await lm.make_teams(s, lobby)
        info = await gc.prepare_join(s, lobby, FakeTournamentRiot(fail=True))
        assert info.code is None and info.password and "couldn't be made" in info.note


async def test_tournament_code_request_uses_v5_fields(monkeypatch):
    from bot.services import riot_api as ra
    sent = {}

    async def fake_post(self, host, path, payload, api_key=None):
        sent.update(path=path, payload=payload)
        return ["CODE"]
    monkeypatch.setattr(ra.RiotClient, "_post", fake_post)
    await ra.RiotClient(object()).create_tournament_code(22, allowed_participants=[f"p{i}" for i in range(10)])
    assert sent["payload"]["allowedParticipants"] == [f"p{i}" for i in range(10)]
    assert sent["payload"]["enoughPlayers"] is True and "allowedSummonerIds" not in sent["payload"]
    await ra.RiotClient(object()).create_tournament_code(22, allowed_participants=["p1", "p2"])
    assert sent["payload"]["enoughPlayers"] is False


# --------------------------------------------------------------------------- #

async def test_secrets_never_appear_in_the_public_teams_post(db, monkeypatch):
    monkeypatch.setattr(config, "RIOT_TOURNAMENT_API_KEY", "")
    async with db() as s:
        lobby = await _lobby(s)
        await lm.make_teams(s, lobby)
        await gc.prepare_join(s, lobby)
        lobby.tournament_code = "NA04-SECRET-CODE"
        lobby.drafter_links = {"blue": "https://drafter.lol/x/blue", "red": "https://drafter.lol/x/red"}
        names = {lp.player_id: f"n{lp.player_id}" for lp in lobby.players}
        public = str(embeds.teams_embed(lobby, names).to_dict())
        for secret in (lobby.join_password, "NA04-SECRET-CODE", "drafter.lol"):
            assert secret not in public, f"{secret} leaked into the public teams post"
        private = str(embeds.join_embed(lobby, "u1").to_dict())
        assert "NA04-SECRET-CODE" in private and "drafter.lol/x/blue" in private
        lobby.tournament_code = None
        private = str(embeds.join_embed(lobby, "u1").to_dict())
        assert lobby.join_password in private and lobby.join_name in private and "u1" in private


async def test_commands_find_the_lobby_from_its_private_channel(db):
    async with db() as s:
        lobby = await _lobby(s)
        lobby.game_channel_id = "555"
        await s.commit()
        assert (await gc.lobby_for_channel(s, "42", 555)).id == lobby.id
        assert await gc.lobby_for_channel(s, "42", 777) is None
        assert await gc.lobby_for_channel(s, "other-guild", 555) is None
        lobby.status = "completed"
        await s.commit()
        assert await gc.lobby_for_channel(s, "42", 555) is None   # finished games are read-only


async def test_cleanup_timing(db, monkeypatch):
    monkeypatch.setattr(config, "GAME_CHANNEL_CLEANUP_MINUTES", 15)
    async with db() as s:
        lobby = await _lobby(s)
        lobby.game_channel_id = "555"
        lobby.status = "completed"
        await s.commit()
        await s.refresh(lobby)
        ended = gc._aware(lobby.updated_at)
        assert await gc.due_for_cleanup(s, now=ended + timedelta(minutes=5)) == []
        due = await gc.due_for_cleanup(s, now=ended + timedelta(minutes=16))
        assert [l.id for l in due] == [lobby.id]
        monkeypatch.setattr(config, "GAME_CHANNEL_CLEANUP_MINUTES", 0)          # 0 keeps channels
        assert await gc.due_for_cleanup(s, now=ended + timedelta(days=5)) == []
        monkeypatch.setattr(config, "GAME_CHANNEL_CLEANUP_MINUTES", 15)
        lobby.status = "active"                                                 # a live game is never removed
        await s.commit()
        assert await gc.due_for_cleanup(s, now=ended + timedelta(days=5)) == []
