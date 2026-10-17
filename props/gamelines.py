"""Moneylines, totals and spreads for every game on the slate, book by book.

Written for `python -m props lines`, which builds `site/lines.html` — a page of
its own, not linked from the board: the board prices player props, this prices
the games themselves. Each book is a fetch/parse pair here, the way the props
books are modules: fetch returns the raw payload, parse the league's games, each
{"start", "away_name", "home_name", "ml": {...}, "totals": [...], "spreads":
[...]} (or "away" and "home", the page's team codes, for a book that has them).
Moneyline and total prices are decimal odds; a spread entry's "line" is
the home team's points, so -1.5 is the home favorite.

Betano is the featured book, the one the page measures EV on by default; Mise-o-jeu and
Stake are the other sites it can measure value on. Every book reads the same open
endpoints the props board does, none behind a login; Novig posts no game lines its API
lets us find, so it isn't here. Kalshi is: its moneyline and game-total contracts, read
the way its props are (kalshi.py). Betway, shown only, is read from the data its league
page is served with: one plain request a league, no browser.

Books price several totals and spreads (Mise lists a rung per line), so parse
keeps them all and the merge picks each book's line nearest the slate's median,
which is where the main lines cluster.
"""
import datetime as dt
import json
import math
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from . import betano, books, bovada, draftkings, fanduel, kalshi, kambi, mise, net, pinnacle, stake
from .icons import icon
from .slate import ET

TEMPLATE = Path(__file__).with_name("template_lines.html")


def american(decimal: float) -> int:
    """American odds as the books quote them, from a decimal payout."""
    return round((decimal - 1) * 100) if decimal >= 2 else round(-100 / (decimal - 1))


# Books name teams differently ("New Jersey Devils", DraftKings' "NJ Devils"), so a
# team is matched by its full name, then by the mascot phrase it ends with ("Golden
# Knights", "White Sox"), which is unique within a league. Codes are the board's own.
_TEAMS = {
    "nhl": {"anaheim ducks": "ANA", "boston bruins": "BOS", "buffalo sabres": "BUF", "calgary flames": "CGY",
            "carolina hurricanes": "CAR", "chicago blackhawks": "CHI", "colorado avalanche": "COL",
            "columbus blue jackets": "CBJ", "dallas stars": "DAL", "detroit red wings": "DET",
            "edmonton oilers": "EDM", "florida panthers": "FLA", "los angeles kings": "LAK",
            "minnesota wild": "MIN", "montreal canadiens": "MTL", "nashville predators": "NSH",
            "new jersey devils": "NJD", "new york islanders": "NYI", "new york rangers": "NYR",
            "ottawa senators": "OTT", "philadelphia flyers": "PHI", "pittsburgh penguins": "PIT",
            "san jose sharks": "SJS", "seattle kraken": "SEA", "st. louis blues": "STL", "st louis blues": "STL",
            "tampa bay lightning": "TBL", "toronto maple leafs": "TOR", "utah mammoth": "UTA",
            "utah hockey club": "UTA", "utah": "UTA", "vancouver canucks": "VAN", "vegas golden knights": "VGK",
            "vegas": "VGK", "washington capitals": "WSH", "winnipeg jets": "WPG"},
    "nfl": {"arizona cardinals": "ARI", "atlanta falcons": "ATL", "baltimore ravens": "BAL",
            "buffalo bills": "BUF", "carolina panthers": "CAR", "chicago bears": "CHI",
            "cincinnati bengals": "CIN", "cleveland browns": "CLE", "dallas cowboys": "DAL",
            "denver broncos": "DEN", "detroit lions": "DET", "green bay packers": "GB",
            "houston texans": "HOU", "indianapolis colts": "IND", "jacksonville jaguars": "JAX",
            "kansas city chiefs": "KC", "las vegas raiders": "LV", "los angeles chargers": "LAC",
            "los angeles rams": "LAR", "miami dolphins": "MIA", "minnesota vikings": "MIN",
            "new england patriots": "NE", "new orleans saints": "NO", "new york giants": "NYG",
            "new york jets": "NYJ", "philadelphia eagles": "PHI", "pittsburgh steelers": "PIT",
            "san francisco 49ers": "SF", "seattle seahawks": "SEA", "tampa bay buccaneers": "TB",
            "tennessee titans": "TEN", "washington commanders": "WAS", "washington": "WAS"},
    "nba": {"atlanta hawks": "ATL", "boston celtics": "BOS", "brooklyn nets": "BKN", "brooklyn": "BKN",
            "charlotte hornets": "CHA", "charlotte": "CHA", "chicago bulls": "CHI", "cleveland cavaliers": "CLE",
            "dallas mavericks": "DAL", "denver nuggets": "DEN", "detroit pistons": "DET",
            "golden state warriors": "GSW", "golden state": "GSW", "houston rockets": "HOU",
            "indiana pacers": "IND", "los angeles clippers": "LAC", "los angeles lakers": "LAL",
            "memphis grizzlies": "MEM", "miami heat": "MIA", "milwaukee bucks": "MIL",
            "minnesota timberwolves": "MIN", "new orleans pelicans": "NO", "new york knicks": "NYK",
            "oklahoma city thunder": "OKC", "oklahoma city": "OKC", "orlando magic": "ORL",
            "philadelphia 76ers": "PHI", "phoenix suns": "PHX", "portland trail blazers": "POR",
            "portland": "POR", "sacramento kings": "SAC", "san antonio spurs": "SA", "san antonio": "SA",
            "toronto raptors": "TOR", "utah jazz": "UTA", "washington wizards": "WAS"},
    "mlb": {"arizona diamondbacks": "ARI", "arizona": "ARI", "atlanta braves": "ATL", "baltimore orioles": "BAL",
            "boston red sox": "BOS", "chicago cubs": "CHC", "chicago white sox": "CWS", "cincinnati reds": "CIN",
            "cleveland guardians": "CLE", "colorado rockies": "COL", "detroit tigers": "DET",
            "houston astros": "HOU", "kansas city royals": "KC", "los angeles angels": "LAA",
            "los angeles dodgers": "LAD", "miami marlins": "MIA", "milwaukee brewers": "MIL",
            "minnesota twins": "MIN", "new york mets": "NYM", "new york yankees": "NYY",
            "athletics": "ATH", "oakland athletics": "ATH", "philadelphia phillies": "PHI",
            "pittsburgh pirates": "PIT", "san diego padres": "SD", "san francisco giants": "SF",
            "seattle mariners": "SEA", "st. louis cardinals": "STL", "st louis cardinals": "STL",
            "tampa bay rays": "TB", "texas rangers": "TEX", "toronto blue jays": "TOR",
            "washington nationals": "WSH"},
}
# the phrases a team's name ends with ("golden knights", "white sox"), kept only where they
# name one team in the league, longest first, so "golden knights" beats "knights"
_MASCOTS = {}
for _league, _table in _TEAMS.items():
    _phrases: dict = {}
    for _name, _code in _table.items():
        _words = _name.split()
        for _n in (1, 2):
            if len(_words) > _n:
                _phrases.setdefault(" ".join(_words[-_n:]), set()).add(_code)
    _MASCOTS[_league] = sorted(((phrase, codes.pop()) for phrase, codes in _phrases.items() if len(codes) == 1),
                               key=lambda pair: -len(pair[0]))


