"""NFL: weekly player stats, snap counts and the schedule, from nflverse.

nflverse publishes one file per season for each, refreshed after every slate,
so a whole season is three downloads.

The stat file only has a row for a player who recorded something. A receiver
who played 36 snaps without a target is missing from it, and leaving that game
out would flatter every hit rate. Snap counts say who actually played, so
those games are added back as zeros.
"""
import datetime as dt
import io

import httpx
import polars as pl

from ..slate import ET, fetch_board_date, slate_date

KEY, NAME, PAGE = "nfl", "NFL", "nfl.html"

RELEASES = "https://github.com/nflverse/nflverse-data/releases/download"
HEADERS = {"User-Agent": "Mozilla/5.0"}

# nflverse stat column -> ours
STAT_COLUMNS = {
    "attempts": "pass_att", "completions": "pass_cmp", "passing_yards": "pass_yds",
    "passing_tds": "pass_tds", "passing_interceptions": "pass_int",
    "carries": "rush_att", "rushing_yards": "rush_yds", "rushing_tds": "rush_tds",
    "targets": "targets", "receptions": "receptions", "receiving_yards": "rec_yds",
    "receiving_tds": "rec_tds", "fumbles_lost_total": "fumbles_lost",
    "fg_made": "fg_made", "pat_made": "xp_made",
}
COMBINED = {
    "rush_rec_tds": pl.col("rush_tds") + pl.col("rec_tds"),
    "rush_rec_yds": pl.col("rush_yds") + pl.col("rec_yds"),
    "pass_rush_yds": pl.col("pass_yds") + pl.col("rush_yds"),
    "kicking_points": 3 * pl.col("fg_made") + pl.col("xp_made"),
}
STATS = [*STAT_COLUMNS.values(), *COMBINED]

# One row per player per game.
SCHEMA = {
    "game_id": pl.String, "date": pl.Date, "season": pl.Int32, "game_type": pl.Int8,
    "player_id": pl.String, "name": pl.String, "team": pl.String, "opponent": pl.String,
    "home": pl.Boolean, "position": pl.String,
    "snaps": pl.Int16, "snap_pct": pl.Float32,     # offense; null when snap counts are not out yet
    "started": pl.Boolean,                         # the schedule lists him as the starting quarterback; see lineups()
    **{stat: pl.Int16 for stat in STATS},
    "headshot": pl.String,
}

_TEAMS = {"LA": "LAR"}   # nflverse calls the Rams LA
_QB_SHARE = 0.5          # a quarterback's game counts when he took half the snaps


def _download(path: str, http: httpx.Client) -> pl.DataFrame:
    response = http.get(f"{RELEASES}/{path}")
    response.raise_for_status()
    return pl.read_parquet(io.BytesIO(response.content))


def _client() -> httpx.Client:
    return httpx.Client(timeout=120, headers=HEADERS, follow_redirects=True,
                        transport=httpx.HTTPTransport(retries=3))


def _team(column: str) -> pl.Expr:
    return pl.col(column).replace(_TEAMS)


def games(http: httpx.Client | None = None) -> pl.DataFrame:
    """Every game, played or scheduled: game_id, season, date, start (UTC), teams, the
    quarterbacks listed as starting, whether it is final."""
    raw = _download("schedules/games.parquet", http or _client())
    kickoff = (pl.col("gameday") + " " + pl.col("gametime").fill_null("13:00")).str.to_datetime(
        "%Y-%m-%d %H:%M", time_zone="America/New_York", ambiguous="earliest")
    return raw.select(
        "game_id",
        pl.col("season").cast(pl.Int32),
        pl.col("gameday").str.to_date().alias("date"),
        kickoff.dt.convert_time_zone("UTC").dt.strftime("%Y-%m-%dT%H:%M:%SZ").alias("start"),
        pl.when(pl.col("game_type") == "REG").then(2).otherwise(3).cast(pl.Int8).alias("game_type"),
        _team("away_team").alias("away"),
        _team("home_team").alias("home"),
        pl.col("away_qb_id").alias("away_qb"),
        pl.col("home_qb_id").alias("home_qb"),
        pl.col("home_score").is_not_null().alias("final"),
    )


