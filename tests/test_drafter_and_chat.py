import time

from bot import config
from bot.services import chat_service as cs
from bot.services import drafter_api as da
from bot.services.chat_service import TriggerInput


def test_extract_links_by_key_and_shape():
    raw = {"id": "abc123", "blueLink": "https://drafter.lol/abc/blue", "redLink": "https://drafter.lol/abc/red",
           "spectatorLink": "https://drafter.lol/abc/spec"}
    links = da.extract_links(raw)
    assert links == {"blue": "https://drafter.lol/abc/blue", "red": "https://drafter.lol/abc/red", "spectator": "https://drafter.lol/abc/spec"}
    assert da.extract_series_id(raw) == "abc123"
    nested = {"series": {"seriesId": 42, "links": {"team1": "https://x/1/blue", "team2": "https://x/1/red", "watch": "https://x/1/spectate"}}}
    assert da.extract_series_id(nested) == "42"
    assert set(da.extract_links(nested)) == {"blue", "red", "spectator"}
    shape_only = {"urls": ["https://d.lol/q/blue", "https://d.lol/q/red", "https://d.lol/q/spectator"]}
    assert set(da.extract_links(shape_only)) == {"blue", "red", "spectator"}


def test_parse_completed_drafts_variants():
    raw = {"drafts": [
        {"gameNumber": 1, "bluePicks": ["Ahri", "Lee Sin"], "redPicks": [{"name": "Jinx"}], "blueBans": ["Zed"], "redBans": []},
        {"gameNumber": 2, "completed": False, "bluePicks": []},
        {"game": 3, "blue": {"picks": ["Garen"], "bans": ["Darius"]}, "red": {"picks": ["Thresh"], "bans": ["Nami"]}},
    ]}
    out = da.parse_completed_drafts(raw)
    assert len(out) == 2
    assert out[0].blue_picks == ["Ahri", "Lee Sin"] and out[0].red_picks == ["Jinx"] and out[0].blue_bans == ["Zed"]
    assert out[1].blue_picks == ["Garen"] and out[1].red_bans == ["Nami"]
    assert da.parse_completed_drafts({}) == []


def _t(content, **kw):
    base = dict(content=content, mentions_bot=False, is_reply_to_bot=False, is_dm=False, channel_id=1, author_id=7,
                bot_name="Ranked Bot", last_interaction=None, now=time.time())
    base.update(kw)
    return TriggerInput(**base)


def test_triggers():
    assert cs.decide_trigger(_t("hey", mentions_bot=True)) == (True, "mention")
    assert cs.decide_trigger(_t("what?", is_reply_to_bot=True)) == (True, "reply")
    assert cs.decide_trigger(_t("yo", is_dm=True)) == (True, "dm")
    assert cs.decide_trigger(_t("ranked bot what's my rank")) == (True, "name")
    assert cs.decide_trigger(_t("hey ranked, gimme my rank")) == (True, "name")
    assert cs.decide_trigger(_t("anything", channel_id=555)) == (True, "bot_channel")
    assert cs.decide_trigger(_t("random chatter")) == (False, "none")
    assert cs.decide_trigger(_t("/rank")) == (False, "command")
    assert cs.decide_trigger(_t("hi", is_bot_author=True, mentions_bot=True)) == (False, "bot")


def test_followup_window():
    now = time.time()
    assert cs.decide_trigger(_t("and what about mid?", last_interaction=now - 30, now=now)) == (True, "followup")
    assert cs.decide_trigger(_t("and what about mid?", last_interaction=now - config.CHAT_FOLLOWUP_WINDOW_SECS - 5, now=now)) == (False, "none")
    ok, reason = cs.decide_trigger(_t("thanks!", last_interaction=now - 10, now=now))
    assert ok and reason == "closer"
    assert cs.closes_window("ok thanks") and not cs.closes_window("thanks for nothing, what about top?")


def test_system_prompt_has_context_and_memory():
    p = cs.build_system_prompt("Dan", "likes Yasuo", {"this user's rank": "Gold II 1150 LP"})
    assert "likes Yasuo" in p and "Gold II" in p and "performance score" in p


class _FakeTracker:
    pass


def test_followup_tracker():
    t = cs.FollowupTracker()
    assert t.get(1, 2) is None
    t.touch(1, 2)
    assert t.get(1, 2) is not None
    t.close(1, 2)
    assert t.get(1, 2) is None
