"""Stake player props: the crypto sportsbook's Swish pick-builder prices.

Stake's site reads its odds from a GraphQL endpoint on stake.com itself that
answers plain POSTs carrying the headers its own pages send (Referer, Origin
and the x-operation-* names) — no cookies, no key. One query lists a sport's
fixtures, then one per fixture asks its Swish game for every player's markets:
a stat, and the over and under decimal prices at each line. The endpoint
refuses clients whose TLS handshake isn't a browser's, so a fetch goes through
curl_cffi impersonating Chrome.

Only full-game player markets count: a half's or a quarter's ("1st Half
Receiving Yards"), a longest play and the defensive stats are never board rows.
Each league's `STAKE_STATS` maps Swish's stat names to ours, and `STAKE_SPORT`
says the sport's slug on stake.com ("ice-hockey").
"""
import datetime as dt
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime

from curl_cffi import requests as creq

from . import matchups, net
from .icons import icon

KEY, NAME, SHORT = "stake", "Stake", "Stake"
WEIGHT = 0     # a soft recreational book: never part of the fair chance
TARGET = True  # a site to find EV on
BOARD_GAMES_ONLY = True   # a request per game: the board asks only for its games' (site.build passes `until`)
PER_GAME = True   # costs a request per game: an odds board asks it only for the next two days' games
LEAGUE_ATTR = "STAKE_SPORT"
ICON = icon("stake.png")     # shown in place of its name in the EV toggle

SITE = "https://stake.com"
GRAPHQL = f"{SITE}/_api/graphql"
IMPERSONATE = "chrome146"
HEADERS = {"Referer": f"{SITE}/sports/home", "Origin": SITE, "Content-Type": "application/json",
           "x-language": "en", "x-operation-name": "SportIndex", "x-operation-type": "query",
           "Accept": "*/*"}

INDEX_QUERY = """query SportIndex($sport: String!, $type: SportSearchEnum = popular) {
  slugSport(sport: $sport) {
    firstTournament: tournamentList(type: $type, limit: 1) {
      fixtureList(type: $type, limit: 50) {
        slug
        status
        data { __typename ... on SportFixtureDataMatch { startTime competitors { name } } }
      }
    }
  }
}"""

MARKETS_QUERY = """query StakeMarkets($fixture: String!, $inPlay: Boolean!) {
  slugFixture(fixture: $fixture) {
    id
    status
    data { __typename ... on SportFixtureDataMatch { startTime competitors { name } } }
    swishGame {
      status
      players {
        name
        markets(inPlay: $inPlay, statTypes: [match, player, match_props, team_props]) {
          stat { name value }
          lines { line over under suspended }
        }
      }
    }
  }
}"""

_PART = re.compile(r"1st half|2nd half|1st quarter|\dth quarter|longest|first touchdown|last touchdown"
                   r"|defensive|tackles|sacks|fumbles|at bat", re.I)


def _post(payload: dict):
    """A GraphQL answer. A refusal or a network failure is a ValueError, which the build
    reports and moves past like any other book's failed fetch."""
    def ask():
        try:
            with net.site("stake"):
                r = creq.post(GRAPHQL, json=payload, headers=HEADERS, impersonate=IMPERSONATE, timeout=60)
            r.raise_for_status()
        except creq.exceptions.RequestException as e:
            raise ValueError(f"Stake: {e}") from e
        return r.json()
    return net.once(("stake-graphql", json.dumps(payload, sort_keys=True)), ask)


def fetch(league, now: dt.datetime | None = None, until: dt.datetime | None = None) -> dict:
    """The priced player markets of every Swish-enabled game in the league starting from
    `now` until `until` (by default, the end of its slate)."""
    now = now or net.now()
    until = until or now + dt.timedelta(days=league.SLATE_DAYS + 1)
    index = _post({"query": INDEX_QUERY, "operationName": "SportIndex",
                   "variables": {"sport": league.STAKE_SPORT, "type": "popular"}})
    fixtures = [fixture for tournament in ((index.get("data") or {}).get("slugSport") or {}).get("firstTournament") or []
                for fixture in tournament.get("fixtureList") or [] if fixture.get("status") == "active"]

    def started(fixture) -> dt.datetime | None:
        data = fixture.get("data")
        if isinstance(data, dict) and data.get("startTime"):
            return parsedate_to_datetime(data["startTime"])
        return None

    on_the_slate = [fixture for fixture in fixtures
                    if (kick := started(fixture)) and now < kick <= until
                    and matchups.on_board(league, kick, fixture["slug"])]

    with ThreadPoolExecutor(max_workers=6) as pool:
        pages = list(pool.map(lambda fixture: _post(
            {"query": MARKETS_QUERY, "operationName": "StakeMarkets",
             "variables": {"fixture": fixture["slug"], "inPlay": False}}), on_the_slate))
    return {"fixtures": [((page.get("data") or {}).get("slugFixture") or {}) for page in pages]}


def parse(payload: dict, league) -> tuple[list[dict], Counter]:
    """The league's player props, plus a count of the stats we don't map (by Swish's name)."""
    stat_names = {re.sub(r"\s+", " ", key).strip().lower(): stat for key, stat in league.STAKE_STATS.items()}
    props, skipped = [], Counter()
    for fixture in payload.get("fixtures", []):
        swish = fixture.get("swishGame") or {}
        for player in swish.get("players") or []:
            for market in player.get("markets") or []:
                name = re.sub(r"\s+", " ", (market.get("stat") or {}).get("name") or "").strip().lower()
                if not name or _PART.search(name):
                    continue
                stat = stat_names.get(name)
                if not stat:
                    skipped[name] += 1
                    continue
                for line in market.get("lines") or []:
                    if line.get("suspended") or not line.get("over") or not line.get("under"):
                        continue
                    props.append({"player": player.get("name"), "stat": stat,
                                  "line": float(line["line"]), "over": float(line["over"]),
                                  "under": float(line["under"])})
    return props, skipped