def assemble(stats: pl.DataFrame, snaps: pl.DataFrame, players: pl.DataFrame, schedule: pl.DataFrame) -> pl.DataFrame:
    """Stat rows, plus a zero row for anyone who took an offensive snap without
    recording a stat, in SCHEMA. Inputs are the raw nflverse frames and games()."""
    recorded = (
        stats.filter(pl.col("position").is_in(list(POSITIONS)) & pl.col("player_id").is_not_null())
        .select("game_id", "player_id", pl.col("player_display_name").alias("name"), "position",
                _team("team").alias("team"), pl.col("headshot_url").alias("headshot"),
                *[pl.col(source).fill_null(0).alias(ours) for source, ours in STAT_COLUMNS.items()])
    )
    who = players.filter(pl.col("gsis_id").is_not_null() & pl.col("pfr_id").is_not_null()).select(
        pl.col("pfr_id"), pl.col("gsis_id").alias("player_id"), pl.col("display_name").alias("name"),
        pl.col("position"), pl.col("headshot")).unique("pfr_id")
    played = (
        snaps.join(who, left_on="pfr_player_id", right_on="pfr_id", suffix="_listed")
        .filter(pl.col("position_listed").is_in(list(POSITIONS)))
        .select("game_id", "player_id", pl.col("name").alias("name_snap"),
                pl.col("position_listed").alias("position_snap"), _team("team").alias("team_snap"),
                pl.col("headshot").alias("headshot_snap"),
                pl.col("offense_snaps").cast(pl.Int16).alias("snaps"),
                pl.col("offense_pct").cast(pl.Float32).alias("snap_pct"))
        .unique(["game_id", "player_id"])
    )
    rows = recorded.join(played, on=["game_id", "player_id"], how="full", coalesce=True)
    rows = rows.filter(pl.col("name").is_not_null() | (pl.col("snaps") > 0)).with_columns(
        pl.coalesce("name", "name_snap").alias("name"),
        pl.coalesce("position", "position_snap").alias("position"),
        pl.coalesce("team", "team_snap").alias("team"),
        pl.coalesce("headshot", "headshot_snap").alias("headshot"),
        *[pl.col(stat).fill_null(0) for stat in STAT_COLUMNS.values()],
    ).with_columns(   # a kicker takes no offensive snaps; leave his blank rather than zero
        pl.when(pl.col("position") == "K").then(None).otherwise(pl.col(c)).alias(c) for c in ("snaps", "snap_pct")
    )
    df = rows.join(schedule, on="game_id").with_columns(
        home=pl.col("team") == pl.col("home"),
        opponent=pl.when(pl.col("team") == pl.col("home")).then(pl.col("away")).otherwise(pl.col("home")),
        started=pl.col("player_id") == pl.when(pl.col("team") == pl.col("home")).then(pl.col("home_qb")).otherwise(pl.col("away_qb")),
        **COMBINED,
    )
    return df.select(pl.col(c).cast(t) for c, t in SCHEMA.items()).sort("date", "game_id", "team", "player_id")


def player_games(season: int, http: httpx.Client | None = None) -> pl.DataFrame:
    """One season of player games in SCHEMA."""
    http = http or _client()
    return assemble(
        _download(f"stats_player/stats_player_week_{season}.parquet", http),
        _download(f"snap_counts/snap_counts_{season}.parquet", http),
        _download("players/players.parquet", http),
        games(http),
    )


def season_id(day: dt.date) -> int:
    """The season a date falls in: the 2026 season runs into February 2027."""
    return day.year if day.month >= 8 else day.year - 1


def previous_season(season: int) -> int:
    return season - 1


def season_label(season: int) -> str:
    return str(season)


def schedule(today: dt.date, http: httpx.Client | None = None) -> list[dict]:
    upcoming = games(http).filter(pl.col("date").is_between(today, today + dt.timedelta(days=SLATE_DAYS - 1)))
    return [
        {"date": str(g["date"]), "start": g["start"], "away": g["away"], "home": g["home"],
         "state": "final" if g["final"] else "upcoming"}
        for g in upcoming.sort("start").iter_rows(named=True)
    ]


def rosters(teams: list[str], today: dt.date) -> dict:
    return {}   # no roster source yet: a player belongs to the team he last played for


_PLAYED = pl.col("snaps").is_null() | (pl.col("snaps") > 0)
_QB_PLAYED = pl.when(pl.col("snap_pct").is_not_null()).then(pl.col("snap_pct") >= _QB_SHARE).otherwise(pl.col("pass_att") >= 15)


def samples(df: pl.DataFrame) -> pl.DataFrame:
    """Games that count toward a prop: the player took an offensive snap, and a
    quarterback took at least half of them (a mop-up series is not a start).
    Rows without snap counts (kickers, or counts not published yet) count as played."""
    return df.filter(_PLAYED & ((pl.col("position") != "QB") | _QB_PLAYED))


