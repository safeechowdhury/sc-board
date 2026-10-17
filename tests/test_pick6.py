import unittest

from props import pick6
from props.leagues import mlb, nfl


def card(pickable_id, dk_id, markets):
    return {"pickableId": pickable_id, "entities": [{"dkId": dk_id, "compIds": [1]}],
            "activePickableMarkets": markets}


def market(market_id, line, sides, paused=False):
    """sides: {"More": its multiplier, "Less": its multiplier}, or a list of sides at 1x."""
    sides = sides if isinstance(sides, dict) else dict.fromkeys(sides, 1.0)
    return {"pickSixMarketId": market_id, "targetValue": line, "isPaused": paused,
            "activeSelections": [{"statLinePropositionId": {"More": 1, "Less": 2}[side], "standingsMultiplier": mult}
                                 for side, mult in sides.items()]}


def payload(*cards):
    return {"pickCardsResponse": {
        "entityInfoByDkId": {"32242": {"name": "S. Ohtani", "fullName": "Shohei Ohtani"},
                             "31694": {"name": "B. Snell", "fullName": "Blake Snell"}},
        "pickSixMarketById": {"279": {"name": "Strikeouts Thrown"}, "288": {"name": "Hits"},
                              "290": {"name": "Hits + Runs + RBIs"}, "296": {"name": "Home Runs"},
                              "289": {"name": "Rush + Rec Yards"}, "301": {"name": "Longest Reception"}},
        "pickCardByPickableId": {str(i): card for i, card in enumerate(cards, 1)},
    }}


class ParseTest(unittest.TestCase):
    def test_a_standard_pick_is_the_three_pick_rate_per_leg(self):
        props, skipped = pick6.parse(payload(
            card(1, 31694, [market(279, 6.5, ["More", "Less"])]),
            card(2, 32242, [market(288, 1.5, ["More", "Less"]), market(290, 1.5, ["More", "Less"])]),
        ), mlb)
        self.assertEqual(props, [
            {"player": "Blake Snell", "stat": "strikeouts", "line": 6.5, "over": 1.817, "under": 1.817},
            {"player": "Shohei Ohtani", "stat": "hits", "line": 1.5, "over": 1.817, "under": 1.817},
            {"player": "Shohei Ohtani", "stat": "hits_runs_rbis", "line": 1.5, "over": 1.817, "under": 1.817},
        ])
        self.assertFalse(skipped)

    def test_each_side_is_priced_by_its_own_multiplier(self):
        props, _ = pick6.parse(payload(
            card(1, 32242, [market(296, 0.5, {"More": 0.7}),                   # a gimme "more" pays less
                            market(288, 1.5, {"More": 1.4, "Less": 0.7}),      # a longer "more", a shorter "less"
                            market(290, 2.5, {"Less": 0.8})]),                 # "less" only: no over to price
        ), mlb)
        self.assertEqual(props, [
            {"player": "Shohei Ohtani", "stat": "home_runs", "line": 0.5, "over": 1.272, "under": None},
            {"player": "Shohei Ohtani", "stat": "hits", "line": 1.5, "over": 2.544, "under": 1.272},
        ])

    def test_paused_cards_and_unmapped_titles_are_skipped(self):
        props, skipped = pick6.parse(payload(
            card(1, 31694, [market(279, 6.5, ["More", "Less"], paused=True)]),
            card(2, 32242, [market(301, 12.5, ["More", "Less"])]),
        ), mlb)
        self.assertEqual(props, [])
        self.assertEqual(dict(skipped), {"Longest Reception": 1})


if __name__ == "__main__":
    unittest.main()
