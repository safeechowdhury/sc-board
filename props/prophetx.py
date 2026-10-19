"""ProphetX player props: a peer-to-peer exchange's prices, a sharp source.

ProphetX's site reads its odds from an open REST API (`cash.api.prophetx.co/trade/public/api`),
no key, cookies or login. One request lists a tournament's games
("/v1/tournaments/132/events": the NBA), then one per game lists its markets
("/v2/events/<id>/markets"), the player props among them, each named by its `subType`
("player_total_points").

A player's market ("Cade Cunningham Total Points") holds a line per rung ("Fixed total
24.5"), and each line the offers on its two sides: the American odds a bettor can take
right now, the best first. A yes/no market ("Trevor Zegras To Score A Goal") is one line
whose YES is the over at 0.5. Each side's offers say which side they are ("over 24.5",
"YES"), since a yes/no lists its NO first. A line is only read with both sides on offer
and their chances no more than kalshi.SHOW_WIDTH over 100%, as Kalshi's and Polymarket's
are; Fair leaves out one wider than books.MAX_OVERROUND. ProphetX's commission isn't
taken off.

Each league's `PROPHETX_TOURNAMENT` says its tournament id and `PROPHETX_STATS` maps the
subTypes to ours. Only games that haven't started are read.
"""
import datetime as dt
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import matchups, net
from .icons import icon
from .kalshi import shown   # both sides on offer, within kalshi.SHOW_WIDTH

KEY, NAME, SHORT = "prophetx", "ProphetX", "PX"
WEIGHT = 5       # an exchange, a step below Novig and Kalshi: thinner books, not in the study (books.py)
ICON = icon("prophetx.png")   # shown in place of its name in the column heading
TARGET = False
BOARD_GAMES_ONLY = True   # a request per game: the board asks only for its games' (site.build passes `until`)
PER_GAME = True   # costs a request per game: an odds board asks it only for the next two days' games
LEAGUE_ATTR = "PROPHETX_TOURNAMENT"

URL = "https://cash.api.prophetx.co/trade/public/api"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Origin": "https://www.prophetx.co"}

# "Cade Cunningham Total Points", "Brock Purdy Passing Yards", "Trevor Zegras To Score A Goal"
_PLAYER = re.compile(r"^(?P<player>.+?) (?:Total |To |Pitcher |Power Play |Passing |Rushing |Receiving "
                     r"|Singles$|Stolen Bases$|Hits Allowed$|Walks Allowed$|Interceptions Thrown$)")
OVER, UNDER = ("over", "yes"), ("under", "no")


def _get(http: httpx.Client, path: str) -> dict:
    with net.site("prophetx"):
        r = http.get(f"{URL}{path}")
    r.raise_for_status()
    return r.json()


def fetch(league, now: dt.datetime | None = None, until: dt.datetime | None = None) -> dict:
    """The player markets of every game in the league starting from now until `until` (by default,
    the end of its slate), before it starts."""
    now = now or net.now()
    until = until or now + dt.timedelta(days=league.SLATE_DAYS + 1)
    with httpx.Client(timeout=60, headers=HEADERS, transport=httpx.HTTPTransport(retries=3)) as http:
        listed = _get(http, f"/v1/tournaments/{league.PROPHETX_TOURNAMENT}/events").get("data") or []
        games = {e["id"]: matchups.game_of(e["scheduled"], e.get("name")) for e in listed
                 if e.get("status") == "not_started" and e.get("scheduled")
                 and now < matchups._when(e["scheduled"]) <= until
                 and matchups.on_board(league, e["scheduled"], e.get("name") or "")}
        with ThreadPoolExecutor(max_workers=2) as pool:
            pages = pool.map(lambda game: _get(http, f"/v2/events/{game}/markets"), games)
            markets = []
            for game, page in zip(games.values(), pages):
                for m in (page.get("data") or {}).get("markets") or []:
                    kind = m.get("subType") or ""
                    if not kind.startswith("player") or m.get("status") != "active":
                        continue
                    lines = m.get("marketLines") or [m]   # a yes/no is its own one line
                    markets.append({"type": kind, "name": m.get("name") or "",
                                    "lines": [_sides(line) for line in lines],
                                    **({"game": game} if game else {})})
    return {"markets": markets}


def _sides(line: dict) -> dict:
    """A line's best offer on each side (American odds), and its line where it has one."""
    best = {}
    for offers in line.get("selections") or []:
        for offer in offers or []:
            side = (offer.get("name") or "").split(" ")[0].lower()
            side = "over" if side in OVER else "under" if side in UNDER else None
            if side and offer.get("odds") is not None and offer["odds"] > best.get(side, -10**9):
                best[side] = offer["odds"]
    points = next((o.get("line") for o in line.get("outcomes") or [] if o.get("line") is not None), None)
    return {"line": points, **best}


def decimal(american) -> float | None:
    """American odds as a decimal price; None for nothing."""
    if not american:
        return None
    return round(1 + (american / 100 if american > 0 else 100 / -american), 3)


def parse(payload: dict, league) -> tuple[list[dict], Counter]:
    """The league's player props, plus a count of the market types we don't map. A line missing a
    side or priced wider than kalshi.SHOW_WIDTH is left out, so every ProphetX price is two-way."""
    props, skipped = [], Counter()
    for m in payload.get("markets", []):
        stat = league.PROPHETX_STATS.get(m["type"])
        if not stat:
            skipped[m["type"]] += 1
            continue
        named = _PLAYER.match(m["name"])
        if not named:
            continue
        for line in m["lines"]:
            over, under = decimal(line.get("over")), decimal(line.get("under"))
            if not over or not under or not shown(1 / over, 1 / under):
                continue
            props.append({"player": named["player"], "stat": stat,
                          "line": float(line["line"]) if line.get("line") else 0.5,   # a yes/no: one or more
                          "over": over, "under": under, **({"game": m["game"]} if m.get("game") else {})})
    return props, skipped
