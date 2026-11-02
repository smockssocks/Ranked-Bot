# Player guide

Two things here. **Part 1** is a message to paste into Discord for your players.
**Part 2** is the full reference for you.

---

## Part 1: paste this into your server

`/admin setup` posts and pins this for you in `#how-to-play`, with a clickable link to your
queue channel and the **Link my account**, **My rank** and **My last game** buttons it
mentions. If you'd rather post it yourself, it fits in one Discord message; replace
`#inhouse-queue` with your actual queue channel.

```
**How to play inhouses** :trophy:

**1. Link your account (once)**
Press **Link my account** below and type your Riot ID exactly as the League client shows it, like `Danman#NA1`. You'll switch your profile icon once to prove it's yours. You can't queue until you're linked.

**2. Join the queue**
All queues happen in #inhouse-queue.
When a lobby is open, press **Join** and pick your main and second role (remembered for next time). Changed your mind? Press **Leave**.

**3. Accept, then get drafted**
At 10 players you're pinged to press **Accept** (declining means a short queue timeout). The two players highest on the leaderboard become captains and pick teams from a dropdown, snake style. Captains see everyone's rank and roles, so set yours when you join.
You get a **private thread for your game** with how to get in, and your team gets its own voice channel.

**4. Play**
Join the custom game and play. **You don't report anything.** The bot finds the game and posts everyone's LP within a couple of minutes.

**Your rank**
Winning matters most, but how you played counts too, judged against everyone else who played your role. Your first 5 games are placements and move faster.
Press **My rank** or **My last game** below, check the leaderboard channel, or use `/rank`, `/explain` and `/leaderboard`.

Stuck? Run `/help` any time.
```

---

## Part 2: your reference

### What a player does, in order

| Step | Command | Notes |
| --- | --- | --- |
| Link | **Link my account** button, or `/link Danman#NA1` | Once, ever. Blocks queueing until done. |
| Join | **Join** button, or `/queue` | Then choose main and second role (or Fill) from the dropdowns. Remembered. |
| Leave | **Leave** button, or `/dequeue` | Only while the lobby is still filling. |
| Play | nothing | Results are detected automatically. |
| Confirm | **Confirm my role & champion** in the game thread | Two dropdowns, after the game. Decides which role they're scored in. |
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

**Captain draft** is the default and the only mode switched on out of the box. The two players
highest on the season leaderboard captain, the lower-ranked of them picks first, and they pick
from a dropdown that shows each player's ladder place, LP and preferred roles.

See or change what hosts are allowed to open:

```
/admin modes
/admin modes pick_order:False balanced:False captain:True default:Captain draft   <- captain only
/admin modes casual:False                     <- every game is ranked
```

| Mode | How teams are made | Roles |
| --- | --- | --- |
| **Captain draft** (default) | The top two on the leaderboard pick players | Suggested from preferences; captains can move people |
| Pick order | Fairest split by rating | Claimed in champ select by pick position |
| Balanced | Fairest split by rating and role preference | Assigned from preferences (role queue) |

**Casual lobbies** post results and stats but change nobody's LP. Use them for joke games,
practice, or teaching a newcomer. Hosts open one with `/inhouse create casual:True`.

### Running a game night as host

Mods run game nights from the **control panel** in #bot-admin (run `/panel` there if it's
missing). Every button answers privately.

1. Press **Open lobby** (or **Open casual lobby** if tonight shouldn't count). The lobby appears
   in the queue channel with you as host. `/inhouse create` does the same.
2. Wait for 10. When the lobby fills, the bot pings all ten players to **Accept**. The
   message updates live as people accept. Anyone who declines or doesn't answer in time
   (90 seconds) is removed with a 3-minute queue timeout, and the queue reopens for their
   spot. Someone AFK? **Remove player** on the panel; that calls off the check without a timeout.
3. When all ten accept, the draft starts by itself. The top two on the leaderboard captain,
   and the draft board with its dropdown appears in a private thread under the queue channel,
   named after you (e.g. "Dan's inhouse"). Want different captains? **Set captains** on the panel.
4. When the draft is done, the thread gets the teams and the tournament code (or lobby name
   and password, and who creates the lobby). Each team gets its own voice channel, and players
   already in voice are moved in. Captains fix roles with **Set Blue roles** / **Set Red roles**
   under the teams. Meanwhile the next lobby has already opened in the queue channel, so the
   next ten can queue while this game is played.
5. Play. When the bot finds the game, it asks the ten players in the thread to confirm the role
   they played and their champion (5 minutes at most; **Finish now** if people wander off). Then
   it posts results, updates #leaderboard and closes the lobby. The thread closes and the voice
   channels go away a little while after the game.

If a captain goes AFK, the host or an admin can pick from the dropdown for them, unless they
are the other captain.

Testing on your own? `/admin testfill` fills the lobby with filler players (they accept the
ready check by themselves) and `/admin testclear` removes them afterwards.

If the bot somehow misses a game, press **Submit game** and type the game ID from the
post-game screen, for example `5650481942`. **Undo a game** takes one back.

### Banning someone from inhouses

Moderators can stop a player queueing for a while without kicking them from the server. On
the control panel: **Queue ban** (choose the member, then type the length and reason) and
**Lift ban**. Or with commands:

```
/queueban add @player 3d Leaving games early
/queueban lift @player
/queueban list
/queueban history @player
```

Durations look like `30m`, `12h`, `3d`, `1w` or `permanent`. A banned player can't queue,
can't be force-queued and can't host. They get a DM with the reason and when it ends, and
the ban is logged in your mod channel. It ends on its own when the time is up.

### Things players ask

**"Why did I lose LP when I played well?"**
Tell them to run `/explain`. It shows the exact stats that helped and hurt, and the numbers
behind their LP change.

**"Why am I always / never a captain?"**
Captains are the two highest on the season leaderboard among the ten in the lobby. Climb the
ladder and you'll captain. A mod can choose different captains with **Set captains**.

**"I got stuck on a role I don't play."**
They're judged against the average for the role they actually played, not against their main,
so a decent off-role game is not punished. Setting their roles when they join helps the
captain place them.

**"Why am I not gaining much for winning?"**
They were favoured, or they were carried. Both reduce the gain.

**"My rank says placements."**
First 5 games. LP moves about twice as fast until they are done.

**"Why do I have to change my icon to link?"**
It proves the account is theirs, so nobody can link someone else's main. It takes a minute and
they can change it straight back afterwards.

**"I can't join the queue."**
They have not run `/link`, or they are in the wrong channel.
