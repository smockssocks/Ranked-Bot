# Ranked Inhouse Bot

A Discord bot that runs League of Legends inhouse (custom) games from queue to results, and
ranks players with a system built for a small community rather than Riot's ladder.

**In one paragraph:** a host opens a lobby, ten people press Join, the bot makes teams
(captain draft, MMR-balanced, or first-come-first-serve roles), posts drafter.lol draft links,
and then watches the Riot API for the custom game. When the game ends it pulls the match and
its minute-by-minute timeline, gives everyone a performance score, moves LP, posts the results,
and flags suspected smurfs to your mods. Nobody has to type a match ID. It also talks back if
you give it an OpenRouter key.

---

## 1. Getting it running

### On Windows

**Read [SETUP-WINDOWS.md](SETUP-WINDOWS.md).** It is one page, written for someone who has
never used a terminal, and it ends with you double-clicking a single file.

The short version: install Python from python.org **with the "Add python.exe to PATH" box
ticked**, download this repository as a ZIP and extract it, then double-click
**`START-BOT.bat`**. That script builds everything, opens your settings file in Notepad, checks
your keys, and tells you in plain English about anything that is wrong.

Do not use Docker on Windows. It needs virtualization enabled in your BIOS and is not worth
the trouble for one bot.

### On Linux or macOS

```bash
./start.sh
```

Same idea: it creates the environment, installs dependencies, copies `.env.example` to `.env`
on first run, checks your settings and starts the bot.

### What you will need either way

| Thing | Where to get it | Required? |
| --- | --- | --- |
| Discord bot token | https://discord.com/developers/applications | yes |
| Your Discord server ID | Right-click your server with Developer Mode on | yes |
| Riot API key | https://developer.riotgames.com | yes |
| drafter.lol API key | https://drafter.lol | optional: draft links |
| OpenRouter API key | https://openrouter.ai/keys | optional: chat |

Two things catch everyone out:

- In the Discord developer portal, **Bot** tab, you must turn on **Server Members Intent**
  and **Message Content Intent**. The bot refuses to start without them.
- The Riot **development** key on the front page of their site **expires every 24 hours**.
  For permanent use, click **Register Product** there and apply for a **Personal API Key**.

### Checking your setup at any time

```
.venv\Scripts\python.exe tools\preflight.py     (Windows)
.venv/bin/python tools/preflight.py              (Linux/macOS)
```

That prints a tick or a fix for every requirement, including whether your `RIOT_REGION` and
`RIOT_PLATFORM` agree with each other, which is a common cause of mysterious 403 errors.

### Running it on a server

The bot must stay running to pick up games. On a Linux box:

```ini
[Unit]
Description=Ranked inhouse bot
After=network.target

[Service]
WorkingDirectory=/opt/ranked-bot
ExecStart=/opt/ranked-bot/.venv/bin/python -m bot.main
Restart=always
User=ranked

[Install]
WantedBy=multi-user.target
```

SQLite is the default and is fine for one community. For Postgres, set
`DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/ranked_bot` and run `alembic upgrade head`
once before the first start. A `Dockerfile` and `docker-compose.yml` are included for anyone who
prefers containers on a Linux host.

### First things to do in Discord

1. Type `/` in a channel and confirm `/link`, `/queue`, `/inhouse` and `/rank` appear.
2. `/admin queuechannel channel:#inhouse-queue` so all queueing happens in one place.
3. `/admin modchannel #mods` so smurf alerts have somewhere to go.
4. Everyone runs `/link GameName#TAG` once. Nobody can queue before linking.
5. Pin the player message from [docs/PLAYER-GUIDE.md](docs/PLAYER-GUIDE.md). Players can also
   run `/help` at any time, which tells them what to do next and where your queue channel is.

To test scoring without playing a fresh game, take any past 10-player custom from your match
history and run `/admin submit NA1_1234567890`.

Run the test suite any time with `pytest`. It needs no network, Discord or keys.

## 2. A game night, step by step

1. **Host:** `/inhouse create` (pick a mode, see below). The bot posts an embed with
   **Join / Leave / Start** buttons.
2. **Players:** press **Join**, or `/queue role:Mid secondary:Top` to state role preferences.
   The embed updates live and pings the host when it hits 10.
