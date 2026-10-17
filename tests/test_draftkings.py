import unittest
from unittest import mock

from props import draftkings
from props.leagues import mlb, nfl, nhl


def selection(market, side, line, odds, main=True, player="Austin Riley"):
    return {"marketId": market, "outcomeType": side, "label": side, "points": line, "trueOdds": odds, "main": main,
            "participants": [{"name": player, "type": "Player", "venueRole": "AwayPlayer"}]}


SUBCATEGORY = {
    "events": [{"id": "e1", "status": "NOT_STARTED"}, {"id": "e2", "status": "STARTED"}],
    "markets": [{"id": "m1", "eventId": "e1", "name": "Austin Riley Hits + Runs + RBIs O/U"},
                {"id": "m2", "eventId": "e2", "name": "Jose Ramirez Hits + Runs + RBIs O/U"},
                {"id": "m3", "eventId": "e1", "name": "Will Smith (LAD) Hits + Runs + RBIs O/U"}],
    "selections": [
        selection("m1", "Over", 1.5, 1.625), selection("m1", "Under", 1.5, 2.2),
        selection("m1", "Over", 2.5, 3.1, main=False), selection("m1", "Under", 2.5, 1.35, main=False),
        selection("m2", "Over", 1.5, 1.9, player="Jose Ramirez"), selection("m2", "Under", 1.5, 1.9, player="Jose Ramirez"),
        selection("m3", "Over", 0.5, 2.0, player="Will Smith (LAD)"), selection("m3", "Under", 0.5, 1.8, player="Will Smith (LAD)"),
    ],
}


LADDER = {
    "events": [{"id": "e1", "status": "NOT_STARTED"}],
    "markets": [{"id": "m4", "eventId": "e1", "name": "Austin Riley Hits + Runs + RBIs"}],
    "selections": [
        {"marketId": "m4", "label": f"{n}+", "outcomeType": None, "points": None, "milestoneValue": n, "trueOdds": odds,
         "participants": [{"name": "Austin Riley", "type": "Player"}]}
        for n, odds in ((2, 1.6), (3, 2.55))
    ],
}


class FetchAndParseTest(unittest.TestCase):
    def fetch(self):
        pages = {
            "https://sportsbook-nash.draftkings.com/api/sportscontent/dkusny/v1/leagues/84240":
                {"categories": [{"id": 743, "name": "Batter Props"}, {"id": 985, "name": "Live Batter Props"}],
                 "subcategories": [
                {"id": 17406, "categoryId": 743, "name": "Hits + Runs + RBIs O/U"},
                {"id": 17843, "categoryId": 743, "name": "Hits + Runs + RBIs"},            # an N+ ladder
                {"id": 17844, "categoryId": 743, "name": "Triples"},                        # a ladder not mapped
                {"id": 99999, "categoryId": 743, "name": "Triples O/U"},                    # not mapped
                {"id": 18773, "categoryId": 985, "name": "Hits + Runs + RBIs O/U"}]},       # another category's
            ".../categories/743/subcategories/17406": SUBCATEGORY,
            ".../categories/743/subcategories/17843": LADDER,
        }
        calls = []

        class Response:
            def __init__(self, data):
                self.data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self.data

        class Client:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                pass

            def close(self):
                pass

            def get(self, url):
                calls.append(url)
                if url == draftkings.HOME:
                    return Response({})
                key = url if url in pages else ".../" + url.split("/84240/", 1)[1]
                return Response(pages[key])

        with mock.patch.object(draftkings.httpx, "Client", Client), mock.patch.object(draftkings, "SITE", "dkusny"):
            raw = draftkings.fetch(mlb)
        return raw, calls

    def test_opens_the_home_page_first_and_reads_only_mapped_subcategories(self):
        raw, calls = self.fetch()
        self.assertEqual(calls[0], draftkings.HOME)
        # the league's page lists the subcategories: no category's own page is asked for
        self.assertEqual(sorted(c.rsplit("/v1/leagues/", 1)[1] for c in calls[1:]),
                         ["84240", "84240/categories/743/subcategories/17406",
                          "84240/categories/743/subcategories/17843"])
        self.assertEqual([s["name"] for s in raw["subcategories"]], ["Hits + Runs + RBIs O/U", "Hits + Runs + RBIs", "Triples O/U"])

    def test_main_line_of_games_not_started(self):
        props, skipped = draftkings.parse(self.fetch()[0], mlb)
        self.assertEqual(props, [
            {"player": "Austin Riley", "stat": "hits_runs_rbis", "line": 1.5, "over": 1.625, "under": 2.2},
            {"player": "Will Smith", "stat": "hits_runs_rbis", "line": 0.5, "over": 2.0, "under": 1.8, "team": "LAD"},
            # each rung of a ladder is a one-way over half a point below it
            {"player": "Austin Riley", "stat": "hits_runs_rbis", "line": 1.5, "over": 1.6, "under": None},
            {"player": "Austin Riley", "stat": "hits_runs_rbis", "line": 2.5, "over": 2.55, "under": None},
        ])
        self.assertEqual(dict(skipped), {"Triples O/U": 1})


