"""Captains from the leaderboard, the draft dropdown, remembered roles, the #leaderboard post, seasons, panel."""
import itertools
from types import SimpleNamespace

import discord
import pytest
from sqlalchemy import select

from bot import config
from bot.models.lobby import Lobby
from bot.models.player import Player
from bot.models.rating import RoleBaseline
from bot.services import ladder, seasons, settings
from bot.services import lobby_manager as lm
from bot.services.game_processor import get_or_create_rating, load_baselines
from bot.ui import embeds

_ids = itertools.count(7_000_000)


async def _players(s, n=10):
    for i in range(1, n + 1):
        s.add(Player(discord_id=str(i), discord_username=f"u{i}", riot_puuid=f"puuid-{i}"))
    await s.commit()
    return {p.discord_id: p for p in (await s.execute(select(Player))).scalars().all()}


async def _rate(s, player, lp, games=6, mmr=1500.0, season=None, wins=None):
    r = await get_or_create_rating(s, player.id, "OVERALL", season or config.CURRENT_SEASON)
    r.lp, r.games_played, r.mmr, r.wins = lp, games, mmr, games // 2 if wins is None else wins
    await s.commit()
    return r


# --------------------------------------------------------------------------- #
# The ladder                                                                   #
# --------------------------------------------------------------------------- #

async def test_ladder_order_matches_the_leaderboard(db):
    async with db() as s:
        ps = await _players(s)
        await _rate(s, ps["1"], 1100)
        await _rate(s, ps["2"], 1300)
        await _rate(s, ps["3"], 1200, mmr=1600)
        await _rate(s, ps["4"], 1200, mmr=1700)                    # same LP: higher MMR first
        await _rate(s, ps["5"], 1500, games=0)                     # no games this season: not on the board
        board = [p.discord_id for _, p in await ladder.standings(s)]
        assert board == ["2", "4", "3", "1"]
        assert (await ladder.positions(s))[ps["4"].id] == 2


async def test_players_not_on_the_board_come_after_by_mmr(db):
    async with db() as s:
        ps = await _players(s)
        await _rate(s, ps["1"], 1050)
        await _rate(s, ps["2"], 900, games=0, mmr=1800)            # strong, but no games yet
        await _rate(s, ps["3"], 900, games=0, mmr=1600)
        order = await ladder.ladder_order(s, [ps[k].id for k in ("3", "2", "1", "4")])
        assert order == [ps["1"].id, ps["2"].id, ps["3"].id, ps["4"].id]


async def test_fillers_never_appear_on_the_board(db):
    async with db() as s:
        await _players(s, 1)
        lobby = await lm.create_lobby(s, "42", "c", "1", mode="captain")
        await lm.test_fill(s, lobby, count=3)
        for f in (await s.execute(select(Player).where(Player.is_test.is_(True)))).scalars().all():
            await _rate(s, f, 2000)
        assert await ladder.standings(s) == []


# --------------------------------------------------------------------------- #
# Captains                                                                     #
# --------------------------------------------------------------------------- #

async def _full_captain_lobby(s, ps):
    lobby = await lm.create_lobby(s, "42", "c", "1", mode="captain")
    for i in range(1, 11):
        await lm.queue_player(s, lobby, str(i))
    return lobby


async def test_top_two_on_the_leaderboard_captain_and_the_lower_one_picks_first(db, monkeypatch):
    monkeypatch.setattr(config, "CAPTAIN_FIRST_PICK", "lower")
    async with db() as s:
        ps = await _players(s)
        for i in range(1, 11):
            await _rate(s, ps[str(i)], 1000 + i * 10)               # u10 is #1, u9 is #2
        lobby = await _full_captain_lobby(s, ps)
        cap1, cap2 = await lm.start_captain_draft(s, lobby)
        assert (cap1.discord_id, cap2.discord_id) == ("9", "10")    # #2 is blue and picks first
        assert lm.current_captain_id(lobby) == cap1.id


async def test_higher_ranked_captain_can_pick_first_instead(db, monkeypatch):
    monkeypatch.setattr(config, "CAPTAIN_FIRST_PICK", "higher")
    async with db() as s:
        ps = await _players(s)
        for i in range(1, 11):
            await _rate(s, ps[str(i)], 1000 + i * 10)
        lobby = await _full_captain_lobby(s, ps)
        cap1, cap2 = await lm.start_captain_draft(s, lobby)
        assert (cap1.discord_id, cap2.discord_id) == ("10", "9")


