"""Assemble a league's board and write its page.

    site/index.html, site/nfl.html, site/mlb.html   one board per league; all of its data is embedded, so it opens from disk
    site/p/<league>/<id>.js          one player's full game log and teammates, loaded when their page is opened
    site/ud.html                     every Underdog line in every sport, fetched live by the page
    site/ud/<league>.js              which of a league's Underdog lines the board covers, so the viewer can link to them
"""
import datetime as dt
import json
import shutil
from collections import Counter
from pathlib import Path

import httpx
import polars as pl

from . import books, metrics, store, underdog
from .leagues import LEAGUES
from .slate import ET, board_date, slate_date

OUT = store.ROOT / "site"
CACHE = store.DATA / "cache"
TEMPLATE = Path(__file__).with_name("template.html")
VIEWER = Path(__file__).with_name("ud.html")
VIEWER_PAGE = "ud.html"


def _dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _fetch_cached(name: str, fetch, offline: bool):
    """(data, fetched_at, error). A failed fetch falls back to the last good copy.

    A copy saved on an earlier slate is never used: its lines and prices are for games
    already played, and a book's would land on today's rows by player and stat. It is
    fetched again even offline, and if that fails the source is left out with an error."""
    path = CACHE / f"{name}.json"
    saved = dt.datetime.fromtimestamp(path.stat().st_mtime, ET) if path.exists() else None
    stale = saved is not None and slate_date(saved) < slate_date()
    error = None
    if not offline or stale:
        try:
            data = fetch()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))
            return data, dt.datetime.now(ET), None
        except (httpx.HTTPError, ValueError) as e:
            error = f"{type(e).__name__}: {e}"
    if stale:
        return None, None, f"{error}; the saved copy is from the {slate_date(saved)} slate, so it was not used"
    if saved:
        return json.loads(path.read_text()), saved, error
    return None, None, error or "no cached copy"


def _slate_date(start: str) -> dt.date:
    return slate_date(dt.datetime.fromisoformat(start.replace("Z", "+00:00")))


def _nav() -> list[list[str]]:
    return [[league.NAME, league.PAGE] for league in LEAGUES.values()] + [["UD Lines", VIEWER_PAGE]]


_sports: list = []   # the viewer's sport list, fetched once a run however many leagues it builds


def write_viewer(league, players: pl.DataFrame, offline: bool) -> list[str]:
    """Write the Underdog lines viewer and this league's link file. Returns messages for the build log.

    The viewer fetches lines in the browser, so the page holds only what it can't get there:
    the sports Underdog has lines up for, and, per league, the stats the board carries and
    each player's id, so a line the board covers opens the player's page at that line."""
    messages = []
    if not _sports:
        found, at, error = _fetch_cached("underdog-sports", underdog.sports, offline)
        _sports.append(found or [])
        if error:
            messages.append(f"Underdog sports list fetch failed ({error})"
                            + (f"; used the list cached at {at:%Y-%m-%d %H:%M}" if found else ""))
    names = Counter(underdog.norm_name(name) for name in players["name"])
    links = {
        "league": league.KEY, "page": league.PAGE, "sport": league.UNDERDOG_SPORT,
        "stats": league.UNDERDOG_STATS, "fixed": getattr(league, "FIXED_LINES", {}), "teams": league.TEAM_ALIASES,
        # a name two players share is left out rather than guessed
        "players": {underdog.norm_name(name): pid for pid, name in players.select("player_id", "name").iter_rows()
                    if names[underdog.norm_name(name)] == 1},
    }
    (OUT / "ud").mkdir(parents=True, exist_ok=True)
    (OUT / "ud" / f"{league.KEY}.js").write_text(f"UDLINKS.push({_dumps(links)})", encoding="utf-8")
    data = {"nav": _nav(), "url": underdog.request_url(""), "sports": _sports[0],
            "leagues": [other.KEY for other in LEAGUES.values()]}
    (OUT / VIEWER_PAGE).write_text(VIEWER.read_text(encoding="utf-8")
                                   .replace("__DATA__", _dumps(data).replace("</", "<\\/")), encoding="utf-8")
    return messages


