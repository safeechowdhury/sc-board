"""MLB: per-game batting and pitching lines from the MLB Stats API (statsapi.mlb.com).

The API has no bulk game log, so a day costs one schedule request plus one
boxscore per game, trimmed with `fields` to the columns stored here. A
whole season is about 2,500 boxscores, fetched in parallel.

A hitter's game counts when he was in the starting lineup, and a pitcher's
when he started: a pinch-hit at-bat or an inning of relief says nothing
about a full-game line.
"""
import datetime as dt
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import polars as pl

from ..slate import fetch_board_date

KEY, NAME, PAGE = "mlb", "MLB", "mlb.html"

API = "https://statsapi.mlb.com/api/v1"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# MLB game type -> ours: 2 regular season, 3 playoffs (wild card, division, league, World Series)
GAME_TYPES = {"R": 2, "F": 3, "D": 3, "L": 3, "W": 3}
_FINAL = {"F", "O"}   # codedGameState: final, game over (not postponed, suspended or cancelled)

# boxscore field -> ours
BATTING = {
    "plateAppearances": "pa", "atBats": "ab", "hits": "hits", "doubles": "doubles", "triples": "triples",
    "homeRuns": "home_runs", "runs": "runs", "rbi": "rbis", "baseOnBalls": "walks",
    "strikeOuts": "batter_ks", "stolenBases": "stolen_bases", "totalBases": "total_bases",
}
PITCHING = {
    "outs": "outs", "strikeOuts": "strikeouts", "hits": "hits_allowed", "earnedRuns": "earned_runs",
    "baseOnBalls": "walks_allowed", "numberOfPitches": "pitches", "battersFaced": "batters_faced",
}
BOX_FIELDS = ",".join([
    "teams", "away", "home", "team", "abbreviation", "batters", "pitchers", "players", "person", "id",
    "fullName", "position", "allPositions", "battingOrder", "stats", "batting", "pitching",
    "gamesStarted", "wins", "losses", *BATTING, *PITCHING,
])
HITTING_STATS = [*BATTING.values(), "singles", "hits_runs_rbis"]
PITCHING_STATS = list(PITCHING.values())

# One row per player per game. Batting columns are null for a pitcher who didn't bat,
# pitching columns for anyone who didn't pitch.
SCHEMA = {
    "game_id": pl.Int64, "date": pl.Date, "season": pl.Int32, "game_type": pl.Int8,
    "player_id": pl.Int32, "name": pl.String, "team": pl.String, "opponent": pl.String,
    "home": pl.Boolean, "position": pl.String,
    "order": pl.Int8,        # lineup spot 1-9 when he started at the plate, else null
    "started": pl.Boolean,   # started on the mound
    **{stat: pl.Int16 for stat in HITTING_STATS},
    **{stat: pl.Int16 for stat in PITCHING_STATS},
    "decision": pl.String,
}

_NOT_A_POSITION = {"PH", "PR"}


def client() -> httpx.Client:
    return httpx.Client(timeout=60, headers=HEADERS, follow_redirects=True,
                        transport=httpx.HTTPTransport(retries=3))


def _get(http: httpx.Client, url: str, params: dict | None = None) -> dict:
    for attempt in range(4):
        r = http.get(url, params=params)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        break
    r.raise_for_status()
    return r.json()


def _schedule(http: httpx.Client, start: dt.date, end: dt.date, hydrate: str | None = None) -> list[dict]:
    """Regular-season and playoff games between start and end, as the API returns them."""
    params = {"sportId": 1, "startDate": str(start), "endDate": str(end), "gameType": ",".join(GAME_TYPES)}
    if hydrate:
        params["hydrate"] = hydrate
    return [g for day in _get(http, f"{API}/schedule", params).get("dates", []) for g in day["games"]
            if g["gameType"] in GAME_TYPES]


def _two_way(http: httpx.Client, season: int) -> set[int]:
    """Players listed as two-way (Ohtani): they keep one position whichever role they played that day."""
    people = _get(http, f"{API}/sports/1/players", {"season": season}).get("people", [])
    return {p["id"] for p in people if (p.get("primaryPosition") or {}).get("abbreviation") == "TWP"}


