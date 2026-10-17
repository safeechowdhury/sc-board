"""Kalshi player props and game lines: an exchange's prices, a sharp source.

Kalshi is a prediction market, and its public market-data API
(`api.elections.kalshi.com/trade-api/v2`) needs no key, cookies or login. One
request lists a league's games (`/events` on its game series, "KXNHLGAME", with
each game's milestone: its start and which team is home), then one request per
stat series lists that stat's open markets (`/markets?series_ticker=KXNHLGOAL`).
A game's events share its ticker's tail across series ("KXNHLGOAL-26OCT14MTLWSH"
is the goals of the game "KXNHLGAME-26OCT14MTLWSH"), which is how a prop finds
its game's start.

Every market is a yes/no contract on one rung: "Tom Wilson: 2+ points" is the
over at 1.5 (its `floor_strike`), yes the over and no the under, each priced as
what it costs to buy (`yes_ask_dollars`, `no_ask_dollars`: 0.23 is 23%, a 4.35x
price). A first goalscorer has no strike: yes is the over at 0.5. A market is
only read with both sides on offer and its bid and ask no more than MAX_WIDTH
apart: otherwise nobody is really making a price there (a rung with no bids shows
a 98-cent ask), and its asks would read as a chance they don't carry. Kalshi's
trading fee isn't taken off, the way Novig's isn't.

Kalshi trades games while they are played, so only games that haven't started
are read.
"""
import datetime as dt
import time
from collections import Counter

import httpx

from . import matchups, net
from .icons import icon

KEY, NAME, SHORT = "kalshi", "Kalshi", "Kalshi"
WEIGHT = 7       # an exchange's prices carry the most weight in the fair chance (books.py)
ICON = icon("kalshi.png")   # shown in place of its name in the column heading
TARGET = False
BOARD_GAMES_ONLY = True   # the board asks only for its games' (site.build passes `until`)
LEAGUE_ATTR = "KALSHI_STATS"   # a league with no player series isn't asked for props

URL = "https://api.elections.kalshi.com/trade-api/v2"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
MAX_WIDTH = 0.10   # the widest bid-ask spread a market can have and still count
SHOW_WIDTH = 0.25   # ... and still be shown: a thin market (a goalie's saves before the bettors come) is a real
                    # price to see, but Fair leaves out any pair over books.MAX_OVERROUND, so only shown


def _client() -> httpx.Client:
    return httpx.Client(timeout=60, headers=HEADERS, transport=httpx.HTTPTransport(retries=3))


def _get(http: httpx.Client, path: str, params: dict) -> httpx.Response:
    """One request, at the site's pace (net.LIMITS); a 429 is asked once more after a pause."""
    for wait in (3, None):
        with net.site("kalshi"):
            r = http.get(f"{URL}{path}", params=params)
        if r.status_code != 429 or wait is None:
            break
        time.sleep(wait)
    r.raise_for_status()
    return r


def _pages(http: httpx.Client, path: str, params: dict, key: str) -> list[dict]:
    """Every page of a listing, following its cursor."""
    found, cursor = [], None
    while True:
        r = _get(http, path, {**params, **({"cursor": cursor} if cursor else {})})
        page = r.json()
        found += page.get(key) or []
        cursor = page.get("cursor")
        if not cursor or not page.get(key):
            return found


def _markets(http: httpx.Client, series: str) -> list[dict]:
    """A series' open markets: asked once a run, whichever page or window wants them."""
    return net.once(("kalshi", "markets", series),
                    lambda: _pages(http, "/markets", {"series_ticker": series, "status": "open", "limit": 1000}, "markets"))


def games(http: httpx.Client, league) -> list[dict]:
    """The league's open games: the tail of its tickers, its start, the home team's id, and its
    moneyline markets (one per team). Asked once a run, for the props and the lines page alike."""
    def ask():
        answer = {"events": [], "milestones": []}
        cursor = None
        while True:
            page = _get(http, "/events", {
                "series_ticker": league.KALSHI_GAME, "status": "open", "limit": 200,
                "with_nested_markets": "true", "with_milestones": "true", **({"cursor": cursor} if cursor else {})}).json()
            answer["events"] += page.get("events") or []
            answer["milestones"] += page.get("milestones") or []
            cursor = page.get("cursor")
            if not cursor or not page.get("events"):
                break
        starts = {}
        for milestone in answer["milestones"]:
            for ticker in milestone.get("primary_event_tickers") or []:
                starts[ticker] = milestone
        out = []
        for event in answer["events"]:
            milestone = starts.get(event["event_ticker"])
            if not milestone or not milestone.get("start_date"):
                continue
            details = milestone.get("details") or {}
            out.append({"game": event["event_ticker"].split("-", 1)[1], "start": milestone["start_date"],
                        "title": event.get("title") or "",
                        "home_id": details.get("home_team_id"), "away_id": details.get("away_team_id"),
                        "markets": event.get("markets") or []})
        return out
    return net.once(("kalshi", "games", league.KALSHI_GAME), ask)   # by series: soccer has one a competition


