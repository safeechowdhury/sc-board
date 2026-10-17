"""NHL: per-game player stats from the stats REST API, plus the schedule and rosters.

The stats API returns official per-game numbers for every player in a date
range, so a whole day costs five requests (four skater reports + goalies)
no matter how many games were played.
"""
import datetime as dt
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import polars as pl

from ..slate import fetch_board_date

KEY, NAME, PAGE = "nhl", "NHL", "index.html"

STATS_URL = "https://api.nhle.com/stats/rest/en"
SCHEDULE_URL = "https://api-web.nhle.com/v1/schedule"
ROSTER_URL = "https://api-web.nhle.com/v1/roster"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

ROW_CAP = 10_000    # the stats API silently truncates a response at this many rows
WINDOW_DAYS = 14    # ~4k skater rows at the busiest point of a season
GAME_TYPES = (2, 3)  # regular season, playoffs

_IDS = {
    "gameId": "game_id", "gameDate": "date", "playerId": "player_id",
    "teamAbbrev": "team", "opponentTeamAbbrev": "opponent", "homeRoad": "home",
}
# report -> {api field: column}
SKATER_REPORTS = {
    "skater/summary": {
        **_IDS, "skaterFullName": "name", "positionCode": "position",
        "timeOnIcePerGame": "toi", "goals": "goals", "assists": "assists", "points": "points",
        "shots": "shots", "plusMinus": "plus_minus", "penaltyMinutes": "pim",
        "ppGoals": "ppg", "ppPoints": "ppp",
    },
    "skater/realtime": {
        "gameId": "game_id", "playerId": "player_id",
        "hits": "hits", "blockedShots": "blocked_shots", "giveaways": "giveaways",
        "takeaways": "takeaways", "missedShots": "missed_shots",
        "totalShotAttempts": "shot_attempts", "firstGoals": "first_goals",
    },
    "skater/faceoffwins": {
        "gameId": "game_id", "playerId": "player_id",
        "totalFaceoffWins": "faceoff_wins", "totalFaceoffLosses": "faceoff_losses",
    },
    "skater/timeonice": {
        "gameId": "game_id", "playerId": "player_id",
        "shifts": "shifts", "ppTimeOnIce": "pp_toi",
    },
}
GOALIE_REPORT = {
    "goalie/summary": {
        **_IDS, "goalieFullName": "name", "timeOnIce": "toi",
        "saves": "saves", "shotsAgainst": "shots_against", "goalsAgainst": "goals_against",
        "gamesStarted": "started", "wins": "wins", "losses": "losses", "otLosses": "ot_losses",
    },
}

SKATER_STATS = [
    "goals", "assists", "points", "shots", "plus_minus", "pim", "ppg", "ppp", "hits",
    "blocked_shots", "giveaways", "takeaways", "missed_shots", "shot_attempts",
    "first_goals", "faceoff_wins", "faceoff_losses", "shifts",
]
GOALIE_STATS = ["saves", "shots_against", "goals_against"]

# One row per player per game. Skater columns are null for goalies and vice versa.
SCHEMA = {
    "game_id": pl.Int64, "date": pl.Date, "season": pl.Int32, "game_type": pl.Int8,
    "player_id": pl.Int32, "name": pl.String, "team": pl.String, "opponent": pl.String,
    "home": pl.Boolean, "position": pl.String, "toi": pl.Int32, "pp_toi": pl.Int32,
    **{c: pl.Int16 for c in SKATER_STATS},
    **{c: pl.Int16 for c in GOALIE_STATS},
    "started": pl.Boolean, "decision": pl.String,
}


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


def _report(http: httpx.Client, report: str, start: dt.date, end: dt.date) -> list[dict]:
    exp = (f'gameDate>="{start}" and gameDate<="{end} 23:59:59" '
           f"and gameTypeId in ({','.join(map(str, GAME_TYPES))})")
    rows = _get(http, f"{STATS_URL}/{report}", {
        "isAggregate": "false", "isGame": "true", "start": 0, "limit": -1, "cayenneExp": exp,
    })["data"]
    if len(rows) < ROW_CAP:
        return rows
    if start == end:
        raise RuntimeError(f"{report} returned {len(rows)} rows for {start}; cannot split further")
    mid = start + (end - start) // 2
    return _report(http, report, start, mid) + _report(http, report, mid + dt.timedelta(days=1), end)


