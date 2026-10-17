import datetime as dt
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from props import site, store
from props.leagues import LEAGUES
from props.leagues import mlb, nba, nfl, nhl

from .helpers import game_logs, pinnacle_payload, underdog_payload

TODAY = dt.date(2026, 10, 2)
EVENING = dt.datetime(2026, 10, 2, 18, 0, tzinfo=site.ET)   # the clock during a build, unless a test moves it


def season_rows(season: int, dates: list[str], **extra) -> list[dict]:
    return [{"game_id": int(d.replace("-", "")), "date": d, "season": season, "player_id": 1,
             "name": "David Pastrnak", "team": "BOS", "opponent": "WPG", **extra} for d in dates]


class TempStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        patches = [
            mock.patch.object(store, "DATA", root / "data"),
            mock.patch.object(site, "OUT", root / "site"),
            mock.patch.object(site, "CACHE", root / "data" / "cache"),
            mock.patch.object(site, "_sports", []),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.store = store.Store(nhl)
        self.rosters = {}
        self.pin = pinnacle_payload([])
        self.fd = {"markets": []}
        self.dk = {"subcategories": []}
        self.kambi = {"offers": []}
        self.mise = {"markets": []}
        self.bovada = {"markets": []}
        self.novig = {"markets": []}
        self.kalshi = {"markets": []}
        self.polymarket = {"markets": []}
        self.betano = {"markets": []}
        self.stake = {"fixtures": []}
        self.pick6 = {"pickCardsResponse": {}}
        self.now = EVENING

    def build(self, fetch=None, league=nhl, pin_fetch=None, fd_fetch=None, dk_fetch=None, kambi_fetch=None, mise_fetch=None,
              bovada_fetch=None, novig_fetch=None, kalshi_fetch=None, polymarket_fetch=None, betano_fetch=None, stake_fetch=None,
              pick6_fetch=None, kambi_games=None, **kwargs):
        slate, board = site.slate_date, site.board_date
        with mock.patch.object(site, "slate_date", side_effect=lambda now=None: slate(now or self.now)), \
             mock.patch.object(site, "board_date", side_effect=lambda games, now=None: board(games, now or self.now)), \
             mock.patch.object(site, "_utcnow", lambda: self.now), \
             mock.patch.object(site.underdog, "fetch", fetch or (lambda sport: self.lines)), \
             mock.patch.object(site.books.pinnacle, "fetch", pin_fetch or (lambda league: self.pin)), \
             mock.patch.object(site.books.fanduel, "fetch",
                               lambda league, until=None, now=None: (fd_fetch or (lambda league: self.fd))(league)), \
             mock.patch.object(site.books.draftkings, "fetch", dk_fetch or (lambda league: self.dk)), \
             mock.patch.object(site.books.kambi, "fetch", kambi_fetch or (lambda league: self.kambi)), \
             mock.patch.object(site.kambi, "games", kambi_games or (lambda league: getattr(self, "kambi_games", []))), \
             mock.patch.object(site.books.mise, "fetch", mise_fetch or (lambda league: self.mise)), \
             mock.patch.object(site.books.bovada, "fetch",   # asked for the board's games only: until the last one
                               lambda league, until=None, now=None: (setattr(self, "bovada_until", until),
                                                           (bovada_fetch or (lambda league: self.bovada))(league))[1]), \
             mock.patch.object(site.books.novig, "fetch",
                               lambda league, until=None, now=None: (novig_fetch or (lambda league: self.novig))(league)), \
             mock.patch.object(site.books.kalshi, "fetch",
                               lambda league, until=None, now=None: (kalshi_fetch or (lambda league: self.kalshi))(league)), \
             mock.patch.object(site.books.polymarket, "fetch",
                               lambda league, until=None, now=None: (polymarket_fetch or (lambda league: self.polymarket))(league)), \
             mock.patch.object(site.books.betano, "fetch",
                               lambda league, until=None, now=None: (betano_fetch or (lambda league: self.betano))(league)), \
             mock.patch.object(site.books.stake, "fetch",
                               lambda league, until=None, now=None: (stake_fetch or (lambda league: self.stake))(league)), \
             mock.patch.object(site.books.pick6, "fetch", pick6_fetch or (lambda league, until=None, now=None: self.pick6)), \
             mock.patch.object(league, "schedule", return_value=self.schedule, create=True), \
             mock.patch.object(league, "rosters", side_effect=lambda teams, today: self.rosters, create=True), \
             mock.patch("builtins.print"):
            out = site.build(league, **kwargs)
        html = out.read_text()
        return json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S)
                          .group(1).replace("<\\/", "</"))


