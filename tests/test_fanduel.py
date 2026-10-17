import datetime as dt
import unittest
from unittest import mock

from props import fanduel
from props.leagues import mlb, nfl, nhl


def market(name, over, under, handicap=0, status="ACTIVE"):
    return {"name": name, "runners": [
        {"name": over[0], "handicap": handicap, "status": status, "odds": over[1]},
        {"name": under[0], "handicap": handicap, "status": "ACTIVE", "odds": under[1]}]}


class ParseTest(unittest.TestCase):
    def test_every_way_fanduel_names_a_side(self):
        props, skipped = fanduel.parse({"markets": [
            market("Gerrit Cole - Strikeouts", ("Gerrit Cole Over", 2.34), ("Gerrit Cole Under", 1.581), handicap=5.5),
            market("Tarik Skubal Outs Recorded", ("Tarik Skubal Over 17.5", 1.5), ("Tarik Skubal Under 17.5", 2.42)),
        ]}, mlb)
        self.assertEqual(props, [
            {"player": "Gerrit Cole", "stat": "strikeouts", "line": 5.5, "over": 2.34, "under": 1.581},
            {"player": "Tarik Skubal", "stat": "outs", "line": 17.5, "over": 1.5, "under": 2.42},
        ])
        self.assertFalse(skipped)
        hockey, _ = fanduel.parse({"markets": [market("Juraj Slafkovsky Total Goals", ("Juraj Slafkovsky - Over", 3.3),
                                                      ("Juraj Slafkovsky - Under", 1.3125), handicap=0.5)]}, nhl)
        self.assertEqual(hockey, [{"player": "Juraj Slafkovsky", "stat": "goals", "line": 0.5, "over": 3.3, "under": 1.312}])

    def test_game_totals_ladders_and_suspended_sides_are_left_out(self):
        props, skipped = fanduel.parse({"markets": [
            market("Total Points", ("Over", 1.9), ("Under", 1.9), handicap=46.5),
            {"name": "Player to Record 2+ Shots on Goal", "runners": [
                {"name": "Cole Caufield", "handicap": 0, "status": "ACTIVE", "odds": 1.5},
                {"name": "Sidney Crosby", "handicap": 0, "status": "ACTIVE", "odds": 1.8}]},
            market("Derrick Henry - Receiving Yds", ("Derrick Henry Over", 1.88), ("Derrick Henry Under", 1.88),
                   handicap=7.5, status="SUSPENDED"),
        ]}, nfl)
        self.assertEqual((props, dict(skipped)), ([], {}))
        suspended = market("Gerrit Cole - Strikeouts", ("Gerrit Cole Over", 2.34), ("Gerrit Cole Under", 1.581), handicap=5.5)
        self.assertEqual(fanduel.parse({"markets": [{**suspended, "status": "SUSPENDED"}]}, mlb)[0], [])

    def test_unmapped_stats_are_counted_not_guessed(self):
        props, skipped = fanduel.parse({"markets": [
            market("Josh Allen - 1st Qtr Passing Yds", ("Josh Allen Over", 1.9), ("Josh Allen Under", 1.9), handicap=60.5)]}, nfl)
        self.assertEqual((props, dict(skipped)), ([], {"1st Qtr Passing Yds": 1}))

    def test_a_title_that_shortens_the_name_reads_the_stat_after_the_dash(self):
        from props.leagues import nba
        props, skipped = fanduel.parse({"markets": [
            market("S Gilgeous-Alexander - Pts + Reb + Ast", ("Shai Gilgeous-Alexander Over", 1.893),
                   ("Shai Gilgeous-Alexander Under", 1.847), handicap=40.5)]}, nba)
        self.assertEqual(([(p["player"], p["stat"], p["line"]) for p in props], dict(skipped)),
                         ([("Shai Gilgeous-Alexander", "pts_rebs_asts", 40.5)], {}))


