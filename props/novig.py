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

KEY, NAME, SHORT = "novig", "Novig", "Novig"
SHARP = True     # an exchange's prices count toward the fair chance
TARGET = False

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


def fetch(league, now: dt.datetime | None = None) -> dict:
    """The player props of every game in the league starting within its slate, before it starts."""
    now = now or dt.datetime.now(dt.timezone.utc)
    until = now + dt.timedelta(days=league.SLATE_DAYS + 1)
    with httpx.Client(timeout=60, headers=HEADERS, transport=httpx.HTTPTransport(retries=3)) as http:
        def get(path: str, params: dict | None = None) -> dict:
            r = http.get(f"{URL}{path}", params=params)
            r.raise_for_status()
            return r.json()

        page = get(f"/trading/{league.NOVIG_LEAGUE}/page")
        games = [c["eventId"] for s in page.get("sections", []) if (s.get("content") or {}).get("type") == "components"
                 for c in s["content"].get("components", [])
                 if c.get("type") == "game_event_card" and c.get("eventStatus") == "OPEN_PREGAME"
                 and now < dt.datetime.fromisoformat(c["scheduledStart"].replace("Z", "+00:00")) < until]
        with ThreadPoolExecutor(max_workers=3) as pool:
            pages = pool.map(lambda game: get(f"/event-details/{game}/player-props", {"displayUnseededMarkets": "false"}),
                             games)
            return {"markets": [m for p in pages for m in _compact(p)]}


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
                      "over": over, "under": _price(m["under"])})
    return props, skipped
