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
Soccer's are all ladders of that kind ("Player To Have 2 Or More Shots On Target",
"Anytime Goalscorer", "To Score Or Assist"), and its games ("Roma v Real Madrid") come from
the sport's page, each competition picked out by its id (FANDUEL_COMPETITION).
"""
import datetime as dt
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import matchups, net
from .icons import icon

KEY, NAME, SHORT = "fanduel", "FanDuel", "FanDuel"
WEIGHT = 1     # its share of the fair chance (books.py)
ICON = icon("fanduel.png")   # shown in place of its name in the column heading
TARGET = True   # a site to find EV on: measured against a fair it is left out of (template.html)
# a request per game and tab: the board asks only for its games' (site.build passes `until`); the
# lines page asks for every game's first tab itself
BOARD_GAMES_ONLY = True
PER_GAME = True   # costs a request per game: an odds board asks it only for the next two days' games
LEAGUE_ATTR = "FANDUEL_PAGE"

STATE = os.environ.get("FANDUEL_STATE", "nj")
URL = f"https://sbapi.{STATE}.sportsbook.fanduel.com/api"
KEY_PARAM = {"_ak": "FhMFpcPWXMeyZxOx"}   # the public key FanDuel's own pages send
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# "Gerrit Cole Over", "Tarik Skubal Over 17.5", "Juraj Slafkovsky - Over"
_SIDE = re.compile(r"^(?P<player>.+?)\s*-?\s+(?P<side>Over|Under)(?:\s+(?P<line>\d+(?:\.\d+)?))?$")
# One-way ladders whose market names the threshold, with a runner per player:
# "To Record 2+ Hits", "Player to Score 2+ Goals", "1+ Points", "To Record A Hit", "Anytime Goal Scorer",
# "First Goal Scorer"
# and soccer's: "Player To Have 2 Or More Shots On Target", "To Score 2 or More Goals",
# "Anytime Goalscorer", "Anytime Assist", "To Score Or Assist"
_EVERY_PLAYER = [
    re.compile(r"^(?:Player )?(?:To (?:Record|Hit|Score) )?(?P<n>\d+)\+ (?P<stat>.+)$", re.I),
    re.compile(r"^(?:Player )?To (?:Record|Hit|Score) (?P<n>An?) (?P<stat>.+)$", re.I),
    re.compile(r"^(?P<n>Any ?Time) (?P<stat>.+) Scorer$", re.I),
    re.compile(r"^(?P<stat>(?P<n>First) Goal) Scorer$", re.I),
    re.compile(r"^(?:Player )?To (?:Have|Score|Commit) (?P<n>\d+) Or More (?P<stat>.+)$", re.I),
    re.compile(r"^(?P<n>Any ?Time) (?P<stat>Goal|Assist)(?:scorer)?$", re.I),
    re.compile(r"^To (?P<n>)(?P<stat>Score Or Assist)$", re.I),
]
# ...and one player's alternate lines: "Zay Flowers - Alt Receptions" with "Zay Flowers 4+ Receptions",
# "Dan Vladar Alt Saves" with "27+"; never a part of the game's ("1st Period Dan Vladar Alt Saves",
# "Josh Allen - 1st Half Alt Passing Yds", "1st Qtr")
_ALT = re.compile(r"^(?!.*\b(?:Period|Half|Qtr|Quarter|Inning)\b)(?P<player>.+?)(?: -)? Alt (?P<stat>.+)$", re.I)
_RUNG = re.compile(r"^(?:(?P<player>.+?) )?(?P<n>\d+)\+")

_PERIOD_HEAD = re.compile(r"half|quarter|period|inning|\b1st\b|\b2nd\b|\b3rd\b|\b\dth\b", re.I)

def _get(http: httpx.Client, path: str, params: dict) -> dict:
    def ask():
        with net.site("fanduel"):
            r = http.get(f"{URL}/{path}", params={**KEY_PARAM, **params})
        r.raise_for_status()
        return r.json(), getattr(r, "headers", {})
    key = ("fanduel", path, tuple(sorted(params.items())))
    return net.once(key, lambda: net.fresh(key, ask))   # the league page serves both pages; kept 30 seconds


def _compact(market: dict) -> dict:
    return {"name": market.get("marketName", ""), "status": market.get("marketStatus"), "runners": [
        {"name": r.get("runnerName", ""), "handicap": r.get("handicap"), "status": r.get("runnerStatus"),
         "odds": ((r.get("winRunnerOdds") or {}).get("trueOdds") or {}).get("decimalOdds", {}).get("decimalOdds")}
        for r in market.get("runners", [])]}


def _wanted(market: dict) -> bool:
    name = market.get("marketName", "")
    return len(market.get("runners", [])) == 2 or bool(_ALT.match(name)) or any(p.match(name) for p in _EVERY_PLAYER)


def _tabs(page: dict) -> set[str]:
    """The tabs a game page lists, as the names its `tab` parameter takes ("Points/Assists" is points-assists)."""
    return {re.sub(r"[^a-z0-9]+", "-", (t.get("title") or "").lower()).strip("-")
            for t in ((page.get("layout") or {}).get("tabs") or {}).values()}


def league_events(http: httpx.Client, league) -> list[dict]:
    """The league's games from its page: a league's own (FANDUEL_PAGE names it, "nfl"), or the
    sport's (FANDUEL_PAGE is the page's parameters) with the competition's games picked out."""
    page = league.FANDUEL_PAGE
    params = {"page": "CUSTOM", "customPageId": page} if isinstance(page, str) else dict(page)
    events = _get(http, "content-managed-page", {**params, "timezone": "America/New_York"}).get("attachments", {}).get("events", {})
    competition = getattr(league, "FANDUEL_COMPETITION", None)
    return [e for e in events.values() if (" @ " in e.get("name", "") or " v " in e.get("name", ""))
            and (competition is None or e.get("competitionId") == competition)]


