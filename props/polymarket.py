"""Polymarket player props: a second exchange's prices, a sharp source.

Polymarket's public market data (`gamma-api.polymarket.com`) needs no key, cookies or login.
One request lists its sports, each with the id of its series of games ("nfl" -> 12185); then
a request per hundred of the series' open events lists them with their markets nested. A
game's player props are an event of their own ("Texans vs. Jaguars - Player Props", soccer's
too), with the game's start (`startTime`).

Every market is one rung, "C.J. Stroud: Passing Yards O/U 149.5" (its `line`) or "Bhayshul
Tuten: 2+ Touchdowns" (no line: the over at 1.5; a plain yes/no, "Anytime Touchdown", is the
over at 0.5), its type saying the stat (`sportsMarketType`: "passing_yards"). Its first
outcome is the over ("Over", "Yes"), and `bestBid` and `bestAsk` are that side's: the over
costs the ask, the under one less the bid. A market is read, as Kalshi's are, only with both
sides on offer and no more than kalshi.MAX_WIDTH apart: most of Polymarket's prop books hold nothing
but a 1-cent bid and a 99-cent ask, which would read as a chance they don't carry. Its fee
isn't taken off.

Each league's `POLYMARKET_STATS` maps the market types to ours and `POLYMARKET_SPORT` names
its sport ("nfl"; soccer's competitions "epl", "ucl"...). Only games that haven't started are read.
"""
import datetime as dt
import json
import re
from collections import Counter

import httpx

from . import matchups, net
from .icons import icon
from .kalshi import chance, shown   # both sides on offer, within kalshi.SHOW_WIDTH

KEY, NAME, SHORT = "polymarket", "Polymarket", "Poly"
WEIGHT = 5       # an exchange, a step below Novig and Kalshi: not in the study, few props graded (books.py)
ICON = icon("polymarket.png")   # shown in place of its name in the column heading
TARGET = False
BOARD_GAMES_ONLY = True   # the board asks only for its games' (site.build passes `until`)
LEAGUE_ATTR = "POLYMARKET_STATS"   # a league with no player markets mapped isn't asked

URL = "https://gamma-api.polymarket.com"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
PAGE = 100   # events a request
OVER, UNDER = ("over", "yes"), ("under", "no")
_COUNT = re.compile(r"(\d+)\+")   # "2+ Touchdowns", "3+ goals"


def _client() -> httpx.Client:
    return httpx.Client(timeout=60, headers=HEADERS, transport=httpx.HTTPTransport(retries=3))


def _get(http: httpx.Client, path: str, params: dict | None = None):
    with net.site("polymarket"):
        r = http.get(f"{URL}{path}", params=params)
    r.raise_for_status()
    return r.json()


def _series(http: httpx.Client) -> dict[str, str]:
    """{sport code: its series id}, asked once a run."""
    return net.once(("polymarket", "sports"),
                    lambda: {s["sport"]: s["series"] for s in _get(http, "/sports") if s.get("sport") and s.get("series")})


def _events(http: httpx.Client, series: str) -> list[dict]:
    """A series' open events with their markets, every page, asked once a run."""
    def ask():
        found, offset = [], 0
        while True:
            page = _get(http, "/events", {"series_id": series, "closed": "false", "limit": PAGE, "offset": offset})
            found += page
            if len(page) < PAGE:
                return found
            offset += PAGE
    return net.once(("polymarket", "events", series), ask)


def fetch(league, now: dt.datetime | None = None, until: dt.datetime | None = None) -> dict:
    """The player markets of every game in the league starting from now until `until` (by default,
    the end of its slate), before it starts."""
    if not getattr(league, "POLYMARKET_STATS", None):   # no player markets there (the NHL's): nothing to ask
        return {"markets": []}
    now = now or net.now()
    until = until or now + dt.timedelta(days=league.SLATE_DAYS + 1)
    with net.shared("polymarket", _client) as http:
        series = _series(http).get(league.POLYMARKET_SPORT)
        if not series:
            return {"markets": []}
        markets = []
        for event in _events(http, series):
            start = event.get("startTime")
            name = (event.get("title") or "").split(" - ", 1)[0]   # "Texans vs. Jaguars - Player Props"
            if not start or not now < matchups._when(start) <= until or not matchups.on_board(league, start, name):
                continue
            game = matchups.game_of(start, name)
            for m in event.get("markets") or []:
                kind = m.get("sportsMarketType")
                if kind not in league.POLYMARKET_STATS or m.get("closed") or not m.get("acceptingOrders"):
                    continue
                markets.append({"type": kind, "question": m.get("question") or "", "line": m.get("line"),
                                "outcomes": m.get("outcomes"), "bid": m.get("bestBid"), "ask": m.get("bestAsk"),
                                **({"game": game} if game else {})})
    return {"markets": markets}


def _line(m: dict) -> float:
    if m.get("line") is not None:
        return float(m["line"])
    count = _COUNT.search(m["question"].split(":", 1)[-1])
    return int(count.group(1)) - 0.5 if count else 0.5


def parse(payload: dict, league) -> tuple[list[dict], Counter]:
    """The league's player props, plus a count of the market types we don't map. A rung missing a
    side or priced wider than kalshi.SHOW_WIDTH is left out, so every Polymarket price is two-way (and Fair
    leaves out one wider than books.MAX_OVERROUND)."""
    props, skipped = [], Counter()
    for m in payload.get("markets", []):
        stat = getattr(league, "POLYMARKET_STATS", {}).get(m["type"])
        if not stat:
            skipped[m["type"]] += 1
            continue
        try:
            first = (json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"])[0].lower()
        except (TypeError, ValueError, IndexError, AttributeError):
            continue
        bid, ask = chance(m.get("bid")), chance(m.get("ask"))
        if first in OVER:
            over, under = ask, (1 - bid if bid else None)
        elif first in UNDER:
            over, under = (1 - bid if bid else None), ask
        else:
            continue
        player = m["question"].split(":", 1)[0].strip() if ":" in m["question"] else ""
        if not player or not shown(over, under):
            continue
        props.append({"player": player, "stat": stat, "line": _line(m),
                      "over": round(1 / over, 3), "under": round(1 / under, 3),
                      **({"game": m["game"]} if m.get("game") else {})})
    return props, skipped
