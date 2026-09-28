"""Each game's private thread and team voice: who can see and join, join details, privacy, cleanup."""
import random
from datetime import timedelta
from types import SimpleNamespace

import discord
import pytest

from bot import config
from bot.models.player import Player
from bot.services import game_channel as gc
from bot.services import lobby_manager as lm
from bot.services import server_setup as ss
from bot.services import settings
from bot.services.server_setup import REMEMBER_PREFIX
from bot.ui import embeds
from tests.test_server_setup import FakeChannel, FakeGuild, FakeRole, full_perms


def _http(status):
    resp = SimpleNamespace(status=status, reason="x")
    return {404: discord.NotFound, 403: discord.Forbidden}.get(status, discord.HTTPException)(resp, "x")


class Member:
    def __init__(self, uid, name=None, perms=None):
        self.id, self.display_name = uid, name or f"user{uid}"
        self.guild_permissions = perms or discord.Permissions.none()
        self.voice = None
        self.bot = False

    @property
    def mention(self):
        return f"<@{self.id}>"

    async def move_to(self, channel, reason=None):
        if self.voice and self.voice.channel is not None and self in self.voice.channel.members:
            self.voice.channel.members.remove(self)
        self.voice = SimpleNamespace(channel=channel)
        channel.members.append(self)


class Chan(FakeChannel):
    """A text or voice channel that can hold threads, members and permission checks."""
    def __init__(self, guild, *a, **kw):
        super().__init__(*a, **kw)
        self.guild, self.members, self.deleted, self.fail = guild, [], False, None

    mention = property(lambda self: f"<#{self.id}>")

    def overwrites_for(self, target):
        return self.overwrites.get(target, discord.PermissionOverwrite())

    def permissions_for(self, member):
        base = discord.Permissions(member.guild_permissions.value)
        if base.administrator:
            return discord.Permissions.all()
        for target in (self.guild.default_role, member):
            allow, deny = self.overwrites_for(target).pair()
            base.value = (base.value & ~deny.value) | allow.value
        return base

    async def set_permissions(self, target, overwrite=None, reason=None):
        self.overwrites[target] = overwrite

    async def create_thread(self, name, type=None, invitable=True, auto_archive_duration=None, reason=None):
        t = Thread(self, name, type, invitable)
        self.guild.threads.append(t)
        return t

    async def edit(self, overwrites=None, reason=None):
        self.overwrites = dict(overwrites)

    async def delete(self, reason=None):
        if self.fail:
            raise _http(self.fail)
        self.deleted = True
        self.guild.channels.remove(self)


class Thread:
    def __init__(self, parent, name, type_, invitable):
        self.id, self.parent, self.name, self.type, self.invitable = next_id(), parent, name, type_, invitable
        self.added, self.archived, self.locked, self.sent = [], False, False, []

    mention = property(lambda self: f"<#{self.id}>")

    def permissions_for(self, member):
        return self.parent.permissions_for(member)

    async def add_user(self, user):
        self.added.append(user)

    async def edit(self, archived=None, locked=None):
        self.archived, self.locked = bool(archived), bool(locked)

    async def send(self, content=None, **kw):
        self.sent.append(content)


_n = iter(range(900_000, 10**9))


def next_id():
    return next(_n)


class GameGuild(FakeGuild):
    """FakeGuild plus members, threads and voice, so a whole game space can be built."""
    def __init__(self, member_ids=(), **kw):
        super().__init__(**kw)
        self.members = {int(m): Member(int(m)) for m in member_ids}
        self.threads, self.afk_channel = [], None

    def get_member(self, uid):
        return self.members.get(uid)

    def get_channel_or_thread(self, cid):
        return self.get_channel(cid) or next((t for t in self.threads if t.id == cid), None)

    async def fetch_channel(self, cid):
        raise _http(404)

    def text(self, name, category=None, overwrites=None):
        return self._add(Chan(self, name, "text", category, overwrites))

    async def create_voice_channel(self, name, category=None, overwrites=None, reason=None):
        return self._add(Chan(self, name, "voice", category, overwrites))


