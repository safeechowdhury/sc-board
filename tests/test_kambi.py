import datetime as dt
import unittest
import unittest.mock

from props import kambi
from props.leagues import mlb, nfl, nhl


def offer(label, *outcomes):
    return {"criterion": label, "outcomes": [{"player": player, "type": kind, "line": line, "odds": odds, "status": "OPEN"}
                                             for player, kind, line, odds in outcomes]}


class ParseTest(unittest.TestCase):
    def test_over_under_offers_carry_their_line_in_thousandths(self):
        props, skipped = kambi.parse({"offers": [
            offer("Total Receiving Yards by the Player - Including Overtime",
                  ("Jonathan Taylor", "OT_OVER", 18500, 1890), ("Jonathan Taylor", "OT_UNDER", 18500, 1870)),
        ]}, nfl)
        self.assertEqual(props, [{"player": "Jonathan Taylor", "stat": "rec_yds", "line": 18.5, "over": 1.89, "under": 1.87}])
        self.assertFalse(skipped)

    def test_a_ladders_count_is_the_over_half_a_point_below_and_its_no_the_under(self):
        props, _ = kambi.parse({"offers": [
            offer("2+ Hits by the Player - Including Extra Innings (Listed player must be in starting lineup for bets to stand)",
                  ("Ty France", "OT_YES", 2000, 3750)),
            offer("Player to score at least 1 points - Including Overtime",
                  ("Joshua Morrissey", "OT_YES", 1000, 1800), ("Joshua Morrissey", "OT_NO", 1000, 1930)),
        ]}, mlb)
        self.assertEqual(props[0], {"player": "Ty France", "stat": "hits", "line": 1.5, "over": 3.75, "under": None})
        hockey, _ = kambi.parse({"offers": [offer("Player to score at least 1 points - Including Overtime",
                                                  ("Joshua Morrissey", "OT_YES", 1000, 1800), ("Joshua Morrissey", "OT_NO", 1000, 1930))]}, nhl)
        self.assertEqual(hockey, [{"player": "Joshua Morrissey", "stat": "points", "line": 0.5, "over": 1.8, "under": 1.93}])

    def test_a_fields_runners_are_the_over_at_one_half(self):
        props, _ = kambi.parse({"offers": [offer("First Goal Scorer - Including Overtime",
                                                 ("Erik Cernak", "OT_PLAYER_PARTICIPANT", None, 41000))]}, nhl)
        self.assertEqual(props, [{"player": "Erik Cernak", "stat": "first_goals", "line": 0.5, "over": 41.0, "under": None}])

    def test_parts_of_a_game_and_suspended_prices_are_left_out_and_other_player_offers_listed(self):
        closed = offer("Total Receptions by the Player - Including Overtime",
                       ("Tyler Warren", "OT_OVER", 5500, 1720), ("Tyler Warren", "OT_UNDER", 5500, 2050))
        closed["outcomes"][1]["status"] = "SUSPENDED"
        props, skipped = kambi.parse({"offers": [
            offer("N+ Receiving Yards By The Player - First Half", ("Tyler Warren", "OT_YES", 40000, 2100)),
            offer("Total Yards of Longest Reception by the Player - Including Overtime",
                  ("Tyler Warren", "OT_OVER", 21500, 1850), ("Tyler Warren", "OT_UNDER", 21500, 1850)),
            closed,
        ]}, nfl)
        self.assertEqual(props, [{"player": "Tyler Warren", "stat": "receptions", "line": 5.5, "over": 1.72, "under": None}])
        self.assertEqual(dict(skipped), {"Total Yards of Longest Reception by the Player - Including Overtime": 1})


class FetchTest(unittest.TestCase):
    def test_reads_the_offers_of_games_on_the_slate_only(self):
        class Http:
            def __init__(self, *a, **k): self.seen = []
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def get(self, url, params):
                self.seen.append(url.rsplit("/", 2)[-2:])
                data = ({"events": [{"event": {"id": 1, "state": "NOT_STARTED", "start": "2026-10-04T17:00:00Z"}},
                                    {"event": {"id": 2, "state": "STARTED", "start": "2026-10-04T13:00:00Z"}},
                                    {"event": {"id": 3, "state": "NOT_STARTED", "start": "2026-10-20T17:00:00Z"}}]}
                        if "listView" in url else
                        {"betOffers": [
                            {"criterion": {"englishLabel": "Total Hits by the Player"}, "outcomes": [
                                {"participant": "Ty France", "type": "OT_OVER", "line": 500, "odds": 1500, "status": "OPEN"}]},
                            {"criterion": {"englishLabel": "Run Line"}, "outcomes": [
                                {"participant": "San Diego Padres", "type": "OT_ONE", "line": -1500, "odds": 2100, "status": "OPEN"}]}]})
                return type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: data})()

        http = Http()
        with unittest.mock.patch.object(kambi.httpx, "Client", return_value=http):
            payload = kambi.fetch(mlb, now=dt.datetime(2026, 10, 4, 12, tzinfo=dt.timezone.utc))
        self.assertEqual(payload, {"offers": [{"criterion": "Total Hits by the Player", "outcomes": [
            {"player": "Ty France", "type": "OT_OVER", "line": 500, "odds": 1500, "status": "OPEN"}]}]})   # a team's run line isn't a prop
        self.assertEqual(http.seen, [["baseball", "mlb.json"], ["event", "1.json"]])   # started and far-off games skipped


if __name__ == "__main__":
    unittest.main()
