"""NBA: box scores from ESPN's open site API, plus the schedule and rosters.

ESPN's scoreboard lists a day's games and its summary carries each game's box
score, so a day costs one request plus one per game (a season about 1,500,
read a few at a time). The NBA's own stats.nba.com hangs for scripted requests
and cdn.nba.com refuses them, so neither is used.

Preseason games are left out of the logs: starters play a half and the rest
are camp invites, which would only muddy a hit rate. They still make the slate,
so Underdog's preseason lines show against last season's games.
"""
import datetime as dt
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import polars as pl

from ..slate import fetch_board_date, slate_date

KEY, NAME, PAGE = "nba", "NBA", "nba.html"

SITE_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
WORKERS = 6

# ESPN's team codes -> the NBA's own, which Underdog and the books use
_TEAMS = {"GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP", "UTAH": "UTA", "WSH": "WAS"}
_ESPN = {ours: theirs for theirs, ours in _TEAMS.items()}
# ESPN season types: 1 preseason, 2 regular season, 3 playoffs, 5 play-in (counted with the playoffs)
_GAME_TYPES = {2: 2, 3: 3, 5: 3}

# ESPN box score key -> column; "made-attempted" pairs fill two
_BOX = {
    "minutes": "minutes", "points": "points", "rebounds": "rebounds", "assists": "assists",
    "turnovers": "turnovers", "steals": "steals", "blocks": "blocks",
    "offensiveRebounds": "oreb", "defensiveRebounds": "dreb", "fouls": "fouls", "plusMinus": "plus_minus",
}
_PAIRS = {
    "fieldGoalsMade-fieldGoalsAttempted": ("fgm", "fga"),
    "threePointFieldGoalsMade-threePointFieldGoalsAttempted": ("threes", "threes_att"),
    "freeThrowsMade-freeThrowsAttempted": ("ftm", "fta"),
}
BOX_STATS = [*_BOX.values(), *(c for pair in _PAIRS.values() for c in pair)]
_DOUBLE = [pl.col(c) >= 10 for c in ("points", "rebounds", "assists", "steals", "blocks")]
COMBINED = {
    "pts_rebs_asts": pl.col("points") + pl.col("rebounds") + pl.col("assists"),
    "pts_rebs": pl.col("points") + pl.col("rebounds"),
    "pts_asts": pl.col("points") + pl.col("assists"),
    "rebs_asts": pl.col("rebounds") + pl.col("assists"),
    "blks_stls": pl.col("blocks") + pl.col("steals"),
    "double_double": (pl.sum_horizontal(_DOUBLE) >= 2).cast(pl.Int16),
    "triple_double": (pl.sum_horizontal(_DOUBLE) >= 3).cast(pl.Int16),
}
STATS = [*BOX_STATS, *COMBINED]

# One row per player per game played (a DNP has no row).
SCHEMA = {
    "game_id": pl.String, "date": pl.Date, "season": pl.Int32, "game_type": pl.Int8,
    "player_id": pl.String, "name": pl.String, "team": pl.String, "opponent": pl.String,
    "home": pl.Boolean, "position": pl.String, "started": pl.Boolean,
    **{stat: pl.Int16 for stat in STATS},
}


def _client() -> httpx.Client:
    return httpx.Client(timeout=60, headers=HEADERS, follow_redirects=True,
                        transport=httpx.HTTPTransport(retries=3))


def _get(http: httpx.Client, path: str, params: dict | None = None) -> dict:
    for attempt in range(4):
        r = http.get(f"{SITE_URL}/{path}", params=params)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        break
    r.raise_for_status()
    return r.json()


def _team(code: str) -> str:
    return _TEAMS.get(code, code)


