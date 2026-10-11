"""What counts as 'today' on a board."""
import datetime as dt
from zoneinfo import ZoneInfo

import httpx

ET = ZoneInfo("America/New_York")


def slate_date(now: dt.datetime | None = None) -> dt.date:
    """Today's slate: Eastern time, rolling over at 5 AM so a late West Coast
    game still belongs to the night it started."""
    now = now or dt.datetime.now(ET)
    return (now.astimezone(ET) - dt.timedelta(hours=5)).date()


def board_date(games: list[dict], now: dt.datetime | None = None) -> dt.date:
    """The day a board covers: slate_date(), except that between midnight and 5 AM it
    moves on to the new day once every game of the night before is final. `games` is
    the league's schedule from slate_date() on; without one it waits for 5 AM."""
    now = (now or dt.datetime.now(ET)).astimezone(ET)
    day = slate_date(now)
    if games and day < now.date() and all(g["state"] == "final" for g in games if g["date"] == str(day)):
        return now.date()
    return day


def fetch_board_date(schedule, now: dt.datetime | None = None) -> dt.date:
    """board_date() for a league, given its schedule function. The schedule is only
    fetched between midnight and 5 AM, and a failed fetch waits for 5 AM."""
    now = (now or dt.datetime.now(ET)).astimezone(ET)
    day = slate_date(now)
    if day == now.date():
        return day
    try:
        return board_date(schedule(day), now)
    except (httpx.HTTPError, ValueError):
        return day
