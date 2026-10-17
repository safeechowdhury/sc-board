"""Betano player props: a Canadian sportsbook's prices, a site to bet on rather than a sharp price.

Betano's site reads its odds from APIs on www.betano.ca itself that refuse
clients whose TLS handshake isn't a browser's (its splash screen answers
everyone else with 403), so a fetch goes through curl_cffi impersonating
Chrome. One request lists a league's page, whose HTML embeds the games; one per
game lists its markets, each a table: a stat, a row per player, and the over
and under prices at the row's line. Goal-scorer tables price the anytime, first
and last scorer as columns instead; the anytime column is the over at 0.5, and
so is every "Yes" and "N+" market, each rung the over half a point below it.

Markets are named by their table's title ("Shots on Goal", "[Player] Points"),
and each league's `BETANO_STATS` maps those titles to ours. A half's or a
quarter's ("Passing Yards - First Half") is never a board stat and is skipped.
Each league says where its page is (`BETANO_PATH`) and which of a game's tabs holds
its player markets (`BETANO_TAB`: soccer's "Player Markets" is the fourth).
"""
import datetime as dt
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from curl_cffi import requests as creq

from . import matchups, net
from .icons import icon

KEY, NAME, SHORT = "betano", "Betano", "Betano"
WEIGHT = 0     # a soft recreational book: never part of the fair chance
TARGET = True    # a site to find EV on
BOARD_GAMES_ONLY = True   # a request per game: the board asks only for its games' (site.build passes `until`)
PER_GAME = True   # costs a request per game: an odds board asks it only for the next two days' games
LEAGUE_ATTR = "BETANO_PATH"
# shown in place of its name in the EV toggle
ICON = icon("betano-tile.png")

SITE = "https://www.betano.ca"
IMPERSONATE = "chrome124"   # the TLS fingerprint the splash screen asks for

_PART = re.compile(r"first half|second half|first quarter|second quarter|third quarter|fourth quarter|period \d|1st|2nd|3rd", re.I)


def _get(url: str):
    """A page or an API answer, impersonating a browser's TLS handshake. A refusal (the splash
    screen's 403) or a network failure is a ValueError, which the build reports and moves past
    like any other book's failed fetch, where curl_cffi's own errors would stop it."""
    def ask():
        try:
            with net.site("betano"):
                r = creq.get(url, impersonate=IMPERSONATE, timeout=60)
            r.raise_for_status()
        except creq.exceptions.RequestException as e:
            raise ValueError(f"Betano: {e}") from e
        return r
    return net.once(("betano", url), ask)   # the league page serves the props and the lines page


def league_events(league) -> list[dict]:
    """The games the league's page lists, each with its main markets (Winner, Spread, Points)."""
    return net.once(("betano-events", league.BETANO_PATH), lambda: _league_events(league))


def _league_events(league) -> list[dict]:
    page = _get(SITE + league.BETANO_PATH)
    embedded = re.search(r'window\["initial_state"\]=', page.text)
    if not embedded:
        raise ValueError(f"Betano league page {league.BETANO_PATH} has no events embedded")
    state, _ = json.JSONDecoder().raw_decode(page.text[embedded.end():])
    return [event for block in state.get("data", {}).get("blocks", []) for event in block.get("events", [])]


def fetch(league, now: dt.datetime | None = None, until: dt.datetime | None = None) -> dict:
    """The player markets of every game in the league starting from now until `until` (by default,
    the end of its slate)."""
    now = now or net.now()
    until = until or now + dt.timedelta(days=league.SLATE_DAYS + 1)
    stamp = lambda event: dt.datetime.fromtimestamp(event["startTime"] / 1000, dt.timezone.utc)
    # the page lists games weeks out: only the slate's are asked for, and none under way
    events = [event for event in league_events(league)
              if isinstance(event.get("startTime"), (int, float)) and now < stamp(event) <= until
              and matchups.on_board(league, event["startTime"], event.get("name") or "")]
    tab = getattr(league, "BETANO_TAB", 1)

    def markets(event) -> dict:
        r = _get(f"{SITE}/api/game-odds/x/{event['id']}/?bt={tab}&req=s")
        r.raise_for_status()
        game = matchups.game_of(stamp(event).isoformat(), event.get("name"))   # which game each market is for
        return {"name": event.get("name"),
                "markets": [{**m, **({"game": game} if game else {})} for m in ((r.json().get("data") or {}).get("event") or {}).get("markets") or []]}

    with ThreadPoolExecutor(max_workers=6) as pool:
        pages = list(pool.map(markets, events))
    return {"markets": [m for page in pages for m in page["markets"]], "games": [page["name"] for page in pages]}


