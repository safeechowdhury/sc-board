import datetime as dt
import json
import math
import unittest
from unittest import mock

from props import gamelines
from props.leagues import mlb, nba, nfl, nhl


class PinnacleTest(unittest.TestCase):
    def test_reads_the_main_moneyline_total_and_spread(self):
        payload = {"matchups": [{"type": "matchup", "id": 7, "startTime": "2026-10-12T23:07:00Z",
                                 "participants": [{"name": "New Jersey Devils", "alignment": "home"},
                                                  {"name": "Ottawa Senators", "alignment": "away"}]}],
                   "markets": [
                       {"matchupId": 7, "type": "moneyline", "period": 0, "status": "open", "isAlternate": False,
                        "prices": [{"designation": "home", "price": -118}, {"designation": "away", "price": 108}]},
                       {"matchupId": 7, "type": "total", "period": 0, "status": "open", "isAlternate": False,
                        "prices": [{"designation": "over", "points": 6.0, "price": -103},
                                   {"designation": "under", "points": 6.0, "price": -113}]},
                       {"matchupId": 7, "type": "spread", "period": 0, "status": "open", "isAlternate": False,
                        "prices": [{"designation": "home", "points": -1.5, "price": 205},
                                   {"designation": "away", "points": 1.5, "price": -248}]},
                       {"matchupId": 7, "type": "total", "period": 0, "status": "open", "isAlternate": True,
                        "prices": [{"designation": "over", "points": 6.5, "price": 120},
                                   {"designation": "under", "points": 6.5, "price": -140}]}]}
        game, = gamelines.parse_pinnacle(payload, nhl, now=dt.datetime(2026, 10, 12, 12, tzinfo=dt.timezone.utc))
        self.assertEqual(game["ml"], {"home": 1.847, "away": 2.08})
        self.assertEqual(game["totals"], [{"line": 6.0, "over": 1.971, "under": 1.885}])   # the alternate is not read
        self.assertEqual(game["spreads"], [{"line": -1.5, "home": 3.05, "away": 1.403}])


class DraftkingsTest(unittest.TestCase):
    def test_reads_the_game_subcategory(self):
        payload = {"data": {
            "events": [{"id": "e1", "status": "NOT_STARTED", "startEventDate": "2026-10-12T23:07:00.000Z",
                        "participants": [{"name": "NJ Devils", "venueRole": "Home"},
                                         {"name": "OTT Senators", "venueRole": "Away"}]}],
            "markets": [{"id": "m1", "eventId": "e1", "name": "Moneyline"},
                        {"id": "m2", "eventId": "e1", "name": "Total"}],
            "selections": [
                {"marketId": "m1", "outcomeType": "Home", "trueOdds": 1.852},
                {"marketId": "m1", "outcomeType": "Away", "trueOdds": 2.08},
                {"marketId": "m2", "outcomeType": "Over", "points": 6.0, "trueOdds": 1.97},
                {"marketId": "m2", "outcomeType": "Under", "points": 6.0, "trueOdds": 1.88}]}}
        game, = gamelines.parse_draftkings(payload, nhl)
        self.assertEqual((game["away_name"], game["home_name"]), ("OTT Senators", "NJ Devils"))
        self.assertEqual(game["ml"], {"home": 1.852, "away": 2.08})
        self.assertEqual(game["totals"], [{"line": 6.0, "over": 1.97, "under": 1.88}])
        self.assertEqual(gamelines.team_code(nhl, "NJ Devils"), "NJD")   # the mascot phrase finds the code
        self.assertEqual(gamelines.team_code(nfl, "LAS Raiders") or
                         gamelines.team_code(nfl, "Las Vegas Raiders"), "LV")