def _games_on(http: httpx.Client, day: dt.date) -> list[dict]:
    """A day's games from ESPN's scoreboard: id, start, teams, ESPN season type, state."""
    out = []
    for e in _get(http, "scoreboard", {"dates": day.strftime("%Y%m%d")}).get("events", []):
        sides = {c.get("homeAway"): _team(c.get("team", {}).get("abbreviation", "")) for c in e["competitions"][0]["competitors"]}
        if set(sides) != {"home", "away"} or not set(sides.values()) <= TEAMS:   # the All-Star game and its like
            continue
        out.append({"id": e["id"], "date": str(day), "start": e["date"], "away": sides["away"], "home": sides["home"],
                    "type": e["season"]["type"],
                    "state": {"post": "final", "pre": "upcoming"}.get(e["status"]["type"]["state"], "live")})
    return out


def _box(http: httpx.Client, game: dict) -> list[dict]:
    """Every player who played in a game, one dict per player in SCHEMA's terms."""
    rows = []
    for side in _get(http, "summary", {"event": game["id"]}).get("boxscore", {}).get("players", []):
        team = _team(side["team"]["abbreviation"])
        home = team == game["home"]
        for block in side.get("statistics", []):
            keys = block.get("keys", [])
            for a in block.get("athletes", []):
                if a.get("didNotPlay") or len(a.get("stats") or []) != len(keys) or "id" not in a.get("athlete", {}):
                    continue
                row = {"game_id": game["id"], "date": game["date"], "game_type": _GAME_TYPES[game["type"]],
                       "player_id": a["athlete"]["id"], "name": a["athlete"]["displayName"],
                       "position": (a["athlete"].get("position") or {}).get("abbreviation"),
                       "team": team, "opponent": game["away"] if home else game["home"], "home": home,
                       "started": bool(a.get("starter"))}
                for key, value in zip(keys, a["stats"]):
                    if key in _BOX:
                        row[_BOX[key]] = int(value.lstrip("+")) if value.lstrip("+-").isdigit() else None
                    elif key in _PAIRS:
                        made, _, tried = value.partition("-")
                        row.update(zip(_PAIRS[key], (int(made), int(tried)) if made.isdigit() and tried.isdigit() else (None, None)))
                if row.get("minutes"):   # listed with no minutes: never got in
                    rows.append(row)
    return rows


def player_games(start: dt.date, end: dt.date, http: httpx.Client | None = None) -> pl.DataFrame:
    """Every regular-season, play-in and playoff player game between start and end (inclusive), in SCHEMA."""
    http = http or _client()
    days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        games = [g for day in pool.map(lambda d: _games_on(http, d), days) for g in day
                 if g["type"] in _GAME_TYPES and g["state"] == "final"]
        rows = [row for box in pool.map(lambda g: _box(http, g), games) for row in box]
    if not rows:
        return pl.DataFrame(schema=SCHEMA)
    df = pl.from_dicts(rows, infer_schema_length=None)
    df = df.with_columns(
        pl.col("date").str.to_date(),
        **{c: pl.col(c) if c in df.columns else pl.lit(None) for c in BOX_STATS},
    ).with_columns(
        season=(pl.col("date").dt.year() - (pl.col("date").dt.month() < 8).cast(pl.Int32)) * 10_001 + 1,
        **COMBINED,
    )
    return (df.select(pl.col(c).cast(t) for c, t in SCHEMA.items())
            .unique(["game_id", "player_id"]).sort("date", "game_id", "team", "player_id"))


def season_id(day: dt.date) -> int:
    """Season a date falls in, e.g. 2026-10-21 -> 20262027."""
    year = day.year if day.month >= 8 else day.year - 1
    return year * 10_000 + year + 1


def season_bounds(season: int) -> tuple[dt.date, dt.date]:
    """Generous date range for a season id like 20252026; preseason days cost a request but add no games."""
    year = season // 10_000
    return dt.date(year, 10, 1), min(dt.date(year + 1, 6, 30), dt.date.today())


def previous_season(season: int) -> int:
    return season - 10_001


def season_label(season: int) -> str:
    return f"{str(season)[2:4]}/{str(season)[6:8]}"