class StoreTest(TempStore):
    def test_empty_store(self):
        self.assertTrue(self.store.load().is_empty())
        self.assertIsNone(self.store.last_date())

    def test_daily_files_then_compaction_at_rollover(self):
        self.store.write_days(game_logs(season_rows(20252026, ["2026-04-10", "2026-04-12"])))
        self.assertEqual(sorted(p.name for p in (self.store.box / "20252026").iterdir()),
                         ["2026-04-10.parquet", "2026-04-12.parquet"])
        self.assertEqual(self.store.compact_finished(), [])              # still the newest season

        self.store.write_days(game_logs(season_rows(20262027, ["2026-10-01"])))
        self.assertEqual(self.store.compact_finished(), [20252026])
        self.assertTrue((self.store.box / "20252026.parquet").is_file())
        self.assertFalse((self.store.box / "20252026").exists())
        self.assertEqual(self.store.load().height, 3)
        self.assertEqual(self.store.last_date(), dt.date(2026, 10, 1))

    def test_finished_seasons_are_not_rewritten_by_a_refresh(self):
        self.store.write_season(20252026, game_logs(season_rows(20252026, ["2026-04-10"], shots=3)))
        written = self.store.write_days(game_logs(season_rows(20252026, ["2026-04-10"], shots=9)))
        self.assertEqual(written, 0)
        self.assertEqual(self.store.load()["shots"].to_list(), [3])

    def test_rewriting_a_day_replaces_it(self):
        self.store.write_days(game_logs(season_rows(20262027, ["2026-10-01"], shots=3)))
        self.store.write_days(game_logs(season_rows(20262027, ["2026-10-01"], shots=4)))   # stat correction
        self.assertEqual(self.store.load()["shots"].to_list(), [4])


class BoardGamesTest(unittest.TestCase):
    def test_a_slow_book_is_asked_for_the_slate_at_once_then_for_later_games_underdog_put_on_the_board(self):
        from concurrent.futures import Future
        slate = dt.datetime(2026, 10, 3, 2, tzinfo=dt.timezone.utc)
        calls = []

        class Book:
            @staticmethod
            def fetch(league, now=None, until=None):
                calls.append((now, until))
                return {"markets": [f"by {until:%d %H}h"]}

        later = Future()
        later.set_result(dt.datetime(2026, 10, 3, 23, tzinfo=dt.timezone.utc))
        self.assertEqual(site._board_games(Book, nhl, slate, later), {"markets": ["by 03 02h", "by 03 23h"]})
        self.assertEqual(calls, [(None, slate), (slate + dt.timedelta(seconds=1), later.result())])

        calls.clear()
        same = Future()
        same.set_result(slate)   # nothing past the slate: asked once
        site._board_games(Book, nhl, slate, same)
        self.assertEqual(len(calls), 1)


class GamesTest(unittest.TestCase):
    def test_a_game_under_way_or_over_is_left_off_the_slate(self):
        now = dt.datetime(2026, 10, 13, 23, 5, tzinfo=dt.timezone.utc)
        game = lambda away, home, start, state: {"date": "2026-10-13", "away": away, "home": home,
                                                  "start": start, "state": state}
        schedule = [game("NJD", "DET", "2026-10-13T22:00:00Z", "live"),
                    game("SEA", "PHI", "2026-10-13T23:00:00Z", "upcoming"),   # started since the schedule was read
                    game("TBL", "NYR", "2026-10-13T23:15:00Z", "upcoming")]
        lines = [{"game": "2026-10-13|NJD@DET", "start": "2026-10-13T22:00:00Z"}]
        games = site._games(schedule, lines, dt.date(2026, 10, 13), 1, now)
        self.assertEqual([g["key"] for g in games], ["2026-10-13|TBL@NYR"])


