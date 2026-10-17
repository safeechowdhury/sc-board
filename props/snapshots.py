"""Every publish's prices, kept for grading: what each book had on each row, and when.

A publish writes only what changed since the last one: a record when a book's price on a
row is new, has moved, or was taken down (a record with no line or prices), so a price
that sat still all day is one record. The price at any moment is the last record at or
before it (`as_of`): the first publish to see a price is its "open", the last before the
game starts its "close". Underdog's own payout is kept as the book "underdog".

    data/nhl/snapshots/20262027/2026-10-14.parquet   a game date's records, rewritten while
                                                     its games are still to come
    data/nhl/snapshots/20262027/publishes.parquet    when each publish ran, and for each book
                                                     whether its prices were fetched then
    data/nhl/snapshots/20252026.parquet              a finished season, folded into one file,
    data/nhl/snapshots/20252026-publishes.parquet    and its publishes

A missing record means "unchanged" only where the publish log says the book was fetched:
a book whose fetch failed (its saved copy, or nothing, on the board) writes no records,
not even takedowns, and the log says so. Games under way are left alone; they leave the
board when they start, which is not a takedown.

Text columns are dictionary-encoded by Parquet, so a player's id or a book's key costs a
few bits a record. Prices are stored as implied chances in hundredths of a percent (1.87x is 5348), raw,
with the vig in: Fair and EV are worked out when grading, with whatever weights are
current then. Results aren't stored either: they are in the box scores.
"""
import datetime as dt
import shutil
from pathlib import Path

import polars as pl

from . import store

KEY = ["game", "player_id", "stat", "book"]       # one price history each
VALUE = ["row_line", "line", "over", "under"]      # a change in any of these is a new record
SCHEMA = {
    "seen_at": pl.Datetime("ms", "UTC"),   # the publish that saw it
    "game": pl.String,                # "2026-10-14|MTL@WSH"
    "start": pl.Datetime("ms", "UTC"),
    "player_id": pl.String,           # as text: NFL ids are "00-0033881"; the name, on a board without game logs
    "stat": pl.String,
    "row_line": pl.Float32,                # the board row's line (Underdog's, or the typical one)
    "book": pl.String,                # "novig", "underdog", ...
    "line": pl.Float32,                    # the book's line, which may differ from the row's
    "over": pl.UInt16,                     # implied chance, hundredths of a percent; null: none
    "under": pl.UInt16,                    # null: one-way, or taken down
}
LOG_SCHEMA = {"seen_at": pl.Datetime("ms", "UTC"), "book": pl.String, "fetched": pl.Boolean}


def folder(league) -> Path:
    return store.DATA / league.KEY / "snapshots"


def chance(price) -> int | None:
    """A decimal price's implied chance in hundredths of a percent."""
    return round(10000 / price) if price else None


