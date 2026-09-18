"""Tier / division mapping for displayed LP. Purely cosmetic; edit freely."""
from __future__ import annotations

# (min_lp, name, emoji). Must be sorted ascending by min_lp.
TIERS: list[tuple[int, str, str]] = [
    (0,    "Wood",      "🪵"),
    (700,  "Bronze",    "🥉"),
    (900,  "Silver",    "🥈"),
    (1100, "Gold",      "🥇"),
    (1300, "Platinum",  "💎"),
    (1500, "Diamond",   "💠"),
    (1700, "Master",    "👑"),
    (1900, "Legend",    "🔥"),
]


def tier_for_lp(lp: int) -> tuple[str, str]:
    """Return (name, emoji) for an LP value."""
    name, emoji = TIERS[0][1], TIERS[0][2]
    for min_lp, n, e in TIERS:
        if lp >= min_lp:
            name, emoji = n, e
        else:
            break
    return name, emoji


def division_for_lp(lp: int) -> str:
    """Tier + roman numeral division (IV..I) inside each 200-LP band, e.g. 'Gold II'."""
    name, _ = tier_for_lp(lp)
    lo = 0
    hi = None
    for i, (min_lp, n, _e) in enumerate(TIERS):
        if n == name:
            lo = min_lp
            hi = TIERS[i + 1][0] if i + 1 < len(TIERS) else None
            break
    if hi is None:
        return name
    span = hi - lo
    frac = (lp - lo) / span
    div = ["IV", "III", "II", "I"][min(3, int(frac * 4))]
    return f"{name} {div}"


def progress_bar(lp: int, width: int = 12) -> str:
    """Text bar of progress to the next tier."""
    name, _ = tier_for_lp(lp)
    for i, (min_lp, n, _e) in enumerate(TIERS):
        if n == name:
            if i + 1 >= len(TIERS):
                return "█" * width
            lo, hi = min_lp, TIERS[i + 1][0]
            filled = int(round((lp - lo) / (hi - lo) * width))
            return "█" * filled + "░" * (width - filled)
    return "░" * width