_PITCHER = re.compile(r"\s*\([A-Z]\.? [A-Z][\w'.-]+(?: [\w'.-]+)*\)\s*$")


def team_code(league, name: str | None) -> str | None:
    table = _TEAMS[league.KEY]
    # FanDuel names MLB's starters: "Milwaukee Brewers (R Gasser)"; not "(HYPER)", a special event
    name = _PITCHER.sub("", name or "")
    key = re.sub(r"\s+", " ", re.sub(r"[.']", "", name.lower())).strip()
    if key in table:
        return table[key]
    for phrase, code in _MASCOTS[league.KEY]:
        if key.endswith(" " + phrase):
            return code
    return None


def game_date(start: str) -> str:
    """The Eastern date a game is on, the way every book's board groups them."""
    begins = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
    return str(begins.astimezone(ET).date())


def _window(now, league) -> tuple[dt.datetime, dt.datetime]:
    return now, now + dt.timedelta(days=league.SLATE_DAYS + 1)


# --- Pinnacle: the sharp lines, from the same payload the props module reads ---

def fetch_pinnacle(league, now=None):
    return pinnacle.fetch(league)


def parse_pinnacle(payload, league, now=None):
    now = now or net.now()
    start, until = _window(now, league)
    markets = {}
    for m in payload.get("markets", []):
        if m.get("period") == 0 and not m.get("isAlternate") and m.get("status") == "open":
            markets.setdefault(m["matchupId"], []).append(m)
    out = []
    for matchup in payload.get("matchups", []):
        if matchup.get("type") != "matchup" or not matchup.get("startTime"):
            continue
        begins = dt.datetime.fromisoformat(matchup["startTime"].replace("Z", "+00:00"))
        if not start <= begins <= until:
            continue
        sides = {p["alignment"]: p["name"] for p in matchup.get("participants", [])}
        if not {"home", "away"} <= set(sides):
            continue
        game = {"start": matchup["startTime"], "away_name": sides["away"], "home_name": sides["home"],
                "ml": {}, "totals": [], "spreads": []}
        for m in markets.get(matchup["id"], []):
            prices = {p["designation"]: pinnacle.decimal(p["price"]) for p in m.get("prices", [])}
            if m["type"] == "moneyline" and {"home", "away"} <= set(prices):
                game["ml"] = {"home": prices["home"], "away": prices["away"]}
            elif m["type"] == "total" and {"over", "under"} <= set(prices):
                game["totals"].append({"line": m["prices"][0]["points"], "over": prices["over"], "under": prices["under"]})
            elif m["type"] == "spread" and {"home", "away"} <= set(prices):
                points = {p["designation"]: p["points"] for p in m.get("prices", [])}
                if points.get("home") is not None:
                    game["spreads"].append({"line": points["home"], "home": prices["home"], "away": prices["away"]})
        out.append(game)
    return out


# --- FanDuel: the main markets on a game's first prop tab ---

def fetch_fanduel(league, now=None):
    now = now or net.now()
    start, until = _window(now, league)
    with httpx.Client(timeout=60, headers=fanduel.HEADERS, transport=httpx.HTTPTransport(retries=3)) as http:
        page = fanduel._get(http, "content-managed-page",
                            {"page": "CUSTOM", "customPageId": league.FANDUEL_PAGE, "timezone": "America/New_York"})
        events = [e for e in page.get("attachments", {}).get("events", {}).values()
                  if " @ " in e.get("name", "") and start <= dt.datetime.fromisoformat(
                      e["openDate"].replace("Z", "+00:00")) <= until]

        # a game's first prop tab carries its moneyline, spread and total too, and is the page the
        # props fetch asks for (so it's asked once a run); the bare game page adds only quarter
        # markets and, for baseball, two-way props that read as totals at 0
        def game(event):
            return fanduel._get(http, "event-page", {"eventId": event["eventId"], "tab": league.FANDUEL_TABS[0]})

        with ThreadPoolExecutor(max_workers=6) as pool:
            pages = pool.map(game, events)
    return {"pages": [page.get("attachments", {}) for page in pages]}


_TOTAL_NAME = re.compile(r"^Total (Goals|Points|Runs)$", re.I)


