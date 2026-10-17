import datetime as dt
import unittest
import unittest.mock

from props import novig
from props.leagues import nfl, nhl


def market(kind, player, strike, over, under):
    return {"type": kind, "player": player, "strike": strike, "over": over, "under": under}


class ParseTest(unittest.TestCase):
    def test_a_lines_prices_are_one_over_their_cost(self):
        props, skipped = novig.parse({"markets": [
            market("SHOTS_ON_GOAL", "Nikita Kucherov", 2.5, "0.55000", "0.47000"),
            market("PLAYER_GOALS", "Nikita Kucherov", 0.5, "0.37500", "0.65000"),
        ]}, nhl)
        self.assertEqual(props, [
            {"player": "Nikita Kucherov", "stat": "shots", "line": 2.5, "over": 1.818, "under": 2.128},
            {"player": "Nikita Kucherov", "stat": "goals", "line": 0.5, "over": 2.667, "under": 1.538},
        ])
        self.assertFalse(skipped)

    def test_a_side_nobody_offers_leaves_the_line_one_way_or_out(self):
        props, _ = novig.parse({"markets": [
            market("PASSING_TOUCHDOWNS", "Tyler Shough", 0.5, "0.89000", None),   # over only: one-way
            market("PASSING_TOUCHDOWNS", "Tyler Shough", 2.5, None, "0.70000"),   # under only: no over to show
        ]}, nfl)
        self.assertEqual(props, [{"player": "Tyler Shough", "stat": "pass_tds", "line": 0.5, "over": 1.124, "under": None}])

    def test_a_yes_no_with_strike_zero_is_the_over_at_one_half(self):
        props, _ = novig.parse({"markets": [market("FIRST_GOAL_SCORER", "Brandon Hagel", 0, "0.14500", None)]}, nhl)
        self.assertEqual(props, [{"player": "Brandon Hagel", "stat": "first_goals", "line": 0.5, "over": 6.897, "under": None}])

    def test_unmapped_market_types_are_counted(self):
        props, skipped = novig.parse({"markets": [market("FIRST_TOUCHDOWN_SCORER", "Chris Olave", 0, "0.19000", "0.81500")]}, nfl)
        self.assertEqual((props, dict(skipped)), ([], {"FIRST_TOUCHDOWN_SCORER": 1}))


class CardsTest(unittest.TestCase):
    def test_games_are_read_from_sections_or_their_subsections(self):
        card = lambda i: {"type": "game_event_card", "eventId": i}
        page = {"sections": [{"content": {"type": "components", "components": [card(1), {"type": "banner"}]}},
                             {"content": {"subSections": [{"components": [card(2)]}, {"components": [card(3)]}]}}]}
        self.assertEqual([c["eventId"] for c in novig._cards(page)], [1, 2, 3])


class FetchTest(unittest.TestCase):
    def test_lists_the_leagues_games_then_reads_the_props_of_those_on_the_slate(self):
        now = dt.datetime(2026, 10, 5, 12, tzinfo=dt.timezone.utc)
        card = lambda game, hours, status="OPEN_PREGAME": {
            "type": "game_event_card", "eventId": game, "eventStatus": status,
            "scheduledStart": (now + dt.timedelta(hours=hours)).isoformat().replace("+00:00", "Z")}
        props = {"sections": [{"type": "player_prop_section", "marketType": "SHOTS_ON_GOAL", "rows": [
            {"player": {"fullName": "Brandon Hagel"}, "lines": [{"strike": 2.5, "outcomes": [
                {"index": 1, "available": "0.47000"}, {"index": 0, "available": "0.55000"}]}]}]}]}   # out of order
        seen = []

        class Http:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def get(self, url, params=None):
                seen.append(url.split("/nbx/v1", 1)[1])
                data = ({"sections": [{"title": "Games", "content": {"type": "components", "components": [
                            card("today", 8), card("live", -1, "OPEN_INGAME"), card("next-week", 24 * 7)]}}]}
                        if url.endswith("/page") else props)
                return type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: data})()

        with unittest.mock.patch.object(novig.httpx, "Client", Http):
            payload = novig.fetch(nhl, now=now)
        self.assertEqual(seen, ["/trading/NHL/page", "/event-details/today/player-props"])
        self.assertEqual(payload, {"markets": [
            {"type": "SHOTS_ON_GOAL", "player": "Brandon Hagel", "strike": 2.5, "over": "0.55000", "under": "0.47000"}]})


if __name__ == "__main__":
    unittest.main()
