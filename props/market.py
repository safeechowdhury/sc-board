"""An odds board built from the market, not from Underdog alone (college football, soccer).

The game list comes from Kambi's league listings, which name every game (with its home and away
team and kickoff) whether or not Underdog has lines for it; Underdog's games are matched onto
them by kickoff and team names (matchups.py), and one Kambi doesn't list is added from Underdog.

Rows are Underdog's lines plus, for every player and stat Underdog has no line for, a row at the
market's line: the line most sharp books post both sides at, then the most books post both
sides at, then the one most books price at all; among ladders' rungs with no two-way price, the
rung nearest even money. A row needs two books, so one book's lone price makes no row, and a
book's prop finds its game by the game the book filed it under (`prop["game"]`).

Books spell some names out in full ("João Pedro Junqueira de Jesus", "Pedro Lomba Neto") where
others don't ("João Pedro", "Pedro Neto"). Within a game, a name whose words take in a shorter
name's (two words at least) is that player, so every book's price finds the one row.
"""
import datetime as dt
import re
from collections import Counter, defaultdict

from . import matchups
from .underdog import norm_name

EVEN = 2.0   # the price a ladder's main rung sits nearest
MIN_BOOKS = 2   # a market row needs this many books pricing the player's stat


def game_key(date: str, away: str, home: str) -> str:
    return f"{date}|{away}@{home}"


def _tokens(name: str) -> frozenset[str]:
    return frozenset(norm_name(name).split())


def merge_games(listed: list[dict], ud_games: list[dict]) -> list[dict]:
    """The board's games: each listed game (start, home, away, comp, date), with the key and line
    count of the Underdog game it matches, then Underdog's games no listing has.

    ud_games: [{key, date, start, away, home, words, lines}] from Underdog's lines."""
    # pairs at the same kickoff that share a team's words, the most words first: two games at one
    # time can share a word ("Kansas State Wildcats", "Northwestern Wildcats"), never as many
    listed = sorted(listed, key=lambda g: g["start"])
    pairs = sorted(((len(mine & u["words"]), i, u["key"]) for i, g in enumerate(listed)
                    for mine in [matchups.words(f"{g['away']} {g['home']}")] for u in ud_games
                    if mine & u["words"] and abs(matchups._when(u["start"]) - matchups._when(g["start"])) <= matchups.START_GAP),
                   reverse=True)
    matched, taken = {}, set()
    for _, i, key in pairs:
        if i not in matched and key not in taken:
            matched[i] = key
            taken.add(key)
    by_key = {u["key"]: u for u in ud_games}
    games = []
    for i, g in enumerate(listed):
        match = by_key.get(matched.get(i))
        games.append({"key": match["key"] if match else game_key(g["date"], g["away"], g["home"]),
                      "date": g["date"], "away": g["away"], "home": g["home"], "start": g["start"],
                      "state": "upcoming", "lines": match["lines"] if match else 0, "comp": g.get("comp")})
    for u in ud_games:
        if u["key"] not in taken:
            games.append({"key": u["key"], "date": u["date"], "away": u["away"], "home": u["home"],
                          "start": u["start"], "state": "upcoming", "lines": u["lines"], "comp": None})
    return sorted(games, key=lambda g: g["start"])


class GameFinder:
    """Which of the board's games a book's [start, name] is, by kickoff and team names."""

    def __init__(self, games: list[dict]):
        self.games = [(matchups._when(g["start"]), matchups.words(f"{g['away']} {g['home']}"), g) for g in games]
        self.seen: dict = {}

    def __call__(self, game) -> dict | None:
        if not game:
            return None
        key = (str(game[0]), game[1])
        if key not in self.seen:
            when, mine = matchups._when(game[0]), matchups.words(game[1])
            found = sorted(((len(mine & theirs), g["key"]) for begins, theirs, g in self.games
                            if abs(when - begins) <= matchups.START_GAP and mine & theirs), reverse=True)
            # the game sharing the most words with the book's name for it; a tie can't be told apart
            best = found[0] if found and (len(found) == 1 or found[0][0] > found[1][0]) else None
            self.seen[key] = next(g for _, _, g in self.games if g["key"] == best[1]) if best else None
        return self.seen[key]


def aliases(rows: list[dict], props_by_book: list[list[dict]], find: GameFinder) -> dict[str, str]:
    """{name: the shorter name that player goes by} for the names a game's books spell out in full."""
    pool: dict[str, set[str]] = defaultdict(set)   # game key -> every name seen in it
    for row in rows:
        pool[row["game"]].add(row["name"])
    for props in props_by_book:
        for prop in props:
            game = find(prop.get("game"))
            if game:
                pool[game["key"]].add(prop["player"])
    found: dict[str, set[str]] = defaultdict(set)
    for names in pool.values():
        tokens = {name: _tokens(name) for name in names}
        for name, mine in tokens.items():
            shorter = [(len(theirs), other) for other, theirs in tokens.items()
                       if len(theirs) >= 2 and theirs < mine]
            if not shorter:
                continue
            most = max(size for size, _ in shorter)
            best = {norm_name(other): other for size, other in shorter if size == most}
            found[name].add(next(iter(best.values())) if len(best) == 1 else None)
    # a name that means one player in one game and another (or nobody) elsewhere is left alone
    return {name: next(iter(to)) for name, to in found.items() if len(to) == 1 and None not in to}


def renamed(props: list[dict], names: dict[str, str]) -> list[dict]:
    return [{**prop, "player": names[prop["player"]]} if prop["player"] in names else prop for prop in props]