def _windows(start: dt.date, end: dt.date):
    while start <= end:
        stop = min(start + dt.timedelta(days=WINDOW_DAYS - 1), end)
        yield start, stop
        start = stop + dt.timedelta(days=1)


def _frame(rows: list[dict], fields: dict[str, str]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=list(fields.values()))
    return pl.from_dicts(rows, infer_schema_length=None).select(
        pl.col(api).alias(col) for api, col in fields.items()
    )


def player_games(start: dt.date, end: dt.date, http: httpx.Client | None = None) -> pl.DataFrame:
    """Every skater and goalie game between start and end (inclusive), in SCHEMA."""
    http = http or client()
    reports = {**SKATER_REPORTS, **GOALIE_REPORT}
    jobs = [(report, a, b) for a, b in _windows(start, end) for report in reports]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = pool.map(lambda job: _report(http, *job), jobs)
    rows: dict[str, list[dict]] = {report: [] for report in reports}
    for (report, _, _), chunk in zip(jobs, results):
        rows[report] += chunk

    if not rows["skater/summary"] and not rows["goalie/summary"]:
        return pl.DataFrame(schema=SCHEMA)
    skaters = _frame(rows["skater/summary"], SKATER_REPORTS["skater/summary"])
    for report in list(SKATER_REPORTS)[1:]:
        skaters = skaters.join(_frame(rows[report], SKATER_REPORTS[report]),
                               on=["game_id", "player_id"], how="left")
    goalies = _frame(rows["goalie/summary"], GOALIE_REPORT["goalie/summary"]).with_columns(
        position=pl.lit("G"),
        started=pl.col("started") > 0,
        decision=pl.when(pl.col("wins") > 0).then(pl.lit("W"))
                   .when(pl.col("losses") > 0).then(pl.lit("L"))
                   .when(pl.col("ot_losses") > 0).then(pl.lit("O")),
    )
    df = pl.concat([skaters, goalies], how="diagonal_relaxed")
    start_year = pl.col("game_id") // 1_000_000
    return df.with_columns(
        date=pl.col("date").str.to_date(),
        home=pl.col("home") == "H",
        season=start_year * 10_000 + start_year + 1,
        game_type=pl.col("game_id") // 10_000 % 100,
    ).select(
        (pl.col(c) if c in df.columns or c in ("season", "game_type") else pl.lit(None)).cast(t).alias(c)
        for c, t in SCHEMA.items()
    ).unique(["game_id", "player_id"]).sort("date", "game_id", "team", "player_id")


def season_id(day: dt.date) -> int:
    """Season a date falls in, e.g. 2026-10-02 -> 20262027."""
    year = day.year if day.month >= 9 else day.year - 1
    return year * 10_000 + year + 1


def season_bounds(season: int) -> tuple[dt.date, dt.date]:
    """Generous date range for a season id like 20252026; the caller filters by season."""
    year = season // 10_000
    return dt.date(year, 9, 1), min(dt.date(year + 1, 8, 31), dt.date.today())


def schedule(day: dt.date, http: httpx.Client | None = None) -> list[dict]:
    """Regular-season and playoff games for the seven days starting at `day`."""
    data = _get(http or client(), f"{SCHEDULE_URL}/{day}")
    return [
        {
            "game_id": g["id"], "date": week["date"], "start": g["startTimeUTC"],
            "away": g["awayTeam"]["abbrev"], "home": g["homeTeam"]["abbrev"],
            "state": {"OFF": "final", "FINAL": "final", "FUT": "upcoming", "PRE": "upcoming"}.get(g["gameState"], "live"),
        }
        for week in data.get("gameWeek", [])
        for g in week["games"]
        if g["gameType"] in GAME_TYPES
    ]


