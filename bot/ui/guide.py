"""The how-to-play message that /admin setup posts and pins. Must stay under Discord's 2000 characters."""
from __future__ import annotations

TEAMS = {
    "captain": (
        "**3. Accept, then get drafted**\n"
        "At 10 players you're pinged to press **Accept** (declining means a short queue timeout). "
        "The two players highest on the leaderboard become captains and pick teams from a dropdown, "
        "snake style. Captains see everyone's rank and roles, so set yours when you join.\n"
        "You get a **private thread for your game** with how to get in, and your team gets its own "
        "voice channel."
    ),
    "pick_order": (
        "**3. Accept, then get your pick**\n"
        "At 10 players you're pinged to press **Accept** (declining means a short queue timeout). "
        "The bot makes fair teams and gives you a **pick position, 1 to 5**. No role queue: in champ "
        "select Pick 1 calls their role first, then Pick 2, and so on.\n"
        "You get a **private thread for your game**, and your team gets its own voice channel."
    ),
    "balanced": (
        "**3. Accept, then play**\n"
        "At 10 players you're pinged to press **Accept** (declining means a short queue timeout). "
        "The bot makes fair teams from everyone's rating and roles.\n"
        "You get a **private thread for your game**, and your team gets its own voice channel."
    ),
}


def how_to_play(queue_channel_id: int | None = None, chat_channel_id: int | None = None,
                mode: str = "captain") -> str:
    queue = f"<#{queue_channel_id}>" if queue_channel_id else "the queue channel"
    chat = f" Chat about games in <#{chat_channel_id}>." if chat_channel_id else ""
    teams = TEAMS.get(mode, TEAMS["captain"])
    return f"""**How to play inhouses** :trophy:

**1. Link your account (once)**
Press **Link my account** below and type your Riot ID exactly as the League client shows it, like `Danman#NA1`. You'll switch your profile icon once to prove it's yours. You can't queue until you're linked.

**2. Join the queue**
All queues happen in {queue}.{chat}
When a lobby is open, press **Join** and pick your main and second role (remembered for next time). Changed your mind? Press **Leave**.

{teams}

**4. Play, then confirm**
Join the custom game and play. You don't report the result: the bot finds the game. It then asks each of you to confirm **the role you played and your champion** from two dropdowns (a few seconds), and posts LP.

**Your rank**
Winning matters most, but how you played counts too, judged against everyone else who played your role. Your first 5 games are placements and move faster.
Press **My rank** or **My last game** below, check the leaderboard channel, or use `/rank`, `/explain` and `/leaderboard`.

Stuck? Run `/help` any time."""
