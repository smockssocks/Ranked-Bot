"""Lobby mode controls, casual games, versatility, and the database auto-upgrade."""
import random

import pytest
from sqlalchemy import select

from bot import config
from bot.models.game import Game, GameParticipant
from bot.models.player import Player
from bot.models.rating import PlayerRating, RoleBaseline
from bot.services import game_processor, settings
from bot.services import rating_engine as re_
from tests.fixtures import make_match


# --------------------------------------------------------------------------- #
# Mode settings                                                               #
# --------------------------------------------------------------------------- #

def test_resolve_mode():
    r = settings.resolve_mode
    assert r(None, ["pick_order", "balanced"], "pick_order") == ("pick_order", None)
    assert r(None, ["balanced"], "pick_order") == ("balanced", None)          # default turned off
    assert r("balanced", ["pick_order", "balanced"], "pick_order") == ("balanced", None)
    mode, err = r("captain", ["pick_order"], "pick_order")
    assert mode is None and "turned off" in err and "Pick order" in err


async def test_mode_settings_roundtrip(db):
    async with db() as s:
        assert await settings.default_mode(s, "g") == "pick_order"
        assert await settings.allowed_modes(s, "g") == ["pick_order", "balanced", "captain"]
        assert await settings.casual_allowed(s, "g") is True
        await settings.set_setting(s, "g", settings.KEY_ALLOWED_MODES, "pick_order")
        await settings.set_setting(s, "g", settings.KEY_CASUAL_ALLOWED, "0")
        assert await settings.allowed_modes(s, "g") == ["pick_order"]
        assert await settings.casual_allowed(s, "g") is False
        await settings.set_setting(s, "g", settings.KEY_ALLOWED_MODES, "nonsense")
        assert await settings.allowed_modes(s, "g") == ["pick_order"]           # never empty


# --------------------------------------------------------------------------- #
# Casual games                                                                #
# --------------------------------------------------------------------------- #

async def _seed(session):
    for i in range(1, 11):
        session.add(Player(discord_id=str(i), discord_username=f"u{i}", riot_puuid=f"puuid-{i}"))
    await session.commit()


async def test_casual_game_changes_nobodys_rating(db):
    async with db() as session:
        await _seed(session)
        # an earlier ranked game so people have real ratings to protect
        m0, t0 = make_match("NA1_r1")
        await game_processor.process_match(session, "NA1_r1", "t", match_data=m0, timeline_data=t0)
        snap = lambda rows: {(r.player_id, r.role): (r.lp, r.mmr, r.rd, r.games_played, r.perf_mean) for r in rows}
        before = snap((await session.execute(select(PlayerRating))).scalars().all())
        bl_before = {b.role: b.sample_size for b in (await session.execute(select(RoleBaseline))).scalars()}

        m, t = make_match("NA1_c1", winner=200)
        res = await game_processor.process_match(session, "NA1_c1", "t", match_data=m, timeline_data=t, ranked=False)

        after = snap((await session.execute(select(PlayerRating))).scalars().all())
        bl_after = {b.role: b.sample_size for b in (await session.execute(select(RoleBaseline))).scalars()}
        assert after == before                                  # no LP, MMR, RD, games or form change
        assert bl_after == bl_before                            # community averages untouched
        assert res.game.status == "casual"
        assert len(res.results) == 10 and all(r.lp_delta == 0 and not r.placement for r in res.results)
        assert any(abs(r.perf_score) > 0 for r in res.results) # stats still computed and shown
        parts = (await session.execute(select(GameParticipant).where(GameParticipant.game_id == res.game.id))).scalars().all()
        assert len(parts) == 10 and all(p.lp_delta == 0 and p.lp_before == p.lp_after for p in parts)
        with pytest.raises(ValueError):                         # still can't be submitted twice
            await game_processor.process_match(session, "NA1_c1", "t", match_data=m, timeline_data=t)
        with pytest.raises(ValueError):                         # nothing to roll back
            await game_processor.rollback_match(session, "NA1_c1")