def rosters(teams: list[str], today: dt.date, http: httpx.Client | None = None) -> dict[str, list[int]]:
    """Player ids on each team's current roster."""
    http = http or client()

    def one(team: str) -> list[int]:
        data = _get(http, f"{ROSTER_URL}/{team}/current")
        return [p["id"] for group in ("forwards", "defensemen", "goalies") for p in data.get(group, [])]

    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(zip(teams, pool.map(one, teams)))


# ============================================================================
# What the board needs to know about hockey
# ============================================================================

UNDERDOG_SPORT = "NHL"
UNDERDOG_STATS = {
    "shots": "shots",
    "goals": "goals",
    "assists": "assists",
    "points": "points",
    "power_play_points": "ppp",
    "hits": "hits",
    "blocked_shots": "blocked_shots",
    "faceoffs_won": "faceoff_wins",
    "plus_minus": "plus_minus",
    "period_first_goal_scorer": "first_goals",
    "saves": "saves",
    "goals_against": "goals_against",
}
TEAM_ALIASES = {
    "NJ": "NJD", "LA": "LAK", "SJ": "SJS", "TB": "TBL", "WAS": "WSH", "MON": "MTL",
    "CLB": "CBJ", "NAS": "NSH", "CAL": "CGY", "WIN": "WPG", "VEG": "VGK", "LV": "VGK",
    "UTAH": "UTA",
}
PINNACLE_LEAGUE = 1456
# Only "Goals" was posted when this was written (preseason). The rest are Pinnacle's
# likely names; the build lists any it doesn't recognize so they can be added here.
PINNACLE_STATS = {
    "Goals": "goals",
    "Shots On Goal": "shots",
    "Points": "points",
    "Assists": "assists",
    "Power Play Points": "ppp",
    "Blocked Shots": "blocked_shots",
    "Saves": "saves",
}
FANDUEL_PAGE = "nhl"
FANDUEL_TABS = ["shots", "goals", "points-assists", "goalies"]
# Points and assists were one-sided ladders when this was written; the build lists any new names.
FANDUEL_STATS = {
    "Shots on Goal": "shots",
    "Total Goals": "goals",
    "Total Saves": "saves",
}
# One-way ladders: "Player to Record 3+ Shots on Goal", "1+ Points", "2+ Assists", "Anytime Goal Scorer",
# "First Goal Scorer", "Dan Vladar Alt Saves"
FANDUEL_LADDERS = {
    "Shots on Goal": "shots", "Goal": "goals", "Goals": "goals", "Points": "points", "Assists": "assists",
    "First Goal": "first_goals", "Saves": "saves",
}
DRAFTKINGS_LEAGUE = 42133
DRAFTKINGS_CATEGORIES = ["Goalscorer", "Shots on Goal", "Points", "Assists", "Blocks", "Player Props", "Goalie Props"]
DRAFTKINGS_STATS = {   # two-way subcategory names; goals are one-way only there
    "Shots on Goal O/U": "shots",
    "Points O/U": "points",
    "Assists O/U": "assists",
    "Blocks O/U": "blocked_shots",
    "Saves O/U": "saves",
}
DRAFTKINGS_LADDERS = {   # "N+" subcategory names
    "Shots on Goal": "shots", "Points": "points", "Assists": "assists", "Blocks": "blocked_shots",
    "Power Play Points": "ppp", "Saves": "saves",
}
# Field markets, a runner per player, with the count as N. The "Anytime Goalscorer"
# subcategory also holds "2+ Goals" and "First Goalscorer"; "3+ Goals" is its own.
DRAFTKINGS_FIELDS = {"Anytime Goalscorer": "goals", "N+ Goals": "goals", "First Goalscorer": "first_goals"}

KAMBI_PATH = "ice_hockey/nhl"
# Kambi criteria with the count as N (kambi.criterion); NHL props there are yes/no ladders but for shots
KAMBI_STATS = {
    "Player's shots on goal - Including Overtime": "shots",
    "To score - Including Overtime": "goals",
    "To score at least N goals - Including Overtime": "goals",
    "Player to score at least N points - Including Overtime": "points",
    "Player to record at least N assists - Including Overtime": "assists",
    "Player To score at least N Power Play Points -  Including Overtime": "ppp",   # the double space is Kambi's
    "First Goal Scorer - Including Overtime": "first_goals",
}