def lineups(df: pl.DataFrame) -> pl.DataFrame:
    """Who counts as having played with his teammates. As samples(), plus a
    starting quarterback who left hurt after a series: the game was planned
    around him, so it is a game with him. The schedule's starter is only ever
    added to the half-the-snaps test, never trusted alone, because it names
    quarterbacks who did not take a snap that day."""
    return df.filter(_PLAYED & ((pl.col("position") != "QB") | _QB_PLAYED | pl.col("started").fill_null(False)))


def headshots(index: pl.DataFrame) -> list:
    return index["headshot"].to_list()


def refresh(store, days: int = 3):
    """Re-download the season in progress. Today's games are left out until every one of
    them is final or the slate rolls over at 5 AM."""
    today = fetch_board_date(schedule)
    season = season_id(today)
    df = player_games(season).filter((pl.col("season") == season) & (pl.col("date") < today))
    files = store.write_days(df)
    yield f"{df.height} player-games in {season} -> {files} daily files"
    for done in store.compact_finished():
        yield f"compacted finished season {done}"


def backfill(store, seasons: list[int]):
    http = _client()
    for season in seasons:
        df = player_games(season, http).filter((pl.col("season") == season) & (pl.col("date") < slate_date()))
        if df.is_empty():
            yield f"no games found for {season}"
            continue
        store.write_season(season, df)
        yield (f"{season}: {df['game_id'].n_unique()} games, {df.height} player-games "
               f"({df['date'].min()} to {df['date'].max()})")


# ============================================================================
# What the board needs to know about football
# ============================================================================

UNDERDOG_SPORT = "NFL"
UNDERDOG_STATS = {
    "passing_yds": "pass_yds",
    "passing_tds": "pass_tds",
    "passing_comps": "pass_cmp",
    "passing_att": "pass_att",
    "passing_ints": "pass_int",
    "passing_and_rushing_yds": "pass_rush_yds",
    "rushing_yds": "rush_yds",
    "rushing_att": "rush_att",
    "rush_rec_yds": "rush_rec_yds",
    "receiving_yds": "rec_yds",
    "receiving_rec": "receptions",
    "receiving_tgts": "targets",
    "rush_rec_tds": "rush_rec_tds",
    "fumbles_lost": "fumbles_lost",
    "field_goals_made": "fg_made",
    "extra_points_made": "xp_made",
    "kicking_points": "kicking_points",
    "period_first_touchdown_scored": "first_td",
    "receiving_long": "longest_rec",
    "rushing_long": "longest_rush",
}
TEAM_ALIASES = {"LA": "LAR", "JAC": "JAX", "WSH": "WAS"}
PINNACLE_LEAGUE = 889
PINNACLE_STATS = {
    "Passing Yards": "pass_yds",
    "Touchdown Passes": "pass_tds",
    "Pass Completions": "pass_cmp",
    "Pass Attempts": "pass_att",
    "Interceptions": "pass_int",
    "Rushing Yards": "rush_yds",
    "Rush Attempts": "rush_att",
    "Receiving Yards": "rec_yds",
    "Receptions": "receptions",
    "Touchdowns": "rush_rec_tds",   # rushing and receiving: a passing TD isn't the passer's
    "Field Goals": "fg_made",
}
FANDUEL_PAGE = "nfl"
FANDUEL_TABS = ["passing-props", "rushing-props", "receiving-props", "td-scorer-props"]
FANDUEL_STATS = {
    "Passing Yds": "pass_yds",
    "Passing TDs": "pass_tds",
    "Rushing Yds": "rush_yds",
    "Receiving Yds": "rec_yds",
    "Total Receptions": "receptions",
    "Rushing + Receiving Yds": "rush_rec_yds",
}
# One-way ladders: "Zay Flowers - Alt Receptions", "Any Time Touchdown Scorer", "To Score 2+ Touchdowns"
FANDUEL_LADDERS = {
    "Passing Yds": "pass_yds", "Passing TDs": "pass_tds", "Rushing Yds": "rush_yds",
    "Receiving Yds": "rec_yds", "Receptions": "receptions", "Touchdown": "rush_rec_tds", "Touchdowns": "rush_rec_tds",
}
DRAFTKINGS_LEAGUE = 88808
DRAFTKINGS_CATEGORIES = ["Passing Props", "Receiving Props", "Rushing Props", "Special Teams Props", "TD Scorers"]
# Field markets, a runner per player, with the count as N. The "TD Scorer" subcategory holds
# "Anytime TD Scorer", "2+ TDs" and "First TD Scorer"; "3+ TDs" is its own. None: read past quietly.
DRAFTKINGS_FIELDS = {"TD Scorer": None, "Anytime TD Scorer": "rush_rec_tds", "N+ TDs": "rush_rec_tds",
                     "First TD Scorer": "first_td"}
