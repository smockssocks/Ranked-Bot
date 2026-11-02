"""DISCORD_GUILD_ID takes one server ID or several, so the bot can serve more than one server."""
from bot import config


def test_one_or_several_server_ids(monkeypatch):
    monkeypatch.setenv("X_IDS", "123456789012345678")
    assert config._ids("X_IDS") == [123456789012345678]
    monkeypatch.setenv("X_IDS", "123456789012345678, 223456789012345678;323456789012345678")
    assert config._ids("X_IDS") == [123456789012345678, 223456789012345678, 323456789012345678]
    monkeypatch.setenv("X_IDS", "your_guild_id_here")              # the .env.example placeholder
    assert config._ids("X_IDS") == []
    monkeypatch.delenv("X_IDS")
    assert config._ids("X_IDS") == []
