"""Kambi player props: the odds Kambi sets for Unibet, BetRivers and other sportsbooks it runs.

Kambi serves every sportsbook it runs from one open offering API, no key or
cookies; a brand code picks the book (`KAMBI_BRAND`, default `ubca`, Unibet
Canada, which posts the most two-way props). One request lists a league's
games, then one per BATCH games lists their bet offers, PAGE offers a request.

An offer is named by its criterion ("Total Receiving Yards by the Player -
Including Overtime") and holds an outcome per player and side. Over/under
outcomes carry the line; a ladder's "Yes" ("2+ Hits by the Player") carries
the count as its line, which is the over at half a point below it, and its
"No", where Kambi posts one, is the under. Odds and lines are in thousandths.
"""
import datetime as dt
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import matchups, net
from .icons import icon

KEY, NAME, SHORT = "kambi", "Kambi", "Kambi"
WEIGHT = 0     # shown only: BetRivers' feed, which the market moves away from (books.py)
ICON = icon("kambi.png")   # shown in place of its name in the column heading
TARGET = False
LEAGUE_ATTR = "KAMBI_PATH"

BRAND = os.environ.get("KAMBI_BRAND", "ubca")
URL = f"https://eu.offering-api.kambicdn.com/offering/v2018/{BRAND}"
PARAMS = {"lang": "en_CA", "market": "CA"}
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
BATCH = 10   # games per bet-offer request
PAGE = 2000   # the most offers Kambi answers a request with

_COUNT = re.compile(r"\d+")
_NOTE = re.compile(r"\s*\([^)]*\)$")   # "(Listed player must be in starting lineup for bets to stand)"
_PART = re.compile(r"Quarter|Half|Period|Innings? N\b|First N Innings", re.I)   # part of a game: never a board stat
_SIDES = {"OT_OVER": "over", "OT_UNDER": "under", "OT_YES": "over", "OT_NO": "under",
          "OT_PLAYER_PARTICIPANT": "over"}   # a field's runner ("First Goal Scorer"): that player, one or more


def criterion(label: str) -> str:
    """A criterion without its trailing note and with its count written as N, so every rung of a
    ladder maps once: "2+ Hits by the Player - Including Extra Innings (Listed player must be in
    starting lineup for bets to stand)" -> "N+ Hits by the Player - Including Extra Innings"."""
    return _COUNT.sub("N", _NOTE.sub("", label))


def _get(http: httpx.Client, path: str, params: dict | None = None) -> dict:
    def ask():
        with net.site("kambi"):
            r = http.get(f"{URL}/{path}", params={**PARAMS, **(params or {})})
        r.raise_for_status()
        return r.json()
    return net.once(("kambi", path, tuple(sorted((params or {}).items()))), ask)   # the game list serves both pages


def offers(http: httpx.Client, games: list) -> list[dict]:
    """The bet offers of the games, BATCH games a request (Kambi answers "betoffer/event/1,2,3"
    with each game's full offers, PAGE offers at most, its `range` saying how many more there
    are). Within a run a game is asked for once, so the lines page reads the props' answers."""
    def batch(ids) -> list[dict]:
        path, offers, start = f"betoffer/event/{','.join(map(str, ids))}.json", {}, 0
        while True:
            page = _get(http, path, {"includeParticipants": "true", "range_start": start, "range_size": PAGE})
            for offer in page.get("betOffers", []):
                offers.setdefault(offer.get("id"), offer)   # an offer the pages shifted onto twice counts once
            start += PAGE
            if start >= ((page.get("range") or {}).get("total") or 0) or not page.get("betOffers"):
                return list(offers.values())

    def ask(keys):
        ids = [key[2] for key in keys]
        with ThreadPoolExecutor(max_workers=4) as pool:
            pages = list(pool.map(batch, [ids[i:i + BATCH] for i in range(0, len(ids), BATCH)]))
        found = {key: [] for key in keys}
        for offers in pages:
            for offer in offers:
                found.setdefault(("kambi", "event", offer.get("eventId")), []).append(offer)
        return found
    answers = net.once_each([("kambi", "event", game) for game in games], ask)
    return [offer for found in answers.values() for offer in found or []]