class KambiTest(unittest.TestCase):
    def offer(self, label, outcomes, event=1):
        return {"eventId": event, "criterion": {"englishLabel": label},
                "outcomes": [{"type": kind, "line": line, "odds": odds, "status": "OPEN"}
                             for kind, line, odds in outcomes]}

    def test_reads_the_two_way_lines_and_leaves_team_totals_out(self):
        payload = {"events": [{"id": 1, "home": "New Jersey Devils", "away": "Ottawa Senators",
                               "start": "2026-10-12T23:07:00Z"}],
                   "offers": [
                       self.offer("Moneyline - Including Overtime and penalty shootout",
                                  [("OT_ONE", None, 1830), ("OT_TWO", None, 2020)]),
                       self.offer("Total Goals - Including Overtime and Penalty Shootout",
                                  [("OT_OVER", 6500, 2180), ("OT_UNDER", 6500, 1680)]),
                       self.offer("Total Goals by New Jersey Devils - Including Overtime and Penalty Shootout",
                                  [("OT_OVER", 1500, 1200), ("OT_UNDER", 1500, 4200)]),
                       self.offer("Puck Line - Including Overtime and Penalty Shootout",
                                  [("OT_ONE", -1500, 1370), ("OT_TWO", 1500, 3100)])]}
        game, = gamelines.parse_kambi(payload, nhl)
        self.assertEqual(game["ml"], {"home": 1.83, "away": 2.02})
        self.assertEqual(game["totals"], [{"line": 6.5, "over": 2.18, "under": 1.68}])
        self.assertEqual(game["spreads"], [{"line": -1.5, "home": 1.37, "away": 3.1}])


class MiseTest(unittest.TestCase):
    def test_matches_the_moneyline_sides_by_team_name(self):
        payload = {"events": [[{"name": "Ottawa Senators at New Jersey Devils", "startTime": "2026-10-12T23:07:00Z",
                                "started": False, "markets": [
                    {"groupCode": "MONEY_LINE", "status": "ACTIVE", "outcomes": [
                        {"name": "New Jersey Devils", "prices": [{"decimal": 1.85}]},
                        {"name": "Ottawa Senators", "prices": [{"decimal": 2.03}]}]},
                    {"groupCode": "TOTAL_GOALS_OVER/UNDER", "status": "ACTIVE", "handicapValue": 6.5, "outcomes": [
                        {"name": "Over", "prices": [{"decimal": 2.2}]},
                        {"name": "Under", "prices": [{"decimal": 1.67}]}]}]}]]}
        game, = gamelines.parse_mise(payload, nhl)
        self.assertEqual(game["ml"], {"away": 2.03, "home": 1.85})   # the outcome order is not the sides
        self.assertEqual(game["totals"], [{"line": 6.5, "over": 2.2, "under": 1.67}])


class BovadaTest(unittest.TestCase):
    def test_reads_the_game_markets(self):
        page = [{"events": [{"description": "Ottawa Senators @ New Jersey Devils", "displayGroups": [
            {"markets": [
                {"description": "Moneyline", "status": "O", "period": {"description": "Game"}, "outcomes": [
                    {"description": "Ottawa Senators", "status": "O", "price": {"decimal": 2.05}},
                    {"description": "New Jersey Devils", "status": "O", "price": {"decimal": 1.8}}]},
                {"description": "Total", "key": "2W-OU", "status": "O", "period": {"description": "Game"}, "outcomes": [
                    {"description": "Over", "status": "O", "price": {"decimal": 1.971, "handicap": "6.0"}},
                    {"description": "Under", "status": "O", "price": {"decimal": 1.855, "handicap": "6.0"}}]}]}]}]}]
        payload = {"pages": [{"name": "Ottawa Senators @ New Jersey Devils",
                              "start": 1791848820000, "page": page}]}
        game, = gamelines.parse_bovada(payload, nhl)
        self.assertEqual(game["ml"], {"away": 2.05, "home": 1.8})
        self.assertEqual(game["totals"], [{"line": 6.0, "over": 1.971, "under": 1.855}])


