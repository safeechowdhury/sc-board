"""Hit rates, rest days and defense-vs-position, computed from the game logs."""
import math

import polars as pl

MAX_REST = 3
PLAYOFFS = 3    # game_type of a playoff game; 2 is the regular season


def with_rest(df: pl.DataFrame) -> pl.DataFrame:
    """Add `rest`: full days off before the game (0 = back-to-back), capped at 3."""
    team_games = df.select("team", "game_id", "date").unique().sort("team", "date")
    rest = team_games.with_columns(
        rest=((pl.col("date") - pl.col("date").shift(1).over("team")).dt.total_days() - 1)
        .clip(0, MAX_REST).fill_null(MAX_REST).cast(pl.Int8)
    )
    return df.join(rest.select("team", "game_id", "rest"), on=["team", "game_id"], how="left")


def tally(values: list, line: float) -> list[int]:
    """[overs, unders, games]. A value equal to the line is a push: neither."""
    return [sum(v > line for v in values), sum(v < line for v in values), len(values)]


def typical_line(values: list) -> float:
    """A stand-in line when no book has posted one: the half-point just above
    the player's recent average rounded down (3.4 shots a game -> 3.5)."""
    return max(0.5, math.floor(sum(values) / len(values)) + 0.5)


def is_open_question(values: list, line: float, floor: float = 0.15) -> bool:
    """False when a line is a foregone conclusion for this player: he lands on
    one side of it in fewer than 15% of games (a stay-at-home defenseman and
    0.5 power-play points). No book posts those, and they would top every sort."""
    over, under, games = tally(values, line)
    return games > 0 and min(over, under) / games >= floor


def prop_row(log: dict[str, list], stat: str, line: float, opponent: str, season: int, playoffs: bool = False) -> dict:
    """Hit rates for one player's line. `log` is that player's games as columns, newest first.
    Playoff games are left out unless `playoffs`."""
    games = [(v, s, o) for v, s, o, kind in zip(log[stat], log["season"], log["opponent"], log["game_type"])
             if v is not None and (playoffs or kind != PLAYOFFS)]
    values = [v for v, _, _ in games]
    return {
        "l5": tally(values[:5], line),
        "l10": tally(values[:10], line),
        "l20": tally(values[:20], line),
        "season": tally([v for v, s, _ in games if s == season], line),
        "h2h": tally([v for v, _, o in games if o == opponent], line),
        "avg": round(sum(values[:10]) / len(values[:10]), 2) if values else None,
        "last": values[:10][::-1],   # oldest first, the way the chart reads
    }


def runs(indexes: list[int]) -> list[int]:
    """Sorted positions as alternating run lengths, starting with a run that is
    in the list: [0, 1, 2, 5] -> [3, 2, 1]. Positions after the last are left off."""
    out, expected = [0], 0
    for i in indexes:
        if i == expected:
            out[-1] += 1
        else:
            out += [i - expected, 1]
        expected = i + 1
    return out


def teammates(logs: pl.DataFrame, lineups: pl.DataFrame, index: pl.DataFrame, players: list) -> dict:
    """Who each of `players` has shared a game with, and in which of his games.

    A teammate is anyone who played for the same team in the same game, so a
    player who has since left still counts for the games he was there. `logs`
    is each player's games that count, newest first; `lineups` is who played
    in each game as far as his teammates are concerned; `index` names every player.
    player -> [[teammate, name, position, runs]], most games together first,
    where `runs` covers the player's own games from the newest: so many
    together, so many apart, so many together...
    """
    mine = logs.select("player_id", "game_id", "team", i=pl.int_range(pl.len()).over("player_id")).filter(
        pl.col("player_id").is_in(players))
    shared = (
        mine.join(lineups.select("game_id", "team", mate="player_id"), on=["game_id", "team"])
        .filter(pl.col("player_id") != pl.col("mate"))
        .group_by("player_id", "mate").agg(pl.col("i").sort())
        .join(index.select(pl.col("player_id").alias("mate"), "name", "position"), on="mate")
        .sort(pl.col("i").list.len(), "name", "mate", descending=[True, False, False])
    )
    out: dict = {}
    for pid, mate, together, name, position in shared.iter_rows():
        out.setdefault(pid, []).append([mate, name, position, runs(together)])
    return out


def dvp(df: pl.DataFrame, season: int, groups: dict[str, str], stats: dict[str, list[str]]) -> pl.DataFrame:
    """Defense vs position: what each team has allowed to each position group per game this season.

    `groups` maps a position to its group and `stats` lists the stats that mean
    something for each group. `delta` is schedule-adjusted: how far above or
    below their own average the opposing group produced against this team.
    Rank 1 allows the least. Columns: team, group, stat, allowed, delta, games, rank.
    """
    columns = sorted({stat for wanted in stats.values() for stat in wanted})
    pairs = pl.DataFrame([(group, stat) for group, wanted in stats.items() for stat in wanted],
                         schema=["group", "stat"], orient="row")
    per_game = (
        df.filter((pl.col("season") == season) & pl.col("position").is_in(list(groups)))
        .with_columns(group=pl.col("position").replace(groups))
        .group_by("game_id", "team", "opponent", "group")
        .agg(pl.col(columns).sum())
        .unpivot(index=["game_id", "team", "opponent", "group"], on=columns,
                 variable_name="stat", value_name="value")
        .join(pairs, on=["group", "stat"])
        .with_columns(norm=pl.col("value").mean().over("team", "group", "stat"))
    )
    return (
        per_game.group_by(pl.col("opponent").alias("team"), "group", "stat")
        .agg(
            allowed=pl.col("value").mean(),
            delta=(pl.col("value") - pl.col("norm")).mean(),
            games=pl.col("game_id").n_unique(),
        )
        .sort("team")
        .with_columns(rank=pl.col("delta").rank("ordinal").over("group", "stat").cast(pl.Int16))
        .sort("stat", "group", "rank")
    )
