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
Want a specific role? Use `/queue role:Mid secondary:Top` instead.
Changed your mind? Press **Leave** or run `/dequeue`.

**3. Play**
At 10 players the host starts it. The bot posts the two teams and your role.
Join the custom game and play. **You do not report anything.** The bot finds the game and posts everyone's LP within a couple of minutes.

**Check your rank**
`/rank` your tier, LP, record and per-role stats
`/history` your recent games
`/explain` exactly why your last game gained or lost LP
`/leaderboard` the ladder
`/howranked` how the ranking actually works

**How LP works, briefly**
Winning matters most, but how you played counts too. You are compared to your lane opponent and to the average for your role on this server, never to your champion. Play great in a loss and you barely drop. Get carried in a win and you gain less. Your first 5 games are placements and move faster.

Stuck? Run `/help` any time.
```

---

## Part 2: your reference

### What a player does, in order

| Step | Command | Notes |
| --- | --- | --- |
| Link | `/link Danman#NA1` | Once, ever. Blocks queueing until done. |
| Join | **Join** button, or `/queue` | `role` and `secondary` are optional preferences. |
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

### Running a game night as host

1. In the queue channel, `/inhouse create` and pick a mode:
   - **Balanced** is the easy default. The bot makes the fairest teams it can.
   - **Captain draft** for a more social night. Two captains pick.
   - **Pick order** gives roles to whoever queued first.
2. Wait for 10. The bot pings you when the lobby fills.
3. Press **Start**.
4. Create the custom lobby in the League client and play.
5. Do nothing else. The bot posts results and closes the lobby.

If the bot somehow misses a game, run `/inhouse submit MATCH_ID` in the queue channel.

### Things players ask

**"Why did I lose LP when I played well?"**
Tell them to run `/explain`. It shows the exact stats that helped and hurt, and the numbers
behind their LP change.

**"Why am I not gaining much for winning?"**
They were favoured, or they were carried. Both reduce the gain.

**"My rank says placements."**
First 5 games. LP moves about twice as fast until they are done.

**"I can't join the queue."**
They have not run `/link`, or they are in the wrong channel.
