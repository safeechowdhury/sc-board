"""Mise-o-jeu+ player props: Loto-Québec's sportsbook, a site to bet on rather than a sharp price.

Mise-o-jeu+ reads its odds from an open content service (SG Digital / OpenBet)
that answers a plain request carrying the site's Origin, no key or cookies. One
request lists a league's games in a time window; the games' markets come a few
games per request.

Its prices are soft, so they never count toward the board's fair chance: the
board shows them, and can measure EV against them in place of Underdog's.
Markets are named by a group code. A two-way one ("TOTAL_RECEPTIONS_OVER_UNDER_PLAYER",
"Tyler Warren Total Receptions 5.5": Over / Under) carries its line; most props
are a one-way ladder instead ("PLAYER_UNDEFINED_TOTAL_HITS", "Ty France Total
Hits": 1+, 2+, 3+), each rung the over at half a point below it.
"""
import datetime as dt
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import matchups, net
from .icons import icon

KEY, NAME, SHORT = "mise", "Mise-o-jeu", "Mise"
WEIGHT = 0     # never part of the fair chance
TARGET = True   # a site to find EV on
LEAGUE_ATTR = "MISE_LEAGUE"
ICON = icon("mise.png")   # shown in place of its name in the EV toggle

URL = "https://content.mojp-sgdigital-jel.com/content-service/api/v1/q"
SITE = "https://miseojeuplus.espacejeux.com"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json",
           "Origin": SITE, "Referer": SITE + "/"}
COMMON = {"lang": "en-CA", "channel": "I"}
BATCH = 10   # games per markets request (each one about a megabyte)

_RUNG = re.compile(r"^(\d+)\+$")                     # "3+"
_PLAYER = re.compile(r"^(?P<player>.+?) (?:Total|Longest) ")   # "Tyler Warren Total Receptions 5.5"


def _get(http: httpx.Client, endpoint: str, params: dict) -> dict:
    def ask():
        with net.site("mise"):
            r = http.get(f"{URL}/{endpoint}", params={**COMMON, **params})
        r.raise_for_status()
        body = r.json()
        if body.get("errors") or body.get("data") is None:
            raise ValueError(f"Mise-o-jeu {endpoint}: {body.get('errors') or 'empty answer'}")
        return body["data"]
    # the props and the lines page ask for the same games on the run's one clock
    return net.once(("mise", endpoint, tuple(sorted(params.items()))), ask)


def events(http: httpx.Client, ids: list[str]) -> list[dict]:
    """The games with their markets, BATCH games a request. Within a run a game is asked for once,
    so the lines page reads the props' answers."""
    def ask(keys):
        wanted = [key[2] for key in keys]
        batches = [wanted[i:i + BATCH] for i in range(0, len(wanted), BATCH)]
        with ThreadPoolExecutor(max_workers=3) as pool:
            pages = list(pool.map(lambda batch: _get(http, "events-by-ids", {
                "eventIds": ",".join(batch), "includeChildMarkets": "true", "includePriceHistory": "false",
                "includeCommentary": "false", "includeMedia": "false"}).get("events") or [], batches))
        return {("mise", "event", str(event.get("id"))): event for page in pages for event in page}
    answers = net.once_each([("mise", "event", str(i)) for i in ids], ask)
    return [event for event in answers.values() if event]


def _compact(market: dict) -> dict:
    return {"name": market.get("name", ""), "code": market.get("groupCode") or "", "line": market.get("handicapValue"),
            "status": market.get("status"), "outcomes": [
                {"name": o.get("name", ""), "status": o.get("status"),
                 "odds": next((p.get("decimal") for p in o.get("prices") or []), None)}
                for o in market.get("outcomes") or []]}


def fetch(league, now: dt.datetime | None = None) -> dict:
    """The player markets of every game in the league starting within its slate."""
    now = now or net.now()
    until = now + dt.timedelta(days=league.SLATE_DAYS + 1)
    stamp = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
    with httpx.Client(timeout=60, headers=HEADERS, transport=httpx.HTTPTransport(retries=3)) as http:
        listed = _get(http, "event-list", {"startTimeFrom": stamp(now), "startTimeTo": stamp(until), "maxEvents": 500,
                                           "maxMarkets": 0, "drilldownTagIds": league.MISE_LEAGUE}).get("events") or []
        ids = [str(e["id"]) for e in listed if not e.get("started")
               and matchups.on_board(league, e.get("startTime"), e.get("name") or "")]
        # each market says which game it's for ([start, "Everton vs Chelsea"])
        return {"markets": [{**_compact(m), **({"game": game} if (game := matchups.game_of(event.get("startTime"), event.get("name"))) else {})}
                            for event in events(http, ids) if not event.get("started")
                            for m in event.get("markets") or []
                            if "PLAYER" in (m.get("groupCode") or "") or m.get("groupCode") in league.MISE_STATS
                            or _by_name(m.get("name") or "", league)]}


_PERIOD = re.compile(r"1ST_(?:PERIOD|HALF|QUARTER)|FIRST_PERIOD")   # part of a game: never a board stat
# "Player 3+ Goals (Inc OT)", soccer's "Player to Score 2+": a runner per player
_FIELD = re.compile(r"^Player (?:to Score )?(?P<n>\d+)\+", re.I)


def _by_name(name: str, league) -> str | None:
    """The stat of a market Mise-o-jeu codes unreliably, by the end of its name: goalie saves come as
    "Linus Ullmark Total Saves" under a game-total code, or none at all."""
    return next((stat for suffix, stat in getattr(league, "MISE_NAMES", {}).items() if name.endswith(" " + suffix)), None)


def _odds(outcome: dict) -> float | None:
    return round(float(outcome["odds"]), 3) if outcome["status"] == "ACTIVE" and outcome["odds"] else None


def parse(payload: dict, league) -> tuple[list[dict], Counter]:
    """The league's player props, plus a count of the player markets we don't map (by group code).
    A one-way price (a ladder's rung) has "under": None."""
    props, skipped = [], Counter()
    prev, game = 0, None
    for market in payload.get("markets", []):
        matchups.tag(props, prev, game)   # the last market's props are for its game
        prev, game = len(props), market.get("game")
        if market.get("status") != "ACTIVE" or _PERIOD.search(market["code"]):
            continue
        stat = league.MISE_STATS.get(market["code"]) or _by_name(market["name"], league)
        if not stat:
            skipped[market["code"]] += 1
            continue
        field = _FIELD.match(market["name"])
        if field:
            line = int(field["n"]) - 0.5
            props += [{"player": o["name"], "stat": stat, "line": line, "over": _odds(o), "under": None}
                      for o in market["outcomes"] if _odds(o)]
            continue
        named = _PLAYER.match(market["name"])
        if not named:   # a field with no count ("First Goalscorer"): each runner the over at 0.5
            props += [{"player": o["name"], "stat": stat, "line": 0.5, "over": _odds(o), "under": None}
                      for o in market["outcomes"] if _odds(o)]
            continue
        player = named["player"]
        sides = {o["name"].lower(): _odds(o) for o in market["outcomes"]}
        if "over" in sides and market["line"] is not None:
            if sides["over"]:
                props.append({"player": player, "stat": stat, "line": float(market["line"]),
                              "over": sides["over"], "under": sides.get("under")})
            continue
        props += [{"player": player, "stat": stat, "line": int(rung[1]) - 0.5, "over": _odds(o), "under": None}
                  for o in market["outcomes"] for rung in [_RUNG.match(o["name"])] if rung and _odds(o)]
    matchups.tag(props, prev, game)
    return props, skipped