def parse_fanduel(payload, league, now=None):
    out = []
    for attachments in payload.get("pages", []):
        event = next(iter(attachments.get("events", {}).values()), {})
        name = event.get("name", "")
        if " @ " not in name:
            continue
        away_name, home_name = name.split(" @ ", 1)
        game = {"start": event.get("openDate"), "away_name": away_name, "home_name": home_name,
                "ml": {}, "totals": [], "spreads": []}
        for m in attachments.get("markets", {}).values():
            if m.get("marketStatus") != "OPEN":
                continue
            runners = [r for r in m.get("runners", []) if r.get("runnerStatus") == "ACTIVE" and r.get("winRunnerOdds")]
            decimal = lambda r: round(float(r["winRunnerOdds"]["trueOdds"]["decimalOdds"]["decimalOdds"]), 3)
            kind, label = m.get("marketType", ""), m.get("marketName", "")
            if kind == "MONEY_LINE":
                for r in runners:
                    side = ((r.get("result") or {}).get("type") or "").lower()
                    if side in ("home", "away"):
                        game["ml"][side] = decimal(r)
            elif "OVER/UNDER" in kind or _TOTAL_NAME.match(label):
                over = next((r for r in runners if r["runnerName"] == "Over"), None)
                under = next((r for r in runners if r["runnerName"] == "Under"), None)
                if over and under and over.get("handicap") is not None and over["handicap"] == under.get("handicap"):
                    game["totals"].append({"line": float(over["handicap"]),
                                           "over": decimal(over), "under": decimal(under)})
            elif kind == "SPREAD" or label in ("Point Spread", "Spread", "Puck Line", "Run Line"):
                sides = {((r.get("result") or {}).get("type") or "").lower(): r for r in runners}
                if {"home", "away"} <= set(sides) and sides["home"].get("handicap") is not None:
                    game["spreads"].append({"line": float(sides["home"]["handicap"]),
                                            "home": decimal(sides["home"]), "away": decimal(sides["away"])})
        out.append(game)
    return out


# --- DraftKings: the league's page, which carries its "Game Lines" category's "Game" markets ---

def fetch_draftkings(league, now=None):
    """The league's page: it carries every game's moneyline, spread and total (its Game Lines' Game
    subcategory), and the props fetch reads it too, so it's asked once a run."""
    with draftkings._client() as http:
        return {"data": draftkings._get(http, league, "")}


def parse_draftkings(payload, league, now=None):
    data = payload.get("data") or {}
    upcoming = {e["id"]: e for e in data.get("events", []) if e.get("status") == "NOT_STARTED"}
    markets = {m["id"]: m for m in data.get("markets", []) if m.get("eventId") in upcoming}
    by_market: dict = {}
    for s in data.get("selections", []):
        if s.get("marketId") in markets and s.get("trueOdds"):
            by_market.setdefault(s["marketId"], []).append(s)
    out = {}
    for market_id, selections in by_market.items():
        market = markets[market_id]
        event = upcoming[market["eventId"]]
        game = out.setdefault(market["eventId"], {
            "start": event["startEventDate"],
            "away_name": next((p["name"] for p in event.get("participants", []) if p.get("venueRole") == "Away"), None),
            "home_name": next((p["name"] for p in event.get("participants", []) if p.get("venueRole") == "Home"), None),
            "ml": {}, "totals": [], "spreads": []})
        label = market.get("name", "").lower()
        sides = {s["outcomeType"].lower(): s for s in selections if s.get("outcomeType")}
        if "money" in label:
            for side in ("home", "away"):
                if side in sides:
                    game["ml"][side] = round(float(sides[side]["trueOdds"]), 3)
        elif "total" in label:
            lines: dict = {}
            for s in selections:
                if s.get("outcomeType") in ("Over", "Under") and s.get("points") is not None:
                    lines.setdefault(float(s["points"]), {})[s["outcomeType"].lower()] = round(float(s["trueOdds"]), 3)
            game["totals"] = [{"line": line, "over": pair.get("over"), "under": pair.get("under")}
                              for line, pair in sorted(lines.items()) if len(pair) == 2]
        elif "spread" in label or "line" in label:
            for s in selections:
                if s.get("outcomeType") == "Home" and s.get("points") is not None:
                    game["spreads"].append({"line": float(s["points"]),
                                            "home": round(float(s["trueOdds"]), 3),
                                            "away": round(float(sides["away"]["trueOdds"]), 3) if "away" in sides else None})
    return list(out.values())


# --- Kambi: the game offers, from the same event pages the props module reads ---

_KAMBI_MAIN = re.compile(r"including overtime|including extra", re.I)
_KAMBI_PART = re.compile(r"period|quarter|half|inning|regular time|\d:\d\d| by ", re.I)
_KAMBI_TOTAL = re.compile(r"^Total (Goals|Points|Runs)\b", re.I)
_KAMBI_SPREAD = re.compile(r"^(Puck Line|Handicap|Point Spread|Run Line)\b", re.I)
_KAMBI_ML = re.compile(r"^(Moneyline|Match Result|Match Odds)\b", re.I)


def fetch_kambi(league, now=None):
    now = now or net.now()
    start, until = _window(now, league)
    with httpx.Client(timeout=60, headers=kambi.HEADERS, transport=httpx.HTTPTransport(retries=3)) as http:
        listed = kambi._get(http, f"listView/{league.KAMBI_PATH}.json").get("events", [])
        games = [e["event"]["id"] for e in listed if e["event"].get("state") == "NOT_STARTED"
                 and start <= dt.datetime.fromisoformat(e["event"]["start"].replace("Z", "+00:00")) <= until]
        # the props fetch asks for the same games' offers: each game is asked for once a run
        found = kambi.offers(http, games)
        return {"events": [{"id": e["event"]["id"], "home": e["event"].get("homeName"),
                            "away": e["event"].get("awayName"), "start": e["event"]["start"]}
                           for e in listed if e["event"]["id"] in games],
                "offers": found}


