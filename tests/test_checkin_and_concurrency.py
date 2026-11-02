"""Several games at once, setting roles after the draft, and the post-game role and champion check-in."""
import copy
import itertools
from types import SimpleNamespace

import discord
import pytest
from sqlalchemy import select

from bot import config
from bot.models.game import Game, GameParticipant
from bot.models.lobby import Lobby
from bot.models.player import Player
from bot.services import auto_detect, checkin
from bot.services import lobby_manager as lm
from bot.services.game_processor import apply_roles, reprocess_match
from bot.services.lobby_manager import LobbyError
from tests.fixtures import make_match

BLANK = {i: {"teamPosition": "", "individualPosition": "Invalid"} for i in range(1, 11)}   # how customs often look
_ids = itertools.count(8_000_000)


async def _seed(s, n=10, start=1):
    for i in range(start, start + n):
        s.add(Player(discord_id=str(i), discord_username=f"u{i}", riot_puuid=f"puuid-{i}"))
    await s.commit()


async def _active_lobby(s, channel="c", ids=range(1, 11), mode="balanced"):
    lobby = await lm.create_lobby(s, "42", channel, "1", mode=mode)
    for i in ids:
        await lm.queue_player(s, lobby, str(i))
    await lm.make_teams(s, lobby)
    return lobby


# --------------------------------------------------------------------------- #
# Several games at once                                                        #
# --------------------------------------------------------------------------- #

async def test_one_filling_lobby_per_channel_but_many_games(db):
    async with db() as s:
        await _seed(s, 20)
        first = await lm.create_lobby(s, "42", "c", "1")
        with pytest.raises(LobbyError, match="already filling"):
            await lm.create_lobby(s, "42", "c", "2")                # one filling at a time
        for i in range(1, 11):
            await lm.queue_player(s, first, str(i))
        await lm.make_teams(s, first)                                # game 1 is underway
        second = await lm.create_lobby(s, "42", "c", "1")           # so the next one can open
        assert (await lm.get_open_lobby(s, "42", "c")).id == second.id
        assert {l.id for l in await lm.live_lobbies(s, "42")} == {first.id, second.id}


async def test_nobody_plays_two_games_at_once(db):
    async with db() as s:
        await _seed(s, 11)
        game1 = await _active_lobby(s)
        nxt = await lm.create_lobby(s, "42", "c", "1")
        with pytest.raises(LobbyError, match="already playing in lobby"):
            await lm.queue_player(s, nxt, "3")
        await lm.queue_player(s, nxt, "11")                          # someone free is fine
        game1.status = "confirming"                                  # their game is over: free to queue
        await s.commit()
        await lm.queue_player(s, nxt, "3")


# --------------------------------------------------------------------------- #
# Check-in: the service                                                        #
# --------------------------------------------------------------------------- #

async def _found(s, lobby, overrides=None, mid="NA1_ci"):
    match, tl = make_match(mid, overrides=overrides)
    await checkin.begin(s, lobby, mid, match, tl)
    return match


async def test_begin_holds_the_game_and_guesses_from_the_draft_when_riot_has_no_roles(db):
    async with db() as s:
        await _seed(s)
        lobby = await _active_lobby(s)
        await _found(s, lobby, BLANK)
        assert lobby.status == "confirming" and checkin.running(lobby)
        game = await s.scalar(select(Game).where(Game.riot_match_id == "NA1_ci"))
        assert game.status == "pending" and game.raw_riot_data["match"]
        rows = await checkin.rows(s, lobby)
        for r in rows:
            assert r.guess_role == r.lobby_player.assigned_role       # Riot was blank: the draft's role
            assert r.team == (1 if int(r.player.discord_id) <= 5 else 2)
            assert r.guess_champion and not r.confirmed


async def test_riot_roles_are_the_guess_when_riot_has_them(db):
    async with db() as s:
        await _seed(s)
        lobby = await _active_lobby(s)
        await _found(s, lobby)
        rows = {r.player.discord_id: r for r in await checkin.rows(s, lobby)}
        assert rows["1"].guess_role == "TOP" and rows["5"].guess_role == "UTILITY"