def _position(player: dict, batted: bool, started: bool) -> str:
    """The position a game is filed under: the first one he fielded, so a pinch hitter who
    stayed in at first base is a 1B; "P" for a pitcher, starter or reliever."""
    if started or not batted:
        return "P"
    fielded = [p["abbreviation"] for p in player.get("allPositions", []) if p["abbreviation"] not in _NOT_A_POSITION]
    return fielded[0] if fielded else player.get("position", {}).get("abbreviation", "PH")


def _rows(game: dict, box: dict, two_way: set[int]) -> list[dict]:
    """One row per player who batted or pitched in a game."""
    rows = []
    sides = box["teams"]
    for side, other in (("away", "home"), ("home", "away")):
        team = sides[side]
        batters, pitchers = set(team.get("batters", [])), set(team.get("pitchers", []))
        for pid in batters | pitchers:
            player = team["players"].get(f"ID{pid}", {})
            stats = player.get("stats", {})
            batting = stats.get("batting") or {} if pid in batters else {}
            pitching = stats.get("pitching") or {} if pid in pitchers else {}
            order = player.get("battingOrder")
            started = bool(pitching.get("gamesStarted"))
            row = {
                "game_id": game["gamePk"], "date": game["officialDate"], "season": int(game["season"]),
                "game_type": GAME_TYPES[game["gameType"]], "player_id": pid,
                "name": player.get("person", {}).get("fullName"),
                "team": team["team"]["abbreviation"], "opponent": sides[other]["team"]["abbreviation"],
                "home": side == "home",
                "position": "TWP" if pid in two_way else _position(player, bool(batting), started),
                "order": int(order) // 100 if order and order.endswith("00") else None,
                "started": started if pitching else None,
                "decision": "W" if pitching.get("wins") else "L" if pitching.get("losses") else None,
            }
            if batting:
                row.update({ours: batting.get(field, 0) for field, ours in BATTING.items()})
                row["singles"] = row["hits"] - row["doubles"] - row["triples"] - row["home_runs"]
                row["hits_runs_rbis"] = row["hits"] + row["runs"] + row["rbis"]
            if pitching:
                row.update({ours: pitching.get(field, 0) for field, ours in PITCHING.items()})
            rows.append(row)
    return rows


def player_games(start: dt.date, end: dt.date, http: httpx.Client | None = None) -> pl.DataFrame:
    """Every batting and pitching line in the games finished between start and end (inclusive), in SCHEMA."""
    http = http or client()
    games = [g for g in _schedule(http, start, end) if g["status"].get("codedGameState") in _FINAL]
    if not games:
        return pl.DataFrame(schema=SCHEMA)
    two_way = set().union(*(_two_way(http, season) for season in {int(g["season"]) for g in games}))

    def one(game: dict) -> list[dict]:
        box = _get(http, f"{API}/game/{game['gamePk']}/boxscore", {"fields": BOX_FIELDS})
        return _rows(game, box, two_way)

    with ThreadPoolExecutor(max_workers=16) as pool:
        rows = [row for chunk in pool.map(one, games) for row in chunk]
    return (
        pl.DataFrame(rows, schema={c: (pl.String if c == "date" else t) for c, t in SCHEMA.items()})
        .with_columns(pl.col("date").str.to_date())
        .unique(["game_id", "player_id"]).sort("date", "game_id", "team", "player_id")
    )


def season_id(day: dt.date) -> int:
    """The season a date falls in. Winter belongs to the season just finished."""
    return day.year if day.month >= 3 else day.year - 1


def season_bounds(season: int) -> tuple[dt.date, dt.date]:
    """Generous date range for a season, from spring through the World Series; the caller filters by season."""
    return dt.date(season, 3, 1), min(dt.date(season, 11, 30), dt.date.today())


def previous_season(season: int) -> int:
    return season - 1


def season_label(season: int) -> str:
    return str(season)


def _slate(day: dt.date, http: httpx.Client) -> list[dict]:
    return _schedule(http, day, day + dt.timedelta(days=SLATE_DAYS - 1), hydrate="team,probablePitcher,lineups")