class BetanoTest(unittest.TestCase):
    def test_reads_the_main_markets_the_league_page_embeds(self):
        sel = lambda name, price, handicap=0.0, team=None: {"name": name, "price": price, "handicap": handicap, "teamId": team}
        event = {"name": "Seattle Seahawks @ Denver Broncos", "startTime": 1792109700000,
                 "participants": [{"id": "1", "name": "Seattle Seahawks"}, {"id": "2", "name": "Denver Broncos"}],
                 "markets": [
                     {"name": "Spread", "selections": [sel("Seattle Seahawks -2.5", 1.8, -2.5, "1"),
                                                       sel("Denver Broncos +2.5", 1.93, 2.5, "2")]},
                     {"name": "Points", "selections": [sel("Over 40.5", 1.85, 40.5), sel("Under 40.5", 1.88, 40.5)]},
                     {"name": "Winner", "selections": [sel("Seattle Seahawks", 1.7, team="1"),
                                                       sel("Denver Broncos", 2.2, team="2")]}]}
        hockey = {**event, "markets": event["markets"][2:] + [
            {"name": "Goals", "selections": [sel("Over 6.5", 1.9, 6.5), sel("Under 6.5", 1.9, 6.5)]}]}
        self.assertEqual(gamelines.parse_betano({"events": [hockey]}, nfl)[0]["totals"],
                         [{"line": 6.5, "over": 1.9, "under": 1.9}])   # NHL's total is "Goals", MLB's "Runs"
        live = {**event, "name": "Houston Texans @ Jacksonville Jaguars",
                "markets": [{**m, "suspended": True} for m in event["markets"]]}
        self.assertEqual(gamelines.parse_betano({"events": [event, live]}, nfl), [{
            "start": "2026-10-16T00:15:00Z", "away_name": "Seattle Seahawks", "home_name": "Denver Broncos",
            "ml": {"away": 1.7, "home": 2.2}, "totals": [{"line": 40.5, "over": 1.85, "under": 1.88}],
            "spreads": [{"line": 2.5, "home": 1.93, "away": 1.8}]}])


class MergeTest(unittest.TestCase):
    def book(self, key, games):
        return {"key": key, "games": games, "error": None, "at": None}

    def game(self, ml=None, totals=None, spreads=None):
        return {"start": "2026-10-12T23:07:00+00:00", "away_name": "Ottawa Senators",
                "home_name": "New Jersey Devils", "ml": ml or {}, "totals": totals or [], "spreads": spreads or []}

    def test_picks_each_books_line_nearest_the_main_line_median(self):
        main = self.game(totals=[{"line": 6.0, "over": 2.0, "under": 1.885}])
        rungs = self.game(totals=[{"line": line, "over": 2.0, "under": 1.9}
                                  for line in (4.5, 5.5, 6.5, 7.5, 8.5)])
        rows = gamelines._merge([self.book("betano", [main]), self.book("mise", [rungs])], nhl)
        row, = rows
        self.assertEqual(row["totalLine"], 6.0)
        self.assertEqual([t["line"] for t in row["books"]["mise"]["totals"]], [5.5])   # 5.5 and 6.5 tie; the first wins
        self.assertEqual(row["books"]["betano"]["totals"][0]["line"], 6.0)

    def test_with_only_ladders_the_most_even_rung_is_the_main_line(self):
        # Kalshi's rungs from 3.5 up: their median is 5.5, but 6.5 is where over and under meet
        ladder = self.game(totals=[{"line": 3.5, "over": 1.05, "under": 6.67}, {"line": 4.5, "over": 1.2, "under": 4.0},
                                   {"line": 5.5, "over": 1.5, "under": 2.5}, {"line": 6.5, "over": 1.96, "under": 1.92},
                                   {"line": 7.5, "over": 3.3, "under": 1.27}])
        row, = gamelines._merge([self.book("kalshi", [ladder])], nhl)
        self.assertEqual(row["totalLine"], 6.5)
        self.assertEqual(row["books"]["kalshi"]["totals"], [{"line": 6.5, "over": 1.96, "under": 1.92}])

    def test_spreads_are_matched_among_the_spreads_not_to_the_total(self):
        single = self.game(totals=[{"line": 6.0, "over": 2.0, "under": 1.885}],
                           spreads=[{"line": -1.5, "home": 2.6, "away": 1.5}])
        ladder = self.game(spreads=[{"line": line, "home": h, "away": a}
                                    for line, h, a in ((-1.5, 2.55, 1.52), (1.5, 1.25, 4.0), (2.5, 1.08, 8.0))])
        row, = gamelines._merge([self.book("pinnacle", [single]), self.book("kambi", [ladder])], nhl)
        self.assertEqual(row["books"]["kambi"]["spreads"], [{"line": -1.5, "home": 2.55, "away": 1.52}])

    def test_fanduels_mlb_names_carry_the_starters(self):
        self.assertEqual(gamelines.team_code(mlb, "Milwaukee Brewers (R Gasser)"), "MIL")
        self.assertIsNone(gamelines.team_code(nba, "Dallas Mavericks (HYPER)"))   # a special event, not the game

    def test_a_team_without_a_code_is_left_out(self):
        rows = gamelines._merge([self.book("betano", [self.game()]),
                              self.book("x", [dict(self.game(), away_name="Northern Ontario X")])], nhl)
        row, = rows
        self.assertEqual(set(row["books"]), {"betano"})


