import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import polars as pl

from props import snapshots, store
from props.leagues import nhl

UTC = dt.timezone.utc
NOON = dt.datetime(2026, 10, 14, 16, tzinfo=UTC)


def row(pid, stat, line, game="2026-10-14|MTL@WSH", start="2026-10-14T23:30:00Z", ud=None):
    return {"id": pid, "name": f"P{pid}", "stat": stat, "line": line, "game": game, "start": start,
            "ud": ud is not None, "over": {"odds": ud[0]} if ud else None, "under": {"odds": ud[1]} if ud else None}


def book(key, prices, fetched=True):
    return {"key": key, "prices": prices, "fetched": fetched}


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(store, "DATA", Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        self.rows = [row(1, "goals", 0.5, ud=(1.87, 1.87)), row(2, "points", 0.5)]

    def publish(self, books, at, rows=None, underdog=True):
        return snapshots.record(nhl, rows or self.rows, books, at, underdog=underdog)

    def test_a_publish_keeps_only_what_is_new_or_moved(self):
        novig = {"1|goals": [0.5, 2.5, 1.6], "2|points": [0.5, 1.9, 1.9]}
        self.assertEqual(self.publish([book("novig", novig)], NOON), 3)   # two Novig prices and Underdog's
        self.assertEqual(self.publish([book("novig", novig)], NOON + dt.timedelta(hours=1)), 0)
        moved = {**novig, "1|goals": [0.5, 2.4, 1.65]}
        self.assertEqual(self.publish([book("novig", moved)], NOON + dt.timedelta(hours=2)), 1)
        records = snapshots.load(nhl)
        self.assertEqual(records.filter(pl.col("player_id") == "1", pl.col("book") == "novig")["over"].to_list(),
                         [4000, 4167])   # 2.5x is 40.00%, 2.4x 41.67%
        self.assertTrue((Path(self.tmp.name) / "nhl/snapshots/20262027/2026-10-14.parquet").exists())
        self.assertEqual(len(snapshots.publishes(nhl)), 3 * 2)   # each publish: Novig and Underdog

    def test_open_and_close_read_back_with_as_of(self):
        self.publish([book("novig", {"1|goals": [0.5, 2.5, 1.6]})], NOON)
        self.publish([book("novig", {"1|goals": [0.5, 2.2, 1.7]})], NOON + dt.timedelta(hours=5))
        records = snapshots.load(nhl).filter(pl.col("book") == "novig")
        self.assertEqual(snapshots.as_of(records, NOON + dt.timedelta(hours=1))["over"].to_list(), [4000])
        self.assertEqual(snapshots.as_of(records, NOON + dt.timedelta(hours=6))["over"].to_list(), [4545])

    def test_a_price_taken_down_is_recorded_unless_the_fetch_failed_or_the_game_started(self):
        novig = {"1|goals": [0.5, 2.5, 1.6], "2|points": [0.5, 1.9, 1.9]}
        self.publish([book("novig", novig)], NOON)
        # the book's fetch failed: its prices are missing but nothing was taken down
        self.assertEqual(self.publish([book("novig", {}, fetched=False)], NOON + dt.timedelta(hours=1)), 0)
        self.assertFalse(snapshots.publishes(nhl).filter(pl.col("book") == "novig")["fetched"].to_list()[-1])
        # fetched, and it dropped player 2's points: a takedown
        self.assertEqual(self.publish([book("novig", {"1|goals": [0.5, 2.5, 1.6]})], NOON + dt.timedelta(hours=2)), 1)
        gone = snapshots.load(nhl).filter(pl.col("player_id") == "2").sort("seen_at")
        self.assertIsNone(gone["line"][-1])
        self.assertEqual(len(snapshots.as_of(snapshots.load(nhl), NOON + dt.timedelta(hours=3))
                             .filter(pl.col("player_id") == "2")), 0)
        # after the start the game leaves the board: that is no takedown
        late = NOON + dt.timedelta(hours=8)
        self.assertEqual(self.publish([book("novig", {})], late, rows=[]), 0)

    def test_underdog_is_left_alone_when_its_lines_came_from_a_saved_copy(self):
        self.publish([book("novig", {})], NOON)
        self.assertEqual(self.publish([book("novig", {})], NOON + dt.timedelta(hours=1), rows=[], underdog=False), 0)

    def test_a_finished_season_is_folded_into_one_file(self):
        last = [row(1, "goals", 0.5, game="2026-04-10|MTL@WSH", start="2026-04-10T23:00:00Z")]
        self.publish([book("novig", {"1|goals": [0.5, 2.5, 1.6]})], dt.datetime(2026, 4, 10, 16, tzinfo=UTC), rows=last)
        self.publish([book("novig", {"1|goals": [0.5, 2.5, 1.6]})], NOON)
        root = Path(self.tmp.name) / "nhl/snapshots"
        self.assertTrue((root / "20252026.parquet").exists())
        self.assertTrue((root / "20252026-publishes.parquet").exists())
        self.assertFalse((root / "20252026").exists())
        self.assertEqual(sorted(snapshots.load(nhl)["game"].unique().to_list()), ["2026-04-10|MTL@WSH", "2026-10-14|MTL@WSH"])


if __name__ == "__main__":
    unittest.main()