def _player_index(league, df: pl.DataFrame) -> pl.DataFrame:
    """Each player's most recent name, team and position plus a photo, newest players first."""
    index = df.sort("date", "game_id", descending=True).unique("player_id", keep="first", maintain_order=True)
    return index.select("player_id", "name", "team", "position", "season",
                        pl.Series("mug", league.headshots(index), dtype=pl.String))


def _position(league, name: str | None) -> str:
    """Underdog's position name as one of ours ("LW" -> "L"), or "" if it isn't one."""
    return next((code for code, (label, _, _) in league.POSITIONS.items() if name in (code, label)), "")


def _dvp_stats(league) -> dict[str, list[str]]:
    """The stats worth ranking defenses on for each position group."""
    stats: dict[str, list[str]] = {}
    for _, name, group in league.POSITIONS.values():
        wanted, kind = stats.setdefault(group, []), league.KINDS[name]
        wanted += [stat for stat in kind.get("dvp", kind["picks"]) if stat not in wanted]
    return stats


class _Board:
    """Builds the board's rows. Holds the lookups every row needs."""

    def __init__(self, league, df: pl.DataFrame, logs: pl.DataFrame, index: pl.DataFrame, dvp: pl.DataFrame, season: int):
        self.league = league
        self.season = season
        self.info = {p["player_id"]: p for p in index.iter_rows(named=True)}
        self.ranks = {(team, group, stat): rank for team, group, stat, rank in
                      dvp.select("team", "group", "stat", "rank").iter_rows()}
        dates = df.select("team", "date").unique().sort("date", descending=True)
        self.team_dates = {team: frame["date"].to_list() for (team,), frame in
                           dates.partition_by("team", as_dict=True, maintain_order=True).items()}
        # each player's games as columns, newest first
        self.logs = {pid: frame.to_dict(as_series=False) for (pid,), frame in
                     logs.select("player_id", "date", "opponent", "season", "game_type", *league.PROPS)
                     .partition_by("player_id", as_dict=True, include_key=False).items()}

    def _row(self, pid, name, team, opponent, home, position, start, game, stat, line, offers=None) -> dict:
        date = dt.date.fromisoformat(game[:10])
        played = self.team_dates.get(team)
        group = self.league.POSITIONS[position][2] if position in self.league.POSITIONS else None
        row = {
            "id": pid, "name": name, "team": team, "opp": opponent, "home": home, "pos": position,
            "start": start, "game": game, "stat": stat, "line": line, "ud": offers is not None,
            "dvp": self.ranks.get((opponent, group, stat)),
        }
        if self.league.REST:
            row["rest"] = min((date - played[0]).days - 1, metrics.MAX_REST) if played else None
        if offers:
            row.update(over=offers["over"], under=offers["under"])
        if pid in self.logs:
            row.update(metrics.prop_row(self.logs[pid], stat, line, opponent, self.season))
            # the same with playoff games counted, for the Include playoffs toggle, where that changes anything
            playoffs = metrics.prop_row(self.logs[pid], stat, line, opponent, self.season, playoffs=True)
            if any(playoffs[key] != row[key] for key in playoffs):
                row["po"] = playoffs
        return row

    def last_ten(self, pid, playoffs: bool) -> list:
        """Dates and opponents of a player's last ten games, oldest first."""
        log = self.logs[pid]
        games = [[str(date), opponent] for date, opponent, kind in zip(log["date"], log["opponent"], log["game_type"])
                 if playoffs or kind != metrics.PLAYOFFS]
        return games[:10][::-1]

    def underdog_rows(self, lines: list[dict]) -> list[dict]:
        rows = []
        for line in lines:
            known = self.info.get(line["player_id"])
            position = known["position"] if known else _position(self.league, line["position"])
            rows.append(self._row(line["player_id"], line["player"], line["team"], line["opponent"], line["home"],
                                  position, line["start"], line["game"], line["stat"], line["line"], line))
        return rows

    def typical_rows(self, games: list[dict], rosters: dict, taken: set, priced: set = frozenset()) -> list[dict]:
        """Every other prop for the players expected to play, at a typical line.

        A player counts if he is on the team's roster (or, when rosters are
        unavailable, last played for it) and appeared within its last
        ACTIVE_GAMES games (or his kind's own "active"). Each team is listed for its next game only.
        `taken` holds the (player, stat) pairs Underdog covers.
        Props that are a foregone conclusion for the player are left out.
        A FIXED_LINES stat (2+ TDs) gets a row at its own line wherever a book
        prices it: `priced` holds the (normalized name, stat) pairs the books cover.
        """
        league, rows, listed = self.league, [], set()
        fixed = getattr(league, "FIXED_LINES", {})
        for g in games:   # in kickoff order, so a team with two games on the slate is listed for the first only
            for team, opponent, home in ((g["away"], g["home"], False), (g["home"], g["away"], True)):
                if team in listed:
                    continue
                listed.add(team)
                ids = rosters.get(team) or [pid for pid, p in self.info.items() if p["team"] == team]
                dates = self.team_dates.get(team, [])
                for pid in ids:
                    log, p = self.logs.get(pid), self.info.get(pid)
                    if not log or p["position"] not in league.POSITIONS:
                        continue
                    kind = league.KINDS[league.POSITIONS[p["position"]][1]]
                    active = kind.get("active", league.ACTIVE_GAMES)
                    if dates and log["date"][0] < dates[min(active, len(dates)) - 1]:
                        continue
                    for stat, (_, line) in fixed.items():
                        if (stat in kind["picks"] and (pid, stat) not in taken
                                and (underdog.norm_name(p["name"]), stat) in priced):
                            rows.append(self._row(pid, p["name"], team, opponent, home, p["position"], g["start"],
                                                  g["key"], stat, line))
                    for stat in kind["typical"]:
                        values = [v for v, kind in zip(log[stat], log["game_type"])
                                  if v is not None and kind != metrics.PLAYOFFS][:league.TYPICAL_GAMES]
                        if (pid, stat) in taken or not values:
                            continue
                        line = metrics.typical_line(values)
                        if (sum(values) / len(values) < league.MIN_AVERAGE.get(stat, 0)
                                or not metrics.is_open_question(values, line)):
                            continue
                        rows.append(self._row(pid, p["name"], team, opponent, home, p["position"], g["start"],
                                              g["key"], stat, line))
        return rows