def _compact(offer: dict, games: dict | None = None) -> dict:
    found = {"criterion": offer["criterion"]["englishLabel"], "outcomes": [
        {"player": o.get("participant"), "type": o.get("type"), "line": o.get("line"), "odds": o.get("odds"),
         "status": o.get("status")}
        for o in offer.get("outcomes", []) if o.get("participant")]}
    if games and games.get(offer.get("eventId")):
        found["game"] = games[offer["eventId"]]   # [start, "Everton - Chelsea"]: which game the prop is for
    return found


def games(league) -> list[dict]:
    """Every game the league's list shows that hasn't started: its id, start, home and away team and
    group ("Premier League"). The same request fetch() starts from, so within a run it is asked once."""
    with httpx.Client(timeout=60, headers=HEADERS, transport=httpx.HTTPTransport(retries=3)) as http:
        listed = _get(http, f"listView/{league.KAMBI_PATH}.json").get("events", [])
    return [{"id": e["event"]["id"], "start": e["event"]["start"], "home": e["event"].get("homeName") or "",
             "away": e["event"].get("awayName") or "", "group": e["event"].get("group") or ""}
            for e in listed if e["event"].get("state") == "NOT_STARTED" and e["event"].get("homeName")]


def fetch(league, now: dt.datetime | None = None) -> dict:
    """The player offers of every game in the league starting within its slate."""
    now = now or net.now()
    until = now + dt.timedelta(days=league.SLATE_DAYS + 1)
    with httpx.Client(timeout=60, headers=HEADERS, transport=httpx.HTTPTransport(retries=3)) as http:
        listed = _get(http, f"listView/{league.KAMBI_PATH}.json").get("events", [])
        games = {e["event"]["id"]: matchups.game_of(e["event"]["start"], e["event"].get("name")) for e in listed
                 if e["event"].get("state") == "NOT_STARTED"
                 and now <= dt.datetime.fromisoformat(e["event"]["start"].replace("Z", "+00:00")) <= until
                 and matchups.on_board(league, e["event"]["start"], e["event"].get("name") or "")}
        return {"offers": [_compact(o, games) for o in offers(http, list(games))
                           if any(oc.get("participant") and oc.get("type") in _SIDES for oc in o.get("outcomes", []))]}


def parse(payload: dict, league) -> tuple[list[dict], Counter]:
    """The league's player props, plus a count of the full-game player offers we don't map (by criterion).
    A ladder's "Yes" with no "No" is a one-way price ("under": None)."""
    props, skipped = [], Counter()
    for offer in payload.get("offers", []):
        name = criterion(offer["criterion"])
        stat = league.KAMBI_STATS.get(name)
        if not stat:
            if "player" in name.lower() and not _PART.search(name):
                skipped[name] += 1
            continue
        sides: dict[tuple, dict] = {}   # (player, line) -> {"over": odds, "under": odds}
        for o in offer["outcomes"]:
            side = _SIDES.get(o["type"])
            if not side or o["status"] != "OPEN" or not o["odds"]:
                continue
            # an over/under's line is the line; a ladder's is the count, the over half a point below it;
            # a field's runner has none, and is the over at 0.5
            if o["type"] == "OT_PLAYER_PARTICIPANT":
                line = 0.5
            elif o["line"] is None:
                continue
            else:
                line = o["line"] / 1000 - (0.5 if o["type"] in ("OT_YES", "OT_NO") else 0)
            sides.setdefault((o["player"], line), {})[side] = round(o["odds"] / 1000, 3)
        props += [{"player": player, "stat": stat, "line": line, "over": odds["over"], "under": odds.get("under"),
                   **({"game": offer["game"]} if offer.get("game") else {})}
                  for (player, line), odds in sides.items() if "over" in odds]
    return props, skipped
