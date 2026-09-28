# Setting up the bot on Windows

Read this one page start to finish. Ignore Docker. Ignore the command prompt.
You will double-click one file and answer what it asks you.

Total time: about 20 minutes, most of it waiting for downloads.

---

## Part 1: Install Python (5 minutes)

This is where most people get stuck, and it is almost always one missed checkbox.

1. Go to **https://www.python.org/downloads/**
2. Click the big yellow **Download Python** button.
3. Open the file you just downloaded.
4. **STOP. Before clicking Install:** at the bottom of that first window there is a
   checkbox that says **"Add python.exe to PATH"**. **Tick it.**
   If you miss this, Windows cannot find Python and nothing works.
5. Now click **Install Now** and let it finish.
6. **Restart your computer.** Windows does not always notice the new install until you do.

**Do not install Python from the Microsoft Store.** The Store version is what makes
`python` open a shop window instead of running anything. If you already did that:
open Windows Settings, search for **"Manage app execution aliases"**, and turn **off**
the two entries called `python.exe` and `python3.exe`. Then install from python.org as above.

---

## Part 2: Get the bot's files (2 minutes)

1. Go to **https://github.com/smockssocks/Ranked-Bot/tree/claude/bold-cannon-yqbk76**
2. Click the green **Code** button, then **Download ZIP**.
3. Find the ZIP in your Downloads folder, **right-click it, choose "Extract All"**,
   and extract it somewhere easy like `C:\RankedBot`.

You must extract it. Windows lets you peek inside a ZIP without extracting, and the bot
cannot run from in there.

When you are done you should have a folder containing `START-BOT.bat`, a `bot` folder,
and a file called `.env.example`.

---

## Part 3: Collect your two keys (10 minutes)

You need a Discord token and a Riot key. Keep a Notepad window open and paste them both
into it as you go.

### Discord bot token

1. Go to **https://discord.com/developers/applications** and sign in.
2. Click **New Application** (top right), give it a name, click **Create**.
3. On the left, click **Bot**.
4. Click **Reset Token**, confirm, then **Copy**. Paste it into your Notepad.
   You only get to see it once. If you lose it, just Reset again.
5. Scroll down that same page to **Privileged Gateway Intents** and turn **ON**:
   - **SERVER MEMBERS INTENT**
   - **MESSAGE CONTENT INTENT**

   The bot will refuse to start without these two. Click **Save Changes**.