def schedule(day: dt.date, http: httpx.Client | None = None) -> list[dict]:
    """Games on the slate starting at `day`, preseason included: Underdog posts lines for those too."""
    http = http or _client()
    return [{key: g[key] for key in ("date", "start", "away", "home", "state")}
            for d in range(SLATE_DAYS) for g in _games_on(http, day + dt.timedelta(days=d))]


def rosters(teams: list[str], today: dt.date, http: httpx.Client | None = None) -> dict[str, list[str]]:
    """ESPN player ids on each team's current roster."""
    http = http or _client()

    def one(team: str) -> list[str]:
        data = _get(http, f"teams/{_ESPN.get(team, team).lower()}/roster")
        return [str(p["id"]) for p in data.get("athletes", [])]

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return dict(zip(teams, pool.map(one, teams)))


# ============================================================================
# What the board needs to know about basketball
# ============================================================================

TEAMS = {"ATL", "BKN", "BOS", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW", "HOU", "IND", "LAC", "LAL", "MEM",
         "MIA", "MIL", "MIN", "NOP", "NYK", "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS"}

UNDERDOG_SPORT = "NBA"
UNDERDOG_STATS = {   # as seen on Underdog; quarter props ("period_1_points") and season-long specials aren't game-log stats
    "points": "points",
    "rebounds": "rebounds",
    "assists": "assists",
    "three_points_made": "threes",
    "three_points_att": "threes_att",
    "field_goals_att": "fga",
    "pts_rebs_asts": "pts_rebs_asts",
    "pts_rebs": "pts_rebs",
    "pts_asts": "pts_asts",
    "rebs_asts": "rebs_asts",
    "steals": "steals",
    "blocks": "blocks",
    "blks_stls": "blks_stls",
    "turnovers": "turnovers",
    "double_doubles": "double_double",   # unconfirmed: "triple_doubles" was seen, this is its pair
    "triple_doubles": "triple_double",
}
TEAM_ALIASES = {"GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP", "UTAH": "UTA", "WSH": "WAS", "BRK": "BKN",
                "PHO": "PHX", "CHO": "CHA"}

PINNACLE_LEAGUE = 487
PINNACLE_STATS = {   # "<Player> Total <units>"; seen before opening night
    "Points": "points",
    "Rebounds": "rebounds",
    "Assists": "assists",
    "Threes Made": "threes",
    "Pts & Rebs & Asts": "pts_rebs_asts",
}
FANDUEL_PAGE = "nba"
FANDUEL_TABS = ["player-points", "player-assists", "player-rebounds", "player-combos", "player-threes"]
FANDUEL_STATS = {
    "Points": "points",
    "Assists": "assists",
    "Rebounds": "rebounds",
    "Made Threes": "threes",
    "Pts + Reb + Ast": "pts_rebs_asts",
    "Pts + Reb": "pts_rebs",
    "Pts + Ast": "pts_asts",
    "Reb + Ast": "rebs_asts",
}
# One-way ladders: "To Score 20+ Points", "3+ Made Threes", "To Record 20+ Pts + Ast", "To Record A Double Double"
FANDUEL_LADDERS = {**FANDUEL_STATS, "Double Double": "double_double", "Triple Double": "triple_double"}
DRAFTKINGS_LEAGUE = 42648
DRAFTKINGS_CATEGORIES = ["Player Points", "Player Rebounds", "Player Assists", "Player Threes", "Player Combos",
                         "Player Defense"]
DRAFTKINGS_STATS = {   # two-way subcategory names
    "Points O/U": "points", "Rebounds O/U": "rebounds", "Assists O/U": "assists", "Threes O/U": "threes",
    "Pts + Reb + Ast O/U": "pts_rebs_asts", "Pts + Reb O/U": "pts_rebs", "Pts + Ast O/U": "pts_asts",
    "Ast + Reb O/U": "rebs_asts", "Steals O/U": "steals", "Blocks O/U": "blocks",
}
DRAFTKINGS_LADDERS: dict[str, str] = {}

