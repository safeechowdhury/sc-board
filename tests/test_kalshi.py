import datetime as dt
import unittest
import unittest.mock

from props import kalshi
from props.leagues import nfl, nhl


def market(series, player, strike, yes, no, team="WSH"):
    return {"series": series, "player": player, "team": team, "strike": strike, "yes": yes, "no": no}


class ParseTest(unittest.TestCase):
    def test_a_rungs_yes_is_the_over_and_its_no_the_under(self):
        props, skipped = kalshi.parse({"markets": [
            market("KXNHLPTS", "Tom Wilson", 1.5, "0.2300", "0.8100"),
            market("KXNHLGOAL", "Tom Wilson", 0.5, "0.3500", "0.6800"),
        ]}, nhl)
        self.assertEqual(props, [
            {"player": "Tom Wilson", "stat": "points", "line": 1.5, "over": 4.348, "under": 1.235, "team": "WSH"},
            {"player": "Tom Wilson", "stat": "goals", "line": 0.5, "over": 2.857, "under": 1.471, "team": "WSH"},
        ])
        self.assertFalse(skipped)

    def test_a_one_sided_or_wide_market_is_left_out(self):
        props, _ = kalshi.parse({"markets": [
            market("KXNHLGOAL", "Tyler Seguin", 2.5, "0.9800", None),        # no bids: a placeholder ask
            market("KXNHLGOAL", "Tyler Seguin", 1.5, "0.3200", "0.9800"),    # 30 cents wide
            market("KXNHLGOAL", "Tyler Seguin", 0.5, "0.3400", "0.7500"),    # 9 cents: kept
        ]}, nhl)
        self.assertEqual([p["line"] for p in props], [0.5])

    def test_a_first_scorer_has_no_strike_and_is_the_over_at_one_half(self):
        props, _ = kalshi.parse({"markets": [market("KXNHLFIRSTGOAL", "Tom Wilson", None, "0.0800", "0.9400")]}, nhl)
        self.assertEqual((props[0]["stat"], props[0]["line"]), ("first_goals", 0.5))

    def test_kalshis_team_codes_become_the_boards(self):
        props, _ = kalshi.parse({"markets": [market("KXNHLGOAL", "Brandon Hagel", 0.5, "0.3500", "0.6800", "TB"),
                                             market("KXNHLGOAL", "Nobody Known", 0.5, "0.3500", "0.6800", None)]}, nhl)
        self.assertEqual(props[0]["team"], "TBL")
        self.assertNotIn("team", props[1])

    def test_a_competitions_series_is_read_by_the_stat_its_market_carries(self):
        # soccer's series are set per competition; parse sees the league, whose own map is empty
        from props.leagues import soccer
        props, skipped = kalshi.parse({"markets": [{**market("KXEPLSOA", "Cole Palmer", 0.5, "0.4500", "0.5800", None),
                                                    "stat": "goals_assists", "game": ["2026-10-17T14:00:00Z", "Everton vs Chelsea"]}]},
                                      soccer)
        self.assertEqual((props[0]["stat"], props[0]["game"], dict(skipped)),
                         ("goals_assists", ["2026-10-17T14:00:00Z", "Everton vs Chelsea"], {}))

    def test_unmapped_series_are_counted(self):
        props, skipped = kalshi.parse({"markets": [market("KXNFLFIRSTTD", "Chris Olave", None, "0.1", "0.9")]}, nfl)
        self.assertEqual((props, dict(skipped)), ([], {"KXNFLFIRSTTD": 1}))


class FetchTest(unittest.TestCase):
    def test_reads_the_props_of_the_games_on_the_slate_before_they_start(self):
        now = dt.datetime(2026, 10, 14, 12, tzinfo=dt.timezone.utc)
        at = lambda hours: (now + dt.timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
        events = {"events": [{"event_ticker": f"KXNHLGAME-{g}", "markets": [{"ticker": f"KXNHLGAME-{g}-{code}"}
                                                                            for code in codes]}
                             for g, codes in (("26OCT14MTLWSH", ("MTL", "WSH")), ("26OCT14DALCOL", ("DAL", "COL")),
                                              ("26OCT20TBVGK", ("TB", "VGK")))],
                  "milestones": [{"primary_event_tickers": [f"KXNHLGAME-{g}"], "start_date": at(hours), "details": {}}
                                 for g, hours in (("26OCT14MTLWSH", 11), ("26OCT14DALCOL", -1), ("26OCT20TBVGK", 24 * 6))]}
        goals = {"markets": [
            {"ticker": f"KXNHLGOAL-{g}-{who}-1", "event_ticker": f"KXNHLGOAL-{g}", "yes_sub_title": f"{name}: 1+",
             "floor_strike": 0.5, "yes_ask_dollars": "0.3500", "no_ask_dollars": "0.6800"}
            for g, who, name in (("26OCT14MTLWSH", "WSHTWILSON43", "Tom Wilson"),
                                 ("26OCT14DALCOL", "COLNMACKINNON29", "Nathan MacKinnon"),   # under way
                                 ("26OCT20TBVGK", "TBBHAGEL38", "Brandon Hagel"))]}          # past the slate
        seen = []

        class Http:
            def __init__(self, *a, **k): pass
            def close(self): pass
            def get(self, url, params=None):
                seen.append((url.split("/v2", 1)[1], params.get("series_ticker")))
                data = events if url.endswith("/events") else goals if params["series_ticker"] == "KXNHLGOAL" else {}
                return type("R", (), {"status_code": 200, "raise_for_status": lambda self: None, "json": lambda self: data})()

        with unittest.mock.patch.object(kalshi.httpx, "Client", Http):
            payload = kalshi.fetch(nhl, now=now)
        self.assertEqual(seen[:2], [("/events", "KXNHLGAME"), ("/markets", "KXNHLGOAL")])
        self.assertEqual(len(seen), 1 + len(nhl.KALSHI_STATS))   # the games, then one request a series
        self.assertEqual(payload, {"markets": [{"series": "KXNHLGOAL", "stat": "goals", "player": "Tom Wilson", "team": "WSH",
                                                "strike": 0.5, "yes": "0.3500", "no": "0.6800"}]})

    def test_an_odds_board_asks_for_no_series_when_none_of_the_competitions_games_is_on_it(self):
        import types
        seen = []
        events = {"events": [{"event_ticker": "KXUELGAME-26OCT14ROMFCP", "title": "Roma vs Porto", "markets": []}],
                  "milestones": [{"primary_event_tickers": ["KXUELGAME-26OCT14ROMFCP"], "start_date": "2026-10-14T19:00:00Z"}]}

        class Http:
            def __init__(self, *a, **k): pass
            def close(self): pass
            def get(self, url, params=None):
                seen.append(params.get("series_ticker"))
                return type("R", (), {"status_code": 200, "raise_for_status": lambda self: None, "json": lambda self: events})()

        league = types.SimpleNamespace(KEY="soccer", KALSHI_GAME="KXUELGAME", KALSHI_STATS={"KXUELGOAL": "goals"}, SLATE_DAYS=6,
                                       BOARD_GAMES=[])   # nothing of the Europa League's on the board
        with unittest.mock.patch.object(kalshi.httpx, "Client", Http):
            payload = kalshi.fetch(league, now=dt.datetime(2026, 10, 14, 12, tzinfo=dt.timezone.utc))
        self.assertEqual((payload, seen), ({"markets": []}, ["KXUELGAME"]))


if __name__ == "__main__":
    unittest.main()