def _games(schedule: list[dict], lines: list[dict], today: dt.date, days: int) -> list[dict]:
    """The slate's games (the next `days` days) plus any later game that already has Underdog lines."""
    counts = Counter(line["game"] for line in lines)
    last = str(today + dt.timedelta(days=days - 1))
    games = {}
    for g in schedule:
        key = f"{g['date']}|{g['away']}@{g['home']}"
        if str(today) <= g["date"] <= last or key in counts:
            games[key] = {"key": key, "date": g["date"], "away": g["away"], "home": g["home"],
                          "start": g["start"], "state": g["state"], "lines": counts[key]}
    for line in lines:  # a game Underdog lists that the schedule fetch did not return
        if line["game"] not in games:
            date, matchup = line["game"].split("|")
            away, home = matchup.split("@")
            games[line["game"]] = {"key": line["game"], "date": date, "away": away, "home": home,
                                   "start": line["start"], "state": "upcoming", "lines": counts[line["game"]]}
    return sorted(games.values(), key=lambda g: g["start"])


def _write_player_files(league, logs: pl.DataFrame, players: pl.DataFrame, mates: dict) -> None:
    folder = OUT / "p" / league.KEY
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    positions = dict(zip(players["player_id"], players["position"]))
    common = ["date", "opponent", "home", "season", "game_type"] + (["rest"] if league.REST else [])
    logs = logs.filter(pl.col("player_id").is_in(list(positions))).with_columns(
        pl.col("date").cast(pl.String), pl.col("home").cast(pl.Int8))
    for (pid,), games in logs.partition_by("player_id", as_dict=True).items():
        if positions[pid] not in league.POSITIONS:
            continue
        kind = league.KINDS[league.POSITIONS[positions[pid]][1]]
        columns = common + list(dict.fromkeys(kind["picks"] + kind["log"] + kind["minis"]))
        data = {"id": pid, "games": games.select(columns).to_dict(as_series=False), "mates": mates.get(pid, [])}
        (folder / f"{pid}.js").write_text(f"SCBOARD.player({_dumps(data)})", encoding="utf-8")