def fetch(league, now: dt.datetime | None = None, until: dt.datetime | None = None) -> dict:
    """The over/under and one-way markets on the league's prop tabs for every game starting from now
    until `until` (by default, the end of its slate)."""
    now = now or net.now()
    until = until or now + dt.timedelta(days=league.SLATE_DAYS + 1)
    with httpx.Client(timeout=60, headers=HEADERS, transport=httpx.HTTPTransport(retries=3)) as http:
        games = {e["eventId"]: matchups.game_of(e["openDate"], e["name"]) for e in league_events(http, league)
                 if now <= dt.datetime.fromisoformat(e["openDate"].replace("Z", "+00:00")) <= until
                 and matchups.on_board(league, e["openDate"], e["name"])}
        events = list(games)
        tab = lambda job: _get(http, "event-page", {"eventId": job[0], "tab": job[1]})
        with ThreadPoolExecutor(max_workers=6) as pool:
            # a game's first tab lists the tabs it has: the others are asked for only where they're
            # listed (one it lacks answers with just the game's moneyline, spread and total)
            firsts = list(pool.map(tab, [(event, league.FANDUEL_TABS[0]) for event in events]))
            jobs = [(event, name) for event, first in zip(events, firsts)
                    for name in league.FANDUEL_TABS[1:] if name in _tabs(first)]
            pages = firsts + list(pool.map(tab, jobs))
            markets = {mid: {**_compact(m), **({"game": games[m["eventId"]]} if games.get(m.get("eventId")) else {})}
                       for p in pages
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
    prev, game = 0, None
    for market in payload.get("markets", []):
        matchups.tag(props, prev, game)   # the last market's props are for its game
        prev, game = len(props), market.get("game")
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
        if player not in market["name"] and " - " in market["name"]:
            head, name = market["name"].split(" - ", 1)   # "S Gilgeous-Alexander - Pts + Reb + Ast": the runner has his full name
            if _PERIOD_HEAD.search(head):   # "1st Half - Points": part of a game, never a board stat
                skipped[market["name"]] += 1
                continue
        stat = stats.get(name.lower())
        if not stat:
            skipped[name] += 1
            continue
        props.append({"player": player, "stat": stat, "line": float(line), "over": over, "under": under})
    matchups.tag(props, prev, game)
    return props, skipped