def _line(prices: dict[float, dict[str, tuple]], sharp: set[str]) -> float:
    """The market's line among a player's stat's lines: {line: {book: (over, under)}}."""
    def two_way(line):
        return [book for book, (_, under) in prices[line].items() if under]

    if any(two_way(line) for line in prices):
        return max(prices, key=lambda line: (len([b for b in two_way(line) if b in sharp]), len(two_way(line)),
                                             len(prices[line]), -line))
    most = max(len(books) for books in prices.values())
    common = [line for line, books in prices.items() if len(books) * 2 >= most]

    def median_over(line):
        overs = sorted(over for over, _ in prices[line].values())
        return overs[len(overs) // 2]
    return min(common, key=lambda line: (abs(median_over(line) - EVEN), line))


def rows(lines: list[dict], props_by_book: list[tuple[str, list[dict]]], find: GameFinder,
         sharp: set[str], stats: set[str]) -> list[dict]:
    """A row at the market's line for each player and stat in a board game that two books price
    and Underdog doesn't. lines: Underdog's rows; props_by_book: [(book key, its renamed props)]."""
    covered = {(norm_name(line["name"]), line["stat"]) for line in lines}
    seen: dict[tuple, dict] = {}   # (game key, norm name, stat) -> {"name", "game", "prices": {line: {book: (over, under)}}}
    for book, props in props_by_book:
        for prop in props:
            if prop["stat"] not in stats or not prop.get("over"):
                continue
            game = find(prop.get("game"))
            if not game:
                continue
            who = norm_name(prop["player"])
            if (who, prop["stat"]) in covered:
                continue
            entry = seen.setdefault((game["key"], who, prop["stat"]), {"names": Counter(), "game": game, "prices": {}})
            entry["names"][prop["player"]] += 1
            at = entry["prices"].setdefault(prop["line"], {})
            if book not in at or (prop["under"] and not at[book][1]):   # a book's two-way price before its rung
                at[book] = (prop["over"], prop["under"])
    games_of = Counter((who, stat) for _, who, stat in seen)
    out = []
    for (key, who, stat), entry in seen.items():
        if games_of[(who, stat)] > 1:   # one name in two games: a book's price couldn't tell them apart
            continue
        if len({book for books in entry["prices"].values() for book in books}) < MIN_BOOKS:
            continue
        game = entry["game"]
        out.append({"id": None, "name": entry["names"].most_common(1)[0][0], "team": "", "opp": "", "home": False,
                    "pos": "", "start": game["start"], "game": key, "stat": stat,
                    "line": _line(entry["prices"], sharp), "ud": False, "over": None, "under": None,
                    "matchup": f"{game['away']} @ {game['home']}"})
    return out


def listed(answers: list[tuple[str | None, list[dict]]], today: dt.date, days: int, now: dt.datetime,
           slate_of) -> list[dict]:
    """The listings' games on the slate (`days` slate days from `today`, by `slate_of` a kickoff, and
    not started by `now`): [(competition, Kambi's games)] -> [{start, date, home, away, comp}]."""
    last = today + dt.timedelta(days=days - 1)
    out = []
    for comp, games in answers:
        for g in games:
            start = matchups._when(g["start"])
            date = slate_of(g["start"])
            if start > now and today <= date <= last:
                out.append({"start": g["start"], "date": str(date), "home": g["home"], "away": g["away"], "comp": comp})
    return out


# the books whose names for a game are shortest, the shortest first: "Mississippi State @ LSU"
SHORT_NAMES = ("draftkings", "fanduel", "bovada", "mise")
LONG_NAME = 18   # a name longer than this, with no book's shorter one, gives way to Underdog's code
_POLL = re.compile(r"\s*\(#\d+\)")   # Bovada's ranking: "Alabama (#6)"
_RANK = re.compile(r"^\((\d+)\)\s*")   # Kambi's college ranking: "(16) Mississippi State Bulldogs"


def _sides(name: str) -> tuple[str, str] | None:
    """(away, home) from a book's name for a game: "Akron @ Miami OH" ("Alabama at Tennessee"), or
    soccer's home team first, "Bayern Munchen vs RB Leipzig" (FanDuel's "Betis v Barcelona")."""
    name = _POLL.sub("", name)
    for between, home_first in ((" @ ", False), (" at ", False), (" vs ", True), (" v ", True)):
        if between in name:
            first, second = (part.strip() for part in name.split(between, 1))
            return (second, first) if home_first else (first, second)
    return None


def short_names(games: list[dict], answers: list[tuple[str, list[dict]]], find: GameFinder) -> None:
    """Name each game as the first of SHORT_NAMES that has it does, for a sidebar that fits on two
    lines: Kambi spells out mascots and clubs in full ("(16) Mississippi State Bulldogs", "1. FC Union
    Berlin"). A college team's ranking is kept apart ("awayRank"), shown small. `names` keeps every
    name a game has gone by, so the page can still match Underdog's own names to it."""
    named = {}
    for book in SHORT_NAMES:
        for key, props in answers:
            if key != book:
                continue
            for prop in props:
                game = find(prop.get("game"))
                sides = _sides(prop["game"][1]) if game else None
                if sides and game["key"] not in named:
                    named[game["key"]] = sides
    for g in games:
        g["names"] = f"{g['away']} {g['home']}"
        for side in ("away", "home"):
            rank = _RANK.match(g[side])
            if rank:
                g[side + "Rank"] = int(rank.group(1))
                g[side] = g[side][rank.end():]
        if g["key"] in named:
            g["away"], g["home"] = named[g["key"]]
        elif g["lines"] and max(len(g["away"]), len(g["home"])) > LONG_NAME:
            g["away"], g["home"] = g["key"].split("|", 1)[1].split("@", 1)   # Underdog's codes: "UVA @ SMU"
