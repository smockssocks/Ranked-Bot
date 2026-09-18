"""Discord embed builders shared by cogs and background tasks."""
from __future__ import annotations

from datetime import datetime, timezone

import discord

from bot import config
from bot.models.lobby import Lobby
from bot.services import lobby_manager
from bot.services.game_processor import ProcessResult
from bot.services.ranks import division_for_lp, progress_bar, tier_for_lp
from bot.services.rating_engine import ROLE_DISPLAY

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
    e = discord.Embed(title=f"Inhouse Lobby #{lobby.id}", color=color)
    e.add_field(name="Mode", value=lobby.mode.replace("_", " "), inline=True)
    e.add_field(name="Players", value=f"{n}/{lobby.max_players}", inline=True)
    e.add_field(name="Status", value=lobby.status, inline=True)
    if lobby.players:
        lines = []
        for i, lp in enumerate(sorted(lobby.players, key=lambda x: (x.joined_at is None, (x.joined_at.replace(tzinfo=timezone.utc) if x.joined_at and not x.joined_at.tzinfo else x.joined_at) or datetime.min.replace(tzinfo=timezone.utc))), 1):
            pref = ROLE_DISPLAY.get(lp.preferred_role or "", "")
            sec = ROLE_DISPLAY.get(lp.secondary_role or "", "")
            role_str = f" ({pref}{'/' + sec if sec else ''})" if pref else ""
            lines.append(f"{i}. {names.get(lp.player_id, '?')}{role_str}")
        e.add_field(name="Queue", value="\n".join(lines), inline=False)
    else:
        e.add_field(name="Queue", value="Nobody yet. Hit **Join**.", inline=False)
    e.set_footer(text=f"Host: {host_mention} • /queue also works")
    return e


def teams_embed(lobby: Lobby, names: dict[int, str], ratings: dict[int, dict] | None = None) -> discord.Embed:
    t1 = [lp for lp in lobby.players if lp.team == 1]
    t2 = [lp for lp in lobby.players if lp.team == 2]
    e = discord.Embed(title=f"Teams — Lobby #{lobby.id}", color=discord.Color.green())
    e.add_field(name=lobby.team1_name or "Blue side", value="\n".join(lobby_manager.team_lines(t1, names)) or "—", inline=True)
    e.add_field(name=lobby.team2_name or "Red side", value="\n".join(lobby_manager.team_lines(t2, names)) or "—", inline=True)
    if ratings:
        m1 = sum(ratings[lp.player_id]["mmr"] for lp in t1) / max(1, len(t1))
        m2 = sum(ratings[lp.player_id]["mmr"] for lp in t2) / max(1, len(t2))
        from bot.services.rating_engine import expected_win
        p = expected_win([m1], [m2])
        e.add_field(name="Predicted", value=f"Blue {p*100:.0f}% — Red {(1-p)*100:.0f}%", inline=False)
    if lobby.drafter_links:
        links = lobby.drafter_links
        parts = [f"[{k.capitalize()}]({v})" for k, v in links.items()]
        e.add_field(name="Draft (drafter.lol)", value=" • ".join(parts), inline=False)
    if lobby.tournament_code:
        e.add_field(name="Tournament code", value=f"`{lobby.tournament_code}`", inline=False)
    e.set_footer(text="Play the custom game; results are picked up automatically. Captains: /inhouse role to move players.")
    return e


def results_embed(result: ProcessResult) -> discord.Embed:
    g = result.game
    if result.remake:
        return discord.Embed(title=f"Remake detected — {g.riot_match_id}",
                             description="Game was too short; no LP changes.", color=discord.Color.light_grey())
    mins = (g.game_duration_secs or 0) // 60
    winner = "Blue" if g.winner_team == 1 else "Red"
    e = discord.Embed(title=f"{winner} side wins — {mins} min", color=discord.Color.blue() if g.winner_team == 1 else discord.Color.red())
    e.description = f"Match `{g.riot_match_id}` • Blue was expected to win {100*(g.team1_expected_win or 0.5):.0f}%"
    for team in (1, 2):
        rows = sorted([r for r in result.results if r.team == team],
                      key=lambda r: ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"].index(r.role) if r.role in ROLE_DISPLAY else 9)
        lines = []
        for r in rows:
            sign = "+" if r.lp_delta >= 0 else ""
            tag = " (placement)" if r.placement else ""
            lines.append(f"**{r.player.discord_username}** {ROLE_DISPLAY.get(r.role, r.role)} {r.champion} {r.kda} "
                         f"• PS {r.perf_score:+.1f} • **{sign}{r.lp_delta} LP**{tag}")
        e.add_field(name=("Blue side" if team == 1 else "Red side") + (" ✅" if g.winner_team == team else ""),
                    value="\n".join(lines) or "—", inline=False)
    mvp = max(result.results, key=lambda r: r.perf_score, default=None)
    if mvp:
        e.add_field(name="MVP", value=f"{mvp.player.discord_username} (PS {mvp.perf_score:+.2f}, "
                                      f"{', '.join(label(m) for m in mvp.top_positive) or 'all-round'})", inline=False)
    if result.unlinked:
        e.add_field(name="Unlinked accounts (no LP)", value=", ".join(result.unlinked), inline=False)
    e.set_footer(text="PS = performance score vs lane opponent and role averages. /explain shows your breakdown.")
    return e


def rank_embed(name: str, overall, role_rows: list, summoner: str | None, recent: list) -> discord.Embed:
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
    if role_rows:
        lines = [f"**{ROLE_DISPLAY.get(r.role, r.role)}** {r.lp} LP • {r.games_played}G • {r.perf_mean:+.2f} PS"
                 for r in sorted(role_rows, key=lambda r: -r.games_played) if r.games_played]
        if lines:
            e.add_field(name="By role", value="\n".join(lines), inline=False)
    if recent:
        e.add_field(name="Recent", value=" ".join("🟩" if p.win else "🟥" for p in recent), inline=False)
    if summoner:
        e.set_footer(text=f"Riot: {summoner}")
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
    e.set_footer(text="Resolve with /flags resolve <id> confirmed|dismissed")
    return e
