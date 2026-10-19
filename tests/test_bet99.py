import unittest

from props import bet99
from props.leagues import nba, nfl


def market(code, name, subtype, *sides, open=True):
    return {"code": code, "name": name, "subtype": subtype, "open": open,
            "sides": [{"name": n, "type": t, "odds": o, "open": True} for n, t, o in sides]}


class ParseTest(unittest.TestCase):
    def test_an_over_under_and_its_milestone_rung(self):
        props, skipped = bet99.parse({"markets": [
            market("PROPPTS", "Payton Pritchard - Points", "M:A:811#14.5",
                   ("Under 14.5", "Under", "1.869565217"), ("Over 14.5", "Over", "1.869565217")),
            market("PROPPTSMILE", "Payton Pritchard - Points Milestone", "M:A:811#19.5",
                   ("Over 19.5", "Over", "3.6"), ("Under 19.5", "Under", "1.263157895")),
        ]}, nba)
        self.assertEqual([(p["player"], p["stat"], p["line"], p["over"], p["under"]) for p in props],
                         [("Payton Pritchard", "points", 14.5, 1.87, 1.87), ("Payton Pritchard", "points", 19.5, 3.6, 1.263)])
        self.assertEqual(skipped, {})

    def test_a_yes_no_is_the_over_at_half(self):
        props, _ = bet99.parse({"markets": [
            market("PROPATD", "Mike Evans - Anytime Touchdown", "M:A:593587#0.5",
                   ("Yes", "Yes", "1.689655172"), ("No", "No", "2.1"))]}, nfl)
        self.assertEqual([(p["player"], p["stat"], p["line"], p["over"], p["under"]) for p in props],
                         [("Mike Evans", "rush_rec_tds", 0.5, 1.69, 2.1)])

    def test_closed_markets_and_unmapped_codes(self):
        props, skipped = bet99.parse({"markets": [
            market("PROPREB", "Paul George - Rebounds", "M:A:1#3.5", ("Over 3.5", "Over", "1.9"), open=False),
            market("PROP1STQPTS", "Mitchell Robinson - First Quarter Points", "M:A:2#1.5", ("Over 1.5", "Over", "1.8")),
        ]}, nba)
        self.assertEqual(props, [])
        self.assertEqual(skipped, {"PROP1STQPTS": 1})

    def test_only_a_players_market_has_his_line(self):
        self.assertTrue(bet99._PLAYER_LINE.match("M:B:1161228#1.5"))
        self.assertFalse(bet99._PLAYER_LINE.match("M:A#3.5:team=A"))   # a team's total


if __name__ == "__main__":
    unittest.main()
