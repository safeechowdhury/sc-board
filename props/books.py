"""Sportsbook prices for the board's props, from every book that posts them.

Each book is a module with the same surface:

    KEY, NAME, SHORT        "fanduel", "FanDuel", the column heading
    WEIGHT                  its share of the fair chance (0: never part of it)
    TARGET                  whether it is a site to find EV on, a toggle beside Underdog (neither: shown only)
    LEAGUE_ATTR             the league setting that says where the book keeps the league ("PINNACLE_LEAGUE"):
                            a league that sets it to None (or leaves it empty) isn't asked of that book
    fetch(league)           the book's raw props for the league, as JSON-ready data: a dict of lists
    parse(payload, league)  -> ([{player, stat, line, over, under}], Counter of stats it doesn't map)

Prices are decimal odds. The board measures a site's price (Underdog's payout,
Mise-o-jeu's or Betano's) against the fair chance: each sharp book (WEIGHT above 0) posting
both sides at the row's line has its vig removed (devig(), the power method), and their
chances are averaged in log-odds by WEIGHT. A book whose two sides add up to more than
1 + MAX_OVERROUND is left out: nobody is making a real price there. A sharp book's one-way
over at the line caps the over's chance.

The weights (set 2026-10-19): Novig and Kalshi 7, Polymarket and ProphetX 5, Pinnacle 1.4,
DraftKings and FanDuel 1, every other book 0. A study of 252,387 graded closing prop prices put
the exchanges (Novig, Kalshi) well ahead of the sportsbooks; a second comparison (upside.tools,
2026-10-16) had ProphetX level with Kalshi and FanDuel a little ahead of DraftKings. Polymarket
and ProphetX were in neither study and have few graded props here, so they sit a step below Novig
and Kalshi. The board's own graded props (python -m props grade) couldn't yet tell these weights
apart from equal ones. Kambi's odds also serve BetRivers, which the study found the market moves away from, so it is shown only.

A fair chance is only counted (toward "+EV only", and shaded) when the books behind it
include one of CONFIDENT's sets: an exchange, or all three of Pinnacle, DraftKings and
FanDuel. Otherwise it is shown in lighter type as low confidence.

An ESTIMATE book (an exchange) with no two-way price at the row's line still counts, from its
other lines: its devigged chances are read across in log-odds between the lines either side of
the row's, or moved from its nearest line by the slope the books' ladders show for that player.
It counts for less the further it reaches, and the row's fair is marked an estimate.
"""
import math
import types

import httpx

from . import bet99, betano, bovada, draftkings, fanduel, kalshi, kambi, mise, novig, pick6, pinnacle, polymarket, prophetx, stake
from .underdog import norm_name

BOOKS = (pinnacle, fanduel, draftkings, kambi, novig, kalshi, polymarket, prophetx, mise, betano, stake, pick6, bovada, bet99)   # the books with odds, the sites to bet on, then the rest

MAX_OVERROUND = 0.10   # a two-way price whose sides imply more than 110% is too wide to count
CONFIDENT = (("novig",), ("kalshi",), ("polymarket",), ("prophetx",), ("pinnacle", "draftkings", "fanduel"))   # any one set, all two-way at the line
ESTIMATE = ("novig", "kalshi", "polymarket", "prophetx")   # off the row's line, still part of fair: estimated from their other lines


def devig(a: float, b: float) -> tuple[float, float]:
    """Two implied chances (1 / decimal price) with the vig removed, by the power method: the k
    with a**k + b**k = 1, then (a**k, b**k). Unlike scaling both sides down equally, it takes
    more of the margin off the longer side, where books put more of it, so a long over isn't
    left looking likelier than it is. Mirrored as devigOver in template.html."""
    if not (0 < a < 1 and 0 < b < 1):
        total = a + b
        return (a / total, b / total) if total else (0.0, 0.0)
    k = 1.0   # Newton from 1: f(k) = a**k + b**k - 1 is convex and falling, so it closes in from one side
    for _ in range(50):
        f = a ** k + b ** k - 1
        step = f / (a ** k * math.log(a) + b ** k * math.log(b))
        k -= step
        if abs(step) < 1e-12:
            break
    return a ** k, b ** k


def carries(book, league) -> bool:
    """Whether the book has the league (or, for a league of several competitions, any of them)."""
    return any(getattr(part, book.LEAGUE_ATTR, None) for part in competitions(league))