6. On the left click **OAuth2**, and copy your **Client ID**.
7. Paste this address into your browser, replacing `YOUR_CLIENT_ID` with the number you
   just copied, then pick your server and authorise it:

   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot%20applications.commands&permissions=361063640144
   ```

   **Already added the bot before?** Open this link again anyway, pick your server and
   approve. It keeps everything and adds the permissions for game threads and team voice.

### Your server ID

1. In Discord: **Settings** (the gear, bottom left) → **Advanced** → turn on **Developer Mode**.
2. Close settings. **Right-click your server's name** in the far left bar → **Copy Server ID**.
3. Paste it into your Notepad. It is a long number like `123456789012345678`.

### Riot API key

1. Go to **https://developer.riotgames.com** and sign in with a **Riot** account.
2. On the front page, copy the **DEVELOPMENT API KEY**. It starts with `RGAPI-`.
   Paste it into your Notepad.

   **Important:** development keys stop working after 24 hours. That is fine for testing.
   When you are ready to run the bot for real, click **Register Product** on that site and
   apply for a **Personal API Key**, which does not expire. Approval usually takes a few days.

---

## Part 4: Start it

**Double-click `START-BOT.bat`.**

That is the whole step. The window will:

1. Find your Python.
2. Build its own private environment and download what it needs. The first run takes a
   couple of minutes and prints a lot of text. That is normal.
3. Create your settings file and **open it in Notepad for you**.

When Notepad opens, fill in these three lines with what you collected in Part 3:

```
DISCORD_TOKEN=paste_your_token_here
DISCORD_GUILD_ID=paste_your_server_id_here
RIOT_API_KEY=paste_your_riot_key_here
```

Paste the value right after the `=` with no spaces and no quotation marks.

Also check these two lines match your region. The defaults are for North America:

```
RIOT_REGION=na1
RIOT_PLATFORM=americas
```

| If your server is | RIOT_REGION | RIOT_PLATFORM |
| --- | --- | --- |
| North America | `na1` | `americas` |
| Brazil | `br1` | `americas` |
| EU West | `euw1` | `europe` |
| EU Nordic & East | `eun1` | `europe` |
| Korea | `kr` | `asia` |
| Oceania | `oc1` | `sea` |

Then **save with Ctrl+S and close Notepad.** The black window carries on by itself.

It now checks your settings and tells you, in plain English, about anything still wrong.
Fix whatever it lists, save, and double-click `START-BOT.bat` again.

When it works you will see `Logged in as ...` and the window will sit there. **That means
it is running. Leave the window open.** Closing it turns the bot off.

---

## Part 5: First things to do in Discord

1. Type `/` in any channel. You should see `/link`, `/queue`, `/inhouse`, `/rank`.
   If nothing appears, your `DISCORD_GUILD_ID` is wrong or the invite in Part 3 did not
   include `applications.commands`. Redo step 7 of the Discord section.
2. **Run `/admin setup`.** The bot shows a preview of everything it will build. Press
   **Build it** and it creates:

   | Category | Channels | Who can post |
   | --- | --- | --- |
   | INFORMATION | `#how-to-play`, `#announcements` | Staff only. The bot posts and pins the player guide in `#how-to-play`. |
   | INHOUSES | `#inhouse-queue`, `#inhouse-chat`, `#match-results`, voice: Lobby, Blue Side, Red Side | Everyone chats in `#inhouse-chat`. The queue channel is commands only, so the lobby post never gets buried. Results are posted by the bot. |
   | STAFF | `#mod-flags`, `#bot-admin` | Hidden from players. Visible to the new **@Inhouse Mod** role. |

   It also points the bot at these channels: queues in `#inhouse-queue`, results in
   `#match-results`, smurf alerts in `#mod-flags`.

   Setup only ever **adds** things. It never deletes, renames or changes channels you
   already have, and if you already have a channel with one of those names it uses yours.
   Running it again only adds what is missing.

   If it says it is **missing permissions**, it gives you a link to fix that in one click.
   Your bot needs Manage Channels, Manage Roles and Manage Messages to build the server.
3. Give your moderators the **@Inhouse Mod** role.
4. Everyone who wants to play runs `/link` once, for example `/link Danman#NA1`. The bot shows
   them a profile icon to switch to in the League client and a **Verify** button. That proves
   the account is theirs. Nobody can join a queue until they have linked.

**Testing on your own:** open a lobby with `/inhouse create`, join it, then run `/admin testfill`
to fill the other nine spots with fillers. Press **Start** to see teams made. For a captain
draft, use `/inhouse create mode:Captain draft`, fill it, then `/inhouse captains` to make
yourself one captain and a filler the other, and pick for both sides. `/admin testclear`
cleans up afterwards.

To test that scoring works without playing a fresh game, find any past 10-player custom
game and run `/admin submit` with the number from its post-game screen, for example
`/admin submit 5650481942`. The bot adds the `NA1_` region part itself.

---

## Updating the bot without losing anyone

**Every linked account, rating, game and setting lives in one file: `ranked_bot.db`**,
in the same folder as `START-BOT.bat`. Your keys live in `.env` next to it. Neither file is
in the download, so updating can't overwrite them, **as long as you update in the same folder.**

To update:

1. Close the bot's black window.
2. Download the new ZIP and **extract it into the same folder you run the bot from.**
   When Windows asks, choose **Replace the files in the destination**.
3. Double-click `START-BOT.bat`.

Nobody has to relink. Every time the bot starts, the setup check prints something like:

```
  Database: C:\...\ranked_bot.db
  [OK]   14 linked player(s). Backups kept in: C:\...\backups
```

