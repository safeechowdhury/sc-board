"""Small builders for game-log frames and Underdog payloads."""
import datetime as dt

import polars as pl

from props.leagues import nhl


def game_logs(rows: list[dict], league=nhl) -> pl.DataFrame:
    """A frame in the league's stored schema; unspecified columns are null."""
    defaults = {"season": 20262027, "game_type": 2, "home": True, "position": "C"}
    filled = []
    for row in rows:
        row = {**defaults, **row}
        if isinstance(row["date"], str):
            row["date"] = dt.date.fromisoformat(row["date"])
        filled.append({col: row.get(col) for col in league.SCHEMA})
    return pl.DataFrame(filled, schema=league.SCHEMA)


def underdog_payload(lines: list[dict]) -> dict:
    """One game, BOS @ WPG, with a player per distinct name in `lines`.

    Each line: {"name", "team" ("BOS" or "WPG"), "stat", "value", "options": [(choice, odds, mult)]}
    plus optional "sport" and "status".
    """
    payload = {
        "games": [{"id": 1, "title": "BOS @ WPG", "home_team_id": "wpg", "away_team_id": "bos",
                   "scheduled_at": "2026-10-03T00:00:00Z"}],
        "players": [], "appearances": [], "over_under_lines": [],
    }
    for i, line in enumerate(lines):
        first, _, last = line["name"].partition(" ")
        payload["players"].append({"id": f"p{i}", "first_name": first, "last_name": last,
                                   "position_name": line.get("position", "C"),
                                   "sport_id": line.get("sport", "NHL")})
        payload["appearances"].append({"id": f"a{i}", "player_id": f"p{i}", "match_id": 1,
                                       "team_id": line["team"].lower()})
        payload["over_under_lines"].append({
            "status": line.get("status", "active"),
            "line_type": "balanced",
            "stat_value": str(line["value"]),
            "over_under": {"appearance_stat": {"appearance_id": f"a{i}", "stat": line["stat"],
                                               "display_stat": line["stat"].replace("_", " ").title()}},
            "options": [{"choice": choice, "odds": {"fantasy": {"decimal": str(odds)}},
                         "payout_multiplier": str(mult), "status": "active"}
                        for choice, odds, mult in line["options"]],
        })
    return payload


def pinnacle_payload(props: list[tuple]) -> dict:
    """Pinnacle's matchups and markets for props given as (player, units, line, over, under) in American odds."""
    payload = {"matchups": [], "markets": []}
    for i, (player, units, line, over, under) in enumerate(props):
        mid = 100 + 10 * i
        payload["matchups"].append({
            "id": mid, "units": units, "startTime": "2099-01-01T00:00:00Z",   # not started, whatever the clock says
            "special": {"category": "Player Props", "description": f"{player} Total {units}"},
            "participants": [{"id": mid + 1, "name": "Over"}, {"id": mid + 2, "name": "Under"}],
        })
        payload["markets"].append({"matchupId": mid, "type": "total", "period": 0, "prices": [
            {"participantId": mid + 1, "points": line, "price": over},
            {"participantId": mid + 2, "points": line, "price": under},
        ]})
    return payload