async def _lobby(session, guild=None, mode="pick_order", host="1", real=10, fillers=0):
    queue = guild.text("inhouse-queue") if guild is not None else None
    for i in range(1, real + 1):
        session.add(Player(discord_id=str(i), discord_username=f"u{i}", riot_puuid=f"puuid-{i}"))
    await session.commit()
    lobby = await lm.create_lobby(session, "42", str(queue.id) if queue else "c", host, mode=mode)
    for i in range(1, real + 1):
        await lm.queue_player(session, lobby, str(i))
    if fillers:
        await lm.test_fill(session, lobby, count=fillers, rng=random.Random(1))
    return lobby


# --------------------------------------------------------------------------- #
# The private thread                                                           #
# --------------------------------------------------------------------------- #

async def test_private_thread_under_the_queue_channel_named_after_the_host(db):
    guild = GameGuild(member_ids=[str(i) for i in range(1, 12)])
    guild.members[11].display_name = "Danman"
    mod = FakeRole("Inhouse Mod")
    mod.members = [Member(50), Member(51, perms=discord.Permissions(manage_threads=True))]
    guild.roles.append(mod)
    async with db() as s:
        await settings.set_setting(s, "42", REMEMBER_PREFIX + "mod_role", str(mod.id))
        lobby = await _lobby(s, guild, host="11")                 # host 11 is organising, not playing
        await lm.make_teams(s, lobby)
        t = await gc.create(guild, s, lobby)
        assert t.parent.name == "inhouse-queue" and t.name == "Danman's inhouse"   # no numbers
        assert t.type == discord.ChannelType.private_thread
        assert t.invitable is False                                # players can't pull friends in
        assert lobby.game_channel_id == str(t.id)
        added = sorted(m.id for m in t.added)
        assert added == list(range(1, 12)) + [50]                  # players, host, and the mod who
        assert await gc.create(guild, s, lobby) is t              # can't already see private threads
        assert len(guild.threads) == 1


async def test_casual_thread_name_and_fillers(db):
    guild = GameGuild(member_ids=["1", "2", "3"])
    async with db() as s:
        lobby = await _lobby(s, guild, real=3, fillers=7)
        lobby.ranked = False
        await lm.make_teams(s, lobby)
        t = await gc.create(guild, s, lobby)
        assert t.name == "user1's casual inhouse"
        assert sorted(m.id for m in t.added) == [1, 2, 3]          # fillers have no Discord account


async def test_bot_gets_past_the_no_threads_lock_on_the_queue_channel(db):
    """/admin setup stops @everyone making threads in #inhouse-queue; that lock hits the bot too."""
    guild = GameGuild(member_ids=[str(i) for i in range(1, 11)])
    async with db() as s:
        lobby = await _lobby(s, guild)
        queue = guild.get_channel(int(lobby.channel_id))
        queue.overwrites = {guild.default_role: discord.PermissionOverwrite(create_private_threads=False)}
        assert gc.thread_access_missing(queue, guild.me) == ["create_private_threads"]
        assert await gc.create(guild, s, lobby) is not None
        assert queue.overwrites[guild.me].create_private_threads is True
        assert queue.overwrites[guild.default_role].create_private_threads is False   # players still can't


async def test_setup_gives_the_bot_thread_access_in_the_queue_channel():
    g = GameGuild()
    ow = ss.overwrites_for("commands", g, None, full_perms())
    assert ow[g.default_role].create_private_threads is False
    assert ow[g.me].create_private_threads and ow[g.me].send_messages_in_threads and ow[g.me].manage_threads
    assert ss.overwrites_for("readonly", g, None, full_perms())[g.me].create_private_threads is None


async def test_no_thread_without_permission_or_when_turned_off(db, monkeypatch):
    lean = discord.Permissions(117824)                            # the basic invite
    guild = GameGuild(member_ids=["1"], perms=lean)
    async with db() as s:
        lobby = await _lobby(s, guild)
        queue = guild.get_channel(int(lobby.channel_id))
        queue.overwrites = {guild.default_role: discord.PermissionOverwrite(create_private_threads=False)}
        assert await gc.create(guild, s, lobby) is None            # can't give itself an exception
        monkeypatch.setattr(config, "GAME_CHANNELS_ENABLED", False)
        assert await gc.create(GameGuild(), s, lobby) is None