DRAFTKINGS_LADDERS = {   # "N+" subcategory names ("Receiving Yards": 25+, 50+, ...)
    "Receiving Yards": "rec_yds", "Receptions": "receptions", "Rush Yards": "rush_yds",
    "Rush + Rec Yards": "rush_rec_yds", "Rush Attempts": "rush_att",
}
DRAFTKINGS_STATS = {   # two-way subcategory names; quarter and half O/Us are left unmapped
    "Pass Yards O/U": "pass_yds",
    "Pass TDs O/U": "pass_tds",
    "Completions O/U": "pass_cmp",
    "Pass Attempts O/U": "pass_att",
    "Interceptions O/U": "pass_int",
    "Pass + Rush Yards O/U": "pass_rush_yds",
    "Rush Yards O/U": "rush_yds",
    "Rush Attempts O/U": "rush_att",
    "Rush + Rec Yards O/U": "rush_rec_yds",
    "Rec Yards O/U": "rec_yds",
    "Receptions O/U": "receptions",
    "FG Made O/U": "fg_made",
    "PAT Made O/U": "xp_made",
    "Kicking Pts O/U": "kicking_points",
    "Longest Reception O/U": "longest_rec",
    "Longest Rush O/U": "longest_rush",
}

KAMBI_PATH = "american_football/nfl"
# Kambi criteria with the count as N (kambi.criterion): "Total ..." are over/under, "N+ ..." ladders
KAMBI_STATS = {
    "Total Passing Yards by the Player - Including Overtime": "pass_yds",
    "N+ Passing Yards By The Player - Including Overtime": "pass_yds",
    "Total Touchdown Passes Thrown by the Player - Including Overtime": "pass_tds",
    "N+ Touchdown Passes By The Player - Including Overtime": "pass_tds",
    "Total Pass Completions by the Player - Including Overtime": "pass_cmp",
    "N+ Pass Completions By The Player - Including Overtime": "pass_cmp",
    "Total Pass Attempts by the Player - Including Overtime": "pass_att",
    "N+ Pass Attempts By The Player - Including Overtime": "pass_att",
    "Total Interceptions Thrown by the Player - Including Overtime": "pass_int",
    "N+ Interceptions Thrown By The Player - Including Overtime": "pass_int",
    "Total Passing & Rushing Yards by the Player - Including Overtime": "pass_rush_yds",
    "N+ Passing & Rushing Yards By The Player - Including Overtime": "pass_rush_yds",
    "Total Rushing Yards by the Player - Including Overtime": "rush_yds",
    "N+ Rushing Yards By The Player - Including Overtime": "rush_yds",
    "Total Rushing Attempts by the Player - Including Overtime": "rush_att",
    "N+ Rush Attempts By The Player - Including Overtime": "rush_att",
    "Total Rushing & Receiving Yards by the Player - Including Overtime": "rush_rec_yds",
    "N+ Rushing & Receiving Yards By The Player - Including Overtime": "rush_rec_yds",
    "Total Receiving Yards by the Player - Including Overtime": "rec_yds",
    "N+ Receiving Yards By The Player - Including Overtime": "rec_yds",
    "Total Receptions by the Player - Including Overtime": "receptions",
    "N+ Receptions By The Player - Including Overtime": "receptions",
    "Touchdown Scorer": "rush_rec_tds",
    "N+ Touchdown To Be Scored By The Player - Including Overtime": "rush_rec_tds",
    "Total Successful Field Goals by the Player - Including Overtime": "fg_made",
    "Total Kicking points by the Player - Including Overtime": "kicking_points",
    "First Touchdown Scorer - Including Overtime": "first_td",
    "Total Yards of Longest Reception by the Player - Including Overtime": "longest_rec",
    "Total Yards of Longest Rush by the Player - Including Overtime": "longest_rush",
}

