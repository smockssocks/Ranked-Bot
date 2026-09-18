# Ranked Inhouse Bot

A Discord bot that runs League of Legends inhouse (custom) games end to end and ranks
players with a system built for small communities, not Riot's ladder.

- **Lobbies with buttons** – `/inhouse create`, Join/Leave/Start, captain draft, MMR-balanced
  teams or first-come-first-serve roles.
- **Results pick themselves up** – once teams are set the bot watches the players' Riot
  match history, finds the custom game, processes it and posts the LP changes. No `/submit`.
- **A fair, explainable rating** – win/loss is the biggest factor, but every player also gets a
  performance score built from the Riot match + timeline data (lane leads vs *their* opponent,
  kill participation, deaths, damage share, objectives, vision, healing/CC for supports). Carry a
  loss and you lose nothing (or gain a little). Get carried and you gain less. `/explain` shows
  exactly what moved your LP.
- **Anti-smurf** – fresh accounts, players performing far above their rating, and new accounts
  that play like an existing member are flagged to a mod channel with evidence.
- **drafter.lol drafts** – blue/red/spectator draft links posted with the teams; picks and bans
  posted when the draft completes.
- **Talks back** – optional OpenRouter-powered chat with cheap rolling memory. Works via @mention,
  reply, its name, a bot channel, or just continuing a conversation.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in DISCORD_TOKEN, DISCORD_GUILD_ID, RIOT_API_KEY
python -m bot.main
```

The bot needs the **Message Content** and **Server Members** privileged intents enabled in the
Discord developer portal. With the default `DATABASE_URL` it uses a local SQLite file and creates
the tables itself. For Postgres set `DATABASE_URL=postgresql+asyncpg://...` and run
`alembic upgrade head` (migration `0002` upgrades a database created by the first version and
lifts existing LP by the new 1000 starting point).

Run the tests with `pytest` (no network, no Discord, SQLite in a temp dir).

## Commands

| Command | Who | What |
| --- | --- | --- |
| `/link GameName#TAG` | anyone | Link your Riot account. Required before queueing. |
| `/queue [role] [secondary]`, `/dequeue` | anyone | Join/leave the open lobby (buttons do the same). |
| `/inhouse create [mode]` | host | Open a lobby. Modes: `captain`, `balanced`, `pick_order`. |
| `/inhouse start [random_captains]` | host/admin | Make teams or start the captain draft. |
| `/inhouse pick @player` | captains | Snake-draft picks (1-2-2-1-1-2-2-1). |
| `/inhouse role @player role` | captain/host | Move a teammate to a role (swaps). |
| `/inhouse teams`, `/inhouse status`, `/inhouse cancel` | | |
| `/inhouse draft [fearless] [games]` | host | (Re)create a drafter.lol draft. |
| `/inhouse submit MATCH_ID` | anyone | Manual fallback if auto-detect misses a game. |
| `/rank [@player]`, `/history`, `/explain [games_ago]`, `/leaderboard [role]`, `/howranked` | anyone | Ratings. |
| `/chat status`, `/chat forget` | anyone | Chat availability / wipe the bot's memory of you. |
| `/flags list|show|resolve` | mods | Review anti-smurf flags. |
| `/admin link|unlink|submit|reprocess|rollback|reset|modchannel|resultschannel|baselines|season` | Manage Server | Administration. |

## How the ranking works

Everything lives in `bot/services/rating_engine.py` (pure math, fully unit tested) and
`bot/services/metrics.py` (Riot JSON → metrics). Change the model there and nowhere else.

Each player has, per role and overall:

| field | meaning |
| --- | --- |
| `lp` | the visible ladder number. Everyone starts at 1000. Tiers (`ranks.py`) are cosmetic bands of LP. |
| `mmr`, `rd` | hidden skill estimate and how unsure we are (Glicko-style). Teams are balanced on MMR. `rd` shrinks with games and grows with inactivity, so newcomers and returning players move fast, veterans move slowly. |
| `perf_mean`, `perf_var` | running performance score and its variance → **consistency**. |

### Performance score (PS)

For every game the bot pulls the match and its minute-by-minute timeline and computes ~20
metrics per player (see `METRICS` in `rating_engine.py`). Three design rules make it fair:

1. **Relative, not absolute.** Lane metrics (gold/XP/CS at 10 and 15, and a time-weighted
   "lane trajectory") are differences against the player in the *same position* on the other
   team. Damage and damage taken are *shares* of the team total. Objective participation is the
   share of the team's dragons/heralds/barons/turrets you took part in.
2. **Normalised per role on *this* server.** Each metric is turned into a z-score against a
   per-role community baseline that learns from every processed game (Welford), blended with
   sensible priors while the sample is small and with the 10 players in the game. A support
   with 1 CS/min is normal; a mid with 1 CS/min is not. Z-scores are clipped at ±2.5 so one
   absurd stat cannot carry the score, and deaths carry a large negative weight so farming
   while feeding does not pay.
3. **Time-dependent weights.** Every metric is tagged early/mid/late and its weight is scaled
   by game length: a 19-minute stomp is judged mostly on laning, a 45-minute game mostly on
   teamfights, objectives and deaths. This is the "operator changes with time" idea from the
   design notes in a form you can read and tune.

Role weight profiles (`ROLE_WEIGHTS`) decide what matters: supports are judged on vision,
KP, healing/shielding and CC, junglers on objectives and KP, laners on lane leads and damage
share. Champion identity is never an input.

### LP change