def test_invite_link_asks_for_thread_and_voice_permissions():
    p = ss.invite_permissions()
    for name in ss.GAME_PERMISSIONS:
        assert getattr(p, name), name
    assert ss.missing_game_permissions(discord.Permissions(268561488)) == list(ss.GAME_PERMISSIONS)
    assert ss.missing_game_permissions(p) == []


# --------------------------------------------------------------------------- #
# Team voice                                                                   #
# --------------------------------------------------------------------------- #

async def _teams(s, guild, **kw):
    lobby = await _lobby(s, guild, **kw)
    await lm.make_teams(s, lobby)
    blue = {int((await s.get(Player, lp.player_id)).discord_id) for lp in lobby.players if lp.team == 1}
    red = {int((await s.get(Player, lp.player_id)).discord_id) for lp in lobby.players if lp.team == 2}
    return lobby, blue, red


async def test_team_voice_only_that_team_can_join_and_players_are_moved(db):
    guild = GameGuild(member_ids=[str(i) for i in range(1, 11)])
    mod = FakeRole("Inhouse Mod")
    guild.roles.append(mod)
    cat = FakeChannel("INHOUSES", "category")
    guild.channels.append(cat)
    lobby_vc = await guild.create_voice_channel("Lobby")
    other_vc = await guild.create_voice_channel("Hangout")
    guild.afk_channel = await guild.create_voice_channel("AFK")
    async with db() as s:
        await settings.set_setting(s, "42", REMEMBER_PREFIX + "mod_role", str(mod.id))
        await settings.set_setting(s, "42", REMEMBER_PREFIX + "cat_inhouse", str(cat.id))
        lobby, blue, red = await _teams(s, guild)
        b1, b2, b3 = sorted(blue)[:3]
        await guild.members[b1].move_to(lobby_vc)                  # in the Lobby voice channel
        await guild.members[b2].move_to(other_vc)                  # somewhere else in voice
        await guild.members[b3].move_to(guild.afk_channel)         # AFK: leave them alone
        blue_vc, red_vc = await gc.open_team_voice(guild, s, lobby)
        assert blue_vc.name == "Blue · user1's inhouse" and red_vc.name == "Red · user1's inhouse"
        assert blue_vc.category is cat and lobby.team1_voice_id == str(blue_vc.id)
        ow = blue_vc.overwrites
        assert ow[guild.default_role].connect is False             # visible, but locked
        assert ow[guild.me].connect and ow[guild.me].move_members
        assert ow[mod].connect
        for uid in blue:
            assert ow[guild.members[uid]].connect
        for uid in red:
            assert guild.members[uid] not in ow                    # the other team can't get in
        assert guild.members[b1].voice.channel is blue_vc and guild.members[b2].voice.channel is blue_vc
        assert guild.members[b3].voice.channel is guild.afk_channel
        someone_red = guild.members[min(red)]
        assert someone_red.voice is None                           # never pulled into voice
        # running it again (e.g. after a team change) reuses the channels
        again = await gc.open_team_voice(guild, s, lobby)
        assert again == (blue_vc, red_vc) and len(guild.voice_channels) == 5


async def test_team_voice_without_move_permission_or_turned_off(db, monkeypatch):
    perms = full_perms()
    perms.move_members = False
    guild = GameGuild(member_ids=[str(i) for i in range(1, 11)], perms=perms)
    lobby_vc = await guild.create_voice_channel("Lobby")
    async with db() as s:
        lobby, blue, _ = await _teams(s, guild)
        await guild.members[min(blue)].move_to(lobby_vc)
        chans = await gc.open_team_voice(guild, s, lobby)
        assert chans and guild.members[min(blue)].voice.channel is lobby_vc   # made, but nobody moved
        monkeypatch.setattr(config, "TEAM_VOICE_ENABLED", False)
        lobby.team1_voice_id = lobby.team2_voice_id = None
        assert await gc.open_team_voice(guild, s, lobby) is None


