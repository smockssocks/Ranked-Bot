"""Linking a Riot account requires proving you own it (profile icon check)."""
import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from bot.models.link import LinkRequest
from bot.models.player import Player
from bot.models.rating import PlayerRating
from bot.services import account_link as al


class FakeRiot:
    """Accounts by Riot ID, and each account's current profile icon (which only its owner can change)."""
    def __init__(self):
        self.accounts = {("Danman", "NA1"): "puuid-dan", ("Victim", "NA1"): "puuid-victim"}
        self.icons = {"puuid-dan": 4000, "puuid-victim": 5000}
        self.summoner_calls = 0

    async def get_account_by_riot_id(self, name, tag):
        return {"puuid": self.accounts[(name, tag)], "gameName": name, "tagLine": tag}

    async def get_summoner_by_puuid(self, puuid):
        self.summoner_calls += 1
        return {"profileIconId": self.icons[puuid], "summonerLevel": 250}

    async def get_league_entries_by_puuid(self, puuid):
        return []


NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


async def test_link_requires_changing_to_the_named_icon(db):
    riot = FakeRiot()
    async with db() as s:
        c = await al.begin(s, riot, "111", "dan", "Danman#NA1", rng=random.Random(1), now=NOW)
        assert isinstance(c, al.Challenge) and c.icon_id in al.STARTER_ICONS and c.icon_id != 4000
        with pytest.raises(al.LinkError, match=f"still \\*\\*#4000\\*\\*, not \\*\\*#{c.icon_id}"):
            await al.verify(s, riot, "111", "dan", now=NOW + timedelta(minutes=1))
        assert await s.scalar(select(LinkRequest)) is not None            # still pending, can retry
        riot.icons["puuid-dan"] = c.icon_id                                 # the owner changes their icon
        linked = await al.verify(s, riot, "111", "dan", now=NOW + timedelta(minutes=2))
        assert linked.riot_id == "Danman#NA1" and linked.transferred_from is None
        p = await s.scalar(select(Player).where(Player.discord_id == "111"))
        assert p.riot_puuid == "puuid-dan" and p.link_verified is True
        assert await s.scalar(select(PlayerRating).where(PlayerRating.player_id == p.id)) is not None
        assert await s.scalar(select(LinkRequest)) is None


async def test_impostor_cannot_link_someone_elses_account(db):
    riot = FakeRiot()
    async with db() as s:
        c = await al.begin(s, riot, "666", "impostor", "Victim#NA1", rng=random.Random(2), now=NOW)
        for minute in (1, 3, 9):          # the victim never changes their icon, so this can never pass
            with pytest.raises(al.LinkError, match="still"):
                await al.verify(s, riot, "666", "impostor", now=NOW + timedelta(minutes=minute))
        with pytest.raises(al.LinkError, match="expired"):
            await al.verify(s, riot, "666", "impostor", now=NOW + timedelta(minutes=al.VERIFY_MINUTES + 1))
        assert await s.scalar(select(Player).where(Player.riot_puuid == "puuid-victim")) is None


async def test_running_link_again_keeps_the_same_icon(db):
    riot = FakeRiot()
    async with db() as s:
        first = await al.begin(s, riot, "111", "dan", "Danman#NA1", rng=random.Random(3), now=NOW)
        riot.icons["puuid-dan"] = first.icon_id                            # already switched, then re-ran /link
        again = await al.begin(s, riot, "111", "dan", "Danman#NA1", rng=random.Random(99),
                               now=NOW + timedelta(minutes=2))
        assert again.icon_id == first.icon_id and again.expires_at == first.expires_at
        later = await al.begin(s, riot, "111", "dan", "Danman#NA1", rng=random.Random(5),
                               now=NOW + timedelta(minutes=al.VERIFY_MINUTES + 5))
        assert later.expires_at > first.expires_at                          # expired: a fresh challenge


async def test_owner_reclaims_an_account_someone_else_linked(db):
    riot = FakeRiot()
    async with db() as s:
        s.add(Player(discord_id="666", discord_username="squatter", riot_puuid="puuid-dan", summoner_name="Danman#NA1"))
        await s.commit()
        c = await al.begin(s, riot, "111", "dan", "Danman#NA1", rng=random.Random(4), now=NOW)
        assert c.reclaim_from == "squatter"
        riot.icons["puuid-dan"] = c.icon_id
        linked = await al.verify(s, riot, "111", "dan", now=NOW + timedelta(minutes=1))
        assert linked.transferred_from == "squatter"
        squatter = await s.scalar(select(Player).where(Player.discord_id == "666"))
        assert squatter.riot_puuid is None and squatter.link_verified is False
        owner = await s.scalar(select(Player).where(Player.discord_id == "111"))
        assert owner.riot_puuid == "puuid-dan" and owner.link_verified


async def test_switching_accounts_needs_a_moderator(db):
    riot = FakeRiot()
    async with db() as s:
        s.add(Player(discord_id="111", discord_username="dan", riot_puuid="puuid-dan",
                     summoner_name="Danman#NA1", link_verified=True))
        await s.commit()
        with pytest.raises(al.LinkError, match="moderator"):
            await al.begin(s, riot, "111", "dan", "Victim#NA1", now=NOW)


async def test_relinking_your_verified_account_just_updates_the_name(db):
    riot = FakeRiot()
    riot.accounts[("Danman2", "NA1")] = "puuid-dan"                      # Riot ID renamed, same account
    async with db() as s:
        s.add(Player(discord_id="111", discord_username="dan", riot_puuid="puuid-dan",
                     summoner_name="Danman#NA1", link_verified=True))
        await s.commit()
        res = await al.begin(s, riot, "111", "dan", "Danman2#NA1", now=NOW)
        assert isinstance(res, al.Linked) and res.refreshed_only and res.riot_id == "Danman2#NA1"
        assert riot.summoner_calls == 0                                     # no icon check needed


async def test_old_unverified_link_can_be_verified(db):
    riot = FakeRiot()
    async with db() as s:
        s.add(Player(discord_id="111", discord_username="dan", riot_puuid="puuid-dan",
                     summoner_name="Danman#NA1", link_verified=False))      # linked before verification existed
        await s.commit()
        c = await al.begin(s, riot, "111", "dan", "Danman#NA1", rng=random.Random(6), now=NOW)
        assert isinstance(c, al.Challenge) and c.reclaim_from is None
        riot.icons["puuid-dan"] = c.icon_id
        await al.verify(s, riot, "111", "dan", now=NOW + timedelta(minutes=1))
        assert (await s.scalar(select(Player).where(Player.discord_id == "111"))).link_verified


async def test_verify_without_link_and_cancel(db):
    riot = FakeRiot()
    async with db() as s:
        with pytest.raises(al.LinkError, match="no link in progress"):
            await al.verify(s, riot, "111", "dan")
        await al.begin(s, riot, "111", "dan", "Danman#NA1", now=NOW)
        assert await al.cancel(s, "111") is True
        assert await al.cancel(s, "111") is False


def test_riot_id_parsing_and_icon_choice():
    assert al.parse_riot_id(" Danman#NA1 ") == ("Danman", "NA1")
    assert al.parse_riot_id("Name With Spaces#EUW") == ("Name With Spaces", "EUW")
    for bad in ("Danman", "#NA1", "Danman#", ""):
        with pytest.raises(al.LinkError):
            al.parse_riot_id(bad)
    rng = random.Random(0)
    for current in (None, 0, 7, 28, 4000):
        for _ in range(50):
            icon = al.pick_icon(current, rng)
            assert icon in al.STARTER_ICONS and icon != current
