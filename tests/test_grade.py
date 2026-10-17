import datetime as dt
import math
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from props import books, grade, snapshots, store
from props.leagues import nhl

from .helpers import game_logs
from .test_snapshots import NOON, book, row


class GradeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patch in (mock.patch.object(store, "DATA", Path(self.tmp.name)),
                      mock.patch.object(grade, "OUT", Path(self.tmp.name) / "grades.json")):
            patch.start()
            self.addCleanup(patch.stop)
        rows = [row(1, "goals", 0.5, ud=(1.87, 1.87))]
        # Novig opens 2.5 / 1.6 on his goal (the under is +EV at Underdog's 1.87) and closes 2.0 / 1.9
        snapshots.record(nhl, rows, [book("novig", {"1|goals": [0.5, 2.5, 1.6]})], NOON)
        snapshots.record(nhl, rows, [book("novig", {"1|goals": [0.5, 2.0, 1.9]})], NOON + dt.timedelta(hours=3))
        store.Store(nhl).write_days(game_logs([{"game_id": 1, "date": "2026-10-14", "player_id": 1, "name": "P1",
                                                "team": "MTL", "opponent": "WSH", "goals": 1}]))

    def test_the_close_is_graded_and_a_pick_at_its_first_plus_ev_publish(self):
        summary = grade.grade({"nhl": nhl})
        g = summary["leagues"]["nhl"]
        close_over = books.devig(1 / 2.0, 1 / 1.9)[0]
        self.assertEqual(g["fair"]["confident"]["n"], 1)                     # Novig alone is confident
        self.assertAlmostEqual(g["books"]["novig"]["log_loss"], -math.log(close_over), places=3)
        ud = g["targets"]["underdog"]
        self.assertEqual(ud["picks"], 1)                                     # the under, at noon
        self.assertAlmostEqual(ud["ev_shown"], (1 - books.devig(1 / 2.5, 1 / 1.6)[0]) * 1.87 - 1, places=3)
        self.assertAlmostEqual(ud["clv"], (1 - close_over) * 1.87 - 1, places=3)   # the line moved against it
        self.assertEqual((ud["won"], ud["roi"]), (0.0, -1.0))                # he scored
        self.assertEqual(json.loads(grade.OUT.read_text())["leagues"]["nhl"]["targets"]["underdog"]["picks"], 1)

    def test_a_player_who_did_not_play_is_void(self):
        for path in (store.DATA / "nhl" / "boxscores").rglob("*.parquet"):
            path.unlink()   # only a teammate played that night
        store.Store(nhl).write_days(game_logs([{"game_id": 1, "date": "2026-10-14", "player_id": 2, "name": "P2",
                                                "team": "MTL", "opponent": "WSH", "goals": 0}]))
        g = grade.grade({"nhl": nhl})["leagues"]["nhl"]
        self.assertEqual((g["fair"]["all"]["n"], g["targets"]), (0, {}))