def schedule(day: dt.date, http: httpx.Client | None = None) -> list[dict]:
    state = {"Final": "final", "Preview": "upcoming"}
    return [
        {"game_id": g["gamePk"], "date": g["officialDate"], "start": g["gameDate"],
         "away": g["teams"]["away"]["team"]["abbreviation"], "home": g["teams"]["home"]["team"]["abbreviation"],
         "state": state.get(g["status"]["abstractGameState"], "live")}
        for g in _slate(day, http or client())
        if g["status"].get("codedGameState") not in {"D", "C"}   # postponed, cancelled
    ]


def rosters(teams: list[str], today: dt.date, http: httpx.Client | None = None) -> dict[str, list[int]]:
    """Who is expected to play for each team on today's slate: the posted batting lineup,
    or before it is out the active roster's hitters; plus the probable starting pitcher.
    A team's other pitchers are left out, so the rest of the rotation doesn't get a line."""
    http = http or client()
    wanted, out = set(teams), {}
    games = _slate(today, http)

    def hitters(team_id: int) -> list[int]:
        roster = _get(http, f"{API}/teams/{team_id}/roster", {"rosterType": "active"}).get("roster", [])
        return [p["person"]["id"] for p in roster if p["position"]["abbreviation"] != "P"]

    sides = []
    for g in games:
        lineups = g.get("lineups") or {}
        for side in ("away", "home"):
            team = g["teams"][side]
            code = team["team"]["abbreviation"]
            if code not in wanted or code in out:
                continue
            posted = [p["id"] for p in lineups.get(f"{side}Players", [])]
            pitcher = (team.get("probablePitcher") or {}).get("id")
            out[code] = posted + ([pitcher] if pitcher else [])
            if not posted:
                sides.append((code, team["team"]["id"]))
    with ThreadPoolExecutor(max_workers=8) as pool:
        for (code, _), ids in zip(sides, pool.map(lambda side: hitters(side[1]), sides)):
            out[code] = ids + out[code]
    return out


# ============================================================================
# What the board needs to know about baseball
# ============================================================================

UNDERDOG_SPORT = "MLB"
UNDERDOG_STATS = {
    "hits": "hits",
    "total_bases": "total_bases",
    "hits_runs_rbis": "hits_runs_rbis",
    "runs": "runs",
    "rbis": "rbis",
    "home_runs": "home_runs",
    "singles": "singles",
    "doubles": "doubles",
    "walks": "walks",
    "batter_strikeouts": "batter_ks",
    "stolen_bases": "stolen_bases",
    "strikeouts": "strikeouts",
    "pitch_outs": "outs",
    "hits_allowed": "hits_allowed",
    "runs_allowed": "earned_runs",   # Underdog shows it as "Pitcher Earned Runs Allowed"
    "walks_allowed": "walks_allowed",
}
TEAM_ALIASES = {
    "ARI": "AZ", "OAK": "ATH", "CHW": "CWS", "WAS": "WSH", "KCR": "KC", "SDP": "SD",
    "SFG": "SF", "TBR": "TB", "LA": "LAD",
}
PINNACLE_LEAGUE = 246
# Pinnacle's name for each prop, the words after "Total" ("Aaron Judge Total Bases").
PINNACLE_STATS = {
    "Bases": "total_bases",
    "Home Runs": "home_runs",
    "Hits": "hits",
    "RBIs": "rbis",
    "Strikeouts": "strikeouts",
    "Pitching Outs": "outs",
    "Hits Allowed": "hits_allowed",
    "Earned Runs": "earned_runs",
    "Walks Allowed": "walks_allowed",
}
FANDUEL_PAGE = "mlb"
FANDUEL_TABS = ["pitcher-props", "batter-props"]
# Market names after the player's ("Gerrit Cole - Strikeouts"). Batter props are one-sided ladders there.
FANDUEL_STATS = {
    "Strikeouts": "strikeouts",
    "Outs Recorded": "outs",
}
# One-way ladders: the stat in "To Record 2+ Hits", "To Hit A Double", "Gerrit Cole - Alt Strikeouts"
FANDUEL_LADDERS = {
    "Hit": "hits", "Hits": "hits", "Total Bases": "total_bases", "Hits + Runs + RBIs": "hits_runs_rbis",
    "Run": "runs", "Runs": "runs", "RBI": "rbis", "RBIs": "rbis", "Home Run": "home_runs", "Home Runs": "home_runs",
    "Single": "singles", "Double": "doubles", "Stolen Base": "stolen_bases", "Stolen Bases": "stolen_bases",
    "Strikeouts": "strikeouts",
}
DRAFTKINGS_LEAGUE = 84240
DRAFTKINGS_CATEGORIES = ["Batter Props", "Pitcher Props"]
DRAFTKINGS_LADDERS = {   # "N+" subcategory names
    "Hits": "hits", "Total Bases": "total_bases", "Hits + Runs + RBIs": "hits_runs_rbis", "Runs": "runs",
    "RBIs": "rbis", "Home Runs": "home_runs", "Singles": "singles", "Doubles": "doubles",
    "Walks (Batter)": "walks", "Strikeouts (Batter)": "batter_ks", "Stolen Bases": "stolen_bases",
    "Strikeouts Thrown": "strikeouts",
}
DRAFTKINGS_STATS = {   # two-way subcategory names
    "Hits O/U": "hits",
    "Total Bases O/U": "total_bases",
    "Hits + Runs + RBIs O/U": "hits_runs_rbis",
    "Runs O/U": "runs",
    "RBIs O/U": "rbis",
    "Singles O/U": "singles",
    "Doubles O/U": "doubles",
    "Walks (Batter) O/U": "walks",
    "Stolen Bases O/U": "stolen_bases",
    "Strikeouts Thrown O/U": "strikeouts",
    "Outs Recorded O/U": "outs",
    "Hits Allowed O/U": "hits_allowed",
    "Earned Runs Allowed O/U": "earned_runs",
    "Walks Allowed O/U": "walks_allowed",
}