NOVIG_LEAGUE = "NFL"
NOVIG_STATS = {   # Novig market types
    "PASSING_YARDS": "pass_yds", "PASSING_TOUCHDOWNS": "pass_tds", "PASSING_ATTEMPTS": "pass_att",
    "PASSING_COMPLETIONS": "pass_cmp", "INTERCEPTIONS_THROWN": "pass_int", "PASSING_AND_RUSHING_YARDS": "pass_rush_yds",
    "RUSHING_YARDS": "rush_yds", "RUSHING_ATTEMPTS": "rush_att", "RUSHING_AND_RECEIVING_YARDS": "rush_rec_yds",
    "RECEIVING_YARDS": "rec_yds", "RECEPTIONS": "receptions", "TOUCHDOWNS": "rush_rec_tds",
    "FIELD_GOALS_MADE": "fg_made", "KICKING_POINTS": "kicking_points", "FIRST_TOUCHDOWN_SCORER": "first_td",
    "LONGEST_RECEPTION": "longest_rec", "LONGEST_RUSH": "longest_rush",
}

PROPHETX_TOURNAMENT = 31   # ProphetX's NFL
PROPHETX_STATS = {   # its market subTypes (seen 2026-10-19); its "Assists" are tackle assists, not ours
    "player_total_passing_yards": "pass_yds", "player_total_passing_touchdowns": "pass_tds",
    "player_total_pass_completions": "pass_cmp", "player_total_passing_attempts": "pass_att",
    "player_interceptions_thrown": "pass_int", "player_total_passing_rushing_yards": "pass_rush_yds",
    "player_total_rushing_yards": "rush_yds", "player_total_rushing_attempts": "rush_att",
    "player_total_rushing_receiving_yards": "rush_rec_yds", "player_total_receiving_yards": "rec_yds",
    "player_total_receptions": "receptions", "player_total_field_goals_made": "fg_made",
    "player_total_kicking_points": "kicking_points", "player_to_score_first_touchdown": "first_td",
    "player_longest_reception": "longest_rec", "player_longest_rush": "longest_rush",
}

BET99_COMPETITION = 1595610   # BET99's NFL (its compId)
BET99_STATS = {   # its stat codes (seen 2026-10-19), each also as <code>MILE, its alternate lines; its AST is tackle assists, not ours; ATDMILE is 2+ TDs
    "PROPPSYDS": "pass_yds", "PROPPSYDSMILE": "pass_yds",
    "PROPPSTD": "pass_tds", "PROPPSTDMILE": "pass_tds",
    "PROPPSCMP": "pass_cmp", "PROPPSCMPMILE": "pass_cmp",
    "PROPPSATT": "pass_att", "PROPPSATTMILE": "pass_att",
    "PROPINT": "pass_int", "PROPINTMILE": "pass_int",
    "PROPPSPRSYDS": "pass_rush_yds", "PROPPSPRSYDSMILE": "pass_rush_yds",
    "PROPRSYDS": "rush_yds", "PROPRSYDSMILE": "rush_yds",
    "PROPRSATT": "rush_att", "PROPRSATTMILE": "rush_att",
    "PROPRSPRCYDS": "rush_rec_yds", "PROPRSPRCYDSMILE": "rush_rec_yds",
    "PROPRCYDS": "rec_yds", "PROPRCYDSMILE": "rec_yds",
    "PROPREC": "receptions", "PROPRECMILE": "receptions",
    "PROPATD": "rush_rec_tds", "PROPATDMILE": "rush_rec_tds",
    "PROPFGM": "fg_made", "PROPFGMMILE": "fg_made",
    "PROPKPT": "kicking_points", "PROPKPTMILE": "kicking_points",
    "PROPEXTPTM": "xp_made", "PROPEXTPTMMILE": "xp_made",
    "PROPFBLLST": "fumbles_lost", "PROPFBLLSTMILE": "fumbles_lost",
    "PROP1STTD": "first_td",
    "PROPLNGREC": "longest_rec", "PROPLNGRECMILE": "longest_rec", "PROPLNGRS": "longest_rush", "PROPLNGRSMILE": "longest_rush",
}