async def test_confirming_checks_the_champion_is_on_your_team(db):
    async with db() as s:
        await _seed(s, 11)
        lobby = await _active_lobby(s)
        await _found(s, lobby)
        blue = await checkin.team_champions(s, lobby, 1)
        red = await checkin.team_champions(s, lobby, 2)
        assert len(blue) == 5 and len(red) == 5
        with pytest.raises(checkin.CheckinError, match="your team's"):
            await checkin.confirm(s, lobby, "1", "TOP", red[0] if red[0] not in blue else "Teemo")
        with pytest.raises(checkin.CheckinError, match="weren't in this game"):
            await checkin.confirm(s, lobby, "11", "TOP", blue[0])
        r = await checkin.confirm(s, lobby, "1", "JUNGLE", blue[0])
        assert r.lobby_player.confirmed_role == "JUNGLE"
        assert not await checkin.everyone_confirmed(s, lobby)
        for i in range(2, 11):
            await checkin.confirm(s, lobby, str(i), "MIDDLE", None)
        assert await checkin.everyone_confirmed(s, lobby)


async def test_confirmed_roles_are_what_gets_scored_and_kept(db):
    async with db() as s:
        await _seed(s)
        lobby = await _active_lobby(s)
        await _found(s, lobby, BLANK)
        swap = {"1": "UTILITY", "2": "TOP", "3": "JUNGLE", "4": "MIDDLE", "5": "BOTTOM",
                "6": "TOP", "7": "JUNGLE", "8": "MIDDLE", "9": "BOTTOM", "10": "UTILITY"}
        for did, role in swap.items():
            await checkin.confirm(s, lobby, did, role, None)
        assert await checkin.end(s, lobby)
        result = await checkin.finalize(s, lobby)
        assert lobby.status == "completed" and len(result.results) == 10
        parts = (await s.execute(select(GameParticipant, Player).join(Player))).all()
        assert {p.discord_id: gp.role for gp, p in parts} == swap
        game = await s.scalar(select(Game).where(Game.riot_match_id == "NA1_ci"))
        assert game.processing_notes["role_overrides"]["puuid-1"] == "UTILITY"
        # an admin reprocess (after a rollback) gives the same roles, from the saved ones
        await reprocess_match(s, "NA1_ci", "admin")
        parts = (await s.execute(select(GameParticipant, Player).join(Player))).all()
        assert {p.discord_id: gp.role for gp, p in parts} == swap


async def test_nobody_answering_uses_the_guesses(db):
    async with db() as s:
        await _seed(s)
        lobby = await _active_lobby(s)
        await _found(s, lobby, BLANK)
        guesses = {r.player.discord_id: r.guess_role for r in await checkin.rows(s, lobby)}
        assert await checkin.end(s, lobby)
        await checkin.finalize(s, lobby)
        parts = (await s.execute(select(GameParticipant, Player).join(Player))).all()
        assert {p.discord_id: gp.role for gp, p in parts} == guesses        # not all "Mid"


async def test_only_one_caller_closes_the_check_in(db):
    async with db() as s:
        await _seed(s)
        lobby = await _active_lobby(s)
        await _found(s, lobby)
        assert await checkin.end(s, lobby) is True
        assert await checkin.end(s, lobby) is False


async def test_flags_a_champion_that_isnt_theirs_and_duplicate_roles(db):
    async with db() as s:
        await _seed(s)
        lobby = await _active_lobby(s)
        await _found(s, lobby)
        rows = {r.player.discord_id: r for r in await checkin.rows(s, lobby)}
        mine, other = rows["1"].guess_champion, rows["2"].guess_champion
        await checkin.confirm(s, lobby, "1", "TOP", other)                 # says they were a teammate's champ
        await checkin.confirm(s, lobby, "2", "TOP", other)
        rs = await checkin.rows(s, lobby)
        assert [r.player.discord_id for r in checkin.mismatches(rs)] == ["1"]
        assert (1, "TOP") in checkin.duplicate_roles(rs)
        assert mine != other


