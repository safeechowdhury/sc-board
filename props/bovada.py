"""Bovada player props: a recreational book's prices, shown for comparison only.

Bovada's site reads its odds from an open coupon service (`www.bovada.lv/services`)
that answers a plain request, no key or cookies. One request lists a league's
games with every market of each, in display groups ("Receiving Yards"): asked with
no market filter, the listing carries what each game's own page would. Requests
go one at a time, a second apart (net.LIMITS): Bovada refuses even three at once.

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

from . import matchups, net
from .icons import icon

KEY, NAME, SHORT = "bovada", "Bovada", "Bovada"
WEIGHT = 0      # soft prices: never part of the fair chance
ICON = icon("bovada.png")   # shown in place of its name in the column heading
TARGET = False   # and not a site to find EV on
# the board reads it only for the games it lists, once it knows them (site.build passes `until`,
# the last one's start); the listing is asked for once a run either way
BOARD_GAMES_ONLY = True
LEAGUE_ATTR = "BOVADA_PATH"

URL = "https://www.bovada.lv/services/sports/event/coupon/events/A/description"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

_PLAYER = re.compile(r"^(?P<name>.+?) - (?P<player>[^()]+?) \([A-Z]{2,4}\)$")   # "Total Receptions - Alvin Kamara (NOR)"
_RUNNER = re.compile(r"^(?P<player>[^()]+?) \([A-Z]{2,4}\)$")                   # "Nikita Kucherov (TB)"
_COUNT = re.compile(r"\d+")
_PART = re.compile(r"Quarter|Half|Period|Inning|Drive", re.I)   # part of a game: never a board stat


def _get(http: httpx.Client, path: str, params: dict) -> list:
    """One page, asked once a run (the props and the lines pages read the same games) and not at
    all while the copy kept from an earlier run is still fresh, one request at a time a second
    apart (net.LIMITS)."""
    def ask():
        with net.site("bovada"):
            for wait in (5, 15, 30, None):   # Bovada answers a burst with 429: back off and ask again
                r = http.get(f"{URL}/{path}", params={"lang": "en", **params})
                if r.status_code != 429 or wait is None:
                    break
                time.sleep(wait)
        r.raise_for_status()
        return r.json(), getattr(r, "headers", {})
    key = ("bovada", path, tuple(sorted(params.items())))
    # its CDN keeps a page 10 minutes: one fetched within that is the same page
    return net.once(key, lambda: net.fresh(key, ask))


def _compact(event: dict) -> list[dict]:
    """A game's full-game markets that name a player, with each open outcome's price."""
    markets = []
    for group in event.get("displayGroups", []):
        for m in group.get("markets", []):
            # the whole game: soccer's is "Regulation Time"
            if m.get("status") != "O" or (m.get("period") or {}).get("description") not in ("Game", "Regulation Time"):
                continue
            outcomes = [{"name": o["description"], "type": o.get("type"), "line": o["price"].get("handicap"),
                         "odds": o["price"].get("decimal")}
                        for o in m.get("outcomes", []) if o.get("status") == "O" and o.get("price")]
            if _PLAYER.match(m["description"]) or any(_RUNNER.match(o["name"]) for o in outcomes):
                markets.append({"name": m["description"], "key": m.get("key"), "outcomes": outcomes})
    return markets


def listing(http: httpx.Client, league) -> list[dict]:
    """The league's games not yet started, each with every market of its own page: the listing
    asked with no market filter carries them all, so no game's page is asked for. The lines page
    reads the same answer (its Game Lines)."""
    found = _get(http, league.BOVADA_PATH, {"preMatchOnly": "true", "eventsLimit": 100})
    return [e for block in found for e in block.get("events", []) if not e.get("live")]


def fetch(league, now: dt.datetime | None = None, until: dt.datetime | None = None) -> dict:
    """The player markets of every game in the league starting from now until `until` (by default,
    the end of its slate)."""
    now = now or net.now()
    until = (until or now + dt.timedelta(days=league.SLATE_DAYS + 1)).timestamp() * 1000
    with httpx.Client(timeout=60, headers=HEADERS, transport=httpx.HTTPTransport(retries=3)) as http:
        # each market says which game it's for ([start, "Chelsea @ Everton"])
        game = lambda e: matchups.game_of(dt.datetime.fromtimestamp(e["startTime"] / 1000, dt.timezone.utc).isoformat(), e.get("description"))
        return {"markets": [{**m, **({"game": game(e)} if game(e) else {})} for e in listing(http, league)
                            if now.timestamp() * 1000 < e["startTime"] <= until
                            and matchups.on_board(league, e["startTime"], e.get("description") or "")
                            for m in _compact(e)]}


def _odds(outcome: dict) -> float | None:
    return round(float(outcome["odds"]), 3) if outcome.get("odds") else None


def parse(payload: dict, league) -> tuple[list[dict], Counter]:
    """The league's player props, plus a count of the player markets we don't map (by name, its
    count written as N). A one-way price (a ladder's rung) has "under": None."""
    props, skipped = [], Counter()
    prev, game = 0, None
    for market in payload.get("markets", []):
        matchups.tag(props, prev, game)   # the last market's props are for its game
        prev, game = len(props), market.get("game")
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
                skipped[name.split(" - ")[0]] += 1   # "First Touchdown Scorer - Texas (#N)" counts as its market
            continue
        count = _COUNT.search(market["name"])
        line = (int(count.group()) if count else 1) - 0.5
        for o in market["outcomes"]:
            runner = _RUNNER.match(o["name"])
            if runner and _odds(o):
                props.append({"player": runner["player"], "stat": stat, "line": line, "over": _odds(o), "under": None})
    matchups.tag(props, prev, game)
    return props, skipped
