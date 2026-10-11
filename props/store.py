"""On-disk layout for player game logs, one folder per league.

    data/nhl/boxscores/20252026.parquet             finished season, one file, never rewritten
    data/nhl/boxscores/20262027/2026-10-01.parquet  season in progress, one file per game date

Daily files keep the refresh commit to a few kilobytes instead of rewriting a
season-sized blob every day.
"""
import shutil
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def _write(df: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path, compression="zstd", compression_level=19, statistics=False)


class Store:
    def __init__(self, league):
        self.league = league

    @property
    def box(self) -> Path:
        return DATA / self.league.KEY / "boxscores"

    def load(self) -> pl.DataFrame:
        files = sorted(self.box.rglob("*.parquet")) if self.box.exists() else []
        if not files:
            return pl.DataFrame(schema=self.league.SCHEMA)
        return pl.read_parquet(files)

    def seasons(self) -> list[int]:
        if not self.box.exists():
            return []
        return sorted({int(p.stem) for p in self.box.iterdir() if p.stem.isdigit()})

    def last_date(self):
        """Most recent game date on disk, or None when the store is empty."""
        latest = max(self.seasons(), default=None)
        if latest is None:
            return None
        folder = self.box / str(latest)
        files = sorted(folder.glob("*.parquet")) if folder.is_dir() else [self.box / f"{latest}.parquet"]
        return pl.read_parquet(files, columns=["date"])["date"].max()

    def write_season(self, season: int, df: pl.DataFrame) -> None:
        """Replace a whole season with one file (backfill)."""
        shutil.rmtree(self.box / str(season), ignore_errors=True)
        _write(df.filter(pl.col("season") == season), self.box / f"{season}.parquet")

    def write_days(self, df: pl.DataFrame) -> int:
        """Write one file per game date. Seasons already compacted are left alone."""
        written = 0
        for (season, date), day in df.partition_by("season", "date", as_dict=True).items():
            if (self.box / f"{season}.parquet").exists():
                continue
            _write(day, self.box / str(season) / f"{date}.parquet")
            written += 1
        return written

    def compact_finished(self) -> list[int]:
        """Fold the daily files of every season except the newest into one file each."""
        done = []
        for season in self.seasons()[:-1]:
            folder = self.box / str(season)
            if not folder.is_dir():
                continue
            _write(pl.read_parquet(sorted(folder.glob("*.parquet"))), self.box / f"{season}.parquet")
            shutil.rmtree(folder)
            done.append(season)
        return done
