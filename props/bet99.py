"""BET99 player props: a Canadian sportsbook's prices, a site to bet on rather than a sharp price.

BET99 runs on Amelco's platform, and its site reads its odds from a GraphQL endpoint on
bet99.com itself (`/java-graphql/graphql`, the `betSync` query) that answers a plain POST,
no key, cookies or login. One request lists a competition's games ("compId" 1595610: the
NFL), then one per game asks for its markets.

A player's market is named "Christian McCaffrey - Receiving Yards", its type ends with the
stat's code ("AMERICAN_FOOTBALL:FT:PROPRCYDS"; "...MILE", a Milestone, is the same stat at
other lines), and its subtype carries the player's id and the line ("M:A:830517#79.5"). Its
two selections are the over and under ("Over 79.5", or "Yes" / "No" for a touchdown), at
decimal odds. Only full-game ("FT") markets count.

Its prices never count toward Fair; it is a site to find EV on (an EV toggle, shown while the
book isn't hidden).
Each league's `BET99_COMPETITION` says its competition id and `BET99_STATS` maps the stat
codes to ours.
"""
import datetime as dt
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import matchups, net
from .icons import icon

KEY, NAME, SHORT = "bet99", "BET99", "BET99"
WEIGHT = 0     # never part of the fair chance
TARGET = True   # a site to find EV on
BOARD_GAMES_ONLY = True   # a request per game: the board asks only for its games' (site.build passes `until`)
PER_GAME = True   # costs a request per game: an odds board asks it only for the next two days' games
LEAGUE_ATTR = "BET99_COMPETITION"
ICON = icon("bet99.png")   # shown in place of its name in the column heading and the EV toggle

URL = "https://bet99.com/java-graphql/graphql"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Content-Type": "application/json",
           "Origin": "https://bet99.com", "Referer": "https://bet99.com/en/sportsbook"}
VARIABLES = {"channel": "INTERNET", "language": "en", "segment": "BET99", "region": "ca"}   # as its site asks (whats-my-segment)

GAMES_QUERY = """query betSync($filters: [Filter], $channel: String, $language: String, $segment: String, $region: String) {
  betSync(language: $language, channel: $channel, cmsSegment: $segment, region: $region) {
    events(filters: $filters) { data { id name eventTime inplay displayed } }
  }
}"""

MARKETS_QUERY = """query betSync($filters: [Filter], $none: [Filter], $channel: String, $language: String, $segment: String, $region: String) {
  betSync(language: $language, channel: $channel, cmsSegment: $segment, region: $region) {
    events(filters: $filters) { data { id
      markets(keyMarkets: false, filters: $none) { name type subtype suspended displayed
        selection { name type displayed suspended odds } } } }
  }
}"""

_PLAYER_LINE = re.compile(r"^M:[AB]:\d+#(?P<line>-?\d+(?:\.\d+)?)$")   # "M:A:830517#79.5"
OVER, UNDER = ("over", "yes"), ("under", "no")


def _post(http: httpx.Client, query: str, variables: dict) -> dict:
    with net.site("bet99"):
        r = http.post(URL, json={"operationName": "betSync", "query": query, "variables": {**VARIABLES, **variables}})
    r.raise_for_status()
    answer = r.json()
    if answer.get("errors"):
        raise ValueError(f"BET99: {answer['errors'][0].get('message')}")
    return (answer.get("data") or {}).get("betSync") or {}


def fetch(league, now: dt.datetime | None = None, until: dt.datetime | None = None) -> dict:
    """The player markets of every game in the league starting from now until `until` (by default,
    the end of its slate), before it starts."""
    now = now or net.now()
    until = until or now + dt.timedelta(days=league.SLATE_DAYS + 1)
    with httpx.Client(timeout=60, headers=HEADERS, transport=httpx.HTTPTransport(retries=3)) as http:
        listed = ((_post(http, GAMES_QUERY, {"filters": [{"field": "compId", "values": [str(league.BET99_COMPETITION)]}]})
                   .get("events") or {}).get("data") or [])
        games = {}
        for event in listed:
            if event.get("inplay") or not event.get("eventTime"):
                continue
            start = dt.datetime.fromtimestamp(event["eventTime"] / 1000, dt.timezone.utc)
            if now < start <= until and matchups.on_board(league, start, event.get("name") or ""):
                games[str(event["id"])] = matchups.game_of(start.isoformat().replace("+00:00", "Z"), event.get("name"))

        def markets(game):
            found = _post(http, MARKETS_QUERY, {"filters": [{"field": "id", "values": [game]}], "none": []})
            return [m for event in (found.get("events") or {}).get("data") or [] for m in event.get("markets") or []]

        with ThreadPoolExecutor(max_workers=2) as pool:
            pages = pool.map(markets, games)
            out = []
            for game, page in zip(games.values(), pages):
                for m in page:
                    if ":FT:" not in (m.get("type") or "") or not _PLAYER_LINE.match(m.get("subtype") or ""):
                        continue
                    out.append({"code": m["type"].rsplit(":", 1)[-1], "name": m.get("name") or "",
                                "subtype": m["subtype"], "open": m.get("displayed") is not False and not m.get("suspended"),
                                "sides": [{"name": s.get("name") or "", "type": s.get("type") or "", "odds": s.get("odds"),
                                           "open": s.get("displayed") is not False and not s.get("suspended")}
                                          for s in m.get("selection") or []],
                                **({"game": game} if game else {})})
    return {"markets": out}


def parse(payload: dict, league) -> tuple[list[dict], Counter]:
    """The league's player props, plus a count of the stat codes we don't map. A market missing a
    side is one-way ("under": None)."""
    props, skipped = [], Counter()
    for m in payload.get("markets", []):
        stat = league.BET99_STATS.get(m["code"])
        if not stat:
            skipped[m["code"]] += 1
            continue
        line = _PLAYER_LINE.match(m["subtype"])
        player = m["name"].split(" - ", 1)[0].strip()
        if not m["open"] or not line or not player:
            continue
        prices = {}
        for side in m["sides"]:
            kind = side["type"].lower()
            key = "over" if kind in OVER else "under" if kind in UNDER else None
            try:
                odds = float(side["odds"])
            except (TypeError, ValueError):
                continue
            if key and side["open"] and odds > 1:
                prices[key] = round(odds, 3)
        if "over" not in prices:
            continue
        props.append({"player": player, "stat": stat, "line": float(line["line"]),
                      "over": prices["over"], "under": prices.get("under"),
                      **({"game": m["game"]} if m.get("game") else {})})
    return props, skipped