KAMBI_PATH = "basketball/nba"
KAMBI_STATS: dict[str, str] = {}   # no NBA player offers in the preseason; the build lists them once they post

NOVIG_LEAGUE = "NBA"
NOVIG_STATS = {
    "POINTS": "points", "REBOUNDS": "rebounds", "ASSISTS": "assists", "THREE_POINTERS_MADE": "threes",
    "REBOUNDS_ASSISTS": "rebs_asts", "POINTS_REBOUNDS_ASSISTS": "pts_rebs_asts", "TRIPLE_DOUBLE": "triple_double",
    "BLOCKS": "blocks",
}

POLYMARKET_SPORT = "nba"
POLYMARKET_STATS = {"points": "points", "rebounds": "rebounds", "assists": "assists"}   # "Jayson Tatum: Points O/U 26.5"
KALSHI_GAME, KALSHI_TOTAL = "KXNBAGAME", "KXNBATOTAL"
KALSHI_STATS = {   # Kalshi series; none had markets up in the preseason, so unconfirmed
    "KXNBAPTS": "points", "KXNBAREB": "rebounds", "KXNBAAST": "assists", "KXNBA3PT": "threes",
    "KXNBAPRA": "pts_rebs_asts", "KXNBAPR": "pts_rebs", "KXNBAPA": "pts_asts", "KXNBARA": "rebs_asts",
    "KXNBASTL": "steals", "KXNBABLK": "blocks", "KXNBASTOCK": "blks_stls",
    "KXNBA2D": "double_double", "KXNBA3D": "triple_double",
}

BOVADA_PATH = "basketball/nba"
BOVADA_STATS: dict[str, str] = {}   # game lines only in the preseason

MISE_LEAGUE = "27726"   # "NBA Preseason"; the regular season may carry another tag
MISE_STATS: dict[str, str] = {}

BETANO_PATH = "/sport/basketball/north-america/nba/441g/"   # its league page on betano.ca

STAKE_SPORT = 'basketball'     # its sport slug on stake.com
PICK6_SPORT = 'NBA'     # its sport code on pick6.draftkings.com
BETANO_STATS = {
    "Points": "points", "Rebounds": "rebounds", "Assists": "assists",
    "Points+Rebounds+Assists": "pts_rebs_asts", "Rebounds+Assists": "rebs_asts",
    "Points+Rebounds": "pts_rebs", "Points+Assists": "pts_asts",
    "Three Point Shots Scored": "threes", "Double-Double": "double_double",
    "Triple-Double": "triple_double",
}
BETANO_FIRST = {}

STAKE_STATS = {
    "Points": "points", "Rebounds": "rebounds", "Assists": "assists",
    "Three Pointers Made": "threes", "Points+Rebounds+Assists": "pts_rebs_asts",
    "Steals": "steals", "Blocks": "blocks",
}
PICK6_STATS = {
    "Points": "points", "Rebounds": "rebounds", "Assists": "assists",
    "Three Pointers Made": "threes",
}