POLYMARKET_SPORT = "nfl"
POLYMARKET_STATS = {   # its market types ("C.J. Stroud: Passing Yards O/U 149.5", "Bhayshul Tuten: 2+ Touchdowns")
    "passing_yards": "pass_yds", "passing_touchdowns": "pass_tds", "passing_attempts": "pass_att",
    "passing_completions": "pass_cmp", "rushing_yards": "rush_yds", "receiving_yards": "rec_yds",
    "receptions": "receptions", "anytime_touchdowns": "rush_rec_tds", "two_plus_touchdowns": "rush_rec_tds",
}
KALSHI_GAME, KALSHI_TOTAL = "KXNFLGAME", "KXNFLTOTAL"
KALSHI_STATS = {   # Kalshi series, each market a rung ("Kirk Cousins: 3+ passing touchdowns")
    "KXNFLPASSYDS": "pass_yds", "KXNFLPASSTDS": "pass_tds", "KXNFLPASSCOMP": "pass_cmp", "KXNFLPASSATT": "pass_att",
    "KXNFLPASSINT": "pass_int", "KXNFLRSHYDS": "rush_yds", "KXNFLRSHATT": "rush_att", "KXNFLRRYDS": "rush_rec_yds",
    "KXNFLRECYDS": "rec_yds", "KXNFLREC": "receptions", "KXNFLTD": "rush_rec_tds",
}

BOVADA_PATH = "football/nfl"
# Bovada market names, before " - Player (TEAM)" or with the count as N (bovada.parse)
BOVADA_STATS = {
    "Total Passing Yards": "pass_yds", "Alternate Passing Yards": "pass_yds",
    "Total Passing Touchdowns": "pass_tds", "Alternate Passing Touchdowns": "pass_tds",
    "Total Completions": "pass_cmp", "Alternate Passing Completions": "pass_cmp",
    "Total Passing Attempts": "pass_att", "Total Interceptions Thrown": "pass_int",
    "Total Passing & Rushing Yards O/U": "pass_rush_yds",
    "Total Rushing Yards": "rush_yds", "Alternate Rushing Yards": "rush_yds",
    "Total Receiving Yards": "rec_yds", "Alternate Receiving Yards": "rec_yds",
    "Total Receptions": "receptions", "Alternate Receptions": "receptions",
    "Anytime Touchdown Scorer": "rush_rec_tds", "Player to Score N or More Touchdowns": "rush_rec_tds",
    "Total Kicking Points": "kicking_points", "First Touchdown Scorer": "first_td",
    "Longest Reception": "longest_rec", "Player to Record a N+ Yard Reception": "longest_rec",   # an N+ yard catch: the longest over N - 0.5
}

# How a sharp book's price is moved to a betting site's other line, for its EV (template.html estimateAt): stats here
# are read from a fitted count distribution, "poisson" or a negative binomial of that size (its
# variance mean + mean^2/size); the rest move in log-odds along the books' ladders. Chosen 2026-10-19
# by backtesting against the sharp books' own alternate lines on the day's boards: where a count
# beat the ladders. The market's ladders are far less spread than the game logs (they know the minutes).
FAIR_DIST = {"receptions": 30}   # 1.35 vs 1.7; yards, attempts and TDs stay on the ladders, which beat any count fit

MISE_LEAGUE = "55"
# Mise-o-jeu group codes, two-way ("..._OVER_UNDER_PLAYER") and one-way ladders alike
MISE_STATS = {
    "TOTAL_PASSING_YARDS_OVER_UNDER_PLAYER": "pass_yds",
    "PLAYER_UNDEFINED_TOTAL_PASSING_YARDS": "pass_yds",
    "TOTAL_PASSING_TOUCHDOWNS_OVER_UNDER_PLAYER": "pass_tds",
    "PLAYER_UNDEFINED_TOTAL_PASSING_TOUCHDOWNS": "pass_tds",
    "TOTAL_PASSING_ATTEMPTS_OVER_UNDER_PLAYER": "pass_att",
    "PLAYER_UNDEFINED_TOTAL_PASSES_ATTEMPTED": "pass_att",
    "PLAYER_UNDEFINED_TOTAL_PASSES_COMPLETED": "pass_cmp",
    "PLAYER_UNDEFINED_TOTAL_PASSING_RUSHING_YARDS": "pass_rush_yds",
    "TOTAL_RUSHING_YARDS_OVER_UNDER_PLAYER": "rush_yds",
    "PLAYER_UNDEFINED_TOTAL_RUSHING_YARDS": "rush_yds",
    "TOTAL_RUSHING_ATTEMPTS_OVER_UNDER_PLAYER": "rush_att",
    "PLAYER_UNDEFINED_TOTAL_RECEIVING_RUSHING_YARDS": "rush_rec_yds",
    "TOTAL_RECEPTION_YARDS_OVER_UNDER_PLAYER": "rec_yds",
    "PLAYER_UNDEFINED_TOTAL_RECEIVING_YARDS": "rec_yds",
    "TOTAL_RECEPTIONS_OVER_UNDER_PLAYER": "receptions",
    "PLAYER_UNDEFINED_TOTAL_RECEPTIONS": "receptions",
    "PLAYER_UNDEFINED_TOTAL_TOUCHDOWNS": "rush_rec_tds",
}