def parse_kambi(payload, league, now=None):
    events = {e["id"]: e for e in payload.get("events", [])}
    games = {eid: {"start": e["start"], "away_name": e["away"], "home_name": e["home"],
                   "ml": {}, "totals": [], "spreads": []} for eid, e in events.items()}
    for offer in payload.get("offers", []):
        event = events.get(offer.get("eventId"))
        if not event:
            continue
        game = games[event["id"]]
        label = offer["criterion"]["englishLabel"]
        outcomes = [o for o in offer.get("outcomes", []) if o.get("status") == "OPEN" and o.get("odds")]
        by_type = {o["type"]: o for o in outcomes}
        main = bool(_KAMBI_MAIN.search(label))
        if _KAMBI_ML.match(label) and len(outcomes) == 2 and {"OT_ONE", "OT_TWO"} <= set(by_type):
            if main or not game["ml"]:
                game["ml"] = {"home": round(by_type["OT_ONE"]["odds"] / 1000, 3),
                              "away": round(by_type["OT_TWO"]["odds"] / 1000, 3)}
        elif _KAMBI_TOTAL.match(label) and not _KAMBI_PART.search(label):
            over, under = by_type.get("OT_OVER"), by_type.get("OT_UNDER")
            if over and under and over.get("line") is not None and over["line"] == under.get("line"):
                # every rung carries the same "main" name; the merge picks the one nearest the slate's line
                game["totals"] = game["totals"] + [{"line": over["line"] / 1000,
                                                    "over": round(over["odds"] / 1000, 3),
                                                    "under": round(under["odds"] / 1000, 3)}]
        elif _KAMBI_SPREAD.match(label) and not _KAMBI_PART.search(label):
            one, two = by_type.get("OT_ONE"), by_type.get("OT_TWO")
            if one and two and one.get("line") is not None and one["line"] == -two.get("line"):
                game["spreads"] = game["spreads"] + [{"line": one["line"] / 1000,
                                                      "home": round(one["odds"] / 1000, 3),
                                                      "away": round(two["odds"] / 1000, 3)}]
    for game in games.values():
        game["totals"] = list({total["line"]: total for total in game["totals"]}.values())
        game["spreads"] = list({spread["line"]: spread for spread in game["spreads"]}.values())
    return list(games.values())


# --- Mise-o-jeu: MONEY_LINE and the game total, a rung per line ---

_MISE_UNIT = {"nhl": "GOALS", "nfl": "POINTS", "mlb": "RUNS", "nba": "POINTS"}


def fetch_mise(league, now=None):
    now = now or net.now()
    start, until = _window(now, league)
    stamp = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
    with httpx.Client(timeout=60, headers=mise.HEADERS, transport=httpx.HTTPTransport(retries=3)) as http:
        events = mise._get(http, "event-list", {"startTimeFrom": stamp(start), "startTimeTo": stamp(until),
                                                "maxEvents": 500, "maxMarkets": 0,
                                                "drilldownTagIds": league.MISE_LEAGUE}).get("events") or []
        ids = [str(e["id"]) for e in events if not e.get("started")]
        # the props fetch asks for the same games: each is asked for once a run
        return {"events": [mise.events(http, ids)]}


def parse_mise(payload, league, now=None):
    total_code = f"TOTAL_{_MISE_UNIT[league.KEY]}_OVER/UNDER"
    out = []
    for page in payload.get("events") or []:
        for event in page:
            if event.get("started") or " at " not in (event.get("name") or ""):
                continue
            away_name, home_name = event["name"].split(" at ", 1)
            game = {"start": event.get("startTime"), "away_name": away_name, "home_name": home_name,
                    "ml": {}, "totals": [], "spreads": []}
            for m in event.get("markets") or []:
                if m.get("status") != "ACTIVE":
                    continue
                sides = {o["name"].casefold(): next((p.get("decimal") for p in o.get("prices") or []), None)
                         for o in m.get("outcomes") or []}
                if m.get("groupCode") == "MONEY_LINE" and len(sides) == 2 and None not in sides.values():
                    away = sides.get(away_name.casefold()) or sides.get(away_name.split()[-1].casefold())
                    home = sides.get(home_name.casefold()) or sides.get(home_name.split()[-1].casefold())
                    if away and home:
                        game["ml"] = {"away": round(float(away), 3), "home": round(float(home), 3)}
                elif m.get("groupCode") == total_code and m.get("handicapValue") is not None \
                        and sides.get("over") and sides.get("under"):
                    game["totals"].append({"line": float(m["handicapValue"]),
                                           "over": round(float(sides["over"]), 3),
                                           "under": round(float(sides["under"]), 3)})
            out.append(game)
    return out


# --- Kalshi: a contract per team to win, and a rung per total ---

def kalshi_team(league, code: str) -> str | None:
    """The lines page's code for a Kalshi team code: as it is ("VGK"), as the board's aliases have
    it (Kalshi's "TB" is the board's "TBL"), or the other way round (Kalshi's "NOP" is the page's "NO")."""
    aliases = getattr(league, "TEAM_ALIASES", {})
    known = set(_TEAMS[league.KEY].values())
    tries = [code, aliases.get(code, code)] + [theirs for theirs, ours in aliases.items() if ours == code]
    return next((c for c in tries if c in known), None)


def fetch_kalshi(league, now=None):
    """The league's games (each with its moneyline contracts) and its game-total ladders, both asked
    once a run: the props fetch reads the same games."""
    now = now or net.now()
    start, until = _window(now, league)
    with net.shared("kalshi", kalshi._client) as http:
        slate = kalshi.upcoming(kalshi.games(http, league), start, until)
        ours = {g["game"] for g in slate}
        totals = [m for m in kalshi._markets(http, league.KALSHI_TOTAL)
                  if m.get("event_ticker", "").split("-", 1)[-1] in ours]
    return {"games": slate, "totals": totals}


MAIN_GAP = 0.3   # a main total's over and under cost within 30 cents of each other (35/65 at most)


