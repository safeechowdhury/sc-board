"""Bovada player props: a recreational book's prices, shown for comparison only.

Bovada's site reads its odds from an open coupon service (`www.bovada.lv/services`)
that answers a plain request, no key or cookies. One request lists a league's
games, then one per game lists its markets in display groups ("Receiving Yards").
The games go one at a time, a moment apart: Bovada refuses even three at once.

Its prices are soft and it doesn't take Canadian players, so it neither counts
toward the board's fair chance nor is a site to find EV on: the board just shows
its column. A market comes in one of three shapes:

    "Total Receptions - Alvin Kamara (NOR)"          Over / Under, each carrying the line
    "Alternate Receptions - Alvin Kamara (NOR)"      a ladder for one player: "2+ Receptions", "3+ Receptions", ...
    "Player to record 2+ Hits"                       a ladder rung for the field: a runner per player

A ladder's rung is the over at half a point below it ("3+" is over 2.5).
"""
import datetime as dt
import re
import time
from collections import Counter

import httpx

KEY, NAME, SHORT = "bovada", "Bovada", "Bovada"
SHARP = False    # soft prices: never part of the fair chance
TARGET = False   # and not a site to find EV on

URL = "https://www.bovada.lv/services/sports/event/coupon/events/A/description"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
PAUSE = 1   # seconds between games

_PLAYER = re.compile(r"^(?P<name>.+?) - (?P<player>[^()]+?) \([A-Z]{2,4}\)$")   # "Total Receptions - Alvin Kamara (NOR)"
_RUNNER = re.compile(r"^(?P<player>[^()]+?) \([A-Z]{2,4}\)$")                   # "Nikita Kucherov (TB)"
_COUNT = re.compile(r"\d+")
_PART = re.compile(r"Quarter|Half|Period|Inning|Drive", re.I)   # part of a game: never a board stat


def _get(http: httpx.Client, path: str, params: dict) -> list:
    for wait in (5, 15, 30, None):   # Bovada answers a burst with 429: back off and ask again
        r = http.get(f"{URL}/{path}", params={"lang": "en", **params})
        if r.status_code != 429 or wait is None:
            break
        time.sleep(wait)
    r.raise_for_status()
    return r.json()


def _compact(event: dict) -> list[dict]:
    """A game's full-game markets that name a player, with each open outcome's price."""
    markets = []
    for group in event.get("displayGroups", []):
        for m in group.get("markets", []):
            if m.get("status") != "O" or (m.get("period") or {}).get("description") != "Game":
                continue
            outcomes = [{"name": o["description"], "type": o.get("type"), "line": o["price"].get("handicap"),
                         "odds": o["price"].get("decimal")}
                        for o in m.get("outcomes", []) if o.get("status") == "O" and o.get("price")]
            if _PLAYER.match(m["description"]) or any(_RUNNER.match(o["name"]) for o in outcomes):
                markets.append({"name": m["description"], "key": m.get("key"), "outcomes": outcomes})
    return markets


def fetch(league, now: dt.datetime | None = None) -> dict:
    """The player markets of every game in the league starting within its slate."""
    now = now or dt.datetime.now(dt.timezone.utc)
    until = (now + dt.timedelta(days=league.SLATE_DAYS + 1)).timestamp() * 1000
    with httpx.Client(timeout=60, headers=HEADERS, transport=httpx.HTTPTransport(retries=3)) as http:
        listing = _get(http, league.BOVADA_PATH, {"marketFilterId": "def", "preMatchOnly": "true", "eventsLimit": 100})
        links = [e["link"] for block in listing for e in block.get("events", [])
                 if not e.get("live") and now.timestamp() * 1000 < e["startTime"] < until]
        markets = []
        for i, link in enumerate(links):
            if i:
                time.sleep(PAUSE)
            try:
                page = _get(http, link.lstrip("/"), {})
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:   # listed, but its page is gone (moved or pulled): skip the game
                    continue
                raise
            markets += [m for block in page for event in block.get("events", []) for m in _compact(event)]
        return {"markets": markets}


def _odds(outcome: dict) -> float | None:
    return round(float(outcome["odds"]), 3) if outcome.get("odds") else None


def parse(payload: dict, league) -> tuple[list[dict], Counter]:
    """The league's player props, plus a count of the player markets we don't map (by name, its
    count written as N). A one-way price (a ladder's rung) has "under": None."""
    props, skipped = [], Counter()
    for market in payload.get("markets", []):
        one = _PLAYER.match(market["name"])
        if one:   # one player's over/under or ladder
            name, player = one["name"], one["player"]
            stat = league.BOVADA_STATS.get(name)
            if not stat:
                if not _PART.search(name):
                    skipped[name] += 1
                continue
            if market["key"] == "2W-OU":
                sides = {o["type"]: o for o in market["outcomes"] if o["type"] in ("O", "U") and o["line"]}
                over, under = sides.get("O"), sides.get("U")
                if not over or not _odds(over) or (under and under["line"] != over["line"]):
                    continue
                # some post only the over ("Total Doubles": Over 0.5), a one-way price
                props.append({"player": player, "stat": stat, "line": float(over["line"]),
                              "over": _odds(over), "under": _odds(under) if under else None})
                continue
            for o in market["outcomes"]:
                count = _COUNT.search(o["name"])
                if count and _odds(o):
                    props.append({"player": player, "stat": stat, "line": int(count.group()) - 0.5,
                                  "over": _odds(o), "under": None})
            continue
        # the field: one rung, a runner per player ("Player to record a Hit" is 1+)
        name = _COUNT.sub("N", market["name"])
        stat = league.BOVADA_STATS.get(name)
        if not stat:
            if not _PART.search(name):
                skipped[name] += 1
            continue
        count = _COUNT.search(market["name"])
        line = (int(count.group()) if count else 1) - 0.5
        for o in market["outcomes"]:
            runner = _RUNNER.match(o["name"])
            if runner and _odds(o):
                props.append({"player": runner["player"], "stat": stat, "line": line, "over": _odds(o), "under": None})
    return props, skipped