# --------------------------------------------------------------------------- #
# Cleanup                                                                      #
# --------------------------------------------------------------------------- #

async def test_cleanup_archives_the_thread_and_sends_voice_back_to_the_lobby(db):
    guild = GameGuild(member_ids=[str(i) for i in range(1, 11)])
    lobby_vc = await guild.create_voice_channel("Lobby")
    async with db() as s:
        await settings.set_setting(s, "42", REMEMBER_PREFIX + "vc_lobby", str(lobby_vc.id))
        lobby, blue, _ = await _teams(s, guild)
        thread = await gc.create(guild, s, lobby)
        blue_vc, red_vc = await gc.open_team_voice(guild, s, lobby)
        await guild.members[min(blue)].move_to(blue_vc)
        lobby.status = "completed"
        await s.commit()
        assert await gc.clean_up(guild, s, lobby) is True
        assert thread.archived and thread.locked                    # kept for staff, closed to chat
        assert blue_vc.deleted and red_vc.deleted
        assert guild.members[min(blue)].voice.channel is lobby_vc
        assert lobby.game_channel_id is None and lobby.team1_voice_id is None


async def test_cleanup_copes_with_missing_old_and_stuck_channels(db):
    guild = GameGuild(member_ids=[str(i) for i in range(1, 11)])
    async with db() as s:
        lobby, _, _ = await _teams(s, guild)
        old = guild.text("game-7")                                 # a channel from before threads
        lobby.game_channel_id, lobby.team1_voice_id = str(old.id), "123456"   # voice already gone
        await s.commit()
        assert await gc.clean_up(guild, s, lobby) is True and old.deleted
        blue_vc, red_vc = await gc.open_team_voice(guild, s, lobby)
        red_vc.fail = 500                                          # Discord hiccup: try again later
        assert await gc.clean_up(guild, s, lobby) is False
        assert lobby.team2_voice_id == str(red_vc.id)
        red_vc.fail = 403                                          # forbidden: retrying won't help
        assert await gc.clean_up(guild, s, lobby) is True


async def test_cleanup_timing(db, monkeypatch):
    monkeypatch.setattr(config, "GAME_CHANNEL_CLEANUP_MINUTES", 15)
    async with db() as s:
        lobby = await _lobby(s)
        lobby.team1_voice_id = "555"                               # voice only still counts
        lobby.status = "completed"
        await s.commit()
        await s.refresh(lobby)
        ended = gc._aware(lobby.updated_at)
        assert await gc.due_for_cleanup(s, now=ended + timedelta(minutes=5)) == []
        due = await gc.due_for_cleanup(s, now=ended + timedelta(minutes=16))
        assert [l.id for l in due] == [lobby.id]
        monkeypatch.setattr(config, "GAME_CHANNEL_CLEANUP_MINUTES", 0)          # 0 keeps them
        assert await gc.due_for_cleanup(s, now=ended + timedelta(days=5)) == []
        monkeypatch.setattr(config, "GAME_CHANNEL_CLEANUP_MINUTES", 15)
        lobby.status = "active"                                                 # a live game is never touched
        await s.commit()
        assert await gc.due_for_cleanup(s, now=ended + timedelta(days=5)) == []


async def test_commands_find_the_lobby_from_its_thread(db):
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


# --------------------------------------------------------------------------- #
# Getting into the game                                                        #
# --------------------------------------------------------------------------- #

@pytest.fixture
def no_tournament(monkeypatch):
    monkeypatch.setattr(config, "RIOT_TOURNAMENT_API_KEY", "")
    monkeypatch.setattr(config, "TOURNAMENT_CODES", False)