class KalshiTest(unittest.TestCase):
    def test_reads_the_moneyline_and_each_total_rung_by_team_id(self):
        team = lambda code, uuid, yes, no: {"ticker": f"KXNHLGAME-26OCT20TBVGK-{code}", "custom_strike": {"hockey_team": uuid},
                                            "yes_ask_dollars": yes, "no_ask_dollars": no}
        rung = lambda strike, yes, no: {"event_ticker": "KXNHLTOTAL-26OCT20TBVGK", "floor_strike": strike,
                                        "yes_ask_dollars": yes, "no_ask_dollars": no}
        payload = {"games": [{"game": "26OCT20TBVGK", "start": "2026-10-21T02:00:00Z", "home_id": "v", "away_id": "t",
                              "markets": [team("VGK", "v", "0.5400", "0.4800"), team("TB", "t", "0.4700", "0.4700")]}],
                   "totals": [rung(4.5, "0.7600", "0.2600"), rung(5.5, "0.5500", "0.4700"),
                              rung(6.5, "0.3200", "0.9800")]}   # 6.5 is 30 cents wide
        game, = gamelines.parse_kalshi(payload, nhl)
        self.assertEqual((game["away"], game["home"]), ("TBL", "VGK"))   # Kalshi's TB is the page's TBL
        # Vegas: its yes at 54 or Tampa's no at 47, whichever is cheaper; Tampa likewise
        self.assertEqual(game["ml"], {"away": round(1 / 0.47, 3), "home": round(1 / 0.47, 3)})
        self.assertEqual(game["totals"], [{"line": 5.5, "over": 1.818, "under": 2.128}])   # the most even rung only
        # a lone lopsided rung is no main line
        payload["totals"] = [rung(3.5, "0.9500", "0.1500")]
        game, = gamelines.parse_kalshi(payload, nhl)
        self.assertEqual(game["totals"], [])

    def test_kalshis_codes_find_the_pages_teams(self):
        self.assertEqual([gamelines.kalshi_team(league, code) for league, code in
                          ((nhl, "LA"), (nfl, "JAC"), (nba, "NOP"), (nba, "SAS"), (mlb, "AZ"), (nhl, "XYZ"))],
                         ["LAK", "JAX", "NO", "SA", "ARI", None])


