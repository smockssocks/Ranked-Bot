"""Discord embed builders shared by cogs and background tasks."""
from __future__ import annotations

from datetime import datetime, timezone

import discord

from bot import config
from bot.models.lobby import Lobby
from bot.services import lobby_manager
from bot.services.settings import MODE_NAMES
from bot.services.game_processor import ProcessResult
from bot.services.ranks import division_for_lp, progress_bar, tier_for_lp
from bot.services.rating_engine import ROLE_DISPLAY, PROVEN_GAMES, versatility

METRIC_LABELS = {
    "gold_diff_10": "gold lead @10", "gold_diff_15": "gold lead @15", "xp_diff_10": "XP lead @10",
    "cs_diff_10": "CS lead @10", "lane_trajectory": "lane control", "cs_per_min": "CS/min",
    "gold_per_min": "gold/min", "kill_participation": "kill participation", "deaths_per_min": "deaths",
    "kda_adj": "KDA", "damage_share": "damage share", "damage_per_min": "damage/min",
    "damage_taken_share": "damage soaked", "solo_kills": "solo kills", "objective_participation": "objectives",
    "turret_plates": "plates", "vision_per_min": "vision", "control_wards": "control wards",
    "wards_killed": "wards cleared", "heal_shield_per_min": "heal/shield", "cc_per_min": "crowd control",
}


def label(metric: str) -> str:
    return METRIC_LABELS.get(metric, metric.replace("_", " "))


def lobby_embed(lobby: Lobby, names: dict[int, str], host_mention: str) -> discord.Embed:
    n = len(lobby.players)
    color = discord.Color.green() if n >= lobby.max_players else discord.Color.blue()
    kind = "Ranked" if getattr(lobby, "ranked", True) else "Casual, no LP"
    e = discord.Embed(title=f"Inhouse Lobby #{lobby.id}", color=color)
    e.add_field(name="Mode", value=f"{MODE_NAMES.get(lobby.mode, lobby.mode)} • {kind}", inline=True)
    e.add_field(name="Players", value=f"{n}/{lobby.max_players}", inline=True)
    ready = lobby.status == "waiting" and getattr(lobby, "ready_deadline", None) is not None
    e.add_field(name="Status", value="ready check" if ready else lobby.status, inline=True)
    if lobby.players:
        lines = []
        for i, lp in enumerate(sorted(lobby.players, key=lambda x: (x.joined_at is None, (x.joined_at.replace(tzinfo=timezone.utc) if x.joined_at and not x.joined_at.tzinfo else x.joined_at) or datetime.min.replace(tzinfo=timezone.utc))), 1):
            pref = ROLE_DISPLAY.get(lp.preferred_role or "", "")
            sec = ROLE_DISPLAY.get(lp.secondary_role or "", "")
            role_str = f" ({pref}{'/' + sec if sec else ''})" if pref and lobby.mode != "pick_order" else ""
            lines.append(f"{i}. {names.get(lp.player_id, '?')}{role_str}")
        e.add_field(name="Queue", value="\n".join(lines), inline=False)
    else:
        e.add_field(name="Queue", value="Nobody yet. Hit **Join**.", inline=False)
    # Mentions only render in descriptions and field values, never in footers or titles.
    e.add_field(name="Host", value=host_mention, inline=False)
    e.set_footer(text="Press Join, or use /queue")
    return e


READY_TITLES = {
    None: ("Match found! Accept or decline", discord.Color.gold()),
    "accepted": ("Everyone accepted!", discord.Color.green()),
    "declined": ("Ready check failed", discord.Color.red()),
    "expired": ("Ready check timed out", discord.Color.red()),
    "cancelled": ("Ready check called off", discord.Color.dark_grey()),
}