```
outcome term      = 2K · (S − E)            S = 1 win / 0 loss, E = expected win prob from team MMR
performance term  = K · Wp · tanh(perf / 2) · consistency
perf              = 0.7·PS + 0.3·(PS − team mean PS)          ← "carry factor"
K                 = 20 × uncertainty(rd)    (≈ ×2 during placements)
```

`Wp` is 1.0 on a win, 1.4 for a good performance in a loss (mitigation) and 1.0 for a bad one.
For a veteran in an even game that gives:

| PS | win | loss |
| --- | --- | --- |
| −2 | +5 | −35 |
| −1 | +11 | −29 |
| 0 | +20 | −20 |
| +1 | +29 | −7 |
| +2 | +35 | +1 |
| +3 | +38 | +6 |

A win never loses LP (floor +2). A loss nets positive only around PS +2, i.e. the player
clearly outplayed the lobby while the team collapsed. Upset wins pay more, expected wins less.
Hidden MMR moves mostly on outcome with a smaller performance term, so smurfs get matched
against stronger teams within a few games. Games shorter than `MIN_GAME_MINUTES` or ended by
early surrender are remakes: no LP.

`/explain` shows the z-scores and contributions behind any game; `/admin baselines` shows what
the server currently considers average per role.

### What it cannot see

The API does not tell us why a fight was taken or whether a pick was bad for the comp. The
carry factor (you vs your teammates) plus the lane-relative and share-based metrics cover most
of "my team lost it, not me", but a great macro call that shows up as nothing in the stats
still goes unrewarded. Tune weights in `ROLE_WEIGHTS`, watch `/admin baselines`, and use
`/admin reprocess` (most recent games only) after changing the model.

## Anti-smurf

`bot/services/smurf_detector.py`, reviewed by humans, never automatic punishment.

| flag | when |
| --- | --- |
| `account_signal` | at `/link`: low summoner level, no ranked history, or a high solo tier / win rate on a low-level account. |
| `performance_anomaly` | after 3–12 games: average PS far above what the hidden MMR predicts. |
| `fingerprint_match` | after 3–15 games: champion pool, role split, stat profile and play-hour histogram look like an existing member (cosine/exp-distance similarity ≥ `SMURF_SIMILARITY_THRESHOLD`). Two accounts that were ever in the same game are never matched. Sequential activity (old account stops, new one starts) adds weight. |

Flags post to the channel set with `/admin modchannel` (or `MOD_CHANNEL_ID`) with the evidence;
mods resolve them with `/flags resolve`.

## drafter.lol

Set `DRAFTER_API_KEY` (their API keys require a subscription). When teams are set the bot
calls `POST https://api.drafter.lol/api/series` with the team names and posts the blue / red /
spectator links in the teams embed. A background loop polls the series and posts picks and
bans once the draft completes. The response is parsed defensively (links found by key name and
URL shape; paths configurable via `DRAFTER_API_BASE` / `DRAFTER_SERIES_PATH`) because the
docs at https://drafter.lol/api-docs could not be fetched from the build environment. If a
field name differs, adjust `extract_links` / `parse_completed_drafts` in `drafter_api.py`.

## Chat (OpenRouter)

Set `OPENROUTER_API_KEY`. Default model `openai/gpt-4o-mini` (cents per day at the default
300k-token cap). The bot answers when:

- it is @mentioned or someone replies to one of its messages,
- the message is a DM or in a channel listed in `CHAT_CHANNEL_IDS`,
- the message starts with its name ("ranked bot, what's my rank"),
- **or the same person talked to it in that channel within `CHAT_FOLLOWUP_WINDOW_SECS`** – that
  is how "people keep talking to it without an @" is handled; the window is per user and
  channel, so other chatter is ignored, and "thanks"/"bye"/"stop" closes it.

Memory is cheap by design: the last few turns per user/channel from the DB, plus one rolling
summary per user refreshed every `CHAT_SUMMARY_EVERY` turns with a single extra request. The
user's live rank, the lobby state and the command list are injected into the prompt so the bot
answers from real data. A daily token budget hard-stops spending; `/chat forget` wipes a user.

## Self-sustaining operation

- Auto-detect loop (`AUTO_DETECT_POLL_SECS`) finds and processes the lobby's custom game; lobbies
  with no game after `AUTO_DETECT_MAX_AGE_HOURS` are cancelled.
- Rating uncertainty grows after `INACTIVITY_DECAY_DAYS` so returning players re-place quickly.
- Everything is per `CURRENT_SEASON`: bump it in `.env` and restart for a fresh ladder; old data
  stays.
- Lobby state lives in the DB; buttons are persistent views, so restarts do not lose a lobby.
- Optional Riot tournament codes are supported in `riot_api.py` if you obtain that key, but
  auto-detect makes them unnecessary.

## Layout

```
bot/
  main.py               entrypoint + background loops (auto-detect, draft polling)
  config.py             every setting, read from .env
  models/               SQLAlchemy models (players, ratings, games, lobbies, smurf flags, chat, settings)
  services/
    rating_engine.py    the model: performance score, LP/MMR/RD updates, consistency
    metrics.py          Riot match + timeline -> metric dicts
    game_processor.py   pipeline, baselines, rollback/reprocess
    lobby_manager.py    queue, balancing, captain draft, roles
    auto_detect.py      finds finished custom games
    smurf_detector.py   anti-smurf signals
    drafter_api.py      drafter.lol client
    chat_service.py     OpenRouter chat + memory + triggers
    riot_api.py         Riot API client
    ranks.py            LP -> tier names
  cogs/                 Discord commands (lobby, ranking, admin, moderation, chat)
  ui/embeds.py          all embeds
migrations/             alembic
tests/                  53 tests, synthetic Riot data in tests/fixtures.py
```