async def test_lobby_name_and_password_without_tournament_codes(db, no_tournament):
    async with db() as s:
        lobby = await _lobby(s, host="3")
        await lm.make_teams(s, lobby)
        info = await gc.prepare_join(s, lobby, riot=FakeTournamentRiot(), rng=random.Random(5))
        assert info.code is None and info.creator.discord_id == "3"          # the host makes the lobby
        assert info.name.startswith(f"Inhouse {lobby.id}-")
        assert len(info.password) == 6 and set(info.password) <= set(gc.PASSWORD_CHARS)
        again = await gc.prepare_join(s, lobby, riot=None, rng=random.Random(99))
        assert (again.name, again.password) == (info.name, info.password)   # stable once posted


async def test_creator_is_blue_first_pick_when_host_is_not_playing(db, no_tournament):
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
        self.calls.append(("provider", region))
        return 10 + len(self.calls)

    async def create_tournament(self, provider_id, name):
        self.calls.append(("tournament", provider_id, name))
        return 20 + len(self.calls)

    async def create_tournament_code(self, tournament_id, allowed_participants=None, metadata=""):
        if self.fail:
            raise RuntimeError("403 Forbidden")
        self.codes.append((tournament_id, list(allowed_participants), metadata))
        return f"NA04-CODE-{len(self.codes)}"


async def _next_lobby(s, previous):
    previous.status = "completed"
    await s.commit()
    nxt = await lm.create_lobby(s, "42", "c2", "1")
    for i in range(1, 5):
        await lm.queue_player(s, nxt, str(i))
    return nxt


async def test_tournament_code_with_tournament_codes_on(db, no_tournament, monkeypatch):
    monkeypatch.setattr(config, "TOURNAMENT_CODES", True)          # the main key has access
    monkeypatch.setattr(config, "RIOT_API_KEY", "RGAPI-production")
    riot = FakeTournamentRiot()
    async with db() as s:
        lobby = await _lobby(s, real=4, fillers=6)
        await lm.make_teams(s, lobby)
        info = await gc.prepare_join(s, lobby, riot)
        assert info.code == "NA04-CODE-1" and lobby.tournament_code == "NA04-CODE-1"
        assert lobby.join_password is None                         # no password needed
        tid, allowed, meta = riot.codes[0]
        assert sorted(allowed) == [f"puuid-{i}" for i in range(1, 5)]   # locked to these players, no fillers
        assert meta == f"lobby:{lobby.id}"
        # provider and tournament are made once and reused
        await gc.prepare_join(s, await _next_lobby(s, lobby), riot)
        assert [c[0] for c in riot.calls] == ["provider", "tournament"]
        assert riot.codes[1][0] == tid


async def test_new_key_gets_a_new_provider_and_new_season_a_new_tournament(db, no_tournament, monkeypatch):
    monkeypatch.setattr(config, "RIOT_TOURNAMENT_API_KEY", "RGAPI-key-one")
    riot = FakeTournamentRiot()
    async with db() as s:
        lobby = await _lobby(s, real=4)
        await gc.prepare_join(s, lobby, riot)
        monkeypatch.setattr(config, "RIOT_TOURNAMENT_API_KEY", "RGAPI-key-two")   # Riot won't honour
        lobby = await _next_lobby(s, lobby)                                         # key one's provider
        await gc.prepare_join(s, lobby, riot)
        assert [c[0] for c in riot.calls] == ["provider", "tournament"] * 2
        monkeypatch.setattr(config, "CURRENT_SEASON", 2)
        await gc.prepare_join(s, await _next_lobby(s, lobby), riot)
        assert [c[0] for c in riot.calls][-1] == "tournament" and riot.calls[-1][2] == "Ranked Inhouses S2"
        assert len(riot.calls) == 5                                                 # same provider
        stored = str(await settings.get_setting(s, "42", f"{gc.KEY_PROVIDER}:{gc.key_fingerprint('RGAPI-key-two')}:NA"))
        assert "RGAPI" not in stored                                                # keys never stored


