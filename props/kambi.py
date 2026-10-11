"""Kambi player props: the odds Kambi sets for Unibet, BetRivers and other sportsbooks it runs.

Kambi serves every sportsbook it runs from one open offering API, no key or
cookies; a brand code picks the book (`KAMBI_BRAND`, default `ubca`, Unibet
Canada, which posts the most two-way props). One request lists a league's
games, then one per game lists its bet offers.

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

KEY, NAME, SHORT = "kambi", "Kambi", "Kambi"
SHARP = True   # its prices count toward the fair chance
TARGET = False

BRAND = os.environ.get("KAMBI_BRAND", "ubca")
URL = f"https://eu.offering-api.kambicdn.com/offering/v2018/{BRAND}"
PARAMS = {"lang": "en_CA", "market": "CA"}
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

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
    r = http.get(f"{URL}/{path}", params={**PARAMS, **(params or {})})
    r.raise_for_status()
    return r.json()


def _compact(offer: dict) -> dict:
    return {"criterion": offer["criterion"]["englishLabel"], "outcomes": [
        {"player": o.get("participant"), "type": o.get("type"), "line": o.get("line"), "odds": o.get("odds"),
         "status": o.get("status")}
        for o in offer.get("outcomes", []) if o.get("participant")]}


def fetch(league, now: dt.datetime | None = None) -> dict:
    """The player offers of every game in the league starting within its slate."""
    now = now or dt.datetime.now(dt.timezone.utc)
    until = now + dt.timedelta(days=league.SLATE_DAYS + 1)
    with httpx.Client(timeout=60, headers=HEADERS, transport=httpx.HTTPTransport(retries=3)) as http:
        listed = _get(http, f"listView/{league.KAMBI_PATH}.json").get("events", [])
        games = [e["event"]["id"] for e in listed if e["event"].get("state") == "NOT_STARTED"
                 and now <= dt.datetime.fromisoformat(e["event"]["start"].replace("Z", "+00:00")) <= until]
        with ThreadPoolExecutor(max_workers=4) as pool:
            pages = pool.map(lambda game: _get(http, f"betoffer/event/{game}.json", {"includeParticipants": "true"}), games)
            return {"offers": [_compact(o) for page in pages for o in page.get("betOffers", [])
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
        props += [{"player": player, "stat": stat, "line": line, "over": odds["over"], "under": odds.get("under")}
                  for (player, line), odds in sides.items() if "over" in odds]
    return props, skipped