3. **Host:** press **Start** (or `/inhouse start`).
   - **Balanced:** the bot tries all 126 possible 5v5 splits, picks the one with the smallest
     MMR gap that also honours role preferences, and posts the teams with a win prediction.
   - **Pick order:** same balancing, but roles inside each team go by who queued first.
   - **Captain:** the two highest-rated players (or random with `random_captains:true`) become
     captains and snake-draft with `/inhouse pick @player`. Roles are auto-suggested from
     preferences; captains can move people with `/inhouse role @player Jungle` (swaps with
     whoever had it).
4. If `DRAFTER_API_KEY` is set, the teams embed includes **blue / red / spectator** draft links.
   Captains draft on drafter.lol; the bot posts picks and bans when the draft completes. Create
   the custom lobby in the client as **Tournament Draft** and lock in the same champions.
5. **Play the game.** Nothing else to do.
6. Within about two minutes of the game ending (`AUTO_DETECT_POLL_SECS`) the bot finds it in
   the players' match history, processes it, posts a results embed (LP changes, performance
   score, MVP) in the lobby channel, and closes the lobby. If it somehow misses it,
   `/inhouse submit MATCH_ID` does the same thing manually.
7. Players check `/rank`, `/history`, and `/explain` to see exactly why they gained or lost LP.

Note: Riot only saves custom games to match history when they are real 5v5 games; a two-player
test custom will not show up, so test the pipeline with `/admin submit` on a past game.

---

## 3. Features

### Lobbies
- One lobby per channel, persistent buttons, state stored in the database so a restart mid-lobby
  loses nothing.
- Three team modes (balanced / pick order / captain draft), primary and secondary role
  preferences, captain role reassignment, `/inhouse teams`, `/inhouse status`, `/inhouse cancel`.
- Lobbies that never produce a game are auto-cancelled after `AUTO_DETECT_MAX_AGE_HOURS`.

### Automatic results
- The bot polls the Riot match history of the lobby's players for custom games (queue 0) that
  contain at least `AUTO_DETECT_MIN_LOBBY_PLAYERS` of the lobby's linked accounts.
- Results are posted with per-player LP change, performance score, KDA, MVP and any unlinked
  accounts (those get no LP).
- Games shorter than `MIN_GAME_MINUTES` or ended by early surrender count as remakes.

### The ranking system
See section 4. Short version: win/loss matters most, but how you played matters too, and it is
judged against your lane opponent and your role, never your champion.

### Player commands
| Command | What |
| --- | --- |
| `/help` | Everything a player needs: whether they have linked, which channel to queue in, and the commands. |
| `/rank [@player]` | Tier, LP, record, streak, peak, average performance, consistency, confidence, per-role LP, last 10 results. |
| `/leaderboard [role]` | Top 15 overall or for one role. Placements marked. |
| `/history [@player]` | Last 8 games with LP before/after. |
| `/explain [games_ago]` | Full breakdown of one game: which stats helped, which hurt, lane numbers, the LP formula terms. |
| `/howranked` | Plain-language explanation of the system. |

### Anti-smurf
Three independent checks; every flag goes to the mod channel with evidence and a
`/flags resolve <id> confirmed|dismissed` workflow. Nothing is automatic beyond the flag.

| Flag | Fires when |
| --- | --- |
| Account looks fresh | On `/link`: low summoner level, no ranked history, or a high solo-queue tier / win rate on a low-level account. |
| Playing far above rating | After 3–12 games: average performance score much higher than the hidden MMR predicts. |
| Looks like an existing member | After 3–15 games: champion pool, role split, stat profile and play-hour pattern resemble another member (and the two accounts were never in the same game). "Old account stops, new one starts" adds weight. |

`/flags list`, `/flags show <id>`. Tune `SMURF_SIMILARITY_THRESHOLD`, `SMURF_MIN_GAMES`,
`SMURF_LOW_LEVEL` in `.env`.

### drafter.lol
With `DRAFTER_API_KEY` set the bot creates a series (`POST api.drafter.lol/api/series`) when
teams are made and polls it every `DRAFTER_POLL_SECS` for the completed draft.
`/inhouse draft fearless:true games:3` re-creates a draft with options. `DRAFTER_FEARLESS`
sets the default. The drafter.lol docs could not be fetched while this was built, so the
response parser looks for links and picks by field name *and* URL shape; if their response
differs, `bot/services/drafter_api.py` (`extract_links`, `parse_completed_drafts`) is the
place to adjust.