_COUNT = re.compile(r"(\d+)\+")
_HAT_TRICK = re.compile(r"hat[- ]trick", re.I)   # soccer's three goals: "Multi-Goalscorer" has "2+ Goals" and "Hat-trick"
_OVER = re.compile(r"^over (\d+(?:\.\d+)?)", re.I)


def _line(selection: dict, *titles: str) -> float:
    """A one-way price's line. An over that names its line ("Over 1.5") is at it; a rung is the over
    half a point below its count, the first "N+" in its name ("50+"), its column's title ("3+ Points")
    or its table's ("To Score 2+ Goals"); a "Yes" with no count is the over at 0.5. Betano's handicap
    is 0 on the rungs, so it says nothing there."""
    name = selection.get("name") or ""
    over = _OVER.match(name)
    if over:
        return float(over.group(1))
    for text in (name, *titles):
        found = _COUNT.search(text or "")
        if found:
            return float(found.group(1)) - 0.5
        if _HAT_TRICK.search(text or ""):
            return 2.5
    return 0.5


def _by_column(picks: list[dict], columns: list[str]) -> list[tuple[str, dict]]:
    """Each price with its column's title. A row with a column missing (no anytime price, only
    first and last) says which column each price is in, so its prices aren't read left to right."""
    return [(columns[i] if isinstance(i := s.get("columnIndex", n), int) and i < len(columns) else "", s)
            for n, s in enumerate(picks)]


def parse(payload: dict, league) -> tuple[list[dict], Counter]:
    """The league's player props, plus a count of the tables we don't map (by title)."""
    titles = {re.sub(r"\s+", " ", key).strip().lower(): stat for key, stat in league.BETANO_STATS.items()}
    firsts = {re.sub(r"\s+", " ", key).strip().lower(): stat for key, stat in league.BETANO_FIRST.items()}
    props, skipped = [], Counter()
    prev, game = 0, None
    for market in payload.get("markets", []):
        matchups.tag(props, prev, game)   # the last market's props are for its game
        prev, game = len(props), market.get("game")
        table = market.get("tableLayout") or {}
        title = re.sub(r"\s+", " ", table.get("title") or "").strip()
        if not title or _PART.search(title):
            continue
        columns = [((column or {}).get("title") or "").lower() for column in table.get("columnTitles") or []]
        scorer = columns and set(columns) <= {"anytime", "first", "last"}
        # tables repeat one stat under several names ("Points O/U", "Total Points O/U", "2+ Points")
        name = title.lower()
        stat = first = None
        for candidate in (name, re.sub(r" o/u$", "", name), re.sub(r"^total ", "", re.sub(r" o/u$", "", name)),
                          re.sub(r"^\d\+ ", "", name), re.sub(r" \d\+$", "", name)):
            stat = stat or titles.get(candidate)
            first = first or firsts.get(candidate)
        if scorer and not (stat or first):
            skipped[title] += 1
            continue
        if not scorer and not stat:
            skipped[title] += 1
            continue
        for row in table.get("rows") or []:
            player = row.get("title")
            for group in row.get("groupSelections") or []:
                picks = group.get("selections") or []
                if scorer:
                    props += [{"player": player, "line": 0.5, "under": None,
                               "stat": {"anytime": stat, "first": first}.get(column),
                               "over": float(s["price"])}
                              for column, s in _by_column(picks, columns) if s.get("price") and column != "last"]
                    continue
                if {"over", "under"} <= set(columns):
                    line = float(group.get("handicap") or 0)
                    sides = {}
                    for s in picks:
                        if not s.get("price"):
                            continue
                        name = (s.get("name") or "").lower()
                        if name.startswith("over"):
                            sides["over"] = float(s["price"])
                        elif name.startswith("under"):
                            sides["under"] = float(s["price"])
                    if "over" in sides:
                        props.append({"player": player, "stat": stat, "line": line,
                                      "over": sides["over"], "under": sides.get("under")})
                elif {"yes", "no"} <= set(columns):
                    # "Double-Double": Yes and No are the two sides at 0.5
                    sides = {(s.get("name") or "").lower(): float(s["price"]) for s in picks if s.get("price")}
                    if "yes" in sides:
                        props.append({"player": player, "stat": stat, "line": 0.5,
                                      "over": sides["yes"], "under": sides.get("no")})
                elif picks and picks[0].get("price"):
                    # one-way prices: rungs ("50+", "2+ Points", a scorer by name), a "Yes", or an over
                    # alone ("Over 0.5"); _line says where each is
                    props += [{"player": player, "stat": stat, "under": None, "line": _line(s, column, title),
                               "over": float(s["price"])}
                              for column, s in _by_column(picks, columns) if s.get("price")]
    matchups.tag(props, prev, game)
    return [prop for prop in props if prop["stat"]], skipped
