"""Pinnacle player props: the sharp price the board measures Underdog's payouts against.

Pinnacle's own site reads these from its guest API, which answers a plain
request without a key. A request carrying a browser Origin header is refused,
so unlike Underdog's lines these can't be refreshed from the page itself.
Each prop is a matchup of its own ("Jordan Addison Total Receptions", units
"Receptions") whose full-game total market holds the over and under prices.
Pinnacle leaves a game's props up after it starts, unmarked, so those are dropped.
"""
import datetime as dt
import re
from collections import Counter

import httpx

KEY, NAME, SHORT = "pinnacle", "Pinnacle", "Pinnacle"
SHARP = True   # its prices count toward the fair chance
TARGET = False

URL = "https://guest.api.arcadia.pinnacle.com/0.1/leagues/{league}/{path}"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def fetch(league) -> dict:
    out = {}
    for name, path in (("matchups", "matchups"), ("markets", "markets/straight")):
        r = httpx.get(URL.format(league=league.PINNACLE_LEAGUE, path=path), timeout=60, headers=HEADERS)
        r.raise_for_status()
        out[name] = r.json()
    return out


def decimal(american: int) -> float:
    """American odds as the decimal payout, the way Underdog's are shown."""
    return round(1 + (american / 100 if american > 0 else 100 / -american), 3)


def parse(payload: dict, league, now: dt.datetime | None = None) -> tuple[list[dict], Counter]:
    """The league's two-sided player props for games not started by `now`, plus a count of the stats
    we don't track (by Pinnacle's name)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    stats = {units.lower(): stat for units, stat in league.PINNACLE_STATS.items()}
    markets = {m["matchupId"]: m for m in payload.get("markets", [])
               if m.get("type") == "total" and m.get("period") == 0}
    props, skipped = [], Counter()
    for matchup in payload.get("matchups", []):
        special = matchup.get("special") or {}
        if special.get("category") != "Player Props":
            continue
        if dt.datetime.fromisoformat(matchup["startTime"].replace("Z", "+00:00")) <= now:
            continue
        # "Jacob Misiorowski Total Earned Runs" has units "Runs": the words after "Total" name the prop
        named = re.match(r"(.+?)\s+Total\s+(.+)$", special.get("description", ""), flags=re.I)
        units = matchup.get("units") or ""
        name = named.group(2) if named else units
        stat = stats.get(name.lower()) or stats.get(units.lower())
        if not stat:
            skipped[name] += 1
            continue
        market = markets.get(matchup["id"])
        if not market or not named:
            continue
        player = named.group(1)
        sides = {p["id"]: p.get("name", "").lower() for p in matchup.get("participants", [])}
        prices = {sides.get(p.get("participantId")): p for p in market.get("prices", [])}
        over, under = prices.get("over"), prices.get("under")
        if not over or not under or over.get("points") is None or over.get("points") != under.get("points"):
            continue
        props.append({"player": player, "stat": stat, "line": float(over["points"]),
                      "over": decimal(over["price"]), "under": decimal(under["price"])})
    return props, skipped