KAMBI_PATH = "baseball/mlb"
# Kambi criteria with the count as N and without the starting-lineup note (kambi.criterion)
KAMBI_STATS = {
    "Total Hits by the Player - Including Extra Innings": "hits",
    "N+ Hits by the Player - Including Extra Innings": "hits",
    "N+ Bases Recorded by the Player - Including Extra Innings": "total_bases",
    "N+ Hits, Runs & RBIs Recorded by the Player - Including Extra Innings": "hits_runs_rbis",
    "N+ Runs Scored by the Player - Including Extra Innings": "runs",
    "Total RBI by the Player - Including Extra Innings": "rbis",
    "N+ RBIs by the Player - Including Extra Innings": "rbis",
    "Player to Hit a Home Run - Including Extra Innings": "home_runs",
    "Player to hit N or more Home Runs - Including Extra Innings": "home_runs",
    "N+ Doubles by the Player - Including Extra Innings": "doubles",
    "Total Stolen Bases by the Player - Including Extra Innings": "stolen_bases",
    "Total Strikeouts by the Player - Including Extra Innings": "strikeouts",   # pitchers: the probable starters
    "N+ Strikeouts thrown by the Player - Including Extra Innings": "strikeouts",
    "Total Outs Recorded by the Player - Including Extra Innings": "outs",
}

NOVIG_LEAGUE = "MLB"
NOVIG_STATS = {   # Novig market types
    "HITS": "hits", "TOTAL_BASES": "total_bases", "HITS_RUNS_RBIS": "hits_runs_rbis", "RUNS": "runs", "RBIS": "rbis",
    "HOME_RUNS": "home_runs", "STOLEN_BASES": "stolen_bases", "BATTING_STRIKEOUTS": "batter_ks", "BATTING_WALKS": "walks",
    "PITCHER_STRIKEOUTS": "strikeouts", "PITCHER_OUTS": "outs", "HITS_ALLOWED": "hits_allowed",
    "EARNED_RUNS": "earned_runs", "WALKS": "walks_allowed",
}