async def test_captain_draft_is_the_default_mode(db):
    async with db() as s:
        assert await settings.default_mode(s, "42") == "captain"
        assert await settings.allowed_modes(s, "42") == ["captain"]


# --------------------------------------------------------------------------- #
# Roles                                                                        #
# --------------------------------------------------------------------------- #

async def test_roles_are_remembered_for_the_next_lobby(db):
    async with db() as s:
        await _players(s, 2)
        lobby = await lm.create_lobby(s, "42", "c", "1", mode="captain")
        await lm.queue_player(s, lobby, "2")
        await lm.set_preferences(s, lobby, "2", "MIDDLE", "TOP")
        await lm.cancel_lobby(s, lobby, "1")
        nxt = await lm.create_lobby(s, "42", "c", "1", mode="captain")
        lp = await lm.queue_player(s, nxt, "2")
        assert (lp.preferred_role, lp.secondary_role) == ("MIDDLE", "TOP")
        lp = await lm.set_preferences(s, nxt, "2", None, "JUNGLE")      # main: fill
        assert (lp.preferred_role, lp.secondary_role) == (None, "JUNGLE")
        lp = await lm.set_preferences(s, nxt, "2", "JUNGLE", "JUNGLE")  # same twice: second cleared
        assert (lp.preferred_role, lp.secondary_role) == ("JUNGLE", None)
        with pytest.raises(lm.LobbyError):
            await lm.set_preferences(s, nxt, "1", "TOP", None)          # the host isn't queued


# --------------------------------------------------------------------------- #
# Seasons                                                                      #
# --------------------------------------------------------------------------- #

async def test_new_season_resets_lp_but_keeps_part_of_mmr(db, monkeypatch):
    monkeypatch.setattr(config, "CURRENT_SEASON", 1)
    monkeypatch.setattr(config, "SEASON_CARRYOVER", 0.5)
    async with db() as s:
        ps = await _players(s, 2)
        old = await _rate(s, ps["1"], 1450, games=30, mmr=1800, season=1)
        old.rd = 80.0
        await s.commit()
        new = await get_or_create_rating(s, ps["1"].id, "OVERALL", 2)
        assert new.lp == config.STARTING_LP and new.games_played == 0          # placements again
        assert new.mmr == pytest.approx(1650)                                   # half of +300 kept
        assert new.rd == pytest.approx(config.SEASON_RESET_RD)                  # less sure than before
        fresh = await get_or_create_rating(s, ps["2"].id, "OVERALL", 2)        # never played
        assert fresh.mmr == config.STARTING_MMR and fresh.rd == config.STARTING_RD


async def test_role_averages_carry_into_the_new_season(db):
    async with db() as s:
        s.add(RoleBaseline(role="MIDDLE", season=1, stats={"kda": {"n": 40, "mean": 3.1, "m2": 9.0}}, sample_size=40))
        await s.commit()
        base = await load_baselines(s, 2)
        row, _ = base["MIDDLE"]
        assert row.season == 2 and row.sample_size == 40 and row.stats["kda"]["mean"] == 3.1
        assert base["TOP"][0].stats is None                                     # nothing to carry


async def test_season_is_saved_and_the_higher_of_env_and_database_wins(db, monkeypatch):
    monkeypatch.setattr(config, "CURRENT_SEASON", 1)
    monkeypatch.setattr(config, "ENV_SEASON", 1)
    async with db() as s:
        assert await seasons.load(s) == 1
        assert await seasons.start_new(s) == (1, 2)
        assert config.CURRENT_SEASON == 2
        config.CURRENT_SEASON = 1                                   # a restart reads it back
        assert await seasons.load(s) == 2
        monkeypatch.setattr(config, "ENV_SEASON", 5)                # raised in .env: that wins
        assert await seasons.load(s) == 5