NOVIG_LEAGUE = "NHL"
NOVIG_STATS = {   # Novig market types
    "PLAYER_GOALS": "goals", "SHOTS_ON_GOAL": "shots", "ASSISTS": "assists", "POINTS": "points",
    "POWER_PLAY_POINTS": "ppp", "SAVES": "saves", "FIRST_GOAL_SCORER": "first_goals",
}

KALSHI_GAME, KALSHI_TOTAL = "KXNHLGAME", "KXNHLTOTAL"
KALSHI_STATS = {   # Kalshi series, each market a rung ("Tom Wilson: 2+ points"); no shots series
    "KXNHLGOAL": "goals", "KXNHLPTS": "points", "KXNHLAST": "assists", "KXNHLSAVE": "saves",
    "KXNHLFIRSTGOAL": "first_goals",
}

BOVADA_PATH = "hockey/nhl"
# Bovada market names, before " - Player (TEAM)" or with the count as N (bovada.parse)
BOVADA_STATS = {
    "Total Shots On Goal": "shots",
    "Total Saves": "saves",
    "Assists Milestones": "assists",
    "Blocks Milestones": "blocked_shots",
    "Player To Record N Or More Points": "points",
    "Player To Record N Or More Powerplay Points": "ppp",
    "Anytime Goalscorer": "goals",
    "Who Will Score N Or More Goals": "goals",
    "Player To Score Nst Goal": "first_goals",   # "Player To Score 1st Goal"
}

MISE_LEAGUE = "574"
# Mise-o-jeu group codes, two-way ("..._OVER_UNDER_PLAYER") and one-way ladders alike
MISE_STATS = {
    "TOTAL_SHOTS_ON_TARGET_OVER/UNDER_PLAYER_INC_OT": "shots",
    "PLAYER_UNDEFINED_TOTAL_SHOTS_ON TARGET_INC_OT": "shots",   # the space is Mise's
    "TOTAL_POINTS_OVER_UNDER_PLAYER": "points",
    "PLAYER_TOTAL_POINTS_UNDEFINED": "points",
    "TOTAL_ASSISTS_OVER_UNDER_PLAYER": "assists",
    "PLAYER_TOTAL_ASSISTS_UNDEFINED": "assists",
    "PLAYER_TOTAL_GOALS": "goals",   # "Player 3+ Goals", a runner per player
    "GOALSCORER_FIRST": "first_goals",   # "First Goalscorer", a runner per player
}
# markets Mise-o-jeu codes unreliably, by how their name ends ("Linus Ullmark Total Saves")
MISE_NAMES = {"Total Saves": "saves"}

BETANO_PATH = "/sport/hockey/north-america/nhl/10118r/"   # its league page on betano.ca

STAKE_SPORT = 'ice-hockey'     # its sport slug on stake.com
PICK6_SPORT = 'NHL'     # its sport code on pick6.draftkings.com
BETANO_STATS = {
    "Shots on Goal": "shots", "Points O/U": "points", "Assists O/U": "assists", "Points": "points",
    "Assists": "assists", "Blocked Shots": "blocked_shots", "Powerplay Points": "ppp",
    "Goal scorer": "goals", "Goal scorer 2+": "goals", "To Score 2+ Goals": "goals",
    "2+ Points": "points", "2+ Assists": "assists",
}
BETANO_FIRST = {"Goal scorer": "first_goals"}

STAKE_STATS = {
    "Shots": "shots", "Goals": "goals", "Assists": "assists", "Points": "points",
    "Powerplay Points": "ppp", "Saves": "saves", "Blocked Shots": "blocked_shots",
    "Hits": "hits", "Plus Minus": "plus_minus", "Goals Against": "goals_against",
}
PICK6_STATS = {
    "Shots on Goal": "shots", "Goals": "goals", "Assists": "assists", "Points": "points",
    "Saves": "saves", "Blocked Shots": "blocked_shots",
}