def competitions(league) -> list:
    """The league as each book is asked for it: itself, or for a league of several competitions
    (soccer's COMPETITIONS), one view of it per competition, with that competition's settings."""
    parts = getattr(league, "COMPETITIONS", None)
    if not parts:
        return [league]
    base = {name: getattr(league, name) for name in dir(league) if name.isupper()}
    return [types.SimpleNamespace(**{**base, **part}) for part in parts]


def fetch(book, league, **when) -> dict:
    """book.fetch for every competition of the league the book carries, the answers merged. A
    competition that fails is named in the answer's "errors" and the rest still count; if every
    one fails, so does the fetch. Competitions with all the same settings for the book (Mise-o-jeu
    lists every soccer competition in one request, so soccer gives it the tags at once) are asked once."""
    prefix = book.LEAGUE_ATTR.split("_")[0] + "_"   # "FANDUEL_": its page, competition, tabs, stats...
    settings = lambda part: repr(sorted((k, v) for k, v in vars(part).items() if k.startswith(prefix))) \
        if isinstance(part, types.SimpleNamespace) else id(part)
    parts = list({settings(part): part for part in competitions(league) if getattr(part, book.LEAGUE_ATTR, None)}.values())
    if len(parts) == 1:
        return book.fetch(parts[0], **when)
    merged, errors = {}, []
    for part in parts:
        try:
            answer = book.fetch(part, **when)
        except (httpx.HTTPError, ValueError) as e:
            errors.append(f"{part.COMPETITION}: {type(e).__name__}: {e}")
            continue
        for key, found in answer.items():
            merged.setdefault(key, []).extend(found)
    if errors and len(errors) == len(parts):
        raise ValueError("; ".join(errors))
    return {**merged, "errors": errors} if errors else merged


def with_fixed(props: list[dict], league) -> list[dict]:
    """A book's props plus a copy of each one at a FIXED_LINES line under that stat:
    a 2+ TDs price is also rush + rec TDs over 1.5, so it serves both rows."""
    fixed = getattr(league, "FIXED_LINES", {})
    return props + [{**prop, "stat": key} for prop in props for key, (column, line) in fixed.items()
                    if prop["stat"] == column and prop["line"] == line]


def prices(props: list[dict], rows: list[dict], anytime=()) -> tuple[dict, int]:
    """A book's [line, over, under] keyed like the board's watchlist ("<player id or name>|<stat>"),
    plus how many props found no row. A one-way price has no under (None).

    A book names the player, and sometimes his team ("team"). A name two players on the
    board share is left out unless the team tells them apart; a team code the board doesn't
    use at all (Kalshi's for soccer clubs) says nothing, and the name alone decides. Where a book has several
    prices for a row (a one-way ladder's rungs), the row gets the most useful one: at its
    line, two-way first; otherwise a two-way price, then the rung nearest its line. The book's
    two-way prices at other lines (its main line where Underdog's is an alternate) follow as a
    fourth item, [[line, over, under], ...], so a player's page moved to one of them finds it.

    A stat in `anytime` (the league's ANYTIME_STATS: a TD, a goal) takes only a price at the row's
    line: there, another rung is another bet (2+ TDs on the anytime row), not the same one at an
    alternate line. Its two-way prices at other lines still follow as the fourth item.
    """
    owners: dict[tuple, dict] = {}
    teams = {row["team"] for row in rows}
    for row in rows:
        owners.setdefault((norm_name(row["name"]), row["stat"]), {}).setdefault(
            row["id"] if row["id"] is not None else row["name"], (row["team"], row["line"]))
    best: dict[str, tuple] = {}
    two_way_at: dict[str, dict] = {}
    missed = 0
    for prop in props:
        found = owners.get((norm_name(prop["player"]), prop["stat"]), {})
        if prop.get("team") in teams:
            found = {owner: seen for owner, seen in found.items() if seen[0] == prop["team"]}
        if len(found) != 1:
            missed += 1
            continue
        (owner, (_, line)), = found.items()
        two_way = prop["under"] is not None
        rank = (prop["line"] == line, two_way, -abs(prop["line"] - line))
        key = f"{owner}|{prop['stat']}"
        if two_way:
            two_way_at.setdefault(key, {}).setdefault(prop["line"], [prop["line"], prop["over"], prop["under"]])
        if prop["stat"] in anytime and prop["line"] != line:
            continue
        if key not in best or rank > best[key][0]:
            best[key] = (rank, [prop["line"], prop["over"], prop["under"]])
    out = {}
    for key, (_, price) in best.items():
        others = [quote for line, quote in sorted(two_way_at.get(key, {}).items()) if line != price[0]]
        out[key] = price + [others] if others else price
    return out, missed