async def test_no_new_season_mid_game(db, monkeypatch):
    monkeypatch.setattr(config, "CURRENT_SEASON", 3)
    async with db() as s:
        await _players(s)
        lobby = await lm.create_lobby(s, "42", "c", "1", mode="captain")
        lobby.status = "active"
        await s.commit()
        with pytest.raises(seasons.SeasonError, match="still being played"):
            await seasons.start_new(s)
        assert config.CURRENT_SEASON == 3


# --------------------------------------------------------------------------- #
# Embeds                                                                       #
# --------------------------------------------------------------------------- #

async def test_leaderboard_post(db):
    async with db() as s:
        ps = await _players(s, 4)
        await _rate(s, ps["1"], 1300, games=10, wins=7)
        r = await _rate(s, ps["2"], 1200, games=3, wins=3)          # in placements
        r.streak = 3
        await s.commit()
        e = embeds.leaderboard_embed(await ladder.standings(s), config.CURRENT_SEASON).to_dict()
        lines = e["description"].split("\n")
        assert lines[0].startswith("🥇") and "<@1>" in lines[0] and "1300 LP" in lines[0] and "7W 3L" in lines[0]
        assert lines[1].startswith("🥈") and "⏳" in lines[1] and "🔥3" in lines[1]
        assert "<@" not in e["title"]
        final = embeds.leaderboard_embed([], 4, final=True).to_dict()
        assert "Season 4 final standings" == final["title"]


# --------------------------------------------------------------------------- #
# Discord side, through the real cogs with stand-ins for Discord               #
# --------------------------------------------------------------------------- #

class Msg:
    def __init__(self, channel, content=None, embed=None, view=None):
        self.id, self.channel, self.content, self.embed, self.view = next(_ids), channel, content, embed, view
        self.pinned = False

    async def edit(self, content=None, embed=None, view=None, **kw):
        if content is not None:
            self.content = content
        if embed is not None:
            self.embed = embed
        self.view = view

    async def pin(self, reason=None):
        self.pinned = True


class Channel:
    def __init__(self, name="chan"):
        self.id, self.name, self.sent = next(_ids), name, []
        self.mention = f"<#{self.id}>"

    async def send(self, content=None, embed=None, view=None, **kw):
        m = Msg(self, content, embed, view)
        self.sent.append(m)
        return m

    def get_partial_message(self, mid):
        for m in self.sent:
            if m.id == mid:
                return m
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")


class Bot:
    def __init__(self, *channels):
        self.channels = {c.id: c for c in channels}
        self.cogs = {}
        self.guilds = []

    def get_channel(self, cid):
        return self.channels.get(cid)

    def get_guild(self, gid):
        return None

    def get_cog(self, name):
        return self.cogs.get(name)

    def add_view(self, view):
        pass


class Inter:
    def __init__(self, user_id, message=None, values=None, admin=False):
        perms = discord.Permissions(manage_guild=True) if admin else discord.Permissions.none()
        self.user = SimpleNamespace(id=int(user_id), mention=f"<@{user_id}>", display_name=f"u{user_id}",
                                    name=f"u{user_id}", guild_permissions=perms, roles=[])
        self.guild_id, self.message, self.data = 42, message, {"values": values or []}
        self.private = []
        outer = self

        class Response:
            done = False

            async def defer(self, **kw):
                self.done = True

            def is_done(self):
                return self.done

            async def send_message(self, content=None, **kw):
                outer.private.append(content)

        class Followup:
            async def send(self, content=None, **kw):
                outer.private.append(content)

        self.response, self.followup = Response(), Followup()

    async def edit_original_response(self, content=None, embed=None, view=None, **kw):
        await self.message.edit(content=content, embed=embed, view=view)


@pytest.fixture
def lobby_cog(monkeypatch):
    from bot.cogs.lobby import LobbyCog
    queue = Channel("inhouse-queue")
    bot = Bot(queue)
    cog = LobbyCog(bot)
    bot.cogs["Lobby"] = cog
    cog.queue, cog.announced = queue, []

    async def fake_announce(session, lobby, names, public=None):
        cog.announced.append(lobby.id)
    monkeypatch.setattr(cog, "announce_teams", fake_announce)
    return cog


