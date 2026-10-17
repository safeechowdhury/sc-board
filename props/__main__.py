"""python -m props [publish | refresh | backfill | build | lines | grade | serve]

With no command: refresh the game logs, build every board and open the first.
"""
import argparse
import functools
import http.server
import webbrowser

from . import gamelines, grade, net, publish, site
from .leagues import LEAGUES
from .store import Store


def refresh(leagues, days: int) -> None:
    for league in [league for league in leagues if not getattr(league, "ODDS_ONLY", False)]:   # no logs to refresh
        for message in league.refresh(Store(league), days):
            print(f"refresh {league.KEY}: {message}")


def backfill(league, seasons: list[int]) -> None:
    for message in league.backfill(Store(league), seasons):
        print(f"backfill {league.KEY}: {message}")


def serve(port: int) -> None:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site.OUT))
    print(f"serving {site.OUT} at http://localhost:{port}")
    http.server.ThreadingHTTPServer(("", port), handler).serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(prog="props", description=__doc__.strip().splitlines()[-1])
    sub = ap.add_subparsers(dest="cmd")
    league = argparse.ArgumentParser(add_help=False)
    league.add_argument("--league", choices=LEAGUES, help="one league only (default: all)")
    p = sub.add_parser("publish", help="refresh every board and the lines page at once, then zip the site for upload")
    p.add_argument("--offline", action="store_true", help="reuse the last fetched lines, schedule and rosters")
    p.add_argument("--logs", action="store_true", help="refresh the game logs even if they were already today")
    p = sub.add_parser("refresh", parents=[league], help="update game logs for the season in progress")
    p.add_argument("--days", type=int, default=3, help="NHL: days to re-read for stat corrections (default 3)")
    p = sub.add_parser("backfill", help="rebuild whole seasons, e.g. backfill nhl 20252026, backfill nfl 2024 2025")
    p.add_argument("league", choices=[key for key, league in LEAGUES.items() if not getattr(league, "ODDS_ONLY", False)])
    p.add_argument("seasons", type=int, nargs="+")
    p = sub.add_parser("build", parents=[league], help="fetch Underdog lines and write the site")
    p.add_argument("--offline", action="store_true", help="reuse the last fetched lines, schedule and rosters")
    p.add_argument("--open", action="store_true", help="open the board in a browser when done")
    p = sub.add_parser("serve", help="serve the built site locally (for phones on the same network)")
    p.add_argument("--port", type=int, default=8000)
    p = sub.add_parser("lines", parents=[league], help="fetch moneylines and totals, write site/lines.html")
    p.add_argument("--offline", action="store_true", help="reuse the last fetched lines")
    p = sub.add_parser("grade", parents=[league], help="grade the books and Fair from the price snapshots")
    a = ap.parse_args()

    one = getattr(a, "league", None) if a.cmd != "backfill" else None
    chosen = [LEAGUES[one]] if one else list(LEAGUES.values())
    if a.cmd == "publish":
        publish.publish(LEAGUES, offline=a.offline, logs=a.logs)
    elif a.cmd == "refresh":
        refresh(chosen, a.days)
    elif a.cmd == "backfill":
        backfill(LEAGUES[a.league], a.seasons)
    elif a.cmd == "grade":
        for line in grade.report(grade.grade({league.KEY: league for league in chosen})):
            print(line)
        print(f"wrote {grade.OUT}")
    elif a.cmd == "serve":
        serve(a.port)
    elif a.cmd == "lines":
        with net.run():
            print(f"wrote {gamelines.build(LEAGUES if one is None else {one: LEAGUES[one]}, offline=a.offline)}")
    else:
        if a.cmd is None:
            refresh(chosen, days=3)
        pages = publish.build_all({league.KEY: league for league in chosen}, getattr(a, "offline", False), lines=False)
        if a.cmd is None or a.open:
            webbrowser.open(pages[0].as_uri())


if __name__ == "__main__":
    main()