### Chat (OpenRouter)
Set `OPENROUTER_API_KEY`. Default model `openai/gpt-4o-mini`; change `OPENROUTER_MODEL` to any
OpenRouter model id. The bot answers when:

- it is @mentioned, or someone replies to one of its messages,
- the message is a DM or in a channel listed in `CHAT_CHANNEL_IDS`,
- the message starts with its name (`CHAT_BOT_NAME`, e.g. "ranked bot, what's my rank?"),
- **the same person spoke to it in that channel within the last `CHAT_FOLLOWUP_WINDOW_SECS`**
  (default 2 minutes). This is what lets people keep talking without @-ing it. The window is
  per person and per channel, so other people's chatter is ignored, and "thanks" / "bye" /
  "stop" ends it.

It knows the speaker's rank, the current lobby, and the command list, so it answers from real
data. Memory is deliberately cheap: the last few turns per person and channel, plus one short
rolling summary per person that is refreshed every `CHAT_SUMMARY_EVERY` turns with a single
extra request. `CHAT_DAILY_TOKEN_BUDGET` (default 300k tokens, roughly a few cents per day on
gpt-4o-mini) is a hard stop. `/chat status` shows what is left; `/chat forget` wipes what it
knows about you. `CHAT_PERSONA` in `.env` changes its personality.

### Admin
| Command | What |
| --- | --- |
| `/admin link`, `/admin unlink` | Manage Riot links for others. |
| `/admin submit MATCH_ID` | Process any custom game. |
| `/admin rollback MATCH_ID` | Undo a game exactly (restores every rating and the learned baselines). |
| `/admin reprocess MATCH_ID` | Rollback + process again (after tuning the model). Most recent games only. |
| `/admin reset @player` | Back to 1000 LP and placements. |
| `/admin queuechannel` | Lock every queue command and lobby button to one channel. Run it with no options to see the current setting, or `clear:True` to allow any channel. |
| `/admin modchannel`, `/admin resultschannel` | Channels. |
| `/admin baselines` | What the server currently considers average per role. |
| `/admin season` | Seasons: change `CURRENT_SEASON` in `.env` and restart for a fresh ladder; old seasons stay in the database. |

---

## 4. How the ranking works

Everything is in `bot/services/rating_engine.py` (pure math, unit tested) and
`bot/services/metrics.py` (Riot JSON → numbers). Change the model there and nowhere else.

Each player has, overall and per role:

| Field | Meaning |
| --- | --- |
| LP | The visible ladder. Everyone starts at 1000. Tiers (Wood → Bronze → Silver → Gold → Platinum → Diamond → Master → Legend, edit `ranks.py`) are cosmetic 200-LP bands. |
| MMR + uncertainty | Hidden skill estimate (Glicko-style). Teams are balanced on MMR. Uncertainty starts high, shrinks each game (placements are the first `PLACEMENT_GAMES`, moving about twice as fast) and grows again after `INACTIVITY_DECAY_DAYS` without playing. |
| Performance mean / variance | Running performance score and how much it varies: shown as **consistency**. |

### Performance score (PS)

After every game the bot pulls the match and its minute-by-minute timeline and computes about
20 metrics per player:

- **Laning** (early-game weight): gold, XP and CS difference at 10 and 15 minutes **against
  the player in the same position on the other team**, plus a time-weighted "lane control"
  curve.
- **Economy**: CS/min, gold/min.
- **Combat**: kill participation, deaths per minute (strong negative), KDA, damage share of
  the team, damage/min, damage taken share (for tanks), solo kills.
- **Objectives**: share of the team's dragons/heralds/barons/turrets you took part in, plates.
- **Vision**: vision score/min, control wards, wards cleared.
- **Utility**: healing/shielding per minute, crowd control per minute.

Three rules keep it fair:

1. **Relative, not absolute.** Diffs against your lane opponent and shares of your team, so a
   free lane or a stomp does not inflate anyone.
2. **Normalised per role on your server.** Every metric becomes a z-score against a per-role
   average that the bot learns from your own games (with sensible priors until it has enough).
   A support with 1 CS/min is normal; a mid with 1 CS/min is not. Each z-score is clipped at
   ±2.5 so one absurd stat cannot carry the score, and deaths carry a large negative weight
   so farming while feeding does not pay.