async def test_falls_back_to_password_if_the_tournament_api_refuses(db, no_tournament, monkeypatch):
    monkeypatch.setattr(config, "RIOT_TOURNAMENT_API_KEY", "RGAPI-no-access")
    async with db() as s:
        lobby = await _lobby(s)
        await lm.make_teams(s, lobby)
        info = await gc.prepare_join(s, lobby, FakeTournamentRiot(fail=True))
        assert info.code is None and info.password and "couldn't be made" in info.note


async def test_bad_callback_url_falls_back_instead_of_calling_riot(db, no_tournament, monkeypatch):
    monkeypatch.setattr(config, "RIOT_TOURNAMENT_API_KEY", "RGAPI-x")
    monkeypatch.setattr(config, "RIOT_TOURNAMENT_CALLBACK_URL", "https://mysite.com:8080/cb")
    riot = FakeTournamentRiot()
    async with db() as s:
        lobby = await _lobby(s)
        info = await gc.prepare_join(s, lobby, riot)
        assert info.code is None and riot.calls == []
    assert gc.callback_problem("https://example.com/riot-callback") is None
    assert gc.callback_problem("http://mysite.com:80/x") is None
    assert "http" in gc.callback_problem("mysite.com/cb")


def test_tournament_codes_switch(monkeypatch):
    monkeypatch.setattr(config, "RIOT_API_KEY", "RGAPI-main")
    monkeypatch.setattr(config, "RIOT_TOURNAMENT_API_KEY", "")
    monkeypatch.setattr(config, "TOURNAMENT_CODES", False)
    assert config.tournament_api_key() == ""                       # off by default
    monkeypatch.setattr(config, "TOURNAMENT_CODES", True)
    assert config.tournament_api_key() == "RGAPI-main"
    monkeypatch.setattr(config, "RIOT_TOURNAMENT_API_KEY", "RGAPI-tournament")
    assert config.tournament_api_key() == "RGAPI-tournament"       # a separate key wins


async def test_tournament_calls_use_v5_fields_and_the_regional_host(monkeypatch):
    from bot.services import riot_api as ra
    sent = {}

    async def fake_post(self, host, path, payload, api_key=None):
        sent.update(host=host, path=path, payload=payload)
        return ["CODE"]
    monkeypatch.setattr(ra.RiotClient, "_post", fake_post)
    monkeypatch.setattr(config, "RIOT_PLATFORM", "americas")
    await ra.RiotClient(object()).create_tournament_code(22, allowed_participants=[f"p{i}" for i in range(10)])
    assert sent["host"] == "americas.api.riotgames.com"
    assert sent["payload"]["allowedParticipants"] == [f"p{i}" for i in range(10)]
    assert sent["payload"]["enoughPlayers"] is True and "allowedSummonerIds" not in sent["payload"]
    await ra.RiotClient(object()).create_tournament_code(22, allowed_participants=["p1", "p2"])
    assert sent["payload"]["enoughPlayers"] is False


async def test_tournament_access_probe(monkeypatch):
    from bot.services import riot_api as ra

    def replying(status):
        async def fake_get(self, host, path, api_key=None):
            raise ra.RiotAPIError(status, "x")
        return fake_get
    monkeypatch.setattr(ra.RiotClient, "_get", replying(403))
    assert await ra.RiotClient(object()).tournament_access() == 403
    monkeypatch.setattr(ra.RiotClient, "_get", replying(404))
    assert await ra.RiotClient(object()).tournament_access() == 404


# --------------------------------------------------------------------------- #
# Privacy                                                                      #
# --------------------------------------------------------------------------- #

async def test_secrets_never_appear_in_the_public_teams_post(db, no_tournament):
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


async def test_host_mention_renders_in_the_lobby_post(db):
    """Mentions don't render in embed footers; the host showed up as raw <@123...> there."""
    async with db() as s:
        lobby = await _lobby(s, real=2)
        e = embeds.lobby_embed(lobby, {lp.player_id: "x" for lp in lobby.players}, "<@1>").to_dict()
        assert "<@1>" not in e.get("footer", {}).get("text", "") and "<@1>" not in e.get("title", "")
        assert any(f["value"] == "<@1>" for f in e["fields"])