def parse_kalshi(payload, league, now=None):
    """A team's moneyline is the better of buying its yes and buying the other team's no; the total
    is the ladder's most even rung, its yes the over and its no the under. A pair wider than
    kalshi.MAX_WIDTH is left out."""
    totals: dict = {}
    for m in payload.get("totals", []):
        if m.get("floor_strike") is not None:
            totals.setdefault(m["event_ticker"].split("-", 1)[1], []).append(m)
    out = []
    for g in payload.get("games", []):
        sides = {}
        for m in g["markets"]:
            side = "home" if kalshi.team_of(m) == g["home_id"] else "away" if kalshi.team_of(m) == g["away_id"] else None
            if side:
                sides[side] = m
        if set(sides) != {"home", "away"}:
            continue
        away, home = (kalshi_team(league, kalshi.code_of(sides[s])) for s in ("away", "home"))
        if not away or not home:
            continue
        game = {"start": g["start"], "away": away, "home": home, "ml": {}, "totals": [], "spreads": []}
        cost = {s: [kalshi.chance(m.get("yes_ask_dollars")), kalshi.chance(m.get("no_ask_dollars"))]
                for s, m in sides.items()}
        win = {}
        for side, other in (("home", "away"), ("away", "home")):
            # a team wins: its yes, or the other team's no; buy whichever is cheaper
            offers = [c for c in (cost[side][0], cost[other][1]) if c]
            win[side] = min(offers) if offers else None
        if kalshi.tight(win["home"], win["away"]):
            game["ml"] = {side: round(1 / win[side], 3) for side in ("away", "home")}
        # a ladder: its main line is the most even tight rung, and only if that is near a coin flip; a
        # lopsided rung left alone (the rest too wide to read) would pass for the game's total
        rungs = [(abs(yes - no), float(m["floor_strike"]), yes, no) for m in totals.get(g["game"], [])
                 for yes, no in [(kalshi.chance(m.get("yes_ask_dollars")), kalshi.chance(m.get("no_ask_dollars")))]
                 if kalshi.tight(yes, no)]
        if rungs:
            gap, line, yes, no = min(rungs)
            if gap <= MAIN_GAP:
                game["totals"] = [{"line": line, "over": round(1 / yes, 3), "under": round(1 / no, 3)}]
        out.append(game)
    return out


# --- Bovada: the game page's own markets ---

def fetch_bovada(league, now=None):
    """The league's listing, which carries every game's moneyline, spread and total (its Game Lines),
    so no game's own page is asked for; the props fetch reads the same listing, once a run."""
    now = now or net.now()
    start, until = _window(now, league)
    with httpx.Client(timeout=60, headers=bovada.HEADERS, follow_redirects=True,
                      transport=httpx.HTTPTransport(retries=3)) as http:
        games = bovada.listing(http, league)
    return {"pages": [{"name": e.get("description"), "start": e.get("startTime"), "page": [{"events": [e]}]}
                      for e in games if start.timestamp() * 1000 < e["startTime"] < until.timestamp() * 1000]}


def parse_bovada(payload, league, now=None):
    out = []
    for entry in payload.get("pages", []):
        name = entry.get("name", "")
        if " @ " not in name:
            continue
        away_name, home_name = name.split(" @ ", 1)
        game = {"start": dt.datetime.fromtimestamp(entry["start"] / 1000, dt.timezone.utc)
                .isoformat().replace("+00:00", "Z"),
                "away_name": away_name, "home_name": home_name, "ml": {}, "totals": [], "spreads": []}
        for block in entry.get("page", []):
            for event in block.get("events", []):
                for group in event.get("displayGroups", []):
                    for m in group.get("markets", []):
                        if m.get("status") != "O" or (m.get("period") or {}).get("description") != "Game":
                            continue
                        runners = [o for o in m.get("outcomes", []) if o.get("status") == "O" and o.get("price")]
                        price = lambda o: bovada._odds({"odds": o["price"].get("decimal")})
                        label = m.get("description")
                        if label == "Moneyline" and len(runners) == 2:
                            by_name = {o["description"]: price(o) for o in runners}
                            if None not in by_name.values() and away_name in by_name:
                                game["ml"] = {"away": by_name[away_name], "home": by_name[home_name]}
                        elif label == "Total" and m.get("key") == "2W-OU":
                            sides = {o["description"].casefold(): o for o in runners}
                            over, under = sides.get("over"), sides.get("under")
                            if over and under and over["price"].get("handicap") == under["price"].get("handicap"):
                                game["totals"].append({"line": float(over["price"]["handicap"]),
                                                       "over": price(over), "under": price(under)})
                        elif label in ("Point Spread", "Puck Line", "Runline") and len(runners) == 2:
                            sides = {o["description"]: o for o in runners}
                            if home_name in sides and away_name in sides \
                                    and sides[home_name]["price"].get("handicap") is not None:
                                game["spreads"].append({"line": float(sides[home_name]["price"]["handicap"]),
                                                        "home": price(sides[home_name]), "away": price(sides[away_name])})
        out.append(game)
    return out


# --- Betano: the main markets its league page embeds for every game ---

def fetch_betano(league, now=None):
    """The league page the props fetch reads too (so one request a run): it carries each game's
    Winner, Spread and Points markets, the same prices its game pages show."""
    return {"events": betano.league_events(league)}


def parse_betano(payload, league, now=None):
    """Each game's Winner (the moneyline), Spread and Points/Goals/Runs (the total) markets: a selection
    names its team (teamId) or its side ("Over 40.5") and carries a decimal price and its line."""
    out = []
    for event in payload.get("events", []):
        name = event.get("name") or ""
        if " @ " not in name or not isinstance(event.get("startTime"), (int, float)):
            continue
        away_name, home_name = name.split(" @ ", 1)
        game = {"start": dt.datetime.fromtimestamp(event["startTime"] / 1000, dt.timezone.utc)
                .isoformat().replace("+00:00", "Z"),
                "away_name": away_name, "home_name": home_name, "ml": {}, "totals": [], "spreads": []}
        teams = {p.get("id"): p.get("name") for p in event.get("participants") or []}
        spread, sides = {}, {}
        for market in event.get("markets") or []:
            if market.get("suspended"):
                continue
            kind = (market.get("name") or "").lower()
            for s in market.get("selections") or []:
                if not s.get("price"):
                    continue
                team, line = teams.get(s.get("teamId")), float(s.get("handicap") or 0)
                side = "away" if team == away_name else "home" if team == home_name else None
                if kind == "winner" and side:
                    game["ml"][side] = float(s["price"])
                elif kind == "spread" and side:
                    spread[side], spread[side + "_line"] = float(s["price"]), line
                elif kind in ("points", "goals", "runs"):   # the total: NFL/NBA, NHL, MLB
                    who = (s.get("name") or "").lower()
                    if who.startswith(("over", "under")):
                        sides.setdefault(line, {})[who[:5].strip()] = float(s["price"])
        game["totals"] = [{"line": line, "over": two.get("over"), "under": two.get("under")}
                          for line, two in sides.items()]
        if spread.get("away") and spread.get("home"):
            game["spreads"] = [{"line": spread.get("home_line", -spread.get("away_line", 0)),
                                "home": spread["home"], "away": spread["away"]}]
        if game["ml"] or game["totals"]:   # a game without a spread still has its moneyline and total
            out.append(game)
    return out