BETANO_PATH = "/sport/football/north-america/nfl/1611r/"   # its league page on betano.ca

STAKE_STATS = {
    "Receiving Yards": "rec_yds", "Receptions": "receptions", "Rushing Yards": "rush_yds",
    "Rushing Attempts": "rush_att", "Passing Yards": "pass_yds", "Passing Attempts": "pass_att",
    "Passing Completions": "pass_cmp", "Passing Touchdowns": "pass_tds", "Interceptions": "pass_int",
    "Passing+Rushing Yards": "pass_rush_yds", "Anytime Touchdown": "rush_rec_tds",
    # "Rushing Touchdowns" and "Receiving Touchdowns" are each only part of a player's TDs: not the TD row
    "Field Goal Made": "fg_made", "Extra Point Made": "xp_made", "Kicking Points": "kicking_points",
    "First Touchdown": "first_td",
}
PICK6_STATS = {
    "Passing Yards": "pass_yds", "Passing TDs": "pass_tds", "Rushing Yards": "rush_yds",
    "Receiving Yards": "rec_yds", "Receptions": "receptions", "Rush + Rec Yards": "rush_rec_yds",
    "Rush + Rec TD": "rush_rec_tds",
    "Pass Completions": "pass_cmp", "Interceptions": "pass_int", "Field Goal Made": "fg_made",
    "Kicking Points": "kicking_points", "Passing Touchdowns": "pass_tds",
}

STAKE_SPORT = 'american-football'     # its sport slug on stake.com
PICK6_SPORT = 'NFL'     # its sport code on pick6.draftkings.com
BETANO_STATS = {
    "Passing Yards": "pass_yds", "Rushing Yards": "rush_yds", "Receiving Yards": "rec_yds",
    "Receptions": "receptions", "Pass Completions": "pass_cmp", "Pass Attempts": "pass_att",
    "Interceptions": "pass_int", "Passing and Rushing Yards": "pass_rush_yds",
    "Rushing and Receiving Yards": "rush_rec_yds", "Anytime Touchdown": "rush_rec_tds",
    # "Rushing Touchdowns" and "Receiving Touchdowns" are each only part of a player's TDs: not the TD row
    "Passing Touchdowns": "pass_tds", "Rushing Attempts": "rush_att",
    "Field Goals Made": "fg_made", "Extra Points Made": "xp_made", "Kicking Points": "kicking_points",
    "Lost Fumbles": "fumbles_lost", "Passing Attempts": "pass_att",
    "Multi Touchdown Scorer": "rush_rec_tds",
    "Longest Reception": "longest_rec", "Longest Rush": "longest_rush",
}
BETANO_FIRST = {}   # its "First Touchdown" table is each team's first scorer, not the game's: left unmapped

PROPS = {
    "pass_yds": ("Pass Yards", "PASS YDS"),
    "pass_tds": ("Pass TDs", "PASS TD"),
    "pass_cmp": ("Completions", "CMP"),
    "pass_att": ("Pass Attempts", "ATT"),
    "pass_int": ("INTs Thrown", "INT"),
    "pass_rush_yds": ("Pass + Rush Yards", "P+R YDS"),
    "rush_yds": ("Rush Yards", "RUSH YDS"),
    "rush_att": ("Rush Attempts", "CAR"),
    "rush_rec_yds": ("Rush + Rec Yards", "R+R YDS"),
    "rec_yds": ("Receiving Yards", "REC YDS"),
    "receptions": ("Receptions", "REC"),
    "targets": ("Targets", "TGT"),
    "rush_rec_tds": ("Rush + Rec TDs", "TD"),
    "two_tds": ("2+ TDs", "2+ TD"),
    "fumbles_lost": ("Fumbles Lost", "FUM"),
    "fg_made": ("FG Made", "FG"),
    "xp_made": ("XP Made", "XP"),
    "kicking_points": ("Kicking Points", "K PTS"),
    "first_td": ("First TD Scorer", "1ST TD"),
    "longest_rec": ("Longest Reception", "LNG REC"),
    "longest_rush": ("Longest Rush", "LNG RUSH"),
}
TABS = {
    "pass_yds": "Pass Yds", "pass_tds": "Pass TD", "pass_cmp": "Comp", "pass_att": "Pass Att", "pass_int": "INT",
    "pass_rush_yds": "P+R Yds", "rush_yds": "Rush Yds", "rush_att": "Carries", "rush_rec_yds": "R+R Yds",
    "rec_yds": "Rec Yds", "receptions": "Rec", "targets": "Targets", "rush_rec_tds": "TD", "two_tds": "2+ TD",
    "fumbles_lost": "Fum", "fg_made": "FG", "xp_made": "XP", "kicking_points": "K Pts", "first_td": "1st TD",
    "longest_rec": "Long Rec", "longest_rush": "Long Rush",
}
LABELS = {"snaps": ("Snaps", "SNAPS")}
# Stats that are another column at a set line, with a tab of their own: 2+ TDs is rush + rec TDs over 1.5,
# kept apart from the anytime TD row. A board row is made for each player a book prices there.
FIXED_LINES = {"two_tds": ("rush_rec_tds", 1.5)}
# Stats the board only compares prices on: the game logs don't say who scored first or how long a
# player's longest play was, so there are no hit rates. A row is made for each player Underdog or a
# book prices, at Underdog's line, else the stat's set line, else (None) the books' market line.
PRICE_ONLY = {"first_td": 0.5, "longest_rec": None, "longest_rush": None}
ANYTIME_STATS = {"rush_rec_tds"}   # a book's 2+ or 3+ TD rung is never the anytime row's price (books.prices)
CLOCK: list[str] = []