async def test_draft_dropdown_end_to_end(db, lobby_cog, monkeypatch):
    monkeypatch.setattr(config, "CAPTAIN_FIRST_PICK", "lower")
    async with db() as s:
        ps = await _players(s)
        for i in range(1, 11):
            await _rate(s, ps[str(i)], 1000 + i * 10)
        lobby = await lm.create_lobby(s, "42", str(lobby_cog.queue.id), "1", mode="captain")
        for i in range(1, 11):
            await lm.queue_player(s, lobby, str(i))
        cap1, cap2 = await lm.start_captain_draft(s, lobby)
        await lobby_cog.start_captain_space(s, lobby, cap1, cap2, lobby_cog.queue)
        board = lobby_cog.queue.sent[-1]
        assert lobby.draft_state["board"] == str(board.id)
        options = board.view.children[0].options
        assert len(options) == 8 and all("on the ladder" in o.description for o in options)
        assert board.content.startswith("<@9>")                    # the lower-ranked captain is up
        lid = lobby.id
    # the wrong person can't pick
    wrong = Inter(10, board, [options[0].value])
    await lobby_cog.handle_draft_pick(wrong)
    assert "not your turn" in wrong.private[-1]
    # captains alternate 1-2-2-1-1-2-2-1 through the dropdown until the draft ends
    for turn in (9, 10, 10, 9, 9, 10, 10, 9):
        pick = board.view.children[0].options[0].value
        await lobby_cog.handle_draft_pick(Inter(turn, board, [pick]))
    assert board.view is None and "Draft complete" in board.embed.description
    assert lobby_cog.announced == [lid]
    async with db() as s:
        done = await s.get(Lobby, lid)
        assert done.status == "active"
        assert sorted(lp.team for lp in done.players) == [1] * 5 + [2] * 5


async def test_leaderboard_channel_post_updates_in_place(db, monkeypatch):
    from bot.cogs.ranking import RankingCog
    board_ch = Channel("leaderboard")
    cog = RankingCog(Bot(board_ch))
    async with db() as s:
        assert "No leaderboard channel" in await cog.refresh_board("42")
        await settings.set_setting(s, "42", settings.KEY_LEADERBOARD_CHANNEL, str(board_ch.id))
        ps = await _players(s, 3)
        await _rate(s, ps["1"], 1100)
    assert "Posted" in await cog.refresh_board("42")
    assert len(board_ch.sent) == 1 and "<@1>" in board_ch.sent[0].embed.description
    assert "up to date" in await cog.refresh_board("42")           # nothing changed: no edit
    async with db() as s:
        await _rate(s, ps["2"], 1200)
    assert "Updated" in await cog.refresh_board("42")
    assert len(board_ch.sent) == 1                                  # edited, not re-posted
    assert board_ch.sent[0].embed.description.index("<@2>") < board_ch.sent[0].embed.description.index("<@1>")
    board_ch.sent.clear()                                           # someone deleted it
    assert "Posted" in await cog.refresh_board("42", force=True)
    # end of season: the post is frozen as final standings and a fresh one appears
    monkeypatch.setattr(config, "CURRENT_SEASON", config.CURRENT_SEASON + 1)
    await cog.close_season_board("42", config.CURRENT_SEASON - 1)
    assert "final standings" in board_ch.sent[0].embed.title and board_ch.sent[0].view is None
    assert board_ch.sent[-1].embed.title == f"Leaderboard: Season {config.CURRENT_SEASON}"


def test_control_panel_layout():
    from bot.cogs.panel import PanelCog, PanelView
    cog = PanelCog(Bot())
    view = PanelView(cog)
    ids = [c.custom_id for c in view.children]
    assert len(ids) == len(set(ids)) == 14
    assert all(c.row <= 4 for c in view.children)
    for cid in ids:
        assert callable(getattr(cog, f"do_{cid.split(':')[1]}"))   # every button has a handler


async def test_only_staff_can_use_the_panel(db):
    from bot.cogs.panel import is_staff
    from bot.services.server_setup import REMEMBER_PREFIX
    assert await is_staff(Inter(1, admin=True))
    assert not await is_staff(Inter(2))
    async with db() as s:
        await settings.set_setting(s, "42", REMEMBER_PREFIX + "mod_role", "555")
    mod = Inter(3)
    mod.user.roles = [SimpleNamespace(id=555)]
    assert await is_staff(mod)