class BuildTest(TempStore):
    def setUp(self):
        super().setUp()
        shots = {"2026-09-28": 5, "2026-09-29": 2, "2026-09-30": 4, "2026-10-01": 1}
        self.store.write_days(game_logs([
            row for date, n in shots.items()
            for row in season_rows(20262027, [date], position="R", shots=n, points=n % 2, ppp=0, faceoff_wins=0)
        ]))
        self.lines = underdog_payload([
            {"name": "David Pastrnak", "team": "BOS", "stat": "shots", "value": 3.5,
             "options": [("higher", 2.0, 1.07), ("lower", 1.66, 0.89)]},
            {"name": "Viggo Bjorck", "team": "WPG", "stat": "points", "value": 0.5, "options": [("higher", 1.87, 1.0)]},
        ])
        self.rosters = {"BOS": [1], "WPG": []}
        self.schedule = [{"date": "2026-10-02", "start": "2026-10-03T00:00:00Z",
                          "away": "BOS", "home": "WPG", "state": "upcoming"}]

    def test_bovada_is_asked_only_for_games_up_to_the_last_on_the_board(self):
        self.schedule.append({"date": "2026-10-03", "start": "2026-10-03T23:00:00Z",   # tomorrow: not on the board
                              "away": "TOR", "home": "MTL", "state": "upcoming"})
        data = self.build()
        self.assertEqual([g["key"] for g in data["games"]], ["2026-10-02|BOS@WPG"])
        self.assertEqual(self.bovada_until, dt.datetime(2026, 10, 3, tzinfo=dt.timezone.utc))

    def test_board_row_carries_line_rates_and_context(self):
        data = self.build()
        row = data["rows"][0]
        self.assertEqual((row["name"], row["id"], row["team"], row["opp"], row["home"]), ("David Pastrnak", 1, "BOS", "WPG", False))
        self.assertEqual((row["stat"], row["line"], row["over"], row["under"]),
                         ("shots", 3.5, {"odds": 2.0, "mult": 1.07}, {"odds": 1.66, "mult": 0.89}))
        self.assertEqual(data["standardOdds"], 1.87)             # the price of the one 1.0x pick
        self.assertEqual(row["l5"], [2, 2, 4])
        self.assertTrue(row["ud"])
        self.assertEqual(row["last"], [5, 2, 4, 1])
        self.assertEqual(data["recent"]["1"], [["2026-09-28", "WPG"], ["2026-09-29", "WPG"],
                                               ["2026-09-30", "WPG"], ["2026-10-01", "WPG"]])
        self.assertEqual(row["rest"], 0)                         # played last night
        self.assertEqual(row["game"], "2026-10-02|BOS@WPG")
        self.assertEqual(data["games"][0]["lines"], 2)

    def test_defense_ranks_are_out_of_the_teams_playing_now(self):
        self.store.write_season(20212022, game_logs(season_rows(20212022, ["2021-10-12"], team="ARI", opponent="SEA", shots=1)))
        self.assertEqual(self.build()["teams"], 1)               # only WPG has faced anyone in the ranked seasons

    def test_every_other_prop_gets_a_typical_line(self):
        typical = {row["stat"]: row for row in self.build()["rows"] if not row["ud"]}
        self.assertNotIn("shots", typical)                       # Underdog already covers shots
        self.assertNotIn("faceoff_wins", typical)                # he never takes faceoffs
        self.assertNotIn("ppp", typical)                         # never scores on the power play: under is a given
        self.assertNotIn("saves", typical)                       # not a goalie
        points = typical["points"]
        self.assertEqual((points["id"], points["team"], points["opp"], points["line"], points["game"]),
                         (1, "BOS", "WPG", 0.5, "2026-10-02|BOS@WPG"))
        self.assertNotIn("over", points)
        self.assertEqual(points["l5"], [2, 2, 4])

    def test_players_who_have_not_dressed_lately_are_left_out(self):
        self.rosters = {"BOS": [1, 2], "WPG": []}
        old = season_rows(20252026, ["2025-10-10"], shots=2)[0] | {"player_id": 2, "name": "Long Gone"}
        self.store.write_season(20252026, game_logs([old] + season_rows(
            20252026, ["2025-11-01", "2025-11-02", "2025-11-03", "2025-11-04", "2025-11-05"], player_id=3)))
        names = {row["name"] for row in self.build()["rows"]}
        self.assertNotIn("Long Gone", names)

    def test_without_rosters_the_last_team_stands_in(self):
        self.rosters = {}
        self.assertTrue(any(not row["ud"] and row["id"] == 1 for row in self.build()["rows"]))

    def test_player_without_nhl_games_is_listed_but_flagged(self):
        data = self.build()
        rookie = next(row for row in data["rows"] if row["name"] == "Viggo Bjorck")
        self.assertIsNone(rookie["id"])
        self.assertNotIn("l10", rookie)

    def test_board_player_from_seasons_ago_still_gets_a_page(self):
        veteran = season_rows(20222023, ["2022-11-01"], shots=3)[0] | {"player_id": 9, "name": "Old Timer", "team": "WPG"}
        self.store.write_season(20222023, game_logs([veteran]))
        self.lines = underdog_payload([{"name": "Old Timer", "team": "WPG", "stat": "shots", "value": 1.5,
                                        "options": [("higher", 1.87, 1.0)]}])
        data = self.build()
        self.assertEqual(data["rows"][0]["id"], 9)
        self.assertIn(9, [player[0] for player in data["players"]])
        self.assertTrue((site.OUT / "p" / "nhl" / "9.js").is_file())

    def test_player_files_are_rewritten_only_when_the_game_logs_change(self):
        self.build()
        page = site.OUT / "p" / "nhl" / "1.js"
        page.write_text("as uploaded")   # stands in for a file no build should touch
        self.build()
        self.assertEqual(page.read_text(), "as uploaded")   # same logs, same players: left alone
        self.store.write_season(20222023, game_logs(season_rows(20222023, ["2022-11-01"], shots=3)))
        self.build()
        self.assertTrue(page.read_text().startswith("SCBOARD.player("))

    def test_player_log_file_is_written_newest_first(self):
        self.build()
        text = (site.OUT / "p" / "nhl" / "1.js").read_text()
        log = json.loads(text.removeprefix("SCBOARD.player(").removesuffix(")"))
        self.assertEqual(log["id"], 1)
        self.assertEqual(log["games"]["date"][0], "2026-10-01")
        self.assertEqual(log["games"]["shots"], [1, 4, 2, 5])
        self.assertEqual(log["games"]["rest"], [0, 0, 0, 3])

    def test_player_log_file_lists_teammates_and_the_games_they_shared(self):
        linemate = [row | {"player_id": 2, "name": "Pavel Zacha"} for row in
                    season_rows(20262027, ["2026-09-28", "2026-09-29", "2026-10-01"], position="C", shots=1)]
        self.store.write_days(game_logs(season_rows(20262027, ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"],
                                                    position="R", shots=3) + linemate))
        self.build()
        log = json.loads((site.OUT / "p" / "nhl" / "1.js").read_text().removeprefix("SCBOARD.player(").removesuffix(")"))
        self.assertEqual(log["mates"], [[2, "Pavel Zacha", "C", [1, 1, 2]]])   # he sat out 9/30

    def test_playoff_games_are_a_second_set_of_rates_not_part_of_the_first(self):
        self.store.write_season(20252026, game_logs(season_rows(
            20252026, ["2026-05-01", "2026-05-03"], position="R", shots=6, points=1, game_type=3)))
        data = self.build()
        row = data["rows"][0]
        self.assertEqual((row["l10"], row["last"]), ([2, 2, 4], [5, 2, 4, 1]))                    # as without them
        self.assertEqual((row["po"]["l10"], row["po"]["last"]), ([4, 2, 6], [6, 6, 5, 2, 4, 1]))
        self.assertEqual(len(data["recent"]["1"]), 4)
        self.assertEqual(data["recentPo"]["1"][0], ["2026-05-01", "WPG"])
        typical = next(r for r in data["rows"] if not r["ud"] and r["stat"] == "points")
        self.assertEqual(typical["line"], 0.5)                   # from the four regular-season games, not the playoffs

    def test_rows_without_playoff_games_carry_one_set_of_rates(self):
        data = self.build()
        self.assertNotIn("po", data["rows"][0])
        self.assertEqual(data["recentPo"], {})

    def test_failed_fetch_falls_back_to_the_last_saved_lines(self):
        self.build()

        def down(sport):
            raise httpx.ConnectError("blocked")

        data = self.build(fetch=down)
        self.assertEqual(sum(row["ud"] for row in data["rows"]), 2)
        self.assertIn("ConnectError", data["lines"]["error"])

    def test_failed_fetch_with_nothing_saved_builds_an_empty_board(self):
        def down(sport):
            raise httpx.ConnectError("blocked")

        data = self.build(fetch=down)
        self.assertFalse(any(row["ud"] for row in data["rows"]))
        self.assertTrue(data["rows"])                           # the slate is still there at typical lines
        self.assertIsNone(data["lines"]["fetched"])
        self.assertEqual(len(data["games"]), 1)                 # tonight's game still shows
        self.assertEqual(data["players"][0][:2], [1, "David Pastrnak"])
        self.assertEqual(data["players"][0][4], "https://assets.nhle.com/mugs/nhl/20262027/BOS/1.png")

    def age_saved_copies(self):
        """Make every saved fetch look as if it was made on the previous slate."""
        then = dt.datetime(2026, 10, 1, 18, 0, tzinfo=site.ET).timestamp()
        for path in site.CACHE.glob("*.json"):
            os.utime(path, (then, then))

    def test_offline_build_fetches_again_when_the_saved_lines_are_from_an_earlier_slate(self):
        self.build()
        self.age_saved_copies()
        self.lines["over_under_lines"][0]["stat_value"] = "4.5"   # Underdog has moved on since
        data = self.build(offline=True)
        shots = next(row for row in data["rows"] if row["stat"] == "shots" and row["ud"])
        self.assertEqual(shots["line"], 4.5)
        self.assertIsNone(data["lines"]["error"])

    def test_a_saved_copy_from_an_earlier_slate_is_never_used_when_the_fetch_fails(self):
        self.pin = pinnacle_payload([("David Pastrnak", "Shots On Goal", 3.5, -125, 105)])
        self.build()
        self.age_saved_copies()

        def down(_):
            raise httpx.ConnectError("blocked")

        data = self.build(fetch=down, pin_fetch=down)
        self.assertFalse(any(row["ud"] for row in data["rows"]))
        self.assertIn("2026-10-01 slate", data["lines"]["error"])
        self.assertEqual(data["books"][0]["prices"], {})      # yesterday's prices don't land on today's rows

    def test_an_answer_a_book_cant_read_is_reported_and_never_replaces_the_good_copy(self):
        self.novig = {"markets": [{"type": "SHOTS_ON_GOAL", "player": "David Pastrnak", "strike": 3.5,
                                   "over": 0.5, "under": 0.55}]}
        self.build()
        saved = (site.CACHE / "nhl-novig.json").read_text()
        data = self.build(novig_fetch=lambda league: {"markets": [{"renamed": True}]}, offline=False)
        self.assertEqual((site.CACHE / "nhl-novig.json").read_text(), saved)   # the good copy stands
        novig = next(book for book in data["books"] if book["key"] == "novig")
        self.assertIn("1|shots", novig["prices"])                             # and is on the board
        self.assertTrue(any(row["ud"] for row in data["rows"]))               # the rest of the board is fine

    def test_a_saved_copy_that_cant_be_read_leaves_the_book_off_instead_of_stopping_the_build(self):
        self.build()
        (site.CACHE / "nhl-novig.json").write_text(json.dumps({"markets": [{"renamed": True}]}))
        data = self.build(offline=True)
        novig = next(book for book in data["books"] if book["key"] == "novig")
        self.assertEqual(novig["prices"], {})
        self.assertTrue(data["rows"])

    def test_lines_for_a_slate_that_already_finished_are_dropped(self):
        self.lines["games"][0]["scheduled_at"] = "2026-10-01T23:00:00Z"
        self.assertFalse(any(row["ud"] for row in self.build()["rows"]))

    def test_after_midnight_the_board_moves_on_once_last_night_is_final(self):
        self.now = dt.datetime(2026, 10, 3, 1, 30, tzinfo=site.ET)
        self.schedule[0]["state"] = "final"
        self.schedule.append({"date": "2026-10-03", "start": "2026-10-03T23:00:00Z",
                              "away": "WPG", "home": "BOS", "state": "upcoming"})
        data = self.build()
        self.assertEqual(data["date"], "2026-10-03")
        self.assertEqual([g["key"] for g in data["games"]], ["2026-10-03|WPG@BOS"])
        self.assertFalse(any(row["ud"] for row in data["rows"]))   # last night's lines are gone

    def test_after_midnight_a_game_still_going_holds_the_board_without_its_lines(self):
        self.now = dt.datetime(2026, 10, 3, 1, 30, tzinfo=site.ET)
        self.schedule[0]["state"] = "live"
        data = self.build()
        self.assertEqual(data["date"], "2026-10-02")
        self.assertEqual(data["games"], [])
        self.assertFalse(any(row["ud"] for row in data["rows"]))   # under way: no longer bettable

    def test_pinnacle_prices_are_keyed_like_the_rows(self):
        self.pin = pinnacle_payload([("David Pastrnak", "Shots On Goal", 3.5, -125, 105),
                                     ("Someone Else", "Goals", 0.5, 300, -400)])
        pinnacle = self.build()["books"][0]
        self.assertEqual((pinnacle["key"], pinnacle["prices"]), ("pinnacle", {"1|shots": [3.5, 1.8, 2.05]}))
        self.assertIsNotNone(pinnacle["fetched"])

    def test_every_book_gets_its_own_prices_in_order(self):
        self.fd = {"markets": [{"name": "David Pastrnak Shots on Goal", "runners": [
            {"name": "David Pastrnak - Over", "handicap": 3.5, "status": "ACTIVE", "odds": 1.95},
            {"name": "David Pastrnak - Under", "handicap": 3.5, "status": "ACTIVE", "odds": 1.87}]}]}
        data = self.build()
        books = data["books"]
        self.assertEqual([(b["key"], b["short"], b["weight"], b["sharp"], b["target"]) for b in books],
                         [("pinnacle", "Pinnacle", 1.4, True, False),
                          ("fanduel", "FanDuel", 1, True, True),   # sharp, and a site to bet on: left out of fair when picked
                          ("draftkings", "DK", 1.4, True, False),
                          ("kambi", "Kambi", 0, False, False),    # shown only: BetRivers' odds
                          ("novig", "Novig", 7, True, False), ("kalshi", "Kalshi", 7, True, False),
                          ("polymarket", "Poly", 7, True, False),
                          ("mise", "Mise", 0, False, True),       # a site to bet on, never part of the fair chance
                          ("betano", "Betano", 0, False, True),   # likewise
                          ("stake", "Stake", 0, False, True),     # likewise
                          ("pick6", "Pick6", 0, False, False),    # shown only: its one flat pick'em price
                          ("bovada", "Bovada", 0, False, False)])  # shown only
        self.assertEqual(data["fair"], {"maxOverround": 0.1,
                                        "confident": [["novig"], ["kalshi"], ["polymarket"], ["pinnacle", "draftkings", "fanduel"]],
                                        "estimate": ["novig", "kalshi", "polymarket"]})
        self.assertEqual(books[1]["prices"], {"1|shots": [3.5, 1.95, 1.87]})
        self.assertIsNone(books[0]["fetched"])                  # no Pinnacle prices, so no time to show

    def test_a_book_down_leaves_the_board_without_its_prices(self):
        def down(league):
            raise httpx.ConnectError("blocked")

        data = self.build(pin_fetch=down, fd_fetch=down, dk_fetch=down, kambi_fetch=down, mise_fetch=down, bovada_fetch=down,
                          novig_fetch=down, kalshi_fetch=down, polymarket_fetch=down, betano_fetch=down,
                          stake_fetch=down, pick6_fetch=down)
        self.assertEqual([(b["prices"], b["fetched"]) for b in data["books"]], [({}, None)] * 12)
        self.assertEqual(sum(row["ud"] for row in data["rows"]), 2)


