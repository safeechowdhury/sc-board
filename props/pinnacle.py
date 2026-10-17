"""Pinnacle player props: the sharp price the board measures Underdog's payouts against.

Pinnacle's own site reads these from its guest API, which answers a plain
request without a key. A request carrying a browser Origin header is refused,
so unlike Underdog's lines these can't be refreshed from the page itself.
Each prop is a matchup of its own ("Jordan Addison Total Receptions", units
"Receptions") whose full-game total market holds the over and under prices.
Soccer's are Yes/No moneylines instead ("Erling Haaland To Score"): Yes is the over at 0.5.
Pinnacle leaves a game's props up after it starts, unmarked, so those are dropped.
"""
import datetime as dt
import re
from collections import Counter

import httpx

from . import net
from .icons import icon

KEY, NAME, SHORT = "pinnacle", "Pinnacle", "Pinnacle"
WEIGHT = 1.4   # its share of the fair chance (books.py)
ICON = icon("pinnacle.png")   # shown in place of its name in the column heading
TARGET = False
LEAGUE_ATTR = "PINNACLE_LEAGUE"

URL = "https://guest.api.arcadia.pinnacle.com/0.1/leagues/{league}/{path}"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def fetch(league) -> dict:
    out = {}
    for name, path in (("matchups", "matchups"), ("markets", "markets/straight")):
        url = URL.format(league=league.PINNACLE_LEAGUE, path=path)
        # the lines page reads the same feeds, and its CDN keeps them 15 minutes
        out[name] = net.once(("pinnacle", url), lambda url=url: net.fresh(("pinnacle", url), lambda: _ask(url)))
    return out


def _ask(url: str):
    with net.site("pinnacle"):
        r = httpx.get(url, timeout=60, headers=HEADERS)
    r.raise_for_status()
    return r.json(), getattr(r, "headers", {})


def decimal(american: int) -> float:
    """American odds as the decimal payout, the way Underdog's are shown."""
    return round(1 + (american / 100 if american > 0 else 100 / -american), 3)


def parse(payload: dict, league, now: dt.datetime | None = None) -> tuple[list[dict], Counter]:
    """The league's two-sided player props for games not started by `now`, plus a count of the stats
    we don't track (by Pinnacle's name)."""
    now = now or net.now()
    stats = {units.lower(): stat for units, stat in league.PINNACLE_STATS.items()}
    markets = {m["matchupId"]: m for m in payload.get("markets", [])
               if m.get("type") == "total" and m.get("period") == 0}
    yes_no = {m["matchupId"]: m for m in payload.get("markets", [])
              if m.get("type") == "moneyline" and m.get("period") == 0}
    props, skipped = [], Counter()
    for matchup in payload.get("matchups", []):
        special = matchup.get("special") or {}
        if special.get("category") != "Player Props":
            continue
        if dt.datetime.fromisoformat(matchup["startTime"].replace("Z", "+00:00")) <= now:
            continue
        sides = {p["id"]: p.get("name", "").lower() for p in matchup.get("participants", [])}
        scorer = re.match(r"(.+?)\s+(To Score)$", special.get("description", ""), flags=re.I)
        if scorer and set(sides.values()) == {"yes", "no"}:   # soccer: a Yes/No moneyline
            stat = stats.get(scorer.group(2).lower())
            prices = {sides.get(p.get("participantId")): p for p in (yes_no.get(matchup["id"]) or {}).get("prices", [])}
            if not stat:
                skipped[scorer.group(2)] += 1
            elif prices.get("yes") and prices.get("no"):
                props.append({"player": scorer.group(1), "stat": stat, "line": 0.5,
                              "over": decimal(prices["yes"]["price"]), "under": decimal(prices["no"]["price"])})
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
        prices = {sides.get(p.get("participantId")): p for p in market.get("prices", [])}
        over, under = prices.get("over"), prices.get("under")
        if not over or not under or over.get("points") is None or over.get("points") != under.get("points"):
            continue
        props.append({"player": player, "stat": stat, "line": float(over["points"]),
                      "over": decimal(over["price"]), "under": decimal(under["price"])})
    return props, skipped