def upcoming(slate: list[dict], now: dt.datetime, until: dt.datetime) -> list[dict]:
    return [g for g in slate if now < dt.datetime.fromisoformat(g["start"].replace("Z", "+00:00")) <= until]


def team_of(market: dict) -> str | None:
    """A market's team id ("hockey_team", "football_team", ...), from its structured strike."""
    return next((v for k, v in (market.get("custom_strike") or {}).items() if k.endswith("_team")), None)


def code_of(market: dict) -> str:
    """The Kalshi team code a game's moneyline market ends with ("KXNHLGAME-26OCT20TBVGK-VGK")."""
    return market["ticker"].rsplit("-", 1)[1]


def chance(dollars) -> float | None:
    """A side's cost as a chance; None where nobody offers it."""
    try:
        value = float(dollars)
    except (TypeError, ValueError):
        return None
    return value if 0 < value < 1 else None


def tight(yes: float | None, no: float | None) -> bool:
    """Whether both sides are on offer and their costs within MAX_WIDTH of adding up to 100%."""
    return bool(yes and no) and yes + no <= 1 + MAX_WIDTH + 1e-9


def shown(yes: float | None, no: float | None) -> bool:
    """Whether both sides are on offer and within SHOW_WIDTH: worth a place on the board, if not in Fair."""
    return bool(yes and no) and yes + no <= 1 + SHOW_WIDTH + 1e-9


def fetch(league, now: dt.datetime | None = None, until: dt.datetime | None = None) -> dict:
    """The player props of every game in the league starting from now until `until` (by default, the
    end of its slate), before it starts."""
    now = now or net.now()
    until = until or now + dt.timedelta(days=league.SLATE_DAYS + 1)
    with net.shared("kalshi", _client) as http:
        # an odds board (matchups.py) asks for a competition's series only when one of its games is on it
        slate = [g for g in upcoming(games(http, league), now, until) if matchups.on_board(league, g["start"], g["title"])]
        if not slate:
            return {"markets": []}
        codes = {g["game"]: sorted({code_of(m) for m in g["markets"]}, key=len, reverse=True) for g in slate}
        named = {g["game"]: matchups.game_of(g["start"], g["title"]) for g in slate}
        markets = []
        for series in league.KALSHI_STATS:
            for m in _markets(http, series):
                game = m.get("event_ticker", "").split("-", 1)[-1]
                if game not in codes:
                    continue
                # the ticker's player part starts with his team's code: "-WSHTWILSON43-2"
                who = m["ticker"].split("-")[2] if m["ticker"].count("-") >= 2 else ""
                # the stat goes with the market: soccer's series differ by competition, and parse sees only the league
                markets.append({"series": series, "stat": league.KALSHI_STATS[series],
                                "player": (m.get("yes_sub_title") or "").split(":")[0].strip(),
                                "team": next((c for c in codes[game] if who.startswith(c)), None),
                                "strike": m.get("floor_strike"),
                                "yes": m.get("yes_ask_dollars"), "no": m.get("no_ask_dollars"),
                                **({"game": named[game]} if named[game] else {})})
    return {"markets": markets}


def parse(payload: dict, league) -> tuple[list[dict], Counter]:
    """The league's player props, plus a count of the series we don't map. A rung missing a side
    or priced wider than SHOW_WIDTH is left out, so every Kalshi price is two-way; one wider than
    MAX_WIDTH is shown but Fair leaves it out (books.MAX_OVERROUND)."""
    props, skipped = [], Counter()
    aliases = getattr(league, "TEAM_ALIASES", {})
    for m in payload.get("markets", []):
        stat = m.get("stat") or league.KALSHI_STATS.get(m["series"])
        if not stat:
            skipped[m["series"]] += 1
            continue
        yes, no = chance(m["yes"]), chance(m["no"])
        if not m["player"] or not shown(yes, no):
            continue
        prop = {"player": m["player"], "stat": stat, "line": float(m["strike"]) if m["strike"] is not None else 0.5,
                "over": round(1 / yes, 3), "under": round(1 / no, 3)}
        if m.get("team"):
            prop["team"] = aliases.get(m["team"], m["team"])
        if m.get("game"):   # [start, "Everton vs Chelsea"]: an odds board's market rows find the game by it
            prop["game"] = m["game"]
        props.append(prop)
    return props, skipped