class FieldTest(unittest.TestCase):
    def test_a_field_markets_runners_are_the_over_below_its_count(self):
        def runner(market, player, odds):
            return {"market": market, "name": market, "player": player, "side": "", "line": None, "odds": odds,
                    "main": False, "milestone": None}
        props, skipped = draftkings.parse({"subcategories": [
            {"name": "Anytime Goalscorer", "selections": [
                runner("Anytime Goalscorer", "Cameron Crotty", 12.0), runner("First Goalscorer", "Cameron Crotty", 56.0),
                runner("2+ Goals", "Nikita Kucherov", 9.0), runner("First Goalscorer", None, 15.0),
                runner("Last Goalscorer", "Nikita Kucherov", 8.0)]},
            {"name": "3+ Goals", "selections": [runner("3+ Goals", "Nikita Kucherov", 41.0)]},
        ]}, nhl)
        self.assertEqual([(p["player"], p["stat"], p["line"], p["over"], p["under"]) for p in props], [
            ("Cameron Crotty", "goals", 0.5, 12.0, None), ("Cameron Crotty", "first_goals", 0.5, 56.0, None),
            ("Nikita Kucherov", "goals", 1.5, 9.0, None), ("Nikita Kucherov", "goals", 2.5, 41.0, None),
        ])
        self.assertEqual(dict(skipped), {"Last Goalscorer": 1})

    def test_nfl_td_scorers_anytime_and_2_plus_without_first_scorer_or_defenses(self):
        def runner(market, player, odds):
            return {"market": market, "name": market, "player": player, "side": "", "line": None, "odds": odds,
                    "main": False, "milestone": None}
        props, skipped = draftkings.parse({"subcategories": [
            {"name": "TD Scorer", "selections": [
                runner("Anytime TD Scorer", "Jahmyr Gibbs", 1.21), runner("2+ TDs", "Jahmyr Gibbs", 2.15),
                runner("First TD Scorer", "Jahmyr Gibbs", 4.5), runner("2+ TDs", None, 21.0)]},   # D/ST: no participant
            {"name": "3+ TDs", "selections": [runner("3+ TDs", "Jahmyr Gibbs", 7.0)]},
        ]}, nfl)
        self.assertEqual([(p["player"], p["stat"], p["line"], p["over"]) for p in props], [
            ("Jahmyr Gibbs", "rush_rec_tds", 0.5, 1.21), ("Jahmyr Gibbs", "rush_rec_tds", 1.5, 2.15),
            ("Jahmyr Gibbs", "rush_rec_tds", 2.5, 7.0),
        ])
        self.assertEqual(dict(skipped), {})   # First TD Scorer is passed over quietly

if __name__ == "__main__":
    unittest.main()
