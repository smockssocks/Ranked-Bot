# Player guide

Two things here. **Part 1** is a message to paste into Discord for your players.
**Part 2** is the full reference for you.

---

## Part 1: paste this into your server

Post this in a `#how-to-play` or `#rules` channel and pin it. It fits in one Discord
message. Replace `#inhouse-queue` with your actual queue channel.

```
**How to play inhouses** :trophy:

**1. Link your account (once)**
Run `/link` with your full Riot ID, for example:
`/link Danman#NA1`
That is the name and tag exactly as they appear in the League client. You cannot join a queue until you do this.

**2. Join the queue**
All queues happen in #inhouse-queue.
When a lobby is open, press the green **Join** button.
Changed your mind? Press **Leave** or run `/dequeue`.

**3. Get your pick**
At 10 players the host starts it. The bot makes fair teams and gives you a **pick position, 1 to 5**.
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

Stuck? Run `/help` any time.
```

---

## Part 2: your reference

### What a player does, in order

| Step | Command | Notes |
| --- | --- | --- |
| Link | `/link Danman#NA1` | Once, ever. Blocks queueing until done. |
| Join | **Join** button, or `/queue` | Role preferences only apply in balanced and captain lobbies. |
| Leave | **Leave** button, or `/dequeue` | Only while the lobby is still filling. |
| Play | nothing | Results are detected automatically. |
| Check | `/rank`, `/history`, `/explain` | `/explain` is the one that stops arguments. |

`/help` gives players all of this inside Discord, and it knows whether they have linked yet
and which channel your queues live in.

### Locking queues to one channel

By default a host can open a lobby anywhere. To keep it tidy:

```
/admin queuechannel channel:#inhouse-queue
```

After that, every `/queue`, `/dequeue`, `/inhouse ...` command and every lobby button only
works in that channel. Anyone who tries elsewhere gets a private message pointing them at the
right place, so it teaches rather than just failing.

Rank commands (`/rank`, `/leaderboard`, `/history`, `/explain`, `/howranked`, `/help`) keep
working everywhere, because those are not queue spam.

To check the current setting, run `/admin queuechannel` with no options.
To go back to allowing any channel, run `/admin queuechannel clear:True`.

### Suggested channel layout

| Channel | Who can post | What goes there |
| --- | --- | --- |
| `#how-to-play` | admins only | The pinned message from Part 1. |
| `#inhouse-queue` | everyone | The lobby panel and all queue commands. |
| `#results` | bot only | Optional. Set with `/admin resultschannel`. |
| `#mod-smurf-flags` | mods only | Set with `/admin modchannel`. |

Results are always posted in the queue channel. A results channel just adds a second copy,
which is useful if your queue channel gets chatty.

### Choosing which modes exist

Pick order is the default. It is the mode that makes your ladder measure all-round skill,
because players regularly end up off their main role and are scored against the average for
whatever role they actually played. In testing with the real rating engine, a one-trick
ranks far above an all-rounder under role queue, and far below them under pick order.

See or change what hosts are allowed to open:

```
/admin modes
/admin modes balanced:False captain:False     <- pick order only
/admin modes casual:False                     <- every game is ranked
```

| Mode | How teams are made | Roles |
| --- | --- | --- |
| **Pick order** (default) | Fairest split by rating | Claimed in champ select by pick position. Positions rotate fairly. |
| Balanced | Fairest split by rating and role preference | Assigned from preferences (role queue) |
| Captain draft | Two captains pick players | Captains set them |

**Casual lobbies** post results and stats but change nobody's LP. Use them for joke games,
practice, or teaching a newcomer. Hosts open one with `/inhouse create casual:True`.

### Running a game night as host

1. In the queue channel, run `/inhouse create`. It uses your default mode.
   Add `casual:True` if tonight shouldn't count.
2. Wait for 10. The bot pings you when the lobby fills.
3. Press **Start**. The bot posts teams and each player's pick position.
4. Create the custom lobby in the League client, **Tournament Draft**, and have players
   take their slots in pick order, top slot first.
5. Play. The bot posts results and closes the lobby by itself.

If the bot somehow misses a game, run `/inhouse submit` in the queue channel with the game ID
from the post-game screen, for example `/inhouse submit 5650481942`. Just the number is fine.

### Things players ask

**"Why did I lose LP when I played well?"**
Tell them to run `/explain`. It shows the exact stats that helped and hurt, and the numbers
behind their LP change.

**"Why did I get last pick again?"**
Picks rotate based on the last 20 pick order games. If they had late picks recently, they're
first in line for early ones. Over a few nights it evens out for everyone.

**"I got stuck on a role I don't play."**
That's the point of pick order. They're judged against the average for that role, not against
their main, so a decent off-role game is not punished. Their `/rank` shows how they do in
each role, and getting better at their weak ones is how they climb.

**"Why am I not gaining much for winning?"**
They were favoured, or they were carried. Both reduce the gain.

**"My rank says placements."**
First 5 games. LP moves about twice as fast until they are done.

**"I can't join the queue."**
They have not run `/link`, or they are in the wrong channel.
