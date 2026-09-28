# Tournament codes

A tournament code is one line players paste into the League client. It builds the custom
lobby for them: Summoner's Rift, Tournament Draft, five a side, and **only the ten players in
that inhouse can join**. Nobody has to make a lobby, share a password, or kick strangers.

The bot already makes one per game and posts it in the game's private thread. What it needs
from you is a Riot key that Riot has approved for tournament codes. **Until then, nothing
breaks:** every game gets a private lobby name and password instead.

---

## 1. Why your current key can't do it

Riot has three kinds of keys:

| Key | Lasts | Tournament codes |
|---|---|---|
| Development (the one on the front page) | 24 hours | No |
| Personal | Forever | No |
| **Production** | Forever | **Yes, if Riot approves Tournament API access** |

So you need a **Production key**, and you need to ask for **Tournament API** access when you
apply. A Production key also stops the "key expired" problem, because it never expires.

Check what your key can do at any time by double-clicking **CHECK-RIOT-KEY.bat**. The
"Tournament codes" section at the end says whether Riot will let the key make codes.

---

## 2. Apply for a Production key

1. Go to **https://developer.riotgames.com** and sign in.
2. Click **REGISTER PRODUCT**, then choose **PRODUCTION API KEY**.
3. Fill in the form. The parts that matter:
   - **Product name:** your bot's name, e.g. "<Your server> Ranked Inhouses".
   - **Product URL:** a page about the bot. A free GitHub page, a Carrd page or a Google
     Site is fine. It should say what the bot does, show a screenshot or two, and link to
     your Discord. Riot may ask you to prove you own the site, usually by uploading a small
     file they give you.
   - **APIs:** tick the standard League APIs **and Tournament API**.
   - **Description:** see the template below.
4. Submit, then wait. Reviews often take **several weeks**. Riot emails you and the result
   shows on your developer dashboard.

Riot wants to see a **working product**, which you have. Invite a Riot reviewer to your
Discord, or record a short video of a game night: queue, teams, the private thread, results.

Read Riot's **Tournament API policies** on the developer portal before applying and follow
them. Be honest about size. Riot sometimes decides a bot for one community only needs a
Personal key, and Personal keys can't make tournament codes. If that happens, nothing changes
for your players; they keep using lobby passwords.

### Description template

Change the parts in `<angle brackets>`:

> <Bot name> is a Discord bot that runs ranked inhouse (custom game) nights for the
> <server name> League of Legends community, about <number> members and <number> games a
> week on <region>.
>
> Players link their Riot account with /link. We prove ownership by asking them to switch
> their profile icon. When ten players queue, the bot makes balanced teams from its own
> ratings, and after the game it reads the match from match-v5 to update ratings and post
> results.
>
> We want the Tournament API so each game gets a tournament code restricted to exactly those
> ten players (allowedParticipants), instead of a shared lobby password. We create one
> provider, one tournament per season, and one code per game, only when a game starts. That
> is roughly <number> codes a week. We don't charge entry fees or offer prizes.
>
> APIs used: account-v1, summoner-v4, league-v4, match-v5, tournament-v5.
> Product page: <URL>. Discord: <invite link>.

---

## 3. Switch it on

Once Riot approves:

1. Open `.env` in Notepad.
2. **If the Production key is now your main key**, which is normal: paste it into
   `RIOT_API_KEY=` and set:
   ```
   TOURNAMENT_CODES=true
   ```
   **If you keep a separate tournament key**, paste it into `RIOT_TOURNAMENT_API_KEY=`
   instead. That turns tournament codes on by itself.
3. Set the callback address. Riot sends game results there. The bot doesn't need them,
   because it reads results from match history, but Riot requires a valid address:
   ```
   RIOT_TOURNAMENT_CALLBACK_URL=https://your-product-page.example/riot-callback
   ```
   Use your product page's address. It must start with `https://` or `http://` and can't
   have a port number like `:8080`. The default `https://example.com/riot-callback` works,
   but your own address is better.
4. Save, close the bot window, and run **START-BOT.bat** again.
5. Run **CHECK-RIOT-KEY.bat**. The last section should say the key has Tournament API access.

The bot log also says `Tournament codes on` at startup.

---

## 4. What players see

In the game's private thread:

> Everyone, in the League client: click **Play**, then the **trophy icon** at the top of the
> game-mode screen, and paste this code.

Players take their slots in pick order, Pick 1 at the top of the team, same as before. The
game is picked up from match history automatically when it ends.

Fillers from `/admin testfill` have no Riot account, so they're left off the code's player
list. A test game with fillers still gets a code, but only the real players can use it.

---

## 5. How it works, and fixing problems

Riot's system has three layers, and the bot handles all of them:

1. A **provider**: made once per key and region. A regenerated or new key needs a new
   provider, so the bot keeps one per key. It stores a fingerprint of the key, never the key.
2. A **tournament**: one per season, named "Ranked Inhouses S<season>".
3. A **code**: one per game, locked to the players' Riot accounts.

If a code can't be made for any reason, the thread says "A tournament code couldn't be made"
and gives a lobby name and password instead. The bot log says why:

| Log says | Meaning | Fix |
|---|---|---|
| `Riot API 403` | The key has no Tournament API access. | Wait for approval, or check you put the approved key in `.env`. |
| `RIOT_TOURNAMENT_CALLBACK_URL must be...` or `can't use a custom port` | The callback address is wrong. | Fix it in `.env` (step 3 above) and restart. |
| `Riot API 400` | Riot refused the request, for example an unsupported region. | Check `RIOT_REGION` in `.env`. |
| `Could not reach` | Internet or Riot trouble. | Try again later; passwords still work. |