PROPS = {
    "points": ("Points", "PTS"),
    "rebounds": ("Rebounds", "REB"),
    "assists": ("Assists", "AST"),
    "threes": ("3-Pointers Made", "3PM"),
    "pts_rebs_asts": ("Pts + Rebs + Asts", "PRA"),
    "pts_rebs": ("Pts + Rebs", "P+R"),
    "pts_asts": ("Pts + Asts", "P+A"),
    "rebs_asts": ("Rebs + Asts", "R+A"),
    "steals": ("Steals", "STL"),
    "blocks": ("Blocks", "BLK"),
    "blks_stls": ("Blocks + Steals", "B+S"),
    "turnovers": ("Turnovers", "TO"),
    "threes_att": ("3-Pointers Attempted", "3PA"),
    "fga": ("FG Attempted", "FGA"),
    "double_double": ("Double Double", "DD"),
    "triple_double": ("Triple Double", "TD"),
}
TABS = {
    "points": "Pts", "rebounds": "Reb", "assists": "Ast", "threes": "3PM", "pts_rebs_asts": "PRA",
    "pts_rebs": "P+R", "pts_asts": "P+A", "rebs_asts": "R+A", "steals": "Stl", "blocks": "Blk",
    "blks_stls": "B+S", "turnovers": "TO", "threes_att": "3PA", "fga": "FGA", "double_double": "DD",
    "triple_double": "TD",
}
LABELS = {
    "minutes": ("Minutes", "MIN"),
    "plus_minus": ("Plus/Minus", "+/-"),
    "fta": ("FT Attempted", "FTA"),
}
CLOCK: list[str] = []

POSITIONS = {   # ESPN lists most players as G, F or C
    "PG": ("PG", "player", "G"),
    "SG": ("SG", "player", "G"),
    "G": ("G", "player", "G"),
    "SF": ("SF", "player", "F"),
    "PF": ("PF", "player", "F"),
    "F": ("F", "player", "F"),
    "C": ("C", "player", "C"),
}
_TYPICAL = ["points", "rebounds", "assists", "threes", "pts_rebs_asts", "pts_rebs", "pts_asts", "rebs_asts",
            "steals", "blocks", "blks_stls", "turnovers", "double_double"]
KINDS = {
    "player": {
        "picks": [*PROPS],
        "log": ["minutes", "points", "rebounds", "assists", "threes", "steals", "blocks", "turnovers", "fga",
                "threes_att", "fta", "plus_minus"],
        "minis": ["minutes", "fga"],
        "typical": _TYPICAL,
        "dvp": _TYPICAL,
    },
}
GROUPS = {
    "G": ("guards", False),
    "F": ("forwards", False),
    "C": ("centers", False),
}

REST = True            # back-to-backs matter
SLATE_DAYS = 1
ACTIVE_GAMES = 5
TYPICAL_GAMES = 20
# A typical line is only worth a row for a player with a real role.
MIN_AVERAGE = {"points": 6, "pts_rebs_asts": 10, "pts_rebs": 8, "pts_asts": 8}


def samples(df: pl.DataFrame) -> pl.DataFrame:
    """Every game he got into: only players who logged minutes are stored."""
    return df


def lineups(df: pl.DataFrame) -> pl.DataFrame:
    return df


def headshots(index: pl.DataFrame) -> list[str]:
    return [f"https://a.espncdn.com/i/headshots/nba/players/full/{pid}.png" for pid in index["player_id"]]


def refresh(store, days: int = 3):
    """Pull every finished day since the last stored date, re-reading `days` earlier days for
    stat corrections. Today's games wait until every one is final or the slate rolls over at 5 AM."""
    today = fetch_board_date(schedule)
    end = today - dt.timedelta(days=1)
    last = store.last_date()
    opener = season_bounds(season_id(today))[0]
    # a new season starts from its first day, not from last June's finals
    start = opener if last is None or last < opener else last - dt.timedelta(days=days)
    if start > end:
        yield "nothing new"
        return
    df = player_games(start, end)
    files = store.write_days(df)
    yield f"{df.height} player-games from {start} to {end} -> {files} daily files"
    for season in store.compact_finished():
        yield f"compacted finished season {season}"


def backfill(store, seasons: list[int]):
    http = _client()
    for season in seasons:
        start, end = season_bounds(season)
        df = player_games(start, min(end, slate_date() - dt.timedelta(days=1)), http).filter(pl.col("season") == season)
        if df.is_empty():
            yield f"no games found for {season}"
            continue
        store.write_season(season, df)
        yield (f"{season}: {df['game_id'].n_unique()} games, {df.height} player-games "
               f"({df['date'].min()} to {df['date'].max()})")