async def test_due_and_casual_games_go_through_check_in_too(db):
    from datetime import timedelta
    async with db() as s:
        await _seed(s)
        lobby = await _active_lobby(s)
        lobby.ranked = False
        await s.commit()
        await _found(s, lobby)
        assert await checkin.due(s) == []
        assert [l.id for l in await checkin.due(s, now=checkin._aware(lobby.checkin_deadline) + timedelta(seconds=1))] == [lobby.id]
        assert await checkin.end(s, lobby)
        result = await checkin.finalize(s, lobby)
        assert result.game.status == "casual"


def test_apply_roles_leaves_the_original_alone():
    match, _ = make_match("NA1_x", overrides=BLANK)
    before = copy.deepcopy(match)
    out = apply_roles(match, {"puuid-1": "JUNGLE"})
    assert match == before
    assert out["info"]["participants"][0]["teamPosition"] == "JUNGLE"


async def test_auto_detect_sends_the_game_to_the_check_in(db, monkeypatch):
    monkeypatch.setattr(config, "CHECKIN_ENABLED", True)
    match, tl = make_match("NA1_auto_ci", queue_id=0)

    class FakeRiot:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get_recent_match_ids(self, *a, **k): return ["NA1_auto_ci"]
        async def get_match(self, mid): return match
        async def get_match_timeline(self, mid): return tl

    monkeypatch.setattr(auto_detect, "RiotClient", FakeRiot)
    async with db() as s:
        await _seed(s)
        lobby = await _active_lobby(s)
        lid = lobby.id
    found = await auto_detect.poll_once(db)
    assert [(l.id, r) for l, r in found] == [(lid, None)]
    async with db() as s:
        assert (await s.get(Lobby, lid)).status == "confirming"
        assert await auto_detect.poll_once(db) == []                        # not picked up twice


# --------------------------------------------------------------------------- #
# Discord side, through the real cog                                           #
# --------------------------------------------------------------------------- #

class Msg:
    def __init__(self, channel, content=None, embed=None, view=None, embeds=None):
        self.id, self.channel, self.content, self.view = next(_ids), channel, content, view
        self.embed = embed or (embeds[0] if embeds else None)
        self.embeds = embeds or ([embed] if embed else [])
        self.flags = SimpleNamespace(ephemeral=False)

    async def edit(self, content=None, embed=None, view="keep", embeds=None, **kw):
        if embed is not None:
            self.embed = embed
        if embeds is not None:
            self.embeds, self.embed = embeds, embeds[0]
        if view != "keep":
            self.view = view


class Channel:
    def __init__(self):
        self.id, self.sent = next(_ids), []
        self.mention = f"<#{self.id}>"

    async def send(self, content=None, embed=None, view=None, embeds=None, **kw):
        m = Msg(self, content, embed, view, embeds)
        self.sent.append(m)
        return m

    def get_partial_message(self, mid):
        return next(m for m in self.sent if m.id == mid)


class Bot:
    def __init__(self, channel):
        self.channel, self.cogs, self.guilds = channel, {}, []

    def get_channel(self, cid):
        return self.channel if cid == self.channel.id else None

    def get_guild(self, gid):
        return None

    def get_cog(self, name):
        return self.cogs.get(name)

    def add_view(self, view):
        pass


class Inter:
    def __init__(self, user_id, message=None, admin=False):
        perms = discord.Permissions(manage_guild=True) if admin else discord.Permissions.none()
        self.user = SimpleNamespace(id=int(user_id), mention=f"<@{user_id}>", display_name=f"u{user_id}",
                                    name=f"u{user_id}", guild_permissions=perms, roles=[])
        self.guild_id, self.channel_id, self.message = 42, None, message
        self.sent, self.view, self.edited = [], None, None
        outer = self

        class Response:
            done = False

            def is_done(self):
                return self.done

            async def defer(self, **kw):
                self.done = True

            async def send_message(self, content=None, view=None, **kw):
                outer.sent.append(content)
                outer.view = view

            async def edit_message(self, content=None, view=None, **kw):
                outer.edited = content
                outer.view = view

        class Followup:
            async def send(self, content=None, **kw):
                outer.sent.append(content)

        self.response, self.followup = Response(), Followup()