PROPS = {
    "shots": ("Shots on Goal", "SOG"),
    "goals": ("Goals", "G"),
    "points": ("Points", "PTS"),
    "assists": ("Assists", "A"),
    "ppp": ("Power Play Points", "PPP"),
    "hits": ("Hits", "HIT"),
    "blocked_shots": ("Blocked Shots", "BLK"),
    "faceoff_wins": ("Faceoffs Won", "FOW"),
    "plus_minus": ("Plus/Minus", "+/-"),
    "first_goals": ("First Goal Scorer", "1st G"),
    "saves": ("Saves", "SV"),
    "goals_against": ("Goals Against", "GA"),
}
TABS = {
    "shots": "Shots", "goals": "Goals", "points": "Points", "assists": "Assists", "ppp": "PP Points",
    "hits": "Hits", "blocked_shots": "Blocks", "faceoff_wins": "Faceoffs", "plus_minus": "+/-",
    "first_goals": "First Goal", "saves": "Saves", "goals_against": "Goals Against",
}
LABELS = {
    "shot_attempts": ("Shot Attempts", "SAT"),
    "shots_against": ("Shots Against", "SA"),
    "toi": ("Ice time", "TOI"),
    "pp_toi": ("Power-play time", "PP TOI"),
    "decision": ("Decision", "Dec"),
}
CLOCK = ["toi", "pp_toi"]

POSITIONS = {
    "C": ("C", "skater", "C"),
    "L": ("LW", "skater", "W"),   # wingers swap sides too often to rank defenses against each separately
    "R": ("RW", "skater", "W"),
    "D": ("D", "skater", "D"),
    "G": ("G", "goalie", "G"),
}
_SKATER_PROPS = [stat for stat in PROPS if stat not in GOALIE_STATS]
KINDS = {
    "skater": {
        "picks": [*_SKATER_PROPS, "shot_attempts"],
        "log": ["toi", "shots", "goals", "assists", "points", "ppp", "hits", "blocked_shots",
                "faceoff_wins", "plus_minus", "shot_attempts", "pp_toi"],
        "minis": ["shot_attempts", "toi", "pp_toi"],
        "typical": _SKATER_PROPS,
    },
    "goalie": {
        "picks": ["saves", "goals_against", "shots_against"],
        "log": ["decision", "toi", "saves", "shots_against", "goals_against"],
        "minis": ["shots_against"],
        "typical": ["saves", "goals_against"],
    },
}
GROUPS = {
    "C": ("centers", False),
    "W": ("wingers", False),
    "D": ("defensemen", False),
    "G": ("goalies", True),   # "goalies facing BOS average 27 saves", not "BOS allows 27 saves"
}

REST = True
SLATE_DAYS = 1
ACTIVE_GAMES = 5
TYPICAL_GAMES = 20
MIN_AVERAGE = {"faceoff_wins": 2.0}   # wingers rarely take faceoffs


def previous_season(season: int) -> int:
    return season - 10_001


def season_label(season: int) -> str:
    return f"{str(season)[2:4]}/{str(season)[6:8]}"


def samples(df: pl.DataFrame) -> pl.DataFrame:
    """Every skater game, goalie starts only: a relief appearance says nothing
    about a full-game saves line."""
    return df.filter((pl.col("position") != "G") | pl.col("started"))


def lineups(df: pl.DataFrame) -> pl.DataFrame:
    """Who counts as having played with his teammates: the same games. A goalie
    who started counts however early he was pulled."""
    return samples(df)


def headshots(index: pl.DataFrame) -> list[str]:
    return [f"https://assets.nhle.com/mugs/nhl/{season}/{team}/{pid}.png"
            for pid, team, season in index.select("player_id", "team", "season").iter_rows()]


def refresh(store, days: int = 3):
    """Pull every finished slate since the last stored date, re-reading `days`
    earlier days to pick up the league's stat corrections. Today's slate is
    left out until every game in it is final or it rolls over at 5 AM, so a game
    in progress never counts as a miss."""
    today = fetch_board_date(schedule)
    end = today - dt.timedelta(days=1)
    last = store.last_date()
    start = season_bounds(season_id(today))[0] if last is None else last - dt.timedelta(days=days)
    df = player_games(start, end)
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

ANYTIME_STATS = {"goals"}   # an anytime goal: a book's 2+ goals rung is never its price (books.prices)
