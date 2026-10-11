"""Sportsbook prices for the board's props, from every book that posts them.

Each book is a module with the same surface:

    KEY, NAME, SHORT        "fanduel", "FanDuel", the column heading
    SHARP                   whether its prices count toward the fair chance
    TARGET                  whether it is a site to find EV on, a toggle beside Underdog (neither: shown only)
    fetch(league)           the book's raw props for the league, as JSON-ready data
    parse(payload, league)  -> ([{player, stat, line, over, under}], Counter of stats it doesn't map)

Prices are decimal odds. The board measures a site's price (Underdog's payout, or
Mise-o-jeu's) against the best over and best under the sharp books (Pinnacle,
FanDuel, DraftKings, Kambi, Novig) post at the row's line, with the vig removed (both
sides scaled to 100%); a sharp book's one-way over at the line caps the over's chance.
"""
from . import bovada, draftkings, fanduel, kambi, mise, novig, pinnacle
from .underdog import norm_name

BOOKS = (pinnacle, fanduel, draftkings, kambi, novig, mise, bovada)   # the sharp books, the sites to bet on, then the rest


def with_fixed(props: list[dict], league) -> list[dict]:
    """A book's props plus a copy of each one at a FIXED_LINES line under that stat:
    a 2+ TDs price is also rush + rec TDs over 1.5, so it serves both rows."""
    fixed = getattr(league, "FIXED_LINES", {})
    return props + [{**prop, "stat": key} for prop in props for key, (column, line) in fixed.items()
                    if prop["stat"] == column and prop["line"] == line]


def prices(props: list[dict], rows: list[dict]) -> tuple[dict, int]:
    """A book's [line, over, under] keyed like the board's watchlist ("<player id or name>|<stat>"),
    plus how many props found no row. A one-way price has no under (None).

    A book names the player, and sometimes his team ("team"). A name two players on the
    board share is left out unless the team tells them apart. Where a book has several
    prices for a row (a one-way ladder's rungs), the row gets the most useful one: at its
    line, two-way first; otherwise a two-way price, then the rung nearest its line.
    """
    owners: dict[tuple, dict] = {}
    for row in rows:
        owners.setdefault((norm_name(row["name"]), row["stat"]), {}).setdefault(
            row["id"] if row["id"] is not None else row["name"], (row["team"], row["line"]))
    best: dict[str, tuple] = {}
    missed = 0
    for prop in props:
        found = owners.get((norm_name(prop["player"]), prop["stat"]), {})
        if "team" in prop:
            found = {owner: seen for owner, seen in found.items() if seen[0] == prop["team"]}
        if len(found) != 1:
            missed += 1
            continue
        (owner, (_, line)), = found.items()
        two_way = prop["under"] is not None
        rank = (prop["line"] == line, two_way, -abs(prop["line"] - line))
        key = f"{owner}|{prop['stat']}"
        if key not in best or rank > best[key][0]:
            best[key] = (rank, [prop["line"], prop["over"], prop["under"]])
    return {key: price for key, (_, price) in best.items()}, missed