BOVADA_PATH = "baseball/mlb"
# Bovada market names, before " - Player (TEAM)" or with the count as N (bovada.parse)
BOVADA_STATS = {
    "Total Bases": "total_bases", "To Record N+ Total Bases": "total_bases",
    "Total Hits, Runs and RBIs": "hits_runs_rbis", "Total Doubles": "doubles",
    "Player to record a Hit": "hits", "Player to record N+ Hits": "hits", "To Record N+ Hits": "hits",
    "Player to record a Run": "runs", "Player to record N+ Runs": "runs",
    "Player to record an RBI": "rbis", "Player to record N+ RBIs": "rbis",
    "Player to hit a Home Run": "home_runs", "Player to hit N+ Home Runs": "home_runs",
    "Player to record a Single": "singles",
    "Player to record a Stolen Base": "stolen_bases", "To Record N+ Stolen Bases": "stolen_bases",
    "Total Strikeouts": "strikeouts", "Alternate Strikeouts": "strikeouts",
    "Total Pitcher Outs": "outs", "Total Hits Allowed": "hits_allowed", "Total Pitcher Walks": "walks_allowed",
}

MISE_LEAGUE = "597"
# Mise-o-jeu group codes; MLB props there are all one-way ladders ("At least N")
MISE_STATS = {
    "PLAYER_UNDEFINED_TOTAL_HITS": "hits",
    "PLAYER_UNDEFINED_TOTAL_BASES": "total_bases",
    "PLAYER_UNDEFINED_TOTAL_RUNS_BATTED_IN": "rbis",
    "PLAYER_UNDEFINED_TOTAL_HOME_RUNS": "home_runs",
    "PLAYER_UNDEFINED_TOTAL_SINGLES": "singles",
    "PLAYER_UNDEFINED_TOTAL_DOUBLES": "doubles",
    "PLAYER_UNDEFINED_TOTAL_STOLEN_BASES": "stolen_bases",
    "PLAYER_UNDEFINED_TOTAL_BATTERS_STRUCK_OUT": "strikeouts",
}

PROPS = {
    "hits": ("Hits", "H"),
    "total_bases": ("Total Bases", "TB"),
    "hits_runs_rbis": ("Hits + Runs + RBIs", "H+R+RBI"),
    "runs": ("Runs", "R"),
    "rbis": ("RBIs", "RBI"),
    "home_runs": ("Home Runs", "HR"),
    "singles": ("Singles", "1B"),
    "doubles": ("Doubles", "2B"),
    "walks": ("Walks", "BB"),
    "batter_ks": ("Batter Strikeouts", "SO"),
    "stolen_bases": ("Stolen Bases", "SB"),
    "strikeouts": ("Strikeouts", "K"),
    "outs": ("Pitching Outs", "OUTS"),
    "hits_allowed": ("Hits Allowed", "HA"),
    "earned_runs": ("Earned Runs", "ER"),
    "walks_allowed": ("Walks Allowed", "BB"),
}
TABS = {
    "hits": "Hits", "total_bases": "TB", "hits_runs_rbis": "H+R+RBI", "runs": "Runs", "rbis": "RBI",
    "home_runs": "HR", "singles": "1B", "doubles": "2B", "walks": "BB", "batter_ks": "Bat K",
    "stolen_bases": "SB", "strikeouts": "Pitch K", "outs": "Outs", "hits_allowed": "H Allowed",
    "earned_runs": "ER", "walks_allowed": "BB Allowed",
}
LABELS = {
    "order": ("Lineup spot", "BAT"),
    "pa": ("Plate appearances", "PA"),
    "pitches": ("Pitches", "PC"),
    "batters_faced": ("Batters faced", "BF"),
    "decision": ("Decision", "Dec"),
}
CLOCK: list[str] = []