def ready_embed(lobby: Lobby, rows: list, outcome: str | None = None, note: str = "",
                out_ids: set[int] = frozenset()) -> discord.Embed:
    """
    The ready check, updated live as people accept. `rows` are ready_check.Row(player, accepted);
    `out_ids` are players who declined or timed out, shown with a cross.
    """
    from bot.services import ready_check
    title, color = READY_TITLES[outcome]
    done, total = sum(r.accepted for r in rows), len(rows)
    bar = "🟩" * done + "⬛" * (total - done)
    if outcome is None:
        mins = config.READY_CHECK_COOLDOWN_MINUTES
        penalty = f" Decline, leave or miss it and you can't queue for {mins} minutes." if mins > 0 else ""
        desc = (f"{bar}\n**{done}/{total} accepted.** Time runs out "
                f"<t:{ready_check.unix(lobby.ready_deadline)}:R>.{penalty}")
    else:
        desc = f"{bar}\n**{done}/{total} accepted.**" + (f"\n{note}" if note else "")
    e = discord.Embed(title=f"{title} (Lobby #{lobby.id})", description=desc, color=color)
    lines = []
    for r in rows:
        # A mention in a field value shows the name and never pings.
        who = f"**{r.player.discord_username}**" if r.player.is_test else f"<@{r.player.discord_id}>"
        mark = "❌" if r.player.id in out_ids else ("✅" if r.accepted else "⏳")
        lines.append(f"{mark} {who}")
    half = (len(lines) + 1) // 2
    e.add_field(name="Players", value="\n".join(lines[:half]) or "—", inline=True)
    if lines[half:]:
        e.add_field(name="\u200b", value="\n".join(lines[half:]), inline=True)
    return e


def _roles_text(pref: str | None, sec: str | None) -> str:
    if not pref and not sec:
        return "Fill"
    return " / ".join(ROLE_DISPLAY.get(r, r) for r in (pref, sec) if r)


def player_tag(place: int | None, lp: int | None, pref: str | None, sec: str | None) -> str:
    """One line about a player for draft choices: ladder place, LP, roles."""
    where = f"#{place} on the ladder · {lp} LP" if place else "Not ranked yet"
    return f"{where} · {_roles_text(pref, sec)}"


def draft_embed(lobby: Lobby, names: dict[int, str], tags: dict[int, str],
                on_clock: str | None) -> discord.Embed:
    """The captain draft board: both teams so far, who's picking, and who's left."""
    ds = lobby.draft_state or {}
    order = ds.get("pick_order", [])
    made = ds.get("picks_made", 0)
    e = discord.Embed(title=f"Captain draft (Lobby #{lobby.id})", color=discord.Color.blurple())
    if on_clock:
        left = order[made:]
        snake = " ".join("🔵" if t == 1 else "🔴" for t in left)
        e.description = f"**On the clock:** {on_clock}\nPick from the dropdown below.\nPicks left: {snake}"
    else:
        e.description = "**Draft complete.**"
    for team, cap_id, label in ((1, lobby.captain1_id, "🔵 Blue"), (2, lobby.captain2_id, "🔴 Red")):
        picked = sorted((lp for lp in lobby.players if lp.team == team and lp.player_id != cap_id),
                        key=lambda lp: lp.pick_order if lp.pick_order is not None else 99)
        lines = [f"👑 **{names.get(cap_id, '?')}** (captain)"] + [f"{names.get(lp.player_id, '?')}" for lp in picked]
        e.add_field(name=label, value="\n".join(lines), inline=True)
    pool = ds.get("pool", [])
    if pool:
        e.add_field(name="Still available", value="\n".join(
            f"**{names.get(pid, '?')}**: {tags.get(pid, '')}" for pid in pool)[:1024], inline=False)
    return e