# --- Betway: the game data its league page is served with ---

BETWAY_URL = "https://betway.com/g/en-ca/sports/grp/"
BETWAY_PATHS = {
    "nhl": "ice-hockey/north-america/nhl",
    "nfl": "american-football/usa/nfl",
    "nba": "basketball/usa/nba",
    "mlb": "baseball/usa/mlb",
}
BETWAY_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                                "(KHTML, like Gecko) Chrome/146.0 Safari/537.36"}
BETWAY_ICON = icon("betway.png")
_BETWAY_FLIGHT = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')   # the page's Next.js data
_BETWAY_RECORD = re.compile(r'"(\d+)":\{"id":(\d+),')   # an event, market or outcome, keyed by its id
_BETWAY_KINDS = {"to-win": "ml", "handicap": "spread", "handicap-goals-over": "total"}


def _betway_records(html: str) -> dict:
    """The events, markets and outcomes in the page's data, each by id. The page shows its prices
    from a socket, but the server sends them with the page: every outcome's decimal price and line."""
    flight = "".join(json.loads(f'"{chunk}"') for chunk in _BETWAY_FLIGHT.findall(html))
    found = {"events": {}, "markets": {}, "outcomes": {}}
    decoder = json.JSONDecoder()
    for m in _BETWAY_RECORD.finditer(flight):
        if m.group(1) != m.group(2):
            continue
        try:
            record, _ = decoder.raw_decode(flight, m.end(1) + 2)
        except ValueError:
            continue
        kind = ("outcomes" if "oddsDecimal" in record else "markets" if "typeCName" in record
                else "events" if "startsAt" in record else None)
        if kind:
            found[kind][record["id"]] = record
    return found


def fetch_betway(league, now=None):
    """The league page (one request): each game yet to start in the window, with its main
    moneyline, spread and total, as {"events": [{"start", "markets": [{"type", "outcomes"}]}]}."""
    now = now or net.now()
    start, until = _window(now, league)
    with net.site("betway"):
        r = httpx.get(BETWAY_URL + BETWAY_PATHS[league.KEY], headers=BETWAY_HEADERS, timeout=60,
                      follow_redirects=True)
    r.raise_for_status()
    found = _betway_records(r.text)
    markets: dict = {}
    for market in found["markets"].values():
        kind = _BETWAY_KINDS.get(market.get("typeCName"))
        name = (market.get("name") or {}).get("default") or ""
        if not kind or " - " in name or market.get("suspended"):   # "Money Line - 8Up": a promotion's
            continue
        outcomes = [found["outcomes"].get(oid) for oid in market.get("outcomes") or []]
        outcomes = [{"side": (o.get("name") or {}).get("coupon"), "team": (o.get("name") or {}).get("default"),
                     "price": o["oddsDecimal"], "line": o.get("handicapDecimal")}
                    for o in outcomes if o and o.get("displayed") and not o.get("suspended") and o.get("oddsDecimal")]
        markets.setdefault(market.get("eventId"), []).append({"type": kind, "outcomes": outcomes})
    events = []
    for event in found["events"].values():
        begins = dt.datetime.fromisoformat(event["startsAt"].replace("Z", "+00:00"))
        if not event.get("started") and start < begins <= until and markets.get(event["id"]):
            events.append({"start": begins.strftime("%Y-%m-%dT%H:%M:%SZ"), "markets": markets[event["id"]]})
    return {"events": events}


def parse_betway(payload, league, now=None):
    """Each game's moneyline, spread and total: an outcome is the Home or Away team (named) or
    the Over or Under, and a spread's or total's carries its line."""
    out = []
    for event in payload.get("events", []):
        game = {"start": event["start"], "away_name": None, "home_name": None, "ml": {}, "totals": [], "spreads": []}
        for market in event["markets"]:
            sides = {(o["side"] or "").lower(): o for o in market["outcomes"]}
            if market["type"] in ("ml", "spread"):
                home, away = sides.get("home"), sides.get("away")
                if not home or not away:
                    continue
                game["home_name"], game["away_name"] = home["team"], away["team"]
                if market["type"] == "ml":
                    game["ml"] = {"away": away["price"], "home": home["price"]}
                elif home["line"] is not None:
                    game["spreads"].append({"line": float(home["line"]), "home": home["price"], "away": away["price"]})
            else:
                over, under = sides.get("over"), sides.get("under")
                if over and under and over["line"] is not None and over["line"] == under["line"]:
                    game["totals"].append({"line": float(over["line"]), "over": over["price"], "under": under["price"]})
        if game["home_name"] and (game["ml"] or game["totals"] or game["spreads"]):
            out.append(game)
    return out


# --- Stake: every game's main markets in the sport's listing ---

STAKE_LINES_QUERY = """query SportIndex($sport: String!, $type: SportSearchEnum = popular) {
  slugSport(sport: $sport) {
    tournamentList(type: $type, limit: 3) {
      name
      fixtureList(type: $type, limit: 50) {
        status
        data { __typename ... on SportFixtureDataMatch { startTime competitors { name } } }
        groups(groups: ["main"], status: [active]) {
          templates(limit: 3, includeEmpty: true) { name markets { status specifiers outcomes { name active odds } } }
        }
      }
    }
  }
}"""


def fetch_stake(league, now=None):
    """The sport's listing with each game's main markets, in one request: the league's tournament
    (and its preseason) of the sport's first three, its games yet to start in the window."""
    now = now or net.now()
    start, until = _window(now, league)
    answer = stake._post({"query": STAKE_LINES_QUERY, "operationName": "SportIndex",
                          "variables": {"sport": league.STAKE_SPORT, "type": "popular"}})
    if answer.get("errors"):
        raise ValueError(f"Stake: {answer['errors'][0].get('message')}")
    fixtures = []
    for tournament in ((answer.get("data") or {}).get("slugSport") or {}).get("tournamentList") or []:
        if not (tournament.get("name") or "").upper().startswith(league.KEY.upper()):
            continue
        for fixture in tournament.get("fixtureList") or []:
            data = fixture.get("data") or {}
            if fixture.get("status") == "active" and data.get("startTime") \
                    and start < parsedate_to_datetime(data["startTime"]) <= until:
                fixtures.append(fixture)
    return {"fixtures": fixtures}


