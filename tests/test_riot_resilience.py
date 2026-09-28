"""Game ID input, network failures, and the last-resort command error handler."""
import socket

import aiohttp
import pytest
from sqlalchemy import select

from bot import config
from bot.models.game import Game
from bot.models.player import Player
from bot.services import game_processor
from bot.services import riot_api as ra
from tests.fixtures import make_match


# --------------------------------------------------------------------------- #
# Game IDs                                                                    #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("typed, expected", [
    ("5650481942", "NA1_5650481942"),                 # what the post-game screen shows
    ("  5650481942\n", "NA1_5650481942"),
    ("NA1_5650481942", "NA1_5650481942"),
    ("na1_5650481942", "NA1_5650481942"),
    ("NA1-5650481942", "NA1_5650481942"),
    ("NA1 5650481942", "NA1_5650481942"),
    ("EUW1_7012345678", "EUW1_7012345678"),            # explicit prefix beats server region
    ("KR_7123456789", "KR_7123456789"),
    ("https://www.leagueofgraphs.com/match/na/5650481942", "NA1_5650481942"),
    ("https://www.leagueofgraphs.com/match/euw/7012345678#participant3", "EUW1_7012345678"),
])
def test_normalize_match_id(typed, expected):
    assert ra.normalize_match_id(typed, "na1") == expected


def test_bare_number_uses_configured_region():
    assert ra.normalize_match_id("7012345678", "euw1") == "EUW1_7012345678"


@pytest.mark.parametrize("bad", ["", "hello", "12345", "Danman#NA1"])
def test_normalize_rejects_non_ids(bad):
    with pytest.raises(ValueError, match="post-game screen"):
        ra.normalize_match_id(bad, "na1")


async def test_same_game_in_two_formats_is_a_duplicate(db):
    async with db() as session:
        for i in range(1, 11):
            session.add(Player(discord_id=str(i), discord_username=f"u{i}", riot_puuid=f"puuid-{i}"))
        await session.commit()
        m, t = make_match("NA1_5650481942")
        await game_processor.process_match(session, ra.normalize_match_id("5650481942", "na1"), "t",
                                           match_data=m, timeline_data=t)
        with pytest.raises(ValueError, match="already been processed"):
            await game_processor.process_match(session, ra.normalize_match_id("na1-5650481942", "na1"), "t",
                                               match_data=m, timeline_data=t)


# --------------------------------------------------------------------------- #
# Network failures                                                            #
# --------------------------------------------------------------------------- #

def _dns_error():
    """The exact exception from a real user's log on Windows."""
    key = aiohttp.client_reqrep.ConnectionKey("americas.api.riotgames.com", 443, True, True, None, None, None)
    return aiohttp.ClientConnectorDNSError(key, socket.gaierror(11001, "getaddrinfo failed"))


class _Resp:
    status = 200
    headers: dict = {}
    async def json(self): return {"ok": True}
    async def text(self): return ""


class _FlakySession:
    def __init__(self, fail_times, exc_factory=_dns_error):
        self.fail_times, self.calls, self.exc_factory = fail_times, 0, exc_factory

    def request(self, *a, **k):
        outer = self

        class CM:
            async def __aenter__(self_):
                outer.calls += 1
                if outer.calls <= outer.fail_times:
                    raise outer.exc_factory()
                return _Resp()

            async def __aexit__(self_, *x):
                return False
        return CM()


@pytest.fixture
def fast_retries(monkeypatch):
    monkeypatch.setattr(ra, "CONNECT_BACKOFF_SECS", 0)
    monkeypatch.setattr(config, "RIOT_API_KEY", "RGAPI-test")


def test_classify_connection_errors():
    import asyncio
    assert ra.classify_connection_error(_dns_error()) == "dns"
    assert ra.classify_connection_error(socket.gaierror(11001, "getaddrinfo failed")) == "dns"
    assert ra.classify_connection_error(asyncio.TimeoutError()) == "timeout"
    assert ra.classify_connection_error(aiohttp.ServerDisconnectedError()) == "connect"


