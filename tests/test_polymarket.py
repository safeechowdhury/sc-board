import datetime as dt
import json
import unittest
import unittest.mock

from props import polymarket
from props.leagues import nfl, nhl, soccer


def market(kind, question, bid, ask, line=None, outcomes=("Over", "Under")):
    return {"type": kind, "question": question, "line": line, "outcomes": json.dumps(list(outcomes)),
            "bid": bid, "ask": ask}


class ParseTest(unittest.TestCase):
    def test_the_over_costs_the_ask_and_the_under_one_less_the_bid(self):
        props, skipped = polymarket.parse({"markets": [
            market("passing_yards", "C.J. Stroud: Passing Yards O/U 224.5", 0.48, 0.52, 224.5),
            {**market("anytime_touchdowns", "Bhayshul Tuten: Anytime Touchdown", 0.35, 0.38, outcomes=("Yes", "No")),
             "game": ["2026-10-18T13:30:00Z", "Texans vs. Jaguars"]},
        ]}, nfl)
        self.assertEqual(props, [
            {"player": "C.J. Stroud", "stat": "pass_yds", "line": 224.5, "over": 1.923, "under": 1.923},
            {"player": "Bhayshul Tuten", "stat": "rush_rec_tds", "line": 0.5, "over": 2.632, "under": 1.538,
             "game": ["2026-10-18T13:30:00Z", "Texans vs. Jaguars"]},
        ])
        self.assertFalse(skipped)

    def test_a_count_with_no_line_is_the_over_below_it(self):
        props, _ = polymarket.parse({"markets": [
            market("two_plus_touchdowns", "Bhayshul Tuten: 2+ Touchdowns", 0.06, 0.09, outcomes=("Yes", "No")),
        ]}, nfl)
        self.assertEqual((props[0]["stat"], props[0]["line"]), ("rush_rec_tds", 1.5))
        soccer_props, _ = polymarket.parse({"markets": [
            market("soccer_player_shots", "Cole Palmer: 3+ shots", 0.40, 0.44, outcomes=("Yes", "No")),
        ]}, soccer)
        self.assertEqual((soccer_props[0]["stat"], soccer_props[0]["line"]), ("shots", 2.5))

    def test_an_empty_or_wide_book_is_left_out(self):
        props, _ = polymarket.parse({"markets": [
            market("receptions", "Brenton Strange: Receptions O/U 1.5", 0.01, 0.99, 1.5),   # nobody making a price
            market("receptions", "Brenton Strange: Receptions O/U 2.5", None, 0.40, 2.5),   # no bid: no under
            market("receptions", "Brenton Strange: Receptions O/U 3.5", 0.30, 0.38, 3.5),   # 8 cents: kept
        ]}, nfl)
        self.assertEqual([p["line"] for p in props], [3.5])

    def test_an_under_listed_first_is_read_the_other_way_round(self):
        props, _ = polymarket.parse({"markets": [
            market("receptions", "Nico Collins: Receptions O/U 4.5", 0.40, 0.44, 4.5, outcomes=("Under", "Over"))]}, nfl)
        self.assertEqual((props[0]["over"], props[0]["under"]), (round(1 / 0.60, 3), round(1 / 0.44, 3)))

    def test_unmapped_types_are_counted(self):
        props, skipped = polymarket.parse({"markets": [market("longest_reception", "Nico Collins: Longest Reception O/U 24.5",
                                                              0.45, 0.5, 24.5)]}, nfl)
        self.assertEqual((props, dict(skipped)), ([], {"longest_reception": 1}))


class FetchTest(unittest.TestCase):
    def test_reads_the_mapped_markets_of_games_not_yet_started(self):
        now = dt.datetime(2026, 10, 17, 12, tzinfo=dt.timezone.utc)
        at = lambda hours: (now + dt.timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
        rung = lambda kind, q, **more: {"sportsMarketType": kind, "question": q, "line": 0.5, "outcomes": '["Over", "Under"]',
                                       "bestBid": 0.4, "bestAsk": 0.45, "acceptingOrders": True, "closed": False, **more}
        events = [
            {"title": "Texans vs. Jaguars - Player Props", "startTime": at(25), "markets": [
                rung("receptions", "Nico Collins: Receptions O/U 0.5"),
                rung("longest_reception", "Nico Collins: Longest Reception O/U 24.5"),     # not a stat we map
                rung("receptions", "Nico Collins: Receptions O/U 1.5", closed=True)]},       # settled
            {"title": "Bears vs. Falcons - Player Props", "startTime": at(-1), "markets": [   # under way
                rung("receptions", "DJ Moore: Receptions O/U 0.5")]},
        ]
        seen = []

        class Http:
            def __init__(self, *a, **k): pass
            def close(self): pass
            def get(self, url, params=None):
                seen.append(url.split(".com", 1)[1])
                data = [{"sport": "nfl", "series": "12185"}] if url.endswith("/sports") else events
                return type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: data})()

        with unittest.mock.patch.object(polymarket.httpx, "Client", Http):
            payload = polymarket.fetch(nfl, now=now)
        self.assertEqual(seen, ["/sports", "/events"])   # one page of events: fewer than a page's worth
        self.assertEqual([(m["question"], m["game"]) for m in payload["markets"]],
                         [("Nico Collins: Receptions O/U 0.5", [at(25), "Texans vs. Jaguars"])])

    def test_a_league_with_no_markets_mapped_is_not_asked(self):
        with unittest.mock.patch.object(polymarket.httpx, "Client", side_effect=AssertionError("asked")):
            self.assertEqual(polymarket.fetch(nhl), {"markets": []})


if __name__ == "__main__":
    unittest.main()