async def test_auto_detect_respects_casual_lobby(db, monkeypatch):
    from bot.services import auto_detect, lobby_manager as lm
    match, tl = make_match("NA1_autoc", queue_id=0)

    class FakeRiot:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get_recent_match_ids(self, *a, **k): return ["NA1_autoc"]
        async def get_match(self, mid): return match
        async def get_match_timeline(self, mid): return tl

    monkeypatch.setattr(auto_detect, "RiotClient", FakeRiot)
    async with db() as session:
        await _seed(session)
        lobby = await lm.create_lobby(session, "g", "c", "1", ranked=False)
        for i in range(1, 11):
            await lm.queue_player(session, lobby, str(i))
        await lm.make_teams(session, lobby)
    processed = await auto_detect.poll_once(db)
    assert len(processed) == 1 and processed[0][1].game.status == "casual"
    async with db() as session:
        rows = (await session.execute(select(PlayerRating).where(PlayerRating.games_played > 0))).scalars().all()
        assert rows == []


# --------------------------------------------------------------------------- #
# Versatility                                                                 #
# --------------------------------------------------------------------------- #

def test_versatility_rewards_all_round_skill():
    R = re_.ROLES
    one_trick = re_.versatility({"MIDDLE": (20, 1.5), **{r: (5, -1.0) for r in R if r != "MIDDLE"}})
    all_round = re_.versatility({r: (8, 0.6) for r in R})
    one_each = re_.versatility({r: (1, 2.0) for r in R})
    no_supp = re_.versatility({r: (8, 0.6) for r in R if r != "UTILITY"})
    new = re_.versatility({})
    assert all_round.score > one_trick.score
    assert all_round.score > one_each.score, "one game per role must not beat proven skill"
    assert all_round.score > no_supp.score and "UTILITY" in no_supp.weakest
    assert new.score == pytest.approx(re_.VERSATILITY_PRIOR)
    assert all_round.proven == 5 and one_each.proven == 0 and no_supp.proven == 4


def _season(skill: dict, role_queue: bool, seed: int, games: int = 150) -> int:
    rng = random.Random(seed)
    s = re_.RatingState()
    best = max(skill, key=skill.get)
    for _ in range(games):
        if role_queue:
            role = best
        else:
            role = best if rng.randint(1, 5) <= 2 else rng.choice(re_.ROLES)
        ps = max(-3.0, min(3.0, skill[role] + rng.gauss(0, 0.8)))
        won = rng.random() < max(0.05, min(0.95, 0.5 + 0.12 * ps))
        u = re_.compute_update(s, won, ps, [ps] + [rng.gauss(0, 0.7) for _ in range(4)], [s.mmr], [s.mmr])
        s = re_.RatingState(mmr=u.mmr_after, rd=u.rd_after, lp=u.lp_after, games=s.games + 1,
                            perf_mean=u.perf_mean_after, perf_var=u.perf_var_after)
    return s.lp


def test_pick_order_makes_lp_reward_versatility():
    """
    The design claim behind making pick order the default: with the same engine,
    role queue ranks a one-trick above an all-rounder, and pick order reverses it.
    """
    one_trick = {"MIDDLE": 1.5, "TOP": -1.0, "JUNGLE": -1.0, "BOTTOM": -1.0, "UTILITY": -1.0}
    all_round = {r: 0.6 for r in re_.ROLES}
    avg = lambda skill, rq: sum(_season(skill, rq, k) for k in range(12)) / 12
    assert avg(one_trick, True) > avg(all_round, True) + 500      # role queue rewards the one-trick
    assert avg(all_round, False) > avg(one_trick, False) + 500    # pick order rewards the all-rounder


# --------------------------------------------------------------------------- #
# Database auto-upgrade                                                       #
# --------------------------------------------------------------------------- #

async def test_existing_database_gains_new_columns(db):
    """A database from the previous version (no lobbies.ranked) upgrades itself on start."""
    from bot.db.database import engine, init_db
    async with engine.begin() as c:
        await c.exec_driver_sql("ALTER TABLE lobbies DROP COLUMN ranked")
        await c.exec_driver_sql("INSERT INTO lobbies (guild_id, channel_id, host_discord_id) VALUES ('g','c','h')")
    added = await init_db()
    assert added == ["lobbies.ranked"]
    assert await init_db() == []                                   # idempotent
    from bot.models.lobby import Lobby
    async with db() as session:
        lobby = await session.scalar(select(Lobby))
        assert lobby.ranked is True                                # old lobbies count as ranked