async def test_one_network_blip_is_retried_silently(fast_retries):
    s = _FlakySession(fail_times=1)
    assert await ra.RiotClient(s)._get("americas.api.riotgames.com", "/x") == {"ok": True}
    assert s.calls == 2


async def test_persistent_dns_failure_becomes_clear_error(fast_retries):
    s = _FlakySession(fail_times=99)
    with pytest.raises(ra.RiotAPIError) as info:          # caught by every existing handler
        await ra.RiotClient(s)._get("americas.api.riotgames.com", "/x")
    e = info.value
    assert isinstance(e, ra.RiotConnectionError) and e.kind == "dns"
    assert s.calls == ra.CONNECT_RETRIES
    msg = ra.friendly_error(e)
    assert "couldn't find Riot's server" in msg and "never checked" in msg


async def test_network_failure_during_submit_can_be_retried(db, fast_retries, monkeypatch):
    """A failed fetch leaves the game retryable, and a later submit succeeds."""
    class DeadRiot:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get_match(self, mid): raise ra.RiotConnectionError("americas.api.riotgames.com", "dns", "x")
        async def get_match_timeline(self, mid): raise AssertionError("unreachable")

    monkeypatch.setattr(game_processor, "RiotClient", DeadRiot)
    async with db() as session:
        for i in range(1, 11):
            session.add(Player(discord_id=str(i), discord_username=f"u{i}", riot_puuid=f"puuid-{i}"))
        await session.commit()
        with pytest.raises(ra.RiotConnectionError):
            await game_processor.process_match(session, "NA1_5650481942", "t")
        g = await session.scalar(select(Game).where(Game.riot_match_id == "NA1_5650481942"))
        assert g.status == "error"
        m, t = make_match("NA1_5650481942")
        res = await game_processor.process_match(session, "NA1_5650481942", "t", match_data=m, timeline_data=t)
        assert res.game.status == "processed"


def test_match_404_message_is_about_games_not_riot_ids():
    msg = ra.friendly_error(ra.RiotAPIError(404, "Not found"), context="match", match_id="NA1_5650481942")
    assert "NA1_5650481942" in msg and "GameName#TAG" not in msg
    acct = ra.friendly_error(ra.RiotAPIError(404, "Not found"))
    assert "GameName#TAG" in acct


# --------------------------------------------------------------------------- #
# Last-resort command error handler                                           #
# --------------------------------------------------------------------------- #

class _FakeResponse:
    def __init__(self, done): self.done, self.sent = done, []
    def is_done(self): return self.done
    async def send_message(self, msg, ephemeral=False): self.sent.append(msg)


class _FakeFollowup:
    def __init__(self): self.sent = []
    async def send(self, msg, ephemeral=False): self.sent.append(msg)


class _FakeInteraction:
    def __init__(self, done):
        self.response, self.followup, self.command = _FakeResponse(done), _FakeFollowup(), None


async def test_unhandled_errors_never_leave_players_hanging():
    from discord import app_commands
    from bot.main import RankedBot
    bot = RankedBot()

    class Wrapped(app_commands.AppCommandError):
        def __init__(self, original): self.original = original

    # deferred command ("thinking..."): must answer through the followup
    inter = _FakeInteraction(done=True)
    await bot._on_app_command_error(inter, Wrapped(RuntimeError("boom")))
    assert inter.followup.sent and "went wrong" in inter.followup.sent[0]

    # a Riot network failure that slipped past a command still gets the friendly text
    inter = _FakeInteraction(done=False)
    await bot._on_app_command_error(inter, Wrapped(ra.RiotConnectionError("americas.api.riotgames.com", "dns", "x")))
    assert inter.response.sent and "couldn't find Riot's server" in inter.response.sent[0]
    await bot.close()
