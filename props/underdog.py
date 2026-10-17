"""Underdog pick'em lines.

The board comes from the endpoint the Underdog site itself uses.
state_config_id is state-specific: if payouts or availability look wrong, copy
yours from the request in DevTools and set UNDERDOG_STATE_CONFIG_ID.
"""
import os
import re
import unicodedata
from collections import Counter
from urllib.parse import urlencode

import httpx
import polars as pl

from . import net
from .icons import icon

URL = "https://api.underdogfantasy.com/v1/over_under_lines"
ICON = icon("underdog-tile.png")   # shown in place of "UD" in the EV toggle
PARAMS = {
    "product": "fantasy",
    "product_experience_id": "018e1234-5678-9abc-def0-123456789002",
    "state_config_id": os.environ.get("UNDERDOG_STATE_CONFIG_ID", "0e155f41-e0c0-44e8-9eb4-10255324c610"),
}

SIDES = {"higher": "over", "lower": "under"}

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
_NICKNAME = re.compile(r"\((.+?)\)")
# first names the sources swap that neither begins nor ends the other (the NHL and FanDuel say Tommy Novak,
# Underdog, Kambi and Mise say Thomas)
_GIVEN = {"tommy": "thomas", "tom": "thomas"}


def fixed_stat(league, stat: str, line: float) -> str:
    """The stat a prop belongs to: a column at the line of one of the league's FIXED_LINES
    (rush + rec TDs at 1.5) is that stat (2+ TDs)."""
    return next((key for key, (column, at) in getattr(league, "FIXED_LINES", {}).items()
                 if column == stat and at == line), stat)


def request_url(sport: str) -> str:
    """The full lines URL for a sport. The lines viewer (ud.html) calls it straight from the browser."""
    return f"{URL}?{urlencode({**PARAMS, 'sport_id': sport})}"


def sports() -> list[list]:
    """[sport id, Underdog's name for it, lines] for every sport with lines up, most lines first.
    Without a sport_id the endpoint answers every sport at once (some 35 MB), so the build asks
    once for the list the lines viewer offers; the viewer then fetches one sport at a time."""
    payload = fetch("")
    names = {g["sport_id"]: g.get("sport_name") or g["sport_id"]
             for g in payload.get("games", []) + payload.get("solo_games", []) if g.get("sport_id")}
    players = {p["id"]: p.get("sport_id") for p in payload.get("players", [])}
    sport_of = {a["id"]: players.get(a.get("player_id")) for a in payload.get("appearances", [])}
    counts = Counter(sport_of.get(((ln.get("over_under") or {}).get("appearance_stat") or {}).get("appearance_id"))
                     for ln in payload.get("over_under_lines", []) if ln.get("status") == "active")
    counts.pop(None, None)
    return [[sport, names.get(sport, sport), n] for sport, n in counts.most_common()]