class FairTest(unittest.TestCase):
    def test_the_fair_line_devigs_the_sharp_books_and_ev_needs_the_featured_book(self):
        rows = gamelines._merge([
            gamelines_test_book("betano", {"away": 2.08, "home": 1.85}, 6.0, 1.97, 1.88),
            gamelines_test_book("pinnacle", {"away": 2.04, "home": 1.87}, 6.0, 2.0, 1.885),
            gamelines_test_book("bovada", {"away": 2.05, "home": 1.8}, 6.0, 1.971, 1.855),
        ], nhl)
        row, = rows
        row["fair"] = gamelines.fair(row)   # two statements, as the build does: _ev reads row["fair"]
        row["ev"] = gamelines._ev(row)
        self.assertAlmostEqual(row["fair"]["ml"]["home"], 0.522, places=2)
        self.assertAlmostEqual(row["fair"]["total"]["over"], 0.485, places=2)
        self.assertAlmostEqual(row["ev"]["ml"]["away"], 0.47826 * 2.08 - 1, places=3)
        # without the featured book there is no EV
        rows = gamelines._merge([gamelines_test_book("pinnacle", {"away": 2.04, "home": 1.87}, 6.0, 2.0, 1.885)], nhl)
        self.assertIsNone(gamelines._ev(rows[0]))


class WeightedFairTest(unittest.TestCase):
    def test_books_count_by_weight_kambi_not_at_all_and_a_wide_pair_is_left_out(self):
        rows = gamelines._merge([
            gamelines_test_book("kalshi", {"away": 2.0, "home": 2.0}, 6.0, 2.0, 2.0),           # 50%, weight 1.1
            gamelines_test_book("pinnacle", {"away": 1.8, "home": 2.1}, 6.0, 1.8, 2.25),       # weight 1
            gamelines_test_book("kambi", {"away": 3.0, "home": 1.4}, 6.0, 3.0, 1.4),           # shown only
            gamelines_test_book("fanduel", {"away": 1.5, "home": 2.0}, 6.0, 1.5, 2.0),         # 117% implied: too wide
        ], nhl)
        row, = rows
        fair = gamelines.fair(row)
        pin = (1 / 1.8) / (1 / 1.8 + 1 / 2.1)
        expect = 1 / (1 + math.exp(-(1 * math.log(pin / (1 - pin))) / 2.1))   # Kalshi's 50% adds 0 log-odds
        self.assertAlmostEqual(fair["ml"]["away"], expect, places=6)
        self.assertAlmostEqual(fair["ml"]["home"], 1 - expect, places=6)
        self.assertLess(fair["total"]["over"], pin + 1e-9)


def gamelines_test_book(key, ml, line, over, under):
    return {"key": key, "error": None, "at": None, "games": [{
        "start": "2026-10-12T23:07:00+00:00", "away_name": "Ottawa Senators", "home_name": "New Jersey Devils",
        "ml": ml, "totals": [{"line": line, "over": over, "under": under}], "spreads": []}]}



def betway_page(records: dict) -> str:
    """A league page as Betway serves it: its records in the Next.js data, escaped into a push."""
    flight = json.dumps({"store": records}, separators=(",", ":"))
    return f'<html><script>self.__next_f.push([1,{json.dumps(flight)}])</script></html>'