class NflBuildTest(TempStore):
    def setUp(self):
        super().setUp()
        yards = {"2026-09-13": 95, "2026-09-20": 60, "2026-09-27": 110}
        store.Store(nfl).write_days(game_logs([
            {"game_id": f"g{date}", "date": date, "season": 2026, "player_id": "00-0039075", "name": "Puka Nacua",
             "team": "LAR", "opponent": "SF", "position": "WR", "snaps": 55, "snap_pct": 0.9, "rec_yds": n,
             "receptions": n // 12, "targets": n // 9, "rush_rec_tds": n // 100, "rush_yds": 0, "fumbles_lost": 0,
             "headshot": "https://img/puka"}
            for date, n in yards.items()
        ], nfl))
        self.lines = underdog_payload([{"name": "Puka Nacua", "team": "LAR", "stat": "receiving_yds", "value": 80.5,
                                        "position": "WR", "sport": "NFL",
                                        "options": [("higher", 1.87, 1.0), ("lower", 1.87, 1.0)]},
                                       {"name": "Puka Nacua", "team": "LAR", "stat": "receiving_long", "value": 24.5,
                                        "position": "WR", "sport": "NFL", "options": [("higher", 1.87, 1.0)]}])
        self.lines["games"][0].update(title="LAR @ PHI", away_team_id="lar", home_team_id="phi",
                                      scheduled_at="2026-10-04T17:00:00Z")
        self.schedule = [{"date": "2026-10-04", "start": "2026-10-04T17:00:00Z", "away": "LAR", "home": "PHI",
                          "state": "upcoming"}]

    def test_board_uses_the_leagues_stats_ids_and_labels(self):
        data = self.build(league=nfl)
        self.assertEqual((data["league"], data["seasonLabel"], data["prevSeasonLabel"], data["rest"]),
                         ("nfl", "2026", "2025", False))
        row = data["rows"][0]
        self.assertEqual((row["id"], row["stat"], row["line"], row["team"], row["opp"], row["game"]),
                         ("00-0039075", "rec_yds", 80.5, "LAR", "PHI", "2026-10-04|LAR@PHI"))
        self.assertEqual((row["l5"], row["last"]), ([2, 1, 3], [95, 60, 110]))
        self.assertNotIn("rest", row)
        self.assertEqual(data["players"][0], ["00-0039075", "Puka Nacua", "LAR", "WR", "https://img/puka"])
        self.assertEqual(data["standardOdds"], 1.87)
        self.assertEqual(data["props"][0], ["pass_yds", "Pass Yards", "PASS YDS", "Pass Yds"])

    def test_page_carries_every_leagues_underdog_settings_for_refresh(self):
        config = self.build(league=nfl)["underdog"]
        self.assertEqual(set(config), {"nhl", "nfl", "nba", "mlb", "cfb", "soccer"})
        self.assertIn("sport_id=NHL", config["nhl"]["url"])
        self.assertEqual((config["nfl"]["sport"], config["nfl"]["stats"]["receiving_yds"], config["nfl"]["teams"]["LA"]),
                         ("NFL", "rec_yds", "LAR"))

    def test_every_tab_has_a_short_name(self):
        for league in LEAGUES.values():
            self.assertLessEqual(set(league.PROPS), set(league.TABS), league.NAME)

    def test_sunday_game_is_on_fridays_slate_and_untracked_stats_are_dropped(self):
        data = self.build(league=nfl)
        self.assertEqual([(g["key"], g["lines"]) for g in data["games"]], [("2026-10-04|LAR@PHI", 1)])
        self.assertEqual({row["stat"] for row in data["rows"] if row["ud"]}, {"rec_yds"})
        typical = {row["stat"]: row["line"] for row in data["rows"] if not row["ud"]}
        self.assertEqual(typical["receptions"], 7.5)             # 7, 5 and 9 catches: averages 7

    def test_team_with_two_games_this_week_is_listed_for_the_first_only(self):
        self.schedule.append({"date": "2026-10-08", "start": "2026-10-09T00:15:00Z", "away": "SEA", "home": "LAR",
                              "state": "upcoming"})
        data = self.build(league=nfl)
        self.assertEqual(len(data["games"]), 2)
        self.assertEqual({row["game"] for row in data["rows"]}, {"2026-10-04|LAR@PHI"})
        keys = [(row["id"], row["stat"]) for row in data["rows"]]
        self.assertEqual(len(keys), len(set(keys)))

    def test_page_and_player_file_are_the_leagues_own(self):
        self.build(league=nfl)
        self.assertTrue((site.OUT / "nfl.html").is_file())
        log = json.loads((site.OUT / "p" / "nfl" / "00-0039075.js").read_text()
                         .removeprefix("SCBOARD.player(").removesuffix(")"))
        self.assertEqual((log["id"], log["games"]["rec_yds"], log["games"]["snaps"]),
                         ("00-0039075", [110, 60, 95], [55, 55, 55]))
        self.assertNotIn("rest", log["games"])


    # 2+ TDs: rush + rec TDs over 1.5, a tab of its own beside the anytime TD row
    def books_with_two_tds(self):
        rung = lambda name, odds: {"market": "2+ TDs", "name": "2+ TDs", "player": name, "side": "", "line": None,
                                   "odds": odds, "main": False, "milestone": None}
        self.dk = {"subcategories": [{"name": "TD Scorer", "selections": [
            rung("Puka Nacua", 6.5), rung("Davante Adams", 9.0),   # Adams has no game log here
            {**rung(None, 21.0), "name": "2+ TDs"},                # a team's D/ST: no player
            {"market": "Anytime TD Scorer", "name": "Anytime TD Scorer", "player": "Puka Nacua", "side": "",
             "line": None, "odds": 1.9, "main": False, "milestone": None}]}]}
        self.mise = {"markets": [{"name": "Puka Nacua Total touchdowns scored", "code": "PLAYER_UNDEFINED_TOTAL_TOUCHDOWNS",
                                  "line": None, "status": "ACTIVE", "outcomes": [
                                      {"name": "1+", "status": "ACTIVE", "odds": 1.8},
                                      {"name": "2+", "status": "ACTIVE", "odds": 7.0}]}]}

    def test_a_player_a_book_prices_at_two_tds_gets_a_row_at_1_5(self):
        self.books_with_two_tds()
        data = self.build(league=nfl)
        rows = {(row["name"], row["stat"]): row for row in data["rows"]}
        row = rows[("Puka Nacua", "two_tds")]
        self.assertEqual((row["line"], row["ud"], row["last"]), (1.5, False, [0, 0, 1]))
        books = {b["key"]: b["prices"] for b in data["books"]}
        self.assertEqual(books["draftkings"]["00-0039075|two_tds"], [1.5, 6.5, None])
        self.assertEqual(books["mise"]["00-0039075|two_tds"], [1.5, 7.0, None])
        # the anytime row keeps the 0.5 price
        self.assertEqual(books["draftkings"]["00-0039075|rush_rec_tds"], [0.5, 1.9, None])

    def test_without_a_book_price_there_is_no_two_td_row(self):
        data = self.build(league=nfl)
        self.assertNotIn("two_tds", {row["stat"] for row in data["rows"]})

    def test_underdogs_td_line_at_1_5_is_the_two_td_row(self):
        self.lines = underdog_payload([{"name": "Puka Nacua", "team": "LAR", "stat": "rush_rec_tds", "value": 1.5,
                                        "position": "WR", "sport": "NFL", "options": [("higher", 4.1, 2.2)]}])
        self.lines["games"][0].update(title="LAR @ PHI", away_team_id="lar", home_team_id="phi",
                                      scheduled_at="2026-10-04T17:00:00Z")
        data = self.build(league=nfl)
        posted = [(row["stat"], row["line"]) for row in data["rows"] if row["ud"]]
        self.assertEqual(posted, [("two_tds", 1.5)])
        self.assertEqual(data["underdog"]["nfl"]["fixed"], {"two_tds": ["rush_rec_tds", 1.5]})


    def test_viewer_page_lists_underdogs_sports_and_each_league_links_its_players(self):
        self.build(league=nfl)
        html = (site.OUT / "ud.html").read_text()
        data = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S)
                          .group(1).replace("<\\/", "</"))
        self.assertEqual(data["sports"], [["NFL", "NFL", 2]])
        self.assertTrue(data["url"].endswith("sport_id="))
        self.assertNotIn(["UD Lines", "ud.html"], data["nav"])   # an app of its own, not a league
        links = json.loads((site.OUT / "ud" / "nfl.js").read_text().removeprefix("UDLINKS.push(").removesuffix(")"))
        self.assertEqual((links["page"], links["sport"], links["players"]), ("nfl.html", "NFL", {"puka nacua": "00-0039075"}))
        self.assertEqual(links["stats"]["receiving_yds"], "rec_yds")


