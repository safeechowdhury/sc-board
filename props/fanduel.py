"""FanDuel player props: a second sportsbook price next to Pinnacle's.

FanDuel's site reads its odds from sbapi.<state>.sportsbook.fanduel.com with a
public key carried in the page, and answers a plain request. Like Pinnacle it
refuses a request from another site's page, so these refresh on a build only.
A league page lists the games; each game's prop tabs are a request apiece.

Over/under markets ("Gerrit Cole - Strikeouts": Over 5.5 / Under 5.5) give a
fair price. FanDuel posts most batter and NHL point props only as one-way
ladders, an over with no under: "To Record 2+ Hits" lists each player's price
for 2 or more, which is the over at 1.5, and "Zay Flowers - Alt Receptions"
lists one player's price at each rung ("Zay Flowers 4+ Receptions"). Those are
kept as an over without an under: no fair price, but still what FanDuel thinks.
"""
import datetime as dt
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

KEY, NAME, SHORT = "fanduel", "FanDuel", "FanDuel"
SHARP = True   # its prices count toward the fair chance
TARGET = False

STATE = os.environ.get("FANDUEL_STATE", "nj")
URL = f"https://sbapi.{STATE}.sportsbook.fanduel.com/api"
KEY_PARAM = {"_ak": "FhMFpcPWXMeyZxOx"}   # the public key FanDuel's own pages send
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# "Gerrit Cole Over", "Tarik Skubal Over 17.5", "Juraj Slafkovsky - Over"
_SIDE = re.compile(r"^(?P<player>.+?)\s*-?\s+(?P<side>Over|Under)(?:\s+(?P<line>\d+(?:\.\d+)?))?$")
# One-way ladders whose market names the threshold, with a runner per player:
# "To Record 2+ Hits", "Player to Score 2+ Goals", "1+ Points", "To Record A Hit", "Anytime Goal Scorer",
# "First Goal Scorer"
_EVERY_PLAYER = [
    re.compile(r"^(?:Player )?(?:To (?:Record|Hit|Score) )?(?P<n>\d+)\+ (?P<stat>.+)$", re.I),
    re.compile(r"^(?:Player )?To (?:Record|Hit|Score) (?P<n>An?) (?P<stat>.+)$", re.I),
    re.compile(r"^(?P<n>Any ?Time) (?P<stat>.+) Scorer$", re.I),
    re.compile(r"^(?P<stat>(?P<n>First) Goal) Scorer$", re.I),
]
# ...and one player's alternate lines: "Zay Flowers - Alt Receptions" with "Zay Flowers 4+ Receptions",
# "Dan Vladar Alt Saves" with "27+"; never a period's ("1st Period Dan Vladar Alt Saves")
_ALT = re.compile(r"^(?!\d\w* Period )(?P<player>.+?)(?: -)? Alt (?P<stat>.+)$")
_RUNG = re.compile(r"^(?:(?P<player>.+?) )?(?P<n>\d+)\+")


def _get(http: httpx.Client, path: str, params: dict) -> dict:
    r = http.get(f"{URL}/{path}", params={**KEY_PARAM, **params})
    r.raise_for_status()
    return r.json()


def _compact(market: dict) -> dict:
    return {"name": market.get("marketName", ""), "status": market.get("marketStatus"), "runners": [
        {"name": r.get("runnerName", ""), "handicap": r.get("handicap"), "status": r.get("runnerStatus"),
         "odds": ((r.get("winRunnerOdds") or {}).get("trueOdds") or {}).get("decimalOdds", {}).get("decimalOdds")}
        for r in market.get("runners", [])]}


def _wanted(market: dict) -> bool:
    name = market.get("marketName", "")
    return len(market.get("runners", [])) == 2 or bool(_ALT.match(name)) or any(p.match(name) for p in _EVERY_PLAYER)