class BetwayTest(unittest.TestCase):
    def outcome(self, oid, market, side, team, price, line=None):
        return {"id": oid, "name": {"coupon": side, "default": team}, "eventId": 1, "marketId": market,
                "displayed": True, "suspended": False, "oddsDecimal": price, "handicapDecimal": line}

    def test_reads_the_games_main_markets_from_the_pages_data(self):
        events = {"1": {"id": 1, "started": False, "name": "Florida Panthers - Vancouver Canucks",
                        "startsAt": "2026-10-15T23:00:00.000Z"},
                  "2": {"id": 2, "started": True, "name": "Colorado Avalanche - Dallas Stars",
                        "startsAt": "2026-10-15T00:30:00.000Z"}}
        markets = {"10": {"id": 10, "name": {"default": "Money Line"}, "eventId": 1, "outcomes": [100, 101],
                          "suspended": False, "typeCName": "to-win"},
                   "11": {"id": 11, "name": {"default": "Puck Line"}, "eventId": 1, "outcomes": [102, 103],
                          "suspended": False, "typeCName": "handicap"},
                   "12": {"id": 12, "name": {"default": "Total Goals"}, "eventId": 1, "outcomes": [104, 105],
                          "suspended": False, "typeCName": "handicap-goals-over"},
                   "13": {"id": 13, "name": {"default": "Money Line - 8Up"}, "eventId": 1, "outcomes": [106, 107],
                          "suspended": False, "typeCName": "to-win"},
                   "14": {"id": 14, "name": {"default": "Money Line"}, "eventId": 2, "outcomes": [],
                          "suspended": False, "typeCName": "to-win"}}
        outcomes = {str(o["id"]): o for o in [
            self.outcome(100, 10, "Away", "Vancouver Canucks", 3.4), self.outcome(101, 10, "Home", "Florida Panthers", 1.333),
            self.outcome(102, 11, "Away", "Vancouver Canucks", 1.952, 1.5), self.outcome(103, 11, "Home", "Florida Panthers", 1.741, -1.5),
            self.outcome(104, 12, "Over", "Over", 2.1, 6.5), self.outcome(105, 12, "Under", "Under", 1.769, 6.5),
            self.outcome(106, 13, "Away", "Vancouver Canucks", 3.6), self.outcome(107, 13, "Home", "Florida Panthers", 1.4)]}
        page = betway_page({"events": {"byId": events}, "markets": markets, "outcomes": outcomes})

        class Answer:
            text = page
            def raise_for_status(self): pass
        now = dt.datetime(2026, 10, 15, 2, 50, tzinfo=dt.timezone.utc)
        with mock.patch.object(gamelines.httpx, "get", lambda *a, **k: Answer()):
            payload = gamelines.fetch_betway(nhl, now)
        game, = gamelines.parse_betway(payload, nhl)   # the started game is left out
        self.assertEqual(game, {"start": "2026-10-15T23:00:00Z", "away_name": "Vancouver Canucks",
                                "home_name": "Florida Panthers", "ml": {"away": 3.4, "home": 1.333},
                                "totals": [{"line": 6.5, "over": 2.1, "under": 1.769}],
                                "spreads": [{"line": -1.5, "home": 1.741, "away": 1.952}]})


class StakeTest(unittest.TestCase):
    def test_reads_winner_handicap_and_total_with_the_home_team_first(self):
        market = lambda spec, *outcomes: {"status": "active", "specifiers": spec,
                                          "outcomes": [{"name": n, "active": True, "odds": o} for n, o in outcomes]}
        payload = {"fixtures": [{"status": "active", "data": {
            "startTime": "Thu, 15 Oct 2026 23:00:00 GMT",
            "competitors": [{"name": "New Jersey Devils"}, {"name": "New York Rangers"}]},
            "groups": [{"templates": [
                {"name": "Winner (Incl. Overtime and Penalties)",
                 "markets": [market("", ("New Jersey Devils", 1.68), ("New York Rangers", 2.14))]},
                {"name": "Total (Incl. Overtime and Penalties)",
                 "markets": [market("total=5.5", ("Over 5.5", 1.69), ("Under 5.5", 2.09))]},
                {"name": "Handicap (Incl. Overtime and Penalties)",
                 "markets": [market("hcp=-1.5", ("New Jersey Devils (-1.5)", 2.6), ("New York Rangers (1.5)", 1.46))]}]}]}]}
        game, = gamelines.parse_stake(payload, nhl)
        self.assertEqual((game["start"], game["away_name"], game["home_name"]),
                         ("2026-10-15T23:00:00Z", "New York Rangers", "New Jersey Devils"))
        self.assertEqual(game["ml"], {"away": 2.14, "home": 1.68})
        self.assertEqual(game["totals"], [{"line": 5.5, "over": 1.69, "under": 2.09}])
        self.assertEqual(game["spreads"], [{"line": -1.5, "home": 2.6, "away": 1.46}])


if __name__ == "__main__":
    unittest.main()