def fetch(sport: str) -> dict:
    with net.site("underdog"):
        r = httpx.get(request_url(sport), timeout=60,
                      headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def _offer(option: dict) -> dict:
    """odds: the decimal payout Underdog shows for the pick; mult: its payout
    multiplier, where 1.0 is a standard pick."""
    odds = ((option.get("odds") or {}).get("fantasy") or {}).get("decimal")
    return {"odds": float(odds) if odds else None, "mult": float(option.get("payout_multiplier") or 1)}


def matchup(title: str) -> tuple[str, str] | None:
    """A game's (away, home) codes from its title: "PSU @ MICH", or soccer's home team first,
    "ROM vs RMA"."""
    if " @ " in title:
        away, home = title.split(" @ ", 1)
        return away, home
    if " vs " in title:
        home, away = title.split(" vs ", 1)
        return away, home
    return None


def parse(payload: dict, league) -> tuple[list[dict], Counter]:
    """The league's active lines, plus a count of stats we don't track (by display name)."""
    def team(code: str) -> str:
        code = code.strip().upper()
        return league.TEAM_ALIASES.get(code, code)

    players = {p["id"]: p for p in payload.get("players", [])}
    appearances = {a["id"]: a for a in payload.get("appearances", [])}
    games = {g["id"]: g for g in payload.get("games", [])}
    lines, skipped = [], Counter()
    for ln in payload.get("over_under_lines", []):
        if ln.get("status") != "active" or ln.get("line_type", "balanced") != "balanced":
            continue
        stat = (ln.get("over_under") or {}).get("appearance_stat") or {}
        app = appearances.get(stat.get("appearance_id")) or {}
        player = players.get(app.get("player_id"))
        game = games.get(app.get("match_id"))
        teams = matchup(game.get("title", "")) if game else None
        if not player or not teams or player.get("sport_id") != league.UNDERDOG_SPORT:
            continue
        if stat.get("stat") not in league.UNDERDOG_STATS:
            skipped[stat.get("display_stat") or stat.get("stat")] += 1
            continue
        sides = {
            SIDES[o["choice"]]: _offer(o)
            for o in ln.get("options", [])
            if o.get("choice") in SIDES and o.get("status") == "active"
        }
        if not sides:
            continue
        away, home = (team(t) for t in teams)
        is_home = app.get("team_id") == game.get("home_team_id")
        lines.append({
            "player": f"{player.get('first_name') or ''} {player.get('last_name') or ''}".strip(),
            "team": home if is_home else away,
            "opponent": away if is_home else home,
            "home": is_home,
            "position": player.get("position_name"),
            "stat": fixed_stat(league, league.UNDERDOG_STATS[stat["stat"]], float(ln["stat_value"])),
            "line": float(ln["stat_value"]),
            "over": sides.get("over"),
            "under": sides.get("under"),
            "start": game.get("scheduled_at"),
            "matchup": game.get("full_team_names_title") or game["title"],   # "Real Betis Seville vs FC Barcelona"
        })
    return lines, skipped


def _short_for(a: str, b: str) -> bool:
    """Mitch/Mitchell, Drew/Andrew: one name begins or ends the other."""
    short, full = sorted((a, b), key=len)
    return len(short) >= 3 and (full.startswith(short) or full.endswith(short))


def norm_name(name: str) -> str:
    name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    name = name.lower().replace(".", "")   # "A.J." and "AJ" are the same player
    parts = [p for p in re.sub(r"[^a-z0-9 ]", " ", name).split() if p not in _SUFFIXES]
    if len(parts) > 1:
        parts[0] = _GIVEN.get(parts[0], parts[0])
    return " ".join(parts)


def match_players(lines: list[dict], players: pl.DataFrame) -> list[str]:
    """Set line["player_id"] from the NHL player index (player_id, name, team,
    newest first). Returns the names that could not be matched.

    Exact name first; two players sharing a name are split by team. Otherwise
    fall back to surname + team when one first name is short for the other
    (Mitch/Mitchell, Drew/Andrew), which keeps Anton Johansson from borrowing
    Albert's games.
    """
    by_name: dict[str, list[dict]] = {}
    by_surname_team: dict[tuple[str, str], list[dict]] = {}
    for p in players.iter_rows(named=True):
        key = norm_name(_NICKNAME.sub(" ", p["name"]))
        by_name.setdefault(key, []).append(p)
        by_surname_team.setdefault((key.split(" ")[-1], p["team"]), []).append(p)
        nickname = _NICKNAME.search(p["name"])   # the NHL lists "Anthony-John (AJ) Greer"
        if nickname:
            by_name.setdefault(norm_name(f"{nickname.group(1)} {key.split(' ')[-1]}"), []).append(p)

    def find(name: str, team: str):
        key = norm_name(name)
        if not key:
            return None
        same = by_name.get(key, [])
        if same:
            return next((p for p in same if p["team"] == team), same[0])
        # same surname on the same team, where one first name is short for the other
        first = key.split(" ")[0]
        rest = [p for p in by_surname_team.get((key.split(" ")[-1], team), [])
                if _short_for(first, norm_name(p["name"]).split(" ")[0])]
        return rest[0] if len(rest) == 1 else None

    unmatched = set()
    for line in lines:
        hit = find(line["player"], line["team"])
        line["player_id"] = hit["player_id"] if hit else None
        if not hit:
            unmatched.add(line["player"])
    return sorted(unmatched)