class MlbBuildTest(TempStore):
    """Six LAD games: the starter last pitched five games ago, the bench bat last started five games ago."""

    def setUp(self):
        super().setUp()
        dates = ["2026-09-26", "2026-09-27", "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"]
        rows = []
        for i, date in enumerate(dates):
            common = {"game_id": i, "date": date, "season": 2026, "team": "LAD", "opponent": "SD"}
            rows.append({**common, "player_id": 1, "name": "Shohei Ohtani", "position": "TWP", "order": 1,
                         "hits": i % 3, "total_bases": i % 4, "runs": 1, "rbis": i % 2, "hits_runs_rbis": 1 + i % 3 + i % 2,
                         "singles": i % 2, "walks": i % 2, "batter_ks": i % 2, "home_runs": i % 2, "stolen_bases": i % 2,
                         "strikeouts": 8 if i == 1 else None, "started": i == 1 or None, "outs": 18 if i == 1 else None})
            rows.append({**common, "player_id": 2, "name": "Bench Bat", "position": "1B",
                         "order": 7 if i == 1 else None, "hits": i % 2, "total_bases": i % 2})
            if i < 2:
                rows.append({**common, "player_id": 3, "name": "Tarik Skubal", "position": "P", "started": True,
                             "strikeouts": 7 + 2 * i, "outs": 18, "hits_allowed": 5, "earned_runs": 2, "walks_allowed": 1})
        store.Store(mlb).write_days(game_logs(rows, mlb))
        self.lines = underdog_payload([])
        self.rosters = {"LAD": [1, 2, 3], "SD": []}
        self.schedule = [{"date": "2026-10-02", "start": "2026-10-02T23:00:00Z", "away": "SD", "home": "LAD",
                          "state": "upcoming"}]

    def test_each_kind_has_its_own_window_for_who_is_playing(self):
        typical = {(row["name"], row["stat"]) for row in self.build(league=mlb)["rows"]}
        self.assertIn(("Tarik Skubal", "strikeouts"), typical)      # a starter goes every fifth game
        self.assertFalse({name for name, _ in typical} & {"Bench Bat"})   # hasn't started in five
        self.assertIn(("Shohei Ohtani", "hits"), typical)
        self.assertNotIn(("Shohei Ohtani", "strikeouts"), typical)  # his pitching lines come from Underdog

    def test_relief_and_pinch_hit_games_stay_out_of_the_rates(self):
        self.build(league=mlb)
        log = json.loads((site.OUT / "p" / "mlb" / "2.js").read_text()
                         .removeprefix("SCBOARD.player(").removesuffix(")"))
        self.assertEqual(log["games"]["date"], ["2026-09-27"])


if __name__ == "__main__":
    unittest.main()