def parse_stake(payload, league, now=None):
    """Each game's Winner (the moneyline), Handicap (its "hcp" the home team's) and Total markets.
    Stake names the home team first."""
    out = []
    for fixture in payload.get("fixtures", []):
        data = fixture.get("data") or {}
        teams = [c.get("name") for c in data.get("competitors") or []]
        if len(teams) != 2:
            continue
        home_name, away_name = teams
        game = {"start": parsedate_to_datetime(data["startTime"]).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "away_name": away_name, "home_name": home_name, "ml": {}, "totals": [], "spreads": []}
        for group in fixture.get("groups") or []:
            for template in group.get("templates") or []:
                kind = (template.get("name") or "").split(" ", 1)[0]
                for market in template.get("markets") or []:
                    if market.get("status") != "active":
                        continue
                    spec = dict(part.split("=", 1) for part in (market.get("specifiers") or "").split("|") if "=" in part)
                    prices = {o.get("name"): o.get("odds") for o in market.get("outcomes") or [] if o.get("active")}
                    by_team = lambda team: next((odds for name, odds in prices.items()
                                                 if name == team or name.startswith(team + " (")), None)
                    home, away = by_team(home_name), by_team(away_name)
                    if kind == "Winner" and home and away:
                        game["ml"] = {"away": away, "home": home}
                    elif kind == "Handicap" and "hcp" in spec and home and away:
                        game["spreads"].append({"line": float(spec["hcp"]), "home": home, "away": away})
                    elif kind == "Total" and "total" in spec:
                        over, under = prices.get(f"Over {spec['total']}"), prices.get(f"Under {spec['total']}")
                        if over and under:
                            game["totals"].append({"line": float(spec["total"]), "over": over, "under": under})
        if game["ml"] or game["totals"] or game["spreads"]:
            out.append(game)
    return out


# The books of the lines page, in column order: the featured book first, then the
# books the fair line comes from, then the rest. A book's weight is its share of the fair
# line, from the same study as the props board's (books.py), its game-line set: Kalshi and
# FanDuel lead the sportsbooks here. Kambi (BetRivers' odds) is shown only.
BOOKS = (
    {"key": "betano", "name": "Betano", "short": "Betano", "weight": 0, "target": True,
     "icon": betano.ICON, "fetch": fetch_betano, "parse": parse_betano},
    {"key": "pinnacle", "name": "Pinnacle", "short": "Pinnacle", "weight": 1, "target": False,
     "icon": pinnacle.ICON, "fetch": fetch_pinnacle, "parse": parse_pinnacle},
    {"key": "fanduel", "name": "FanDuel", "short": "FanDuel", "weight": 1.1, "target": False,
     "icon": fanduel.ICON, "fetch": fetch_fanduel, "parse": parse_fanduel},
    {"key": "draftkings", "name": "DraftKings", "short": "DK", "weight": 1, "target": False,
     "icon": draftkings.ICON, "fetch": fetch_draftkings, "parse": parse_draftkings},
    {"key": "kambi", "name": "Kambi", "short": "Kambi", "weight": 0, "target": False,
     "icon": kambi.ICON, "fetch": fetch_kambi, "parse": parse_kambi},
    {"key": "kalshi", "name": "Kalshi", "short": "Kalshi", "weight": 1.1, "target": False,
     "icon": kalshi.ICON, "fetch": fetch_kalshi, "parse": parse_kalshi},
    {"key": "mise", "name": "Mise-o-jeu", "short": "Mise", "weight": 0, "target": True,
     "icon": mise.ICON, "fetch": fetch_mise, "parse": parse_mise},
    {"key": "stake", "name": "Stake", "short": "Stake", "weight": 0, "target": True,
     "icon": stake.ICON, "fetch": fetch_stake, "parse": parse_stake},
    {"key": "betway", "name": "Betway", "short": "Betway", "weight": 0, "target": False,
     "icon": BETWAY_ICON, "fetch": fetch_betway, "parse": parse_betway},
    {"key": "bovada", "name": "Bovada", "short": "Bovada", "weight": 0, "target": False,
     "icon": bovada.ICON, "fetch": fetch_bovada, "parse": parse_bovada},
)


