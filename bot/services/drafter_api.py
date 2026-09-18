"""
drafter.lol API client (https://drafter.lol/api-docs).

Auth:   Authorization: Bearer <DRAFTER_API_KEY>   (keys require a drafter.lol subscription)
Create: POST {DRAFTER_API_BASE}{DRAFTER_SERIES_PATH}
        {team1Name, team2Name, fearless, ironman, firstSelection?, gameAmount (1-5), disabledChampions?}
Read:   GET  {DRAFTER_API_BASE}{DRAFTER_SERIES_PATH}/{id}
        -> series with a `drafts` array that only contains COMPLETED drafts.

The response schema is parsed defensively (links are discovered by key name and by
URL shape) so a field rename on their side degrades gracefully instead of crashing.
Paths are configurable through the environment in case they change.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from bot import config

log = logging.getLogger("ranked-bot.drafter")

_URL_RE = re.compile(r"https?://[^\s\"']+")


class DrafterError(Exception):
    pass


@dataclass
class DraftSeries:
    series_id: str
    links: dict[str, str]           # blue / red / spectator (whatever we could identify)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class DraftResult:
    game_number: int
    blue_picks: list[str]
    red_picks: list[str]
    blue_bans: list[str]
    red_bans: list[str]
    winner: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def is_configured() -> bool:
    return bool(config.DRAFTER_API_KEY)


# --------------------------------------------------------------------------- #
# Response parsing (pure)                                                      #
# --------------------------------------------------------------------------- #

def _walk(obj: Any, path: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def extract_links(raw: dict[str, Any]) -> dict[str, str]:
    """Find blue/red/spectator links anywhere in the response."""
    links: dict[str, str] = {}
    leftovers: list[str] = []
    for path, val in _walk(raw):
        if not isinstance(val, str) or not _URL_RE.match(val.strip()):
            continue
        key = path.lower()
        url = val.strip()
        if any(t in key for t in ("blue", "team1", "team_1")) and "blue" not in links:
            links["blue"] = url
        elif any(t in key for t in ("red", "team2", "team_2")) and "red" not in links:
            links["red"] = url
        elif any(t in key for t in ("spec", "observer", "watch", "stream")) and "spectator" not in links:
            links["spectator"] = url
        else:
            leftovers.append(url)
    # URL-shape fallback (e.g. https://drafter.lol/<id>/blue)
    for url in leftovers:
        low = url.lower()
        if "blue" in low and "blue" not in links:
            links["blue"] = url
        elif "red" in low and "red" not in links:
            links["red"] = url
        elif any(t in low for t in ("spec", "watch")) and "spectator" not in links:
            links["spectator"] = url
    if not links and leftovers:
        links["draft"] = leftovers[0]
    return links


def extract_series_id(raw: dict[str, Any]) -> str:
    for k in ("id", "seriesId", "series_id", "uuid", "code"):
        if isinstance(raw.get(k), (str, int)):
            return str(raw[k])
    for k in ("series", "data"):
        inner = raw.get(k)
        if isinstance(inner, dict):
            sid = extract_series_id(inner)
            if sid:
                return sid
    return ""


def _names(items: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(items, list):
        return out
    for it in items:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict):
            for k in ("name", "champion", "championName", "champ", "id"):
                if it.get(k):
                    out.append(str(it[k]))
                    break
    return out


def _side(d: dict[str, Any], side: str, what: str) -> list[str]:
    """Pull e.g. blue picks from many plausible layouts."""
    candidates = [
        f"{side}{what.capitalize()}", f"{side}_{what}", f"{side}{what}",
    ]
    for c in candidates:
        if c in d:
            return _names(d[c])
    for k in (side, f"{side}Team", f"{side}_team", "team1" if side == "blue" else "team2"):
        inner = d.get(k)
        if isinstance(inner, dict):
            for w in (what, f"{what}s", what.rstrip("s")):
                if w in inner:
                    return _names(inner[w])
    inner = d.get(what) or d.get(f"{what}s")
    if isinstance(inner, dict):
        for k in (side, "team1" if side == "blue" else "team2"):
            if k in inner:
                return _names(inner[k])
    return []


def parse_completed_drafts(raw: dict[str, Any]) -> list[DraftResult]:
    drafts = raw.get("drafts")
    if drafts is None and isinstance(raw.get("series"), dict):
        drafts = raw["series"].get("drafts")
    if drafts is None and isinstance(raw.get("data"), dict):
        drafts = raw["data"].get("drafts")
    results: list[DraftResult] = []
    for i, d in enumerate(drafts or []):
        if not isinstance(d, dict):
            continue
        if d.get("completed") is False or str(d.get("status", "")).lower() in ("pending", "in_progress", "active"):
            continue
        results.append(DraftResult(
            game_number=int(d.get("gameNumber", d.get("game", d.get("index", i))) or i) or (i + 1),
            blue_picks=_side(d, "blue", "picks"), red_picks=_side(d, "red", "picks"),
            blue_bans=_side(d, "blue", "bans"), red_bans=_side(d, "red", "bans"),
            winner=d.get("winner"), raw=d,
        ))
    return results


# --------------------------------------------------------------------------- #
# Client                                                                       #
# --------------------------------------------------------------------------- #

class DrafterClient:
    def __init__(self, session: aiohttp.ClientSession | None = None):
        self._session = session
        self._owns = session is None

    async def __aenter__(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *_):
        if self._owns and self._session:
            await self._session.close()

    def _headers(self) -> dict[str, str]:
        if not config.DRAFTER_API_KEY:
            raise DrafterError("DRAFTER_API_KEY is not configured.")
        return {"Authorization": f"Bearer {config.DRAFTER_API_KEY}", "Content-Type": "application/json"}

    async def create_series(
        self, team1: str, team2: str, fearless: bool | None = None, game_amount: int = 1,
        disabled_champions: list[str] | None = None, ironman: bool = False, first_selection: bool | None = None,
    ) -> DraftSeries:
        payload: dict[str, Any] = {
            "team1Name": team1[:35], "team2Name": team2[:35],
            "fearless": config.DRAFTER_FEARLESS if fearless is None else fearless,
            "ironman": ironman, "gameAmount": max(1, min(5, game_amount)),
        }
        if first_selection is not None:
            payload["firstSelection"] = first_selection
        if disabled_champions:
            payload["disabledChampions"] = disabled_champions
        url = f"{config.DRAFTER_API_BASE.rstrip('/')}{config.DRAFTER_SERIES_PATH}"
        async with self._session.post(url, json=payload, headers=self._headers()) as resp:
            text = await resp.text()
            if resp.status not in (200, 201):
                raise DrafterError(f"drafter.lol {resp.status}: {text[:300]}")
            try:
                raw = await resp.json(content_type=None)
            except Exception as e:  # pragma: no cover
                raise DrafterError(f"drafter.lol returned non-JSON: {text[:200]}") from e
        if not isinstance(raw, dict):
            raise DrafterError("Unexpected drafter.lol response shape.")
        links = extract_links(raw)
        sid = extract_series_id(raw)
        if not links:
            raise DrafterError(f"No draft links found in drafter.lol response: {str(raw)[:300]}")
        return DraftSeries(series_id=sid, links=links, raw=raw)

    async def get_series(self, series_id: str) -> dict[str, Any]:
        url = f"{config.DRAFTER_API_BASE.rstrip('/')}{config.DRAFTER_SERIES_PATH}/{series_id}"
        async with self._session.get(url, headers=self._headers()) as resp:
            if resp.status != 200:
                raise DrafterError(f"drafter.lol {resp.status}: {(await resp.text())[:300]}")
            raw = await resp.json(content_type=None)
        return raw if isinstance(raw, dict) else {"data": raw}

    async def completed_drafts(self, series_id: str) -> list[DraftResult]:
        return parse_completed_drafts(await self.get_series(series_id))