def checkin_embed(lobby: Lobby, rows: list, done: bool = False, note: str = "",
                  duplicates: list | None = None) -> discord.Embed:
    """The post-game check-in. rows = checkin.Row, in team order."""
    from bot.services import checkin as ci
    confirmed = sum(1 for r in rows if r.confirmed or r.player.is_test)
    e = discord.Embed(title=f"Game over: confirm your role and champion (Lobby #{lobby.id})",
                      color=discord.Color.green() if done else discord.Color.blurple())
    if done:
        e.title = f"Check-in closed (Lobby #{lobby.id})"
        e.description = note or "Scoring the game now."
    else:
        mins = config.CHECKIN_MINUTES
        e.description = (f"Press **Confirm my role & champion** and pick from the dropdowns. LP is posted when "
                         f"everyone has confirmed, or <t:{int(ci._aware(lobby.checkin_deadline).timestamp())}:R> "
                         f"at the latest ({mins} minutes). If you don't answer, the bot's guess is used.\n"
                         f"**{confirmed}/{len(rows)} confirmed.**")
    for team, label in ((1, "🔵 Blue"), (2, "🔴 Red")):
        lines = []
        for r in rows:
            if r.team != team:
                continue
            mark = "✅" if r.confirmed else ("🤖" if r.player.is_test else "⏳")
            who = f"**{r.player.discord_username}**" if r.player.is_test else f"<@{r.player.discord_id}>"
            role = ROLE_DISPLAY.get(r.role or "", "role?")
            champ = r.champion or "champion?"
            lines.append(f"{mark} {who}: {role} · {champ}")
        if lines:
            e.add_field(name=label, value="\n".join(lines), inline=True)
    if duplicates:
        e.add_field(name="⚠️ Check these", value="\n".join(
            f"{'Blue' if t == 1 else 'Red'} has two {ROLE_DISPLAY.get(role, role)} players." for t, role in duplicates),
            inline=False)
    if not done:
        e.set_footer(text="✅ confirmed · ⏳ waiting (shows the bot's guess) · 🤖 filler")
    return e


def leaderboard_embed(rows: list, season: int, final: bool = False) -> discord.Embed:
    """The #leaderboard post. rows = [(PlayerRating, Player)] in ladder order."""
    title = f"Season {season} final standings" if final else f"Leaderboard: Season {season}"
    e = discord.Embed(title=title, color=discord.Color.gold() if not final else discord.Color.dark_gold())
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    for i, (r, p) in enumerate(rows, 1):
        _, emoji = tier_for_lp(r.lp)
        losses = r.games_played - r.wins
        wr = r.wins / r.games_played * 100 if r.games_played else 0
        plc = " ⏳" if r.games_played < config.PLACEMENT_GAMES else ""
        fire = f" 🔥{r.streak}" if (r.streak or 0) >= 3 else ""
        # A mention in a description shows the name and never pings.
        who = f"<@{p.discord_id}>" if p.discord_id.isdigit() else f"**{p.discord_username}**"
        lines.append(f"{medals.get(i, f'`{i:>2}.`')} {emoji} {who} **{r.lp} LP** {division_for_lp(r.lp)} · "
                     f"{r.wins}W {losses}L ({wr:.0f}%){fire}{plc}")
    e.description = "\n".join(lines) if lines else "Nobody has played a ranked game this season yet."
    if final:
        e.set_footer(text="Final standings. A new season has started.")
    else:
        e.set_footer(text="⏳ still in placements · 🔥 win streak · Updates after every game")
    return e


def teams_embed(lobby: Lobby, names: dict[int, str], ratings: dict[int, dict] | None = None) -> discord.Embed:
    t1 = [lp for lp in lobby.players if lp.team == 1]
    t2 = [lp for lp in lobby.players if lp.team == 2]
    title = f"Teams — Lobby #{lobby.id}" + ("" if getattr(lobby, "ranked", True) else " (casual)")
    e = discord.Embed(title=title, color=discord.Color.green())
    e.add_field(name=lobby.team1_name or "Blue side", value="\n".join(lobby_manager.team_lines(t1, names, lobby.mode)) or "—", inline=True)
    e.add_field(name=lobby.team2_name or "Red side", value="\n".join(lobby_manager.team_lines(t2, names, lobby.mode)) or "—", inline=True)
    if ratings:
        m1 = sum(ratings[lp.player_id]["mmr"] for lp in t1) / max(1, len(t1))
        m2 = sum(ratings[lp.player_id]["mmr"] for lp in t2) / max(1, len(t2))
        from bot.services.rating_engine import expected_win
        p = expected_win([m1], [m2])
        e.add_field(name="Predicted", value=f"Blue {p*100:.0f}% — Red {(1-p)*100:.0f}%", inline=False)
    if lobby.mode == "pick_order":
        e.set_footer(text="Pick 1 calls their role first in champ select. Results are picked up automatically.")
    else:
        e.set_footer(text="Play the custom game; results are picked up automatically. Captains: /inhouse role to move players.")
    return e


