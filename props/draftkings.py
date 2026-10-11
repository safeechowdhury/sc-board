"""DraftKings player props: a third sportsbook price.

DraftKings' site reads its odds from sportsbook-nash.draftkings.com. That host
refuses a request that arrives without the cookies the sportsbook's home page
sets, so a fetch opens the home page first and keeps its cookies, as a browser
would. Like FanDuel and Pinnacle it can't be read from the board's own page.

A league lists its categories ("Batter Props"); a category lists its
subcategories by name, and only the "... O/U" ones are two-way ("Hits O/U").
The rest are one-sided "N+" ladders ("Hits"): each rung is the over half a
point below it ("3+" is over 2.5), read where the league maps them. A few
are a field instead, a runner per player ("Anytime Goalscorer", "2+ Goals",
"First Goalscorer"), read where the league maps the market's name with its
count as N; a field that isn't a player (NFL "WAS Commanders D/ST") has no
participant and is left out. A subcategory named in the fields is read whole
("TD Scorer" holds "Anytime TD Scorer", "2+ TDs" and "First TD Scorer"), and a
field mapped to None is passed over without being listed as unmapped. Each subcategory is one request and covers every game in the league.
"""
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

KEY, NAME, SHORT = "draftkings", "DraftKings", "DK"
SHARP = True   # its prices count toward the fair chance
TARGET = False

SITE = os.environ.get("DRAFTKINGS_SITE", "dkusny")   # DraftKings' state site code
HOME = "https://sportsbook.draftkings.com/"
URL = f"https://sportsbook-nash.draftkings.com/api/sportscontent/{SITE}/v1/leagues"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

_TWO_WAY = re.compile(r"\bO/U$")
_TEAM_TAG = re.compile(r"\s*\(([A-Z]{2,4})\)$")   # "Max Muncy (LAD)": DraftKings names the team when two players share a name
_COUNT = re.compile(r"\d+")


def _field(name: str) -> str:
    """A field market's name with its count as N: "2+ Goals" -> "N+ Goals"."""
    return _COUNT.sub("N", name)


def _player(selection: dict) -> str | None:
    return next((p["name"] for p in selection.get("participants", []) if p.get("type") == "Player"), None)


def _compact(data: dict) -> dict:
    """The parts of a subcategory parse() reads: games not started yet, their markets, each side's price."""
    upcoming = {e["id"] for e in data.get("events", []) if e.get("status") == "NOT_STARTED"}
    markets = {m["id"]: m.get("name", "") for m in data.get("markets", []) if m.get("eventId") in upcoming}
    return {"selections": [
        {"market": s["marketId"], "name": markets[s["marketId"]], "player": _player(s),
         "side": (s.get("outcomeType") or "").lower(), "line": s.get("points"),
         "odds": s.get("trueOdds"), "main": bool(s.get("main")), "milestone": s.get("milestoneValue")}
        for s in data.get("selections", []) if s.get("marketId") in markets
    ]}


def fetch(league) -> dict:
    """Every two-way subcategory in the league's prop categories, and the ladders it maps, by name."""
    with httpx.Client(timeout=60, headers=HEADERS, follow_redirects=True,
                      transport=httpx.HTTPTransport(retries=3)) as http:
        http.get(HOME).raise_for_status()   # sets the cookies the odds host asks for

        def get(path: str) -> dict:
            r = http.get(f"{URL}/{league.DRAFTKINGS_LEAGUE}{path}")
            r.raise_for_status()
            return r.json()

        fields = getattr(league, "DRAFTKINGS_FIELDS", {})
        categories = {str(c["id"]): c.get("name", "").strip() for c in get("").get("categories", [])}
        wanted = [cid for cid, name in categories.items() if name in league.DRAFTKINGS_CATEGORIES]
        subcategories = []
        for cid in wanted:
            subcategories += [(cid, s["id"], s.get("name", "").strip()) for s in get(f"/categories/{cid}").get("subcategories", [])
                              if str(s.get("categoryId")) == cid and (_TWO_WAY.search(s.get("name", "").strip())
                                                                      or s.get("name", "").strip() in league.DRAFTKINGS_LADDERS
                                                                      or _field(s.get("name", "").strip()) in fields)]
        # a few at a time: DraftKings refuses a burst
        with ThreadPoolExecutor(max_workers=3) as pool:
            mapped = league.DRAFTKINGS_STATS.keys() | league.DRAFTKINGS_LADDERS.keys()
            pages = pool.map(lambda sub: get(f"/categories/{sub[0]}/subcategories/{sub[1]}")
                             if sub[2] in mapped or _field(sub[2]) in fields else None, subcategories)
            return {"subcategories": [{"name": name, **(_compact(page) if page else {"selections": []})}
                                      for (_, _, name), page in zip(subcategories, pages)]}


def _prop(player: str, stat: str, line: float, over: float, under: float | None, league) -> dict:
    prop = {"player": _TEAM_TAG.sub("", player), "stat": stat, "line": line,
            "over": round(float(over), 3), "under": None if under is None else round(float(under), 3)}
    tag = _TEAM_TAG.search(player)
    if tag:
        prop["team"] = league.TEAM_ALIASES.get(tag.group(1), tag.group(1))
    return prop


def parse(payload: dict, league) -> tuple[list[dict], Counter]:
    """The league's two-sided player props at DraftKings' main line and every rung of the ladders
    and fields it maps (one-way, "under": None), plus a count of two-way subcategories and field
    markets we don't map (by name), so new ones show up in the build."""
    fields = getattr(league, "DRAFTKINGS_FIELDS", {})
    props, skipped = [], Counter()
    for sub in payload.get("subcategories", []):
        if _field(sub["name"]) in fields:   # a market per count, a runner per player: each the over at count - 0.5
            for s in sub["selections"]:
                stat = fields.get(_field(s["name"]))
                if _field(s["name"]) not in fields:
                    skipped[s["name"]] += 1
                elif stat and s["player"] and s["odds"]:   # None: a market read past quietly ("First TD Scorer")
                    count = _COUNT.search(s["name"])
                    props.append(_prop(s["player"], stat, (int(count.group()) if count else 1) - 0.5,
                                       s["odds"], None, league))
            continue
        ladder = league.DRAFTKINGS_LADDERS.get(sub["name"])
        if ladder:
            props += [_prop(s["player"] or re.sub(rf"\s+{re.escape(sub['name'])}$", "", s["name"]), ladder,
                            s["milestone"] - 0.5, s["odds"], None, league)
                      for s in sub["selections"] if s.get("milestone") is not None and s["odds"]]
            continue
        stat = league.DRAFTKINGS_STATS.get(sub["name"])
        if not stat:
            skipped[sub["name"]] += 1
            continue
        markets: dict = {}
        for s in sub["selections"]:
            if s["side"] in ("over", "under") and s["line"] is not None and s["odds"]:
                markets.setdefault((s["market"], s["line"]), {})[s["side"]] = s
        for (_, line), sides in markets.items():
            over, under = sides.get("over"), sides.get("under")
            if not over or not under or not (over["main"] or under["main"]):
                continue   # an alternate line; the main one is the book's real opinion
            player = over["player"] or re.sub(rf"\s+{re.escape(sub['name'])}$", "", over["name"])
            props.append(_prop(player, stat, float(line), over["odds"], under["odds"], league))
    return props, skipped