def _write(df: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(".part")
    df.write_parquet(part, compression="zstd", compression_level=19, statistics=False)
    part.replace(path)


def _read(paths: list[Path], schema: dict) -> pl.DataFrame:
    paths = [path for path in paths if path.exists()]
    if not paths:
        return pl.DataFrame(schema=schema)
    return pl.concat([pl.read_parquet(path) for path in paths], how="vertical_relaxed").cast(schema)


def _days(league) -> list[Path]:
    root = folder(league)
    return sorted(path for path in root.glob("*/*.parquet") if path.stem != "publishes") if root.exists() else []


def load(league) -> pl.DataFrame:
    """Every record kept for the league: finished seasons and the one in progress."""
    root = folder(league)
    seasons = sorted(path for path in root.glob("*.parquet") if not path.stem.endswith("publishes")) if root.exists() else []
    return _read(seasons + _days(league), SCHEMA)


def publishes(league) -> pl.DataFrame:
    """When each publish ran and, for each book, whether its prices were fetched then."""
    root = folder(league)
    paths = sorted(root.glob("*-publishes.parquet")) + sorted(root.glob("*/publishes.parquet")) if root.exists() else []
    return _read(paths, LOG_SCHEMA)


def as_of(records: pl.DataFrame, when: dt.datetime) -> pl.DataFrame:
    """Each price as it stood at `when`: the last record at or before it, taken-down prices left out."""
    return (records.filter(pl.col("seen_at") <= when).sort("seen_at")
            .group_by(KEY, maintain_order=True).last()
            .filter(pl.col("line").is_not_null()))


def current(rows: list[dict], books: list[dict], seen_at: dt.datetime, by_name: bool = False) -> pl.DataFrame:
    """This publish's prices: each fetched book's [line, over, under] on each board row, and
    Underdog's payout on its own lines. books: {"key", "prices", "fetched"}. A row is keyed by its
    player's id; by_name (a board without game logs, so no ids): by his name, as books.prices keys
    it. A row with no id on a board with them is left out."""
    who = lambda row: row["id"] if row["id"] is not None else (row["name"] if by_name else None)
    by_key = {f"{who(row)}|{row['stat']}": row for row in rows if who(row) is not None}
    out = []

    def add(row, book, line, over, under):
        out.append({"seen_at": seen_at, "game": row["game"], "start": dt.datetime.fromisoformat(row["start"].replace("Z", "+00:00")),
                    "player_id": str(who(row)), "stat": row["stat"], "row_line": row["line"], "book": book,
                    "line": line, "over": chance(over), "under": chance(under)})

    for book in books:
        if book["fetched"]:
            for key, (line, over, under, *_) in book["prices"].items():   # the row's own price, not the other lines
                if key in by_key:
                    add(by_key[key], book["key"], line, over, under)
    for row in by_key.values():
        if row.get("ud"):
            add(row, "underdog", row["line"], (row.get("over") or {}).get("odds"), (row.get("under") or {}).get("odds"))
    return pl.DataFrame(out, schema=SCHEMA)


def changes(before: pl.DataFrame, now: pl.DataFrame, fetched: set[str], seen_at: dt.datetime) -> pl.DataFrame:
    """What this publish adds: prices new or moved since `before` (each key's latest record), and a
    takedown for each price a fetched book had on a game still to start that it no longer has."""
    last = before.sort("seen_at").group_by(KEY, maintain_order=True).last()
    moved = (now.join(last.select(KEY + VALUE), on=KEY, how="left", suffix="_was")
             .filter(pl.any_horizontal(~pl.col(c).eq_missing(pl.col(f"{c}_was")) for c in VALUE))
             .select(list(SCHEMA)))
    gone = (last.filter(pl.col("line").is_not_null() & (pl.col("start") > seen_at)
                        & pl.col("book").is_in(sorted(fetched)))
            .join(now.select(KEY), on=KEY, how="anti")
            .with_columns(pl.lit(seen_at).cast(SCHEMA["seen_at"]).alias("seen_at"),
                          *(pl.lit(None).cast(SCHEMA[c]).alias(c) for c in ("line", "over", "under"))))
    return pl.concat([moved, gone.select(list(SCHEMA))])


def record(league, rows: list[dict], books: list[dict], seen_at: dt.datetime, underdog: bool = True) -> int:
    """Keep this publish's changes in the game dates' files and log the publish. Returns how many
    records it wrote. books: each {"key", "prices", "fetched": whether fetched this publish};
    underdog: whether Underdog's lines were."""
    root = folder(league)
    fetched = {book["key"] for book in books if book["fetched"]} | ({"underdog"} if underdog else set())
    now = current(rows, books, seen_at, by_name=getattr(league, "ODDS_ONLY", False))
    if not underdog:
        now = now.filter(pl.col("book") != "underdog")
    dates = sorted({game.split("|", 1)[0] for game in now["game"].unique()})
    # the files a change can land in: the dates on the board now, and those of prices it may take down
    paths = {path.stem: path for path in _days(league) if path.stem >= str(seen_at.date() - dt.timedelta(days=1))}
    for date in dates:
        paths.setdefault(date, root / str(league.season_id(dt.date.fromisoformat(date))) / f"{date}.parquet")
    before = _read(list(paths.values()), SCHEMA)
    new = changes(before, now, fetched, seen_at)
    for (date,), day in new.with_columns(pl.col("game").str.slice(0, 10).alias("_date")).partition_by("_date", as_dict=True).items():
        _write(pl.concat([_read([paths[date]], SCHEMA), day.drop("_date")]), paths[date])
    season = league.season_id(seen_at.date())
    log = root / str(season) / "publishes.parquet"
    _write(pl.concat([_read([log], LOG_SCHEMA), pl.DataFrame(
        [{"seen_at": seen_at, "book": key, "fetched": key in fetched} for key in [b["key"] for b in books] + ["underdog"]],
        schema=LOG_SCHEMA)]), log)
    _fold_finished(league)
    return len(new)


def _fold_finished(league) -> None:
    """Fold each season but the newest into one file, and its publish log into another."""
    root = folder(league)
    seasons = sorted(int(path.name) for path in root.iterdir() if path.is_dir() and path.name.isdigit()) if root.exists() else []
    for season in seasons[:-1]:
        days = sorted(path for path in (root / str(season)).glob("*.parquet") if path.stem != "publishes")
        _write(_read(days, SCHEMA), root / f"{season}.parquet")
        _write(_read([root / str(season) / "publishes.parquet"], LOG_SCHEMA), root / f"{season}-publishes.parquet")
        shutil.rmtree(root / str(season))