class OneWayTest(unittest.TestCase):
    def ladder(self, name, runners):
        return {"name": name, "runners": [{"name": n, "handicap": 0, "status": "ACTIVE", "odds": o} for n, o in runners]}

    def test_a_threshold_ladder_is_an_over_without_an_under(self):
        props, skipped = fanduel.parse({"markets": [
            self.ladder("To Record 2+ Hits", [("Mookie Betts", 3.4), ("Shohei Ohtani", 2.9), ("Will Smith", 4.1)]),
            self.ladder("To Record A Run", [("Shohei Ohtani", 1.8), ("Mookie Betts", 2.0), ("Will Smith", 2.6)]),
            self.ladder("To Hit A Triple", [("Shohei Ohtani", 21.0), ("Mookie Betts", 26.0), ("Will Smith", 41.0)]),   # no triples column
            self.ladder("Player to Hit a Home Run in First 5 Innings", [("Shohei Ohtani", 7.0), ("Will Smith", 9.0), ("Max Muncy", 9.5)]),
        ]}, mlb)
        self.assertEqual([(p["player"], p["stat"], p["line"], p["over"], p["under"]) for p in props], [
            ("Mookie Betts", "hits", 1.5, 3.4, None), ("Shohei Ohtani", "hits", 1.5, 2.9, None), ("Will Smith", "hits", 1.5, 4.1, None),
            ("Shohei Ohtani", "runs", 0.5, 1.8, None), ("Mookie Betts", "runs", 0.5, 2.0, None), ("Will Smith", "runs", 0.5, 2.6, None),
        ])
        self.assertFalse(skipped)

    def test_alt_lines_and_anytime_scorers(self):
        football, _ = fanduel.parse({"markets": [
            self.ladder("Zay Flowers - Alt Receptions", [("Zay Flowers 3+ Receptions", 1.4), ("Zay Flowers 4+ Receptions", 1.9)]),
            self.ladder("Any Time Touchdown Scorer", [("Derrick Henry", 1.8), ("Zay Flowers", 2.9)]),
            self.ladder("Anytime 1st Half TD Scorer", [("Derrick Henry", 2.6), ("Zay Flowers", 4.0)]),
            self.ladder("Josh Allen - 1st Half Alt Passing Yds", [("Josh Allen 150+ Yards", 1.5)]),
            self.ladder("Josh Allen - 1st Qtr Alt Passing Yds", [("Josh Allen 50+ Yards", 1.6)]),
        ]}, nfl)
        self.assertEqual([(p["player"], p["stat"], p["line"], p["over"]) for p in football], [
            ("Zay Flowers", "receptions", 2.5, 1.4), ("Zay Flowers", "receptions", 3.5, 1.9),
            ("Derrick Henry", "rush_rec_tds", 0.5, 1.8), ("Zay Flowers", "rush_rec_tds", 0.5, 2.9),
        ])
        hockey, _ = fanduel.parse({"markets": [
            self.ladder("2+ Points", [("Quinn Hughes", 3.1), ("Kirill Kaprizov", 2.4)]),
            self.ladder("1st Period 1+ Points", [("Quinn Hughes", 2.8), ("Kirill Kaprizov", 2.5)]),
            self.ladder("Anytime Goal Scorer", [("Kirill Kaprizov", 2.2), ("Matt Boldy", 2.7)]),
        ]}, nhl)
        self.assertEqual([(p["player"], p["stat"], p["line"]) for p in hockey], [
            ("Quinn Hughes", "points", 1.5), ("Kirill Kaprizov", "points", 1.5),
            ("Kirill Kaprizov", "goals", 0.5), ("Matt Boldy", "goals", 0.5),
        ])

    def test_soccers_ladders(self):
        from props.leagues import soccer
        props, _ = fanduel.parse({"markets": [
            self.ladder("Anytime Goalscorer", [("Kylian Mbappe", 1.667)]),
            self.ladder("To Score 2 or More Goals", [("Kylian Mbappe", 4.5)]),
            self.ladder("Anytime Assist", [("Jude Bellingham", 3.1)]),
            self.ladder("To Score Or Assist", [("Jude Bellingham", 2.0)]),
            self.ladder("Player To Have 2 Or More Shots On Target", [("Kylian Mbappe", 2.2)]),
            self.ladder("Player To Have 2 Or More Shots On Target In 1st Half", [("Kylian Mbappe", 6.0)]),
            self.ladder("Player To Have 3 Or More Shots", [("Arda Guler", 2.4)]),
        ]}, soccer)
        self.assertEqual([(p["player"], p["stat"], p["line"]) for p in props], [
            ("Kylian Mbappe", "goals", 0.5), ("Kylian Mbappe", "goals", 1.5), ("Jude Bellingham", "assists", 0.5),
            ("Jude Bellingham", "goals_assists", 0.5), ("Kylian Mbappe", "shots_on_target", 1.5), ("Arda Guler", "shots", 2.5),
        ])

    def test_soccers_games_come_from_the_sport_page_by_competition(self):
        from props.leagues import soccer
        from props import books
        epl = books.competitions(soccer)[0]
        page = {"attachments": {"events": {
            "1": {"eventId": 1, "name": "Brentford v Liverpool", "competitionId": 10932509},
            "2": {"eventId": 2, "name": "Roma v Real Madrid", "competitionId": 228},
            "3": {"eventId": 3, "name": "English Premier League", "competitionId": 10932509}}}}
        with mock.patch.object(fanduel, "_get", return_value=page) as get:
            self.assertEqual([e["eventId"] for e in fanduel.league_events(None, epl)], [1])
        self.assertEqual(get.call_args.args[2]["page"], "SPORT")

    def test_first_goal_scorer_and_alt_saves_named_without_a_dash(self):
        props, _ = fanduel.parse({"markets": [
            self.ladder("First Goal Scorer", [("Brandon Hagel", 11.0), ("No Goalscorer", 41.0)]),
            self.ladder("2nd Period First Goal Scorer", [("Brandon Hagel", 13.0), ("Jake Guentzel", 14.0)]),
            self.ladder("Dan Vladar Alt Saves", [("27+", 1.9), ("28+", 2.18)]),
            self.ladder("1st Period Dan Vladar Alt Saves", [("8+", 1.8), ("9+", 2.2)]),
        ]}, nhl)
        self.assertEqual([(p["player"], p["stat"], p["line"], p["over"]) for p in props], [
            ("Brandon Hagel", "first_goals", 0.5, 11.0), ("No Goalscorer", "first_goals", 0.5, 41.0),
            ("Dan Vladar", "saves", 26.5, 1.9), ("Dan Vladar", "saves", 27.5, 2.18),
        ])


