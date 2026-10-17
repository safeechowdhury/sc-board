"""Novig player props: an exchange's prices, a sharp source.

Novig is a no-vig prediction market, and its site reads its pages from an open
REST API (`api.novig.us/nbx/v1`), no key, cookies or login. One request lists a
league's games ("/trading/NHL/page"), then one per game lists its player props
("/event-details/<id>/player-props"), a section per market type ("SHOTS_ON_GOAL").

Each player's line has a strike and two outcomes: index 0 is the over (Yes,
"1+"), index 1 the under (No). An outcome's "available" is what it costs to buy
it, as a chance (0.375 for 37.5%), so its decimal price is one over that; null
means nobody is offering that side, which leaves the line one-way. Novig's fees
are only readable when logged in, so prices are taken as posted.
"""
import datetime as dt
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import matchups, net
from .icons import icon

KEY, NAME, SHORT = "novig", "Novig", "Novig"
WEIGHT = 7       # an exchange's prices carry the most weight in the fair chance (books.py)
ICON = icon("novig.png")   # shown in place of its name in the column heading
TARGET = False
BOARD_GAMES_ONLY = True   # a request per game: the board asks only for its games' (site.build passes `until`)
PER_GAME = True   # costs a request per game: an odds board asks it only for the next two days' games
LEAGUE_ATTR = "NOVIG_LEAGUE"

URL = "https://api.novig.us/nbx/v1"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Origin": "https://novig.com"}


def _compact(page: dict) -> list[dict]:
    """A game's player-prop markets: type, player, and each line's strike and two prices (chances)."""
    markets = []
    for section in page.get("sections", []):
        if section.get("type") != "player_prop_section":
            continue
        for row in section.get("rows", []):
            for line in row.get("lines", []):
                sides = {o.get("index"): o.get("available") for o in line.get("outcomes", [])}
                markets.append({"type": section.get("marketType"), "player": (row.get("player") or {}).get("fullName"),
                                "strike": line.get("strike"), "over": sides.get(0), "under": sides.get(1)})
    return markets


def _cards(page: dict) -> list[dict]:
    """A league page's games: in its sections' components, or a level down in their subsections
    (college football's page groups its games by conference)."""
    contents = [s.get("content") or {} for s in page.get("sections", [])]
    components = [c for content in contents for c in content.get("components") or []]
    components += [c for content in contents for sub in content.get("subSections") or [] for c in sub.get("components") or []]
    return [c for c in components if c.get("type") == "game_event_card"]


def fetch(league, now: dt.datetime | None = None, until: dt.datetime | None = None) -> dict:
    """The player props of every game in the league starting from now until `until` (by default, the
    end of its slate), before it starts."""
    now = now or net.now()
    until = until or now + dt.timedelta(days=league.SLATE_DAYS + 1)
    with httpx.Client(timeout=60, headers=HEADERS, transport=httpx.HTTPTransport(retries=3)) as http:
        def get(path: str, params: dict | None = None) -> dict:
            with net.site("novig"):
                r = http.get(f"{URL}{path}", params=params)
            r.raise_for_status()
            return r.json()

        page = get(f"/trading/{league.NOVIG_LEAGUE}/page")
        name = lambda c: " @ ".join(((c.get(side) or {}).get("name") or "") for side in ("awayTeam", "homeTeam"))
        games = {c["eventId"]: matchups.game_of(c["scheduledStart"], name(c)) for c in _cards(page)
                 if c.get("eventStatus") == "OPEN_PREGAME"
                 and now < dt.datetime.fromisoformat(c["scheduledStart"].replace("Z", "+00:00")) <= until
                 and matchups.on_board(league, c["scheduledStart"], *(((c.get(side) or {}).get("name") or "")
                                                                      for side in ("awayTeam", "homeTeam")))}
        with ThreadPoolExecutor(max_workers=3) as pool:
            pages = pool.map(lambda game: get(f"/event-details/{game}/player-props", {"displayUnseededMarkets": "false"}),
                             games)
            # each market says which game it's for ([start, "Chelsea FC @ Everton FC"])
            return {"markets": [{**m, **({"game": game} if game else {})} for game, p in zip(games.values(), pages) for m in _compact(p)]}


def _price(chance) -> float | None:
    """A side's decimal price from its cost as a chance; None where nobody offers it."""
    return round(1 / float(chance), 3) if chance and 0 < float(chance) < 1 else None


def parse(payload: dict, league) -> tuple[list[dict], Counter]:
    """The league's player props, plus a count of the market types we don't map.
    A line with only its over on offer is one-way ("under": None)."""
    props, skipped = [], Counter()
    for m in payload.get("markets", []):
        stat = league.NOVIG_STATS.get(m["type"])
        if not stat:
            skipped[m["type"]] += 1
            continue
        over = _price(m["over"])
        if not over or not m["player"] or m["strike"] is None:
            continue
        # a yes/no with no count ("FIRST_GOAL_SCORER") has strike 0: yes is one or more
        props.append({"player": m["player"], "stat": stat, "line": float(m["strike"]) or 0.5,
                      "over": over, "under": _price(m["under"]), **({"game": m["game"]} if m.get("game") else {})})
    return props, skipped