POSITIONS = {
    "QB": ("QB", "qb", "QB"),
    "RB": ("RB", "rb", "RB"),
    "FB": ("FB", "rb", "RB"),
    "WR": ("WR", "receiver", "WR"),
    "TE": ("TE", "receiver", "TE"),
    "K": ("K", "kicker", "K"),
}
KINDS = {
    "qb": {
        "picks": ["pass_yds", "pass_tds", "pass_cmp", "pass_att", "pass_int", "rush_yds", "rush_att",
                  "pass_rush_yds", "rush_rec_tds", "two_tds", "fumbles_lost"],
        "log": ["snaps", "pass_cmp", "pass_att", "pass_yds", "pass_tds", "pass_int", "rush_att", "rush_yds",
                "rush_rec_tds", "fumbles_lost"],
        "minis": ["pass_att", "rush_att"],
        "typical": ["pass_yds", "pass_tds", "pass_cmp", "pass_att", "pass_int", "rush_yds", "pass_rush_yds"],
    },
    "rb": {
        "picks": ["rush_yds", "rush_att", "rec_yds", "receptions", "targets", "rush_rec_yds", "rush_rec_tds",
                  "two_tds", "fumbles_lost"],
        "log": ["snaps", "rush_att", "rush_yds", "targets", "receptions", "rec_yds", "rush_rec_yds",
                "rush_rec_tds", "fumbles_lost"],
        "minis": ["snaps", "rush_att", "targets"],
        "typical": ["rush_yds", "rush_att", "rec_yds", "receptions", "rush_rec_yds", "rush_rec_tds"],
    },
    "receiver": {
        "picks": ["rec_yds", "receptions", "targets", "rush_rec_tds", "two_tds", "rush_yds", "fumbles_lost"],
        "log": ["snaps", "targets", "receptions", "rec_yds", "rush_att", "rush_yds", "rush_rec_tds", "fumbles_lost"],
        "minis": ["snaps", "targets"],
        "typical": ["rec_yds", "receptions", "targets", "rush_rec_tds"],
    },
    "kicker": {
        "picks": ["fg_made", "xp_made", "kicking_points"],
        "log": ["fg_made", "xp_made", "kicking_points"],
        "minis": [],
        "typical": ["fg_made", "xp_made", "kicking_points"],
    },
}
GROUPS = {
    "QB": ("quarterbacks", False),
    "RB": ("running backs", False),
    "WR": ("wide receivers", False),
    "TE": ("tight ends", False),
    "K": ("kickers", False),
}

REST = False          # nearly every game is a week after the last
SLATE_DAYS = 7
ACTIVE_GAMES = 2
TYPICAL_GAMES = 8
# A typical line is only worth a row for a player with a real role.
MIN_AVERAGE = {
    "pass_yds": 100, "pass_att": 15, "pass_cmp": 10, "pass_rush_yds": 100,
    "rush_yds": 10, "rush_att": 3, "rush_rec_yds": 15,
    "rec_yds": 10, "receptions": 1, "targets": 2,
}