@pytest.fixture
def cog(monkeypatch):
    from bot.cogs.lobby import LobbyCog
    ch = Channel()
    c = LobbyCog(Bot(ch))
    c.bot.cogs["Lobby"] = c
    c.channel, c.announced = ch, []

    async def fake_announce(lobby, result):
        c.announced.append((lobby.id, result))
    monkeypatch.setattr(c, "announce_result", fake_announce)
    return c


async def test_check_in_end_to_end_through_the_buttons(db, cog):
    async with db() as s:
        await _seed(s)
        lobby = await _active_lobby(s, channel=str(cog.channel.id))
        await _found(s, lobby, BLANK)
        lid = lobby.id
    await cog.post_checkin(lid)
    msg = cog.channel.sent[-1]
    assert "<@1>" in msg.content and "0/10 confirmed" in msg.embed.description
    outsider = Inter(99, msg)
    await cog.handle_checkin_open(outsider)
    assert "Only the players" in outsider.sent[0]
    for i in range(1, 11):
        press = Inter(i, msg)
        await cog.handle_checkin_open(press)
        form = press.view
        champ_sel = form.children[1]
        assert len(champ_sel.options) == 5                                 # only their team's champions
        form.role = "UTILITY" if i in (1, 6) else form.role
        form.champion = champ_sel.options[0].value if form.champion is None else form.champion
        await cog.handle_checkin_submit(Inter(i, msg), form)
        if i < 10:
            assert f"{i}/10 confirmed" in msg.embed.description
    assert [a[0] for a in cog.announced] == [lid]                            # scored once everyone confirmed
    assert msg.view is None and "closed" in msg.embed.title
    async with db() as s:
        roles = {p.discord_id: gp.role for gp, p in (await s.execute(select(GameParticipant, Player).join(Player))).all()}
        assert roles["1"] == "UTILITY" and roles["6"] == "UTILITY"


async def test_only_host_or_staff_can_finish_early(db, cog):
    async with db() as s:
        await _seed(s)
        lobby = await _active_lobby(s, channel=str(cog.channel.id))
        await _found(s, lobby)
        lid = lobby.id
    await cog.post_checkin(lid)
    msg = cog.channel.sent[-1]
    player = Inter(5, msg)
    await cog.handle_checkin_finish(player)
    assert "Only the host" in player.sent[0] and cog.announced == []
    await cog.handle_checkin_finish(Inter(1, msg))                           # player 1 is the host
    assert [a[0] for a in cog.announced] == [lid]


async def test_captains_set_roles_with_dropdowns(db, cog):
    from bot.cogs.lobby import RoleEditView
    async with db() as s:
        await _seed(s)
        lobby = await _active_lobby(s, channel=str(cog.channel.id))
        blue = await cog._team_members(s, lobby, 1)
        (a_id, _, a_role), (b_id, _, b_role) = blue[0], blue[1]
        view = RoleEditView(cog, lobby.id, 1, blue)
        assert len(view.children) == 5
        lid = lobby.id
    await cog.handle_role_set(Inter(1, admin=True), view, a_id, b_role)       # take a teammate's role: swap
    async with db() as s:
        roles = {did: role for did, _, role in await cog._team_members(s, await s.get(Lobby, lid), 1)}
        assert roles[a_id] == b_role and roles[b_id] == a_role
    stranger = Inter(7)                                                     # red player, not a captain
    await cog.handle_role_set(stranger, view, a_id, a_role)
    assert "Only that team's captain" in stranger.edited


async def test_the_next_lobby_opens_when_one_starts(db, cog, monkeypatch):
    async def fake_teams(session, lobby, names, public=None):
        pass
    monkeypatch.setattr(cog, "announce_teams", fake_teams)
    async with db() as s:
        await _seed(s)
        lobby = await lm.create_lobby(s, "42", str(cog.channel.id), "1", mode="balanced")
        for i in range(1, 11):
            await lm.queue_player(s, lobby, str(i))
        await cog.start_lobby(s, lobby, cog.channel)
        nxt = await lm.get_open_lobby(s, "42", str(cog.channel.id))
        assert nxt is not None and nxt.id != lobby.id
        assert nxt.mode == "captain"                     # balanced is switched off: the server default
        assert any(f"Lobby #{nxt.id} is open" in (m.content or "") for m in cog.channel.sent)