If that ever says **"No linked players"** when you know people have linked, you are running
a fresh copy in a different folder. Close it, copy `ranked_bot.db` and `.env` from your old
bot folder into the new one, and start again.

**Automatic backups.** Every start saves a copy of the database into the `backups` folder,
keeping the newest 14. To restore one: close the bot, copy the backup you want over
`ranked_bot.db`, rename it to exactly `ranked_bot.db`, and start the bot.

**Moving the bot to another folder** (a good idea, since Downloads gets cleaned out): move the
whole folder, then delete the `.venv` folder inside it. `START-BOT.bat` rebuilds that on the
next start. Your players come with you because `ranked_bot.db` moves with the folder.

---

## When something goes wrong

**The black window flashes open and vanishes instantly.**
Something failed before it could print. Open the folder, hold **Shift**, right-click in
empty space, choose **Open PowerShell window here**, type `.\START-BOT.bat` and press Enter.
Now the error stays on screen.

**"Python is not installed, or Windows cannot find it."**
You missed the "Add python.exe to PATH" checkbox, or you have the Microsoft Store version.
Go back to Part 1 and do both fixes.

**Typing `python` opens the Microsoft Store.**
Windows Settings → search "Manage app execution aliases" → turn off `python.exe` and
`python3.exe`. Then install from python.org with the PATH box ticked.

**It says dependencies failed to install.**
Almost always antivirus or a dropped connection. Delete the `.venv` folder inside the bot
folder and run `START-BOT.bat` again.

**"Unknown apikey" or any Riot key error when someone runs `/link`.**

**Double-click `CHECK-RIOT-KEY.bat`.** It tests your key against Riot directly and
tells you exactly which of these it is. In order of how often it happens:

1. **You did not restart the bot.** The key is read once, when the bot starts.
   Editing `.env` while it is running changes nothing. Close the black window
   completely, then run `START-BOT.bat` again.
2. **The key was copied incompletely.** A Riot key is exactly 42 characters:
   `RGAPI-` followed by 36 more. The checker tells you how many you actually have.
3. **The key expired.** Development keys die after 24 hours. Get a fresh one from
   developer.riotgames.com, paste it in, and restart.
4. **A leftover Windows environment variable is overriding your file.** If you ever
   set `RIOT_API_KEY` as a system environment variable, it used to silently beat
   whatever was in `.env`. The bot now always prefers `.env`, but the checker will
   still spot the stray variable and tell you how to delete it.

Anything you change in `.env` needs a bot restart to take effect. Every time.

**"The bot couldn't find Riot's server", or `getaddrinfo failed` in the black window.**
Your computer couldn't look up Riot's address, so the request never reached Riot. Your key
and the game ID were never checked, so changing them won't help. The bot already retries
short blips by itself. If it keeps happening:

1. Double-click `CHECK-RIOT-KEY.bat`. It tests the connection and tells you what it sees.
2. Turn off any VPN.
3. Pause ad-blocking, parental-control or DNS-filtering software. Some block "api" addresses.
4. Press the Windows key, type `cmd`, open Command Prompt, type `ipconfig /flushdns`, press Enter.
5. Still failing? Restart your router.

Discord can keep working while this fails, because each website is looked up separately.

**Riot API 404 when someone runs `/link`.**
Typo in the Riot ID. It must be `GameName#TAG`, exactly as it appears in the League client.

**The game finished but the bot never posted results.**
All ten players must have run `/link`, and it must be a real 5v5 custom game. Give it two
minutes. If it still misses, run `/inhouse submit` with the game ID from the post-game
screen, for example `/inhouse submit 5650481942`.

**Slash commands are missing or stale.**
Stop the bot with Ctrl+C in the black window, then run `START-BOT.bat` again. Commands
re-sync every time it starts.

---

## Running it properly, later

The bot only works while that black window is open, which means it stops when you shut your
PC. That is fine while you are testing. When you want it online all the time, put it on a
cheap virtual server that stays on. A 5 dollar per month box from Hetzner, DigitalOcean or
Oracle Cloud's free tier is plenty. Ask me and I will write the exact commands for that.
