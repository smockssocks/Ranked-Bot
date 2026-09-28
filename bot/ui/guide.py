"""The how-to-play message that /admin setup posts and pins. Must stay under Discord's 2000 characters."""
from __future__ import annotations


def how_to_play(queue_channel_id: int | None = None, chat_channel_id: int | None = None) -> str:
    queue = f"<#{queue_channel_id}>" if queue_channel_id else "the queue channel"
    chat = f" Chat about games in <#{chat_channel_id}>." if chat_channel_id else ""
    return f"""**How to play inhouses** :trophy:

**1. Link your account (once)**
Run `/link` with your full Riot ID, for example:
`/link Danman#NA1`
That is the name and tag exactly as they appear in the League client. The bot asks you to switch your profile icon once to prove it's your account. You cannot join a queue until you do this.

**2. Join the queue**
All queues happen in {queue}.{chat}
When a lobby is open, press the green **Join** button.
Changed your mind? Press **Leave** or run `/dequeue`.

**3. Get your pick**
At 10 players the host starts it. The bot makes fair teams, gives you a **pick position, 1 to 5**, and opens a **private thread for your game** under the queue channel, with how to get into the game. Your team also gets its own voice channel, and if you're already in voice the bot moves you there.
**There is no role queue.** In champ select, Pick 1 calls their role first, then Pick 2, and so on. Line up in the custom lobby in pick order.
Picks rotate: a late pick now means an early pick soon.

**4. Play**
Join the custom game and play. **You do not report anything.** The bot finds the game and posts everyone's LP within a couple of minutes.

**Why no role queue?**
This server ranks all-round skill. You're always judged against the average for the role you actually played, so a great support game counts as much as a great mid game. Players who are good at every role climb highest.

**Check your rank**
`/rank` tier, LP, and how you do in each role
`/explain` exactly why your last game gained or lost LP
`/leaderboard` the ladder, or `sort:Versatility` for all-round skill
`/howranked` how it all works

Winning matters most, but how you played counts too. Your first 5 games are placements and move faster.

Stuck? Run `/help` any time."""