_HITTER = ("hitter", "H")
POSITIONS = {
    "C": ("C", *_HITTER),
    "1B": ("1B", *_HITTER),
    "2B": ("2B", *_HITTER),
    "3B": ("3B", *_HITTER),
    "SS": ("SS", *_HITTER),
    "LF": ("LF", *_HITTER),
    "CF": ("CF", *_HITTER),
    "RF": ("RF", *_HITTER),
    "DH": ("DH", *_HITTER),
    "PH": ("PH", *_HITTER),
    "PR": ("PR", *_HITTER),
    "TWP": ("TWP", "twoway", "H"),   # Ohtani: his hitting counts with the hitters
    "P": ("SP", "pitcher", "P"),      # Underdog lists starters as SP; relievers never count toward a prop
}
_HITTING = {
    "picks": ["hits", "total_bases", "hits_runs_rbis", "runs", "rbis", "home_runs", "singles", "doubles",
              "walks", "batter_ks", "stolen_bases"],
    "log": ["order", "pa", "hits", "total_bases", "runs", "rbis", "home_runs", "walks", "batter_ks", "stolen_bases"],
    "minis": ["pa", "order"],
    "typical": ["hits", "total_bases", "hits_runs_rbis", "runs", "rbis", "home_runs", "singles", "walks",
                "batter_ks", "stolen_bases"],
}
_PITCHING = {
    "picks": ["strikeouts", "outs", "hits_allowed", "earned_runs", "walks_allowed", "pitches", "batters_faced"],
    "log": ["decision", "outs", "pitches", "strikeouts", "hits_allowed", "earned_runs", "walks_allowed"],
    "minis": ["pitches", "batters_faced"],
    "typical": ["strikeouts", "outs", "hits_allowed", "earned_runs", "walks_allowed"],
}
KINDS = {
    "hitter": {**_HITTING, "active": 3},
    # a starter pitches every fifth day; rosters() already narrows a team's pitchers to tonight's
    "pitcher": {**_PITCHING, "active": 10},
    # his pitching lines come from Underdog on the days he starts; a typical one would list him every day
    "twoway": {part: _HITTING[part] + _PITCHING[part] for part in ("picks", "log")}
              | {"minis": ["pa", "pitches"], "typical": _HITTING["typical"], "dvp": _HITTING["picks"], "active": 3},
}
GROUPS = {
    "H": ("hitters", True),    # "hitters facing NYY average 8.1 hits a game": the whole lineup
    "P": ("pitchers", True),   # the whole staff, starter and bullpen
}

REST = False          # nearly every day; the team's rest days say little
SLATE_DAYS = 1
ACTIVE_GAMES = 3      # hitters; KINDS sets each kind's own
TYPICAL_GAMES = 20
MIN_AVERAGE: dict[str, float] = {}


def samples(df: pl.DataFrame) -> pl.DataFrame:
    """Games that count toward a prop: a hitter in the starting lineup, a pitcher's starts.
    A two-way player's start on the mound counts for both."""
    return df.filter(pl.col("order").is_not_null() | pl.col("started").fill_null(False))


def lineups(df: pl.DataFrame) -> pl.DataFrame:
    """Who counts as having played with his teammates: the same, the starting lineup and starter."""
    return samples(df)


def headshots(index: pl.DataFrame) -> list[str]:
    return [f"https://img.mlbstatic.com/mlb-photos/image/upload/d_people:generic:headshot:67:current.png"
            f"/w_120,q_auto:best/v1/people/{pid}/headshot/67/current" for pid in index["player_id"]]


def refresh(store, days: int = 3):
    """Pull every finished game since the last stored date, re-reading `days` earlier days
    for scoring changes. Today's slate is left out until every game in it is final or it
    rolls over at 5 AM."""
    today = fetch_board_date(schedule)
    end = today - dt.timedelta(days=1)
    last = store.last_date()
    first = season_bounds(season_id(today))[0]
    start = first if last is None or last < first else last - dt.timedelta(days=days)
    df = player_games(start, end) if start <= end else pl.DataFrame(schema=SCHEMA)
    files = store.write_days(df)
    yield f"{df.height} player-games from {start} to {end} -> {files} daily files"
    for season in store.compact_finished():
        yield f"compacted finished season {season}"


def backfill(store, seasons: list[int]):
    for season in seasons:
        df = player_games(*season_bounds(season)).filter(pl.col("season") == season)
        if df.is_empty():
            yield f"no games found for {season}"
            continue
        store.write_season(season, df)
        yield (f"{season}: {df['game_id'].n_unique()} games, {df.height} player-games "
               f"({df['date'].min()} to {df['date'].max()})")