def fetch(league, now: dt.datetime | None = None) -> dict:
    """The over/under and one-way markets on the league's prop tabs for every game starting within its slate."""
    now = now or dt.datetime.now(dt.timezone.utc)
    until = now + dt.timedelta(days=league.SLATE_DAYS + 1)
    with httpx.Client(timeout=60, headers=HEADERS, transport=httpx.HTTPTransport(retries=3)) as http:
        page = _get(http, "content-managed-page",
                    {"page": "CUSTOM", "customPageId": league.FANDUEL_PAGE, "timezone": "America/New_York"})
        events = [e["eventId"] for e in page.get("attachments", {}).get("events", {}).values()
                  if " @ " in e.get("name", "")
                  and now <= dt.datetime.fromisoformat(e["openDate"].replace("Z", "+00:00")) <= until]
        jobs = [(event, tab) for event in events for tab in league.FANDUEL_TABS]
        with ThreadPoolExecutor(max_workers=6) as pool:
            pages = pool.map(lambda job: _get(http, "event-page", {"eventId": job[0], "tab": job[1]}), jobs)
            markets = {mid: _compact(m) for p in pages
                       for mid, m in p.get("attachments", {}).get("markets", {}).items()
                       if _wanted(m)}
    return {"markets": list(markets.values())}


def _one_way(market: dict, ladders: dict) -> list[dict]:
    """A ladder's rungs as overs with no under, or [] when the market isn't a ladder we map."""
    alt = _ALT.match(market["name"])
    if alt:
        stat = ladders.get(alt["stat"].lower())
        rungs = [(m["player"] or alt["player"], int(m["n"]), r) for r in market["runners"]
                 for m in [_RUNG.match(r["name"])] if m]
    else:
        m = next((m for p in _EVERY_PLAYER for m in [p.match(market["name"])] if m), None)
        if not m:
            return []
        stat = ladders.get(m["stat"].lower())
        n = int(m["n"]) if m["n"].isdigit() else 1   # "A Hit", "Anytime Goal Scorer", "First Goal Scorer": one
        rungs = [(r["name"], n, r) for r in market["runners"]]
    if not stat:
        return []
    return [{"player": player, "stat": stat, "line": n - 0.5, "over": round(float(r["odds"]), 3), "under": None}
            for player, n, r in rungs if r["status"] == "ACTIVE" and r["odds"]]


def parse(payload: dict, league) -> tuple[list[dict], Counter]:
    """The league's player props, plus a count of the over/under stats we don't track (by FanDuel's name).
    A one-way price has "under": None."""
    stats = {name.lower(): stat for name, stat in league.FANDUEL_STATS.items()}
    ladders = {name.lower(): stat for name, stat in league.FANDUEL_LADDERS.items()}
    props, skipped = [], Counter()
    for market in payload.get("markets", []):
        if market.get("status", "OPEN") != "OPEN":   # suspended while the line moves
            continue
        if len(market["runners"]) != 2 or _ALT.match(market["name"]):
            props += _one_way(market, ladders)
            continue
        sides = {}
        for runner in market["runners"]:
            m = _SIDE.match(runner["name"])
            if not m or runner["status"] != "ACTIVE" or not runner["odds"]:
                break
            line = float(m["line"]) if m["line"] else runner["handicap"]
            sides[m["side"].lower()] = (m["player"], line, round(float(runner["odds"]), 3))
        if set(sides) != {"over", "under"}:
            props += _one_way(market, ladders)   # a ladder with two players on it
            continue
        (player, line, over), (other, under_line, under) = sides["over"], sides["under"]
        if player != other or line != under_line or not line:
            continue
        name = market["name"].replace(player, "", 1).strip(" -")   # "Gerrit Cole - Strikeouts" -> "Strikeouts"
        stat = stats.get(name.lower())
        if not stat:
            skipped[name] += 1
            continue
        props.append({"player": player, "stat": stat, "line": float(line), "over": over, "under": under})
    return props, skipped