3. **Weights change with game length.** Each metric is tagged early / mid / late. A 19-minute
   stomp is judged mostly on laning; a 45-minute game mostly on fights, objectives and deaths.

Role profiles decide what matters: supports are judged on vision, KP, healing and CC;
junglers on objectives and KP; laners on lane leads and damage share. The champion is never an
input: pick what you want, you are judged on what you did with it.

### LP change

```
outcome term      = 2K · (S − E)          S = 1 win / 0 loss, E = expected win chance from team MMR
performance term  = K · Wp · tanh(perf / 2) · consistency
perf              = 0.7·PS + 0.3·(PS − your team's average PS)      ← the "carry factor"
K                 = 20 × uncertainty                                 (≈ ×2 in placements)
```

`Wp` is 1.0 on a win, 1.4 for a good performance in a loss (mitigation), 1.0 for a bad one.
For an established player in an even game:

| PS | win | loss |
| --- | --- | --- |
| −2 (poor) | +5 | −35 |
| −1 | +11 | −29 |
| 0 (average) | +20 | −20 |
| +1 (strong) | +29 | −7 |
| +2 (elite) | +35 | +1 |
| +3 | +38 | +6 |

So: a win never loses LP. A loss only nets positive around PS +2, meaning the player clearly
outplayed the lobby while the team collapsed. Beating a stronger team pays more, beating a
weaker one less, and losing as the favourite costs more. Hidden MMR moves mostly on outcome
with a smaller performance term, so a smurf is matched against stronger teams within a few
games.

### What the API cannot see

Draft quality, shot-calling, and why a fight was taken leave no statistical trace. The carry
factor and the relative metrics cover most of "my team lost it, not me", but a great macro
call that shows up as nothing in the numbers goes unrewarded. If your community feels a weight
is off, edit `ROLE_WEIGHTS` in `rating_engine.py`, compare with `/admin baselines`, and
`/admin reprocess` recent games.

---

## 5. Troubleshooting

| Symptom | Fix |
| --- | --- |
| Slash commands do not appear | Check `DISCORD_GUILD_ID`; re-invite with the URL above (it must include `applications.commands`). |
| Bot exits at start with an intents error | Enable Server Members and Message Content intents in the developer portal. |
| `/link` says "Riot API 403" | Development key expired (24h) or wrong `RIOT_REGION` / `RIOT_PLATFORM`. |
| `/link` says "Riot API 404" | Riot ID typo. Format is `GameName#TAG`, the tag is the part after # in the client. |
| Game never detected | All ten must be `/link`ed and the game must be a real 5v5 custom; check `docker compose logs bot` for "match id lookup failed". Use `/inhouse submit MATCH_ID` meanwhile. |
| Players get "not linked" when queueing | They need `/link` first. |
| Flags never appear | Run `/admin modchannel`. |
| Chat does nothing | `OPENROUTER_API_KEY` missing, Message Content intent off, or the daily budget is spent (`/chat status`). |
| Changed the model and want old games re-scored | `/admin reprocess` newest game first, one at a time. |

Logs go to stdout; `docker compose logs -f bot` or the systemd journal.

---

## 6. Layout

```
bot/
  main.py               entrypoint + background loops (auto-detect, draft polling)
  config.py             every setting, read from .env
  models/               database tables (players, ratings, games, lobbies, smurf flags, chat, settings)
  services/
    rating_engine.py    the model: performance score, LP/MMR updates, consistency
    metrics.py          Riot match + timeline -> metric dicts
    game_processor.py   pipeline, learned baselines, rollback/reprocess
    lobby_manager.py    queue, balancing, captain draft, roles
    auto_detect.py      finds finished custom games
    smurf_detector.py   anti-smurf signals
    drafter_api.py      drafter.lol client
    chat_service.py     OpenRouter chat, triggers, memory, budget
    riot_api.py         Riot API client
    ranks.py            LP -> tier names
  cogs/                 Discord commands (lobby, ranking, admin, moderation, chat)
  ui/embeds.py          every embed
migrations/             alembic (Postgres)
tests/                  53 tests on synthetic Riot data, run with `pytest`
Dockerfile, docker-compose.yml
```