def build(league, offline: bool = False) -> Path:
    games_store = store.Store(league)
    df = games_store.load()
    if df.is_empty():
        raise SystemExit(f"No {league.NAME} game logs in {games_store.box}. "
                         f"Run: python -m props backfill {league.KEY} <season> ...")
    fixed = getattr(league, "FIXED_LINES", {})
    df = df.with_columns(pl.col(column).alias(stat) for stat, (column, _) in fixed.items())

    def say(message: str) -> None:
        print(f"build {league.KEY}: {message}")

    # the schedule from the clock's slate, which says whether last night's games are all final
    clock = slate_date()
    schedule, _, schedule_error = _fetch_cached(f"{league.KEY}-schedule", lambda: league.schedule(clock), offline)
    today = board_date(schedule or [])
    if today != clock:
        schedule, _, schedule_error = _fetch_cached(f"{league.KEY}-schedule", lambda: league.schedule(today), offline)
    season = league.season_id(today)
    previous = league.previous_season(season)
    if league.REST:
        df = metrics.with_rest(df)
    logs = league.samples(df).sort("date", "game_id", descending=True)
    index = _player_index(league, df)

    raw, fetched, error = _fetch_cached(f"{league.KEY}-underdog", lambda: underdog.fetch(league.UNDERDOG_SPORT), offline)
    lines, skipped = underdog.parse(raw, league) if raw else ([], Counter())
    past = len(lines)
    lines = [line for line in lines if _slate_date(line["start"]) >= today]
    past -= len(lines)
    for line in lines:
        away, home = (line["opponent"], line["team"]) if line["home"] else (line["team"], line["opponent"])
        line["game"] = f"{_slate_date(line['start'])}|{away}@{home}"
    unmatched = underdog.match_players(lines, index)
    games = _games(schedule or [], lines, today, league.SLATE_DAYS)
    teams = sorted({team for g in games for team in (g["away"], g["home"])})
    rosters, _, roster_error = _fetch_cached(f"{league.KEY}-rosters", lambda: league.rosters(teams, today), offline)

    groups = {position: group for position, (_, _, group) in league.POSITIONS.items()}
    dvp = metrics.dvp(df, season, groups, _dvp_stats(league))
    fetched_books = []   # per book: (book, props, fetched at, error, saved copy?, unmapped stats)
    for book in books.BOOKS:
        answer, at, failed = _fetch_cached(f"{league.KEY}-{book.KEY}", lambda: book.fetch(league), offline)
        props, unmapped = book.parse(answer, league) if answer else ([], Counter())
        fetched_books.append((book, books.with_fixed(props, league), at, failed, answer is not None, unmapped))
    priced = {(underdog.norm_name(prop["player"]), prop["stat"])
              for _, props, *_ in fetched_books for prop in props if prop["stat"] in fixed}

    board = _Board(league, df, logs, index, dvp, season)
    rows = board.underdog_rows(lines)
    posted = len(rows)
    rows += board.typical_rows(games, rosters or {}, {(row["id"], row["stat"]) for row in rows}, priced)

    quotes = []   # per book: (book, prices, props it missed, fetched at, error, saved copy?, unmapped stats)
    for book, props, at, failed, saved, unmapped in fetched_books:
        quotes.append((book, *books.prices(props, rows), at, failed, saved, unmapped))

    # everyone searchable: active in the last two seasons, plus anyone on the board
    on_board = [row["id"] for row in rows if row["id"] is not None]
    recent = index.filter((pl.col("season") >= previous) | pl.col("player_id").is_in(on_board))

    standard = Counter(offer["odds"] for row in rows[:posted] for offer in (row["over"], row["under"])
                       if offer and offer["mult"] == 1 and offer["odds"])
    # each player's last ten games, shared by all of his rows: regular season, and with the
    # playoffs counted wherever that is a different ten
    recent_games = {pid: board.last_ten(pid, False) for pid in {row["id"] for row in rows} if pid in board.logs}
    recent_playoffs = {pid: board.last_ten(pid, True) for pid in recent_games}
    recent_playoffs = {pid: ten for pid, ten in recent_playoffs.items() if ten != recent_games[pid]}
    dvp_table: dict = {}
    for team, group, stat, allowed, delta, _, rank in dvp.iter_rows():
        dvp_table.setdefault(stat, {}).setdefault(group, {})[team] = [rank, round(allowed, 2), round(delta, 2)]

    payload = {
        "league": league.KEY,
        "name": league.NAME,
        "nav": _nav(),
        "generated": dt.datetime.now(ET).isoformat(timespec="minutes"),
        "date": str(today),
        "season": season,
        "seasonLabel": league.season_label(season),
        "prevSeason": previous,
        "prevSeasonLabel": league.season_label(previous),
        "lastGame": str(df["date"].max()),
        "props": [[key, label, short, league.TABS[key]] for key, (label, short) in league.PROPS.items()],
        "labels": [[key, label, short] for key, (label, short) in league.LABELS.items()],
        "clock": league.CLOCK,
        "positions": league.POSITIONS,
        "kinds": {kind: {part: stats[part] for part in ("picks", "log", "minis")} for kind, stats in league.KINDS.items()},
        "groups": league.GROUPS,
        "teams": dvp["team"].n_unique(),   # teams that have played this season, not every team in the archive
        "rest": league.REST,
        "typicalGames": league.TYPICAL_GAMES,
        "games": games,
        "rows": rows,
        "recent": recent_games,
        "recentPo": recent_playoffs,
        "standardOdds": standard.most_common(1)[0][0] if standard else None,
        "players": recent.select("player_id", "name", "team", "position", "mug").rows(),
        "dvp": dvp_table,
        "lines": {
            "fetched": fetched.isoformat(timespec="minutes") if fetched else None,
            "error": error,
        },
        # Pinnacle's [line, over, under] in decimal odds, keyed "<player id or name>|<stat>"
        # each book's [line, over, under] in decimal odds, keyed "<player id or name>|<stat>"
        "books": [
            {"key": book.KEY, "name": book.NAME, "short": book.SHORT, "sharp": book.SHARP, "target": book.TARGET, "prices": prices,
             "fetched": at.isoformat(timespec="minutes") if at and prices else None}
            for book, prices, _, at, _, _, _ in quotes
        ],
        # What the Refresh button needs to fetch and read Underdog's lines in the browser.
        # Every league is here because one click refreshes them all, whichever page it's on.
        "underdog": {
            other.KEY: {
                "url": underdog.request_url(other.UNDERDOG_SPORT),
                "sport": other.UNDERDOG_SPORT,
                "stats": other.UNDERDOG_STATS,
                "fixed": getattr(other, "FIXED_LINES", {}),
                "teams": other.TEAM_ALIASES,
            } for other in LEAGUES.values()
        },
    }

    OUT.mkdir(exist_ok=True)
    _write_player_files(league, logs, recent,
                        metrics.teammates(logs, league.lineups(df), index, recent["player_id"].to_list()))
    html = (TEMPLATE.read_text(encoding="utf-8")
            .replace("__NAME__", league.NAME)
            .replace("__DATA__", _dumps(payload).replace("</", "<\\/")))
    out = OUT / league.PAGE
    out.write_text(html, encoding="utf-8")
    viewer_messages = write_viewer(league, recent, offline)

    matched = sum(row["id"] is not None for row in rows[:posted])
    say(f"{posted} Underdog lines ({matched} matched to game logs) and {len(rows) - posted} "
        f"typical lines across {len(games)} games; logs through {payload['lastGame']}")
    if past and not lines:
        say(f"WARNING: none of Underdog's {past} lines are for the {today} slate or later; the board has no Underdog lines")
    if error:
        say(f"Underdog fetch failed ({error}); "
            + (f"used lines cached at {fetched:%Y-%m-%d %H:%M}" if raw else "no lines on the board"))
    for book, prices, missed, at, failed, saved, unmapped in quotes:
        say(f"{book.NAME}: {len(prices)} prices on the board"
            + (f", {missed} props for players not on it" if missed else ""))
        if failed:
            say(f"{book.NAME} fetch failed ({failed}); "
                + (f"used prices cached at {at:%Y-%m-%d %H:%M}" if saved else "no prices on the board"))
        if unmapped:
            say(f"skipped {book.NAME} props for stats not mapped in {book.KEY.upper()}_STATS: {dict(unmapped)}")
    if schedule_error:
        say(f"schedule fetch failed ({schedule_error})")
    if roster_error:
        say(f"roster fetch failed ({roster_error}); using each player's last team instead")
    if unmatched:
        say(f"no game log found for: {', '.join(unmatched)}")
    if skipped:
        say(f"skipped lines for stats the game logs don't carry: {dict(skipped)}")
    for message in viewer_messages:
        say(message)
    say(f"wrote {out}")
    return out