def results_embed(result: ProcessResult) -> discord.Embed:
    g = result.game
    if result.remake:
        return discord.Embed(title=f"Remake detected — {g.riot_match_id}",
                             description="Game was too short; no LP changes.", color=discord.Color.light_grey())
    mins = (g.game_duration_secs or 0) // 60
    winner = "Blue" if g.winner_team == 1 else "Red"
    casual = g.status == "casual"
    title = f"{winner} side wins — {mins} min" + (" (casual)" if casual else "")
    e = discord.Embed(title=title, color=discord.Color.blue() if g.winner_team == 1 else discord.Color.red())
    e.description = f"Match `{g.riot_match_id}` • Blue was expected to win {100*(g.team1_expected_win or 0.5):.0f}%"
    for team in (1, 2):
        rows = sorted([r for r in result.results if r.team == team],
                      key=lambda r: ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"].index(r.role) if r.role in ROLE_DISPLAY else 9)
        lines = []
        for r in rows:
            sign = "+" if r.lp_delta >= 0 else ""
            tag = " (placement)" if r.placement else ""
            lp_txt = "" if casual else f" • **{sign}{r.lp_delta} LP**{tag}"
            lines.append(f"**{r.player.discord_username}** {ROLE_DISPLAY.get(r.role, r.role)} {r.champion} {r.kda} "
                         f"• PS {r.perf_score:+.1f}{lp_txt}")
        e.add_field(name=("Blue side" if team == 1 else "Red side") + (" ✅" if g.winner_team == team else ""),
                    value="\n".join(lines) or "—", inline=False)
    mvp = max(result.results, key=lambda r: r.perf_score, default=None)
    if mvp:
        e.add_field(name="MVP", value=f"{mvp.player.discord_username} (PS {mvp.perf_score:+.2f}, "
                                      f"{', '.join(label(m) for m in mvp.top_positive) or 'all-round'})", inline=False)
    if result.unlinked:
        e.add_field(name="Unlinked accounts (no LP)", value=", ".join(result.unlinked), inline=False)
    if casual:
        e.set_footer(text="Casual game: stats recorded, nobody's LP changed.")
    else:
        e.set_footer(text="PS = performance score vs lane opponent and role averages. /explain shows your breakdown.")
    return e


def versatility_of(role_rows: list):
    """Versatility from a player's per-role PlayerRating rows."""
    return versatility({r.role: (r.games_played, r.perf_mean) for r in role_rows if r.role != "OVERALL"})


def rank_embed(name: str, overall, role_rows: list, summoner: str | None, recent: list,
               verified: bool = False) -> discord.Embed:
    e = discord.Embed(title=f"{name}", color=discord.Color.gold())
    if overall is None or overall.games_played == 0:
        e.description = f"No games yet. Everyone starts at {config.STARTING_LP} LP; the first {config.PLACEMENT_GAMES} games are placements."
        return e
    tier, emoji = tier_for_lp(overall.lp)
    wr = overall.wins / overall.games_played * 100
    placement = overall.games_played < config.PLACEMENT_GAMES
    head = f"{emoji} **{division_for_lp(overall.lp)}** — {overall.lp} LP" + (f"  (placements {overall.games_played}/{config.PLACEMENT_GAMES})" if placement else "")
    e.description = f"{head}\n`{progress_bar(overall.lp)}`"
    e.add_field(name="Record", value=f"{overall.wins}W {overall.games_played - overall.wins}L ({wr:.0f}%)", inline=True)
    streak = overall.streak
    e.add_field(name="Streak", value=(f"🔥 {streak}W" if streak > 0 else f"🧊 {-streak}L" if streak < 0 else "—"), inline=True)
    e.add_field(name="Peak", value=f"{overall.peak_lp} LP", inline=True)
    from bot.services.rating_engine import consistency_score
    e.add_field(name="Avg performance", value=f"{overall.perf_mean:+.2f} PS", inline=True)
    e.add_field(name="Consistency", value=f"{consistency_score(overall.perf_var)*100:.0f}%", inline=True)
    e.add_field(name="Confidence", value=f"{max(0, 100 - int(overall.rd / 3.5))}%", inline=True)
    v = versatility_of(role_rows)
    e.add_field(name="Versatility",
                value=f"**{v.score:+.2f}** • {v.proven}/5 roles proven", inline=True)
    grid = []
    for role in ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"):
        g = v.games[role]
        if g == 0:
            grid.append(f"`{ROLE_DISPLAY[role]:<7}` not played yet")
        else:
            mark = "✅" if g >= PROVEN_GAMES else "·"
            grid.append(f"`{ROLE_DISPLAY[role]:<7}` {v.per_role[role]:+.2f} • {g}G {mark}")
    e.add_field(name="By role (performance vs role average)", value="\n".join(grid), inline=False)
    if recent:
        e.add_field(name="Recent", value=" ".join("🟩" if p.win else "🟥" for p in recent), inline=False)
    if summoner:
        e.set_footer(text=f"Riot: {summoner} • " + ("ownership verified" if verified else "ownership not verified"))
    return e


def flag_embed(flag, player_name: str, matched_name: str | None) -> discord.Embed:
    kind = {"account_signal": "Account looks fresh", "performance_anomaly": "Playing far above rating",
            "fingerprint_match": "Looks like an existing member"}.get(flag.kind, flag.kind)
    e = discord.Embed(title=f"🚩 Smurf check #{flag.id}: {kind}", description=flag.summary or "", color=discord.Color.orange())
    e.add_field(name="Player", value=player_name, inline=True)
    if matched_name:
        e.add_field(name="Resembles", value=matched_name, inline=True)
    e.add_field(name="Confidence", value=f"{flag.score*100:.0f}%", inline=True)
    ev = flag.evidence or {}
    if ev:
        lines = [f"{k}: {v}" for k, v in ev.items() if k != "same_game"][:10]
        e.add_field(name="Evidence", value="\n".join(lines) or "—", inline=False)
    status = getattr(flag, "status", "open")
    if status == "open":
        e.set_footer(text="Press Confirmed smurf or False alarm below.")
    else:
        e.color = discord.Color.dark_grey()
        e.add_field(name="Resolved", value=f"**{status}** by {flag.resolved_by or '?'}", inline=False)
    return e


def join_embed(lobby: Lobby, creator_name: str | None) -> discord.Embed:
    """How to get into the custom game. Only ever posted in the lobby's private thread."""
    e = discord.Embed(title="Getting into the game", color=discord.Color.gold())
    slots = ("Take the slots in **pick order**, Pick 1 at the top of your team, so champ select runs in the right order."
             if lobby.mode == "pick_order" else
             "Join the team you're on: **Blue is Team 1**, **Red is Team 2**.")
    if lobby.tournament_code:
        e.description = (
            "Everyone, in the League client: click **Play**, then the **trophy icon** at the top of the "
            "game-mode screen, and paste this code. It sets everything up and only works for the players "
            "in this lobby.\n"
            f"```{lobby.tournament_code}```\n{slots}")
    else:
        who = f"**{creator_name}**" if creator_name else "The host"
        e.description = (
            f"{who} creates the lobby:\n"
            f"**1.** Play, then **Create Custom**, **Summoner's Rift**, game type **Tournament Draft**.\n"
            f"**2.** Lobby name `{lobby.join_name}` and password `{lobby.join_password}`, then **Confirm**.\n\n"
            f"**Everyone else:** Play, then **Create Custom**, the **Join Custom** tab, search "
            f"`{lobby.join_name}`, and enter the password.\n\n{slots}")
    if lobby.drafter_links:
        parts = [f"[{k.capitalize()}]({v})" for k, v in lobby.drafter_links.items()]
        e.add_field(name="Draft (drafter.lol)", value=" • ".join(parts), inline=False)
    e.set_footer(text="Keep this in this thread. Results are picked up automatically after the game.")
    return e