class FetchTest(unittest.TestCase):
    def test_reads_each_prop_tab_of_the_games_on_the_slate(self):
        now = dt.datetime(2026, 10, 3, 16, tzinfo=dt.timezone.utc)
        events = {"1": {"eventId": 1, "name": "A @ B", "openDate": "2026-10-03T23:00:00.000Z"},
                  "2": {"eventId": 2, "name": "C @ D", "openDate": "2026-10-09T23:00:00.000Z"},     # past the slate
                  "3": {"eventId": 3, "name": "MLB Futures", "openDate": "2026-10-03T23:00:00.000Z"},
                  "4": {"eventId": 4, "name": "E @ F", "openDate": "2026-10-03T14:00:00.000Z"},     # under way
                  "5": {"eventId": 5, "name": "G @ H", "openDate": "2026-10-04T00:00:00.000Z"}}     # no batter props yet
        tabs = {1: ["Popular", "Pitcher Props", "Batter Props"], 5: ["Popular", "Pitcher Props"]}
        calls = []

        def get(http, path, params):
            calls.append((path, params.get("eventId"), params.get("tab")))
            if path == "content-managed-page":
                return {"attachments": {"events": events}}
            return {"layout": {"tabs": {str(i): {"id": i, "title": title} for i, title in enumerate(tabs[params["eventId"]])}},
                    "attachments": {"markets": {"m": {"marketName": "X - Strikeouts", "runners": [
                        {"runnerName": "X Over", "handicap": 5.5, "runnerStatus": "ACTIVE",
                         "winRunnerOdds": {"trueOdds": {"decimalOdds": {"decimalOdds": 1.9}}}}] * 2}}}}

        with mock.patch.object(fanduel, "_get", get):
            raw = fanduel.fetch(mlb, now=now)
        # every game's first tab, then the others only where the game lists them
        self.assertEqual(sorted(calls[1:]), [("event-page", 1, "batter-props"), ("event-page", 1, "pitcher-props"),
                                             ("event-page", 5, "pitcher-props")])
        self.assertEqual(raw["markets"][0]["runners"][0], {"name": "X Over", "handicap": 5.5, "status": "ACTIVE", "odds": 1.9})


if __name__ == "__main__":
    unittest.main()