def _median_line(counts: Counter) -> float:
    if not counts:
        return 0.0
    ranked = sorted(counts.elements())
    return ranked[len(ranked) // 2]


def _merge(book_games: list[dict], league) -> list[dict]:
    """Every book's games as rows keyed like the board's games ("<date>|<away>@<home>"),
    with the board's team codes, and each book's total and spread the one nearest the
    median main line — the line the books that post only one agree on. Where no book posts
    only one, each book's most even rung (over and under closest) stands in for its main line:
    a ladder's middle rung, not the median of all its rungs (Kalshi's run from 3.5 up)."""
    rows = {}
    for book in book_games:
        for game in book["games"]:
            # a book that knows the teams' codes (Kalshi) gives them; the rest are matched by name
            away = game.get("away") or team_code(league, game.get("away_name"))
            home = game.get("home") or team_code(league, game.get("home_name"))
            if not away or not home or away == home:
                continue
            key = f"{game_date(game['start'])}|{away}@{home}"
            row = rows.setdefault(key, {"away": away, "home": home, "start": game["start"], "books": {}})
            row["books"][book["key"]] = {"ml": game.get("ml") or None,
                                         "totals": game.get("totals") or [],
                                         "spreads": game.get("spreads") or []}
    for row in rows.values():
        anchors, even = Counter(), Counter()
        for entry in row["books"].values():
            if len(entry["totals"]) == 1:
                anchors[entry["totals"][0]["line"]] += 1
            elif entry["totals"]:
                even[min(entry["totals"], key=lambda total: abs(total["over"] - total["under"]))["line"]] += 1
        row["totalLine"] = _median_line(anchors or even)
        # a spread's line is the home side's, so its middle is found among the spreads, not the totals
        spread_anchors, spread_even = Counter(), Counter()
        for entry in row["books"].values():
            if len(entry["spreads"]) == 1:
                spread_anchors[entry["spreads"][0]["line"]] += 1
            elif entry["spreads"]:
                spread_even[min(entry["spreads"], key=lambda line: abs(line["home"] - line["away"]))["line"]] += 1
        middle = {"totals": row["totalLine"], "spreads": _median_line(spread_anchors or spread_even)}
        for pick in ("totals", "spreads"):
            for entry in row["books"].values():
                if entry[pick]:
                    entry[pick] = [min(entry[pick], key=lambda line: abs(line["line"] - middle[pick]))]
    return sorted(rows.values(), key=lambda row: (row["start"], row["away"]))


def devig(away: float, home: float) -> tuple[float, float]:
    """Both sides' implied chances with the vig removed (books.devig, the power method)."""
    return books.devig(away, home)


def _weighted(chances: list[tuple[float, float]]) -> float:
    """The chances [(chance, weight)] averaged in log-odds by weight."""
    logit = sum(weight * math.log(p / (1 - p)) for p, weight in chances) / sum(weight for _, weight in chances)
    return 1 / (1 + math.exp(-logit))


def fair(row: dict) -> dict:
    """The weighted books' no-vig chance of each side: the moneylines, and the total at the
    game's median line, each book devigged and averaged in log-odds by its weight. A pair
    whose sides imply more than 1 + books.MAX_OVERROUND is left out. Empty where no weighted
    book posts both sides."""
    mls, totals = [], []
    tight = lambda a, b: 1 / a + 1 / b <= 1 + books.MAX_OVERROUND + 1e-9
    for book in BOOKS:
        if not book["weight"]:
            continue
        entry = row["books"].get(book["key"])
        if not entry:
            continue
        ml = entry["ml"] or {}
        if ml.get("away") and ml.get("home") and tight(ml["away"], ml["home"]):
            mls.append((devig(1 / ml["away"], 1 / ml["home"])[0], book["weight"]))
        total = (entry["totals"] or [None])[0]
        if total and total["line"] == row["totalLine"] and total["over"] and total["under"] \
                and tight(total["over"], total["under"]):
            totals.append((devig(1 / total["over"], 1 / total["under"])[0], book["weight"]))
    out = {}
    if mls:
        away = _weighted(mls)
        out["ml"] = {"away": away, "home": 1 - away}
    if totals:
        over = _weighted(totals)
        out["total"] = {"over": over, "under": 1 - over}
    return out


def _ev(row: dict, book: str = "betano") -> dict | None:
    """What the featured book (Betano) pays against the fair chance, fair × price − 1, each side."""
    entry = row["books"].get(book)
    if not entry:
        return None
    out = {}
    fair_line = row.get("fair") or {}
    if entry.get("ml") and fair_line.get("ml") and entry["ml"].get("away") and entry["ml"].get("home"):
        out["ml"] = {"away": fair_line["ml"]["away"] * entry["ml"]["away"] - 1,
                     "home": fair_line["ml"]["home"] * entry["ml"]["home"] - 1}
    total = (entry.get("totals") or [None])[0]
    if total and fair_line.get("total") and total["line"] == row["totalLine"] \
            and total.get("over") and total.get("under"):
        out["total"] = {"over": fair_line["total"]["over"] * total["over"] - 1,
                        "under": fair_line["total"]["under"] * total["under"] - 1}
    return out or None


def build(leagues=None, offline: bool = False) -> Path:
    """Fetch every book's game lines, merge them and write site/lines.html."""
    from . import site as board   # the board's cache, output folder and JSON encoding
    # college football and soccer aren't on it yet: their teams aren't in _TEAMS
    leagues = {key: league for key, league in (leagues or {}).items() if key in _TEAMS}
    now = net.now()
    page = {"generated": dt.datetime.now(board.ET).isoformat(timespec="minutes"), "leagues": []}
    # every league's every book at once; net.LIMITS keeps each site to its own pace
    with ThreadPoolExecutor(max_workers=len(BOOKS) * max(len(leagues), 1)) as pool:
        asked = {(league.KEY, book["key"]): pool.submit(
                     board._fetch_cached, f"{league.KEY}-lines-{book['key']}",
                     lambda book=book, league=league: book["fetch"](league, now), offline,
                     lambda answer, book=book, league=league: book["parse"](answer, league, now))
                 for league in leagues.values() for book in BOOKS}
        answers = {key: future.result() for key, future in asked.items()}
    for league in leagues.values():
        book_games = []
        for book in BOOKS:
            answer, at, error = answers[(league.KEY, book["key"])]
            try:
                games = book["parse"](answer, league, now) if answer else []
            except (KeyError, TypeError, ValueError) as exc:
                games, error = [], f"parse failed: {type(exc).__name__}: {exc}"
            book_games.append({"key": book["key"], "games": games, "error": error, "at": at})
        # a book may still list a game for a while after it starts: once under way it can't be bet
        rows = [row for row in _merge(book_games, league)
                if dt.datetime.fromisoformat(row["start"].replace("Z", "+00:00")) > now]
        for row in rows:
            row["fair"] = fair(row)
            row["ev"] = _ev(row)
        page["leagues"].append({
            "key": league.KEY, "name": league.NAME,
            "books": [{"key": book["key"], "name": book["name"], "short": book["short"],
                       "sharp": book["weight"] > 0, "weight": book["weight"],
                       "target": book["target"],
                       "icon": book.get("icon")} for book in BOOKS],
            "games": rows,
            "fetched": {bg["key"]: bg["at"].isoformat(timespec="minutes") if bg["at"] else None for bg in book_games},
            "errors": {bg["key"]: bg["error"] for bg in book_games if bg["error"]},
        })
    board.OUT.mkdir(exist_ok=True)
    out = board.OUT / "lines.html"
    out.write_text(TEMPLATE.read_text(encoding="utf-8")
                   .replace("__DATA__", board._dumps(page).replace("</", "<\\/")), encoding="utf-8")
    return out
