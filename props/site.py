"""Assemble a league's board and write its page.

    site/index.html, site/nfl.html, site/mlb.html   one board per league; all of its data is embedded, so it opens from disk
    site/p/<league>/<id>.js          one player's full game log and teammates, loaded when their page is opened
    site/ud.html                     every Underdog line in every sport, fetched live by the page
    site/ud/<league>.js              which of a league's Underdog lines the board covers, so the viewer can link to them
"""
import datetime as dt
import hashlib
import json
import threading
import types
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import httpx
import polars as pl

from . import books, kambi, market, matchups, metrics, net, snapshots, store, underdog
from .leagues import LEAGUES
from .slate import ET, board_date, slate_date

OUT = store.ROOT / "site"
CACHE = store.DATA / "cache"
TEMPLATE = Path(__file__).with_name("template.html")
VIEWER = Path(__file__).with_name("ud.html")
VIEWER_PAGE = "ud.html"
SPORTS_KEPT = 30 * 60   # seconds the viewer's sport menu is used again before it is fetched anew


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
    return [[league.NAME, league.PAGE] for league in LEAGUES.values()]   # the Underdog lines viewer is an app of its own


_sports: list = []   # [(the viewer's sport list, messages)], fetched once however many leagues build
_viewer_lock = threading.Lock()   # leagues building at once share the list and the viewer page


def _sports_list(offline: bool) -> tuple[list, list[str]]:
    """The viewer's sport list, fetched once however many leagues build, and what went wrong
    fetching it (told once)."""
    with _viewer_lock:
        if not _sports:
            # the menu changes slowly and is Underdog's biggest answer (every sport's lines, ~17 MB
            # zipped): one fetched in the last half hour is used again; the viewer's lines are live
            kept = CACHE / "underdog-sports.json"
            recent = kept.exists() and dt.datetime.now().timestamp() - kept.stat().st_mtime < SPORTS_KEPT
            found, at, error = _fetch_cached("underdog-sports", underdog.sports, offline or recent)
            _sports.append((found or [], [f"Underdog sports list fetch failed ({error})"
                                          + (f"; used the list cached at {at:%Y-%m-%d %H:%M}" if found else "")]
                            if error else []))
        sports, messages = _sports[0]
        told, messages[:] = list(messages), []
        return sports, told


def write_viewer(league, players: pl.DataFrame, offline: bool) -> list[str]:
    """Write the Underdog lines viewer and this league's link file. Returns messages for the build log.

    The viewer fetches lines in the browser, so the page holds only what it can't get there:
    the sports Underdog has lines up for, and, per league, the stats the board carries and
    each player's id, so a line the board covers opens the player's page at that line."""
    sports, messages = _sports_list(offline)
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
    data = {"nav": _nav(), "url": underdog.request_url(""), "sports": sports,
            "leagues": [other.KEY for other in LEAGUES.values()]}
    with _viewer_lock:
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


def _utcnow() -> dt.datetime:
    return net.now()


def _started(start: str, now: dt.datetime) -> bool:
    return dt.datetime.fromisoformat(start.replace("Z", "+00:00")) <= now


def _last_start(games: list[dict], default: dt.datetime) -> dt.datetime:
    return max((dt.datetime.fromisoformat(g["start"].replace("Z", "+00:00")) for g in games), default=default)


def _board_games(book, league, until: dt.datetime, board_last: Future) -> dict:
    """book.fetch for the board's games: those starting by `until` (the schedule's slate) at once,
    then, once the board's last game is known, any later one Underdog put on it."""
    answer = book.fetch(league, until=until)
    last = board_last.result()
    if last > until:
        more = book.fetch(league, now=until + dt.timedelta(seconds=1), until=last)
        answer = {key: (answer.get(key) or []) + (more.get(key) or []) for key in answer.keys() | more.keys()}
    return answer


def _games(schedule: list[dict], lines: list[dict], today: dt.date, days: int,
           now: dt.datetime | None = None) -> list[dict]:
    """The slate's games (the next `days` days) plus any later game that already has Underdog lines.
    A game under way or over is left off: its props can no longer be bet."""
    now = now or _utcnow()
    counts = Counter(line["game"] for line in lines)
    last = str(today + dt.timedelta(days=days - 1))
    games, gone = {}, set()
    for g in schedule:
        key = f"{g['date']}|{g['away']}@{g['home']}"
        if g["state"] != "upcoming" or _started(g["start"], now):
            gone.add(key)
            continue
        if str(today) <= g["date"] <= last or key in counts:
            games[key] = {"key": key, "date": g["date"], "away": g["away"], "home": g["home"],
                          "start": g["start"], "state": g["state"], "lines": counts[key]}
    for line in lines:  # a game Underdog lists that the schedule fetch did not return
        if line["game"] not in games and line["game"] not in gone:
            date, matchup = line["game"].split("|")
            away, home = matchup.split("@")
            games[line["game"]] = {"key": line["game"], "date": date, "away": away, "home": home,
                                   "start": line["start"], "state": "upcoming", "lines": counts[line["game"]]}
    return sorted(games.values(), key=lambda g: g["start"])


def _player_files_key(league, box: Path, players: list) -> str:
    """What the player files are made from: the game logs (each file by size and time), who gets
    one, and the code that reads the logs and writes the files."""
    made_from = hashlib.blake2b(digest_size=16)
    for path in sorted(box.rglob("*.parquet")) if box.exists() else []:
        stat = path.stat()
        made_from.update(f"{path.relative_to(box)}|{stat.st_size}|{stat.st_mtime_ns}\n".encode())
    made_from.update(repr(sorted(players)).encode())
    for code in (__file__, metrics.__file__, store.__file__, league.__file__):
        made_from.update(Path(code).read_bytes())
    return made_from.hexdigest()


def _write_player_files(league, logs: pl.DataFrame, players: pl.DataFrame, mates: dict) -> None:
    folder = OUT / "p" / league.KEY
    folder.mkdir(parents=True, exist_ok=True)
    stale = {path.name for path in folder.glob("*.js")}   # whoever isn't written again is gone
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
        path, text = folder / f"{pid}.js", f"SCBOARD.player({_dumps(data)})"
        stale.discard(path.name)
        # only what changed is written, so an unchanged file keeps its date and the upload can skip it
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
    for name in stale:
        (folder / name).unlink()


def _books_payload(quotes: list) -> list[dict]:
    """Each book's [line, over, under] in decimal odds, keyed "<player id or name>|<stat>"."""
    return [{"key": book.KEY, "name": book.NAME, "short": book.SHORT, "sharp": book.WEIGHT > 0, "weight": book.WEIGHT,
             "target": book.TARGET, "icon": getattr(book, "ICON", None), "prices": prices,
             "fetched": at.isoformat(timespec="minutes") if at and prices else None}
            for book, prices, _, at, _, _, _ in quotes]


def _fair_payload(keys: set[str]) -> dict:
    """How the books' prices make the fair chance (books.py), among the books on the board: a
    confident set with a book the league doesn't have can never be met."""
    return {"maxOverround": books.MAX_OVERROUND,
            "confident": [list(sure) for sure in books.CONFIDENT if set(sure) <= keys],
            "estimate": [key for key in books.ESTIMATE if key in keys]}


def _underdog_payload() -> dict:
    """What the Refresh button needs to fetch and read Underdog's lines in the browser. Every
    league is here because one click refreshes them all, whichever page it's on."""
    return {other.KEY: {"url": underdog.request_url(other.UNDERDOG_SPORT), "sport": other.UNDERDOG_SPORT,
                        "stats": other.UNDERDOG_STATS, "fixed": getattr(other, "FIXED_LINES", {}),
                        "teams": other.TEAM_ALIASES} for other in LEAGUES.values()}


def _report(say, quotes: list) -> None:
    for book, prices, missed, at, failed, saved, unmapped in quotes:
        say(f"{book.NAME}: {len(prices)} prices on the board"
            + (f", {missed} props for players not on it" if missed else ""))
        if failed:
            say(f"{book.NAME} fetch failed ({failed}); "
                + (f"used prices cached at {at:%Y-%m-%d %H:%M}" if saved else "no prices on the board"))
        if unmapped:
            say(f"skipped {book.NAME} props for stats not mapped in {book.KEY.upper()}_STATS: {dict(unmapped)}")


NEAR_HOURS = 48   # an odds board's books asked a request per game ask for the games this close (and Underdog's)


def _when(start: str) -> dt.datetime:
    return dt.datetime.fromisoformat(start.replace("Z", "+00:00"))


def _listings(league) -> list:
    """[(competition, Kambi's games)] for each of the league's competitions Kambi lists. A competition
    whose listing fails is left out: its games still come from Underdog's lines."""
    out = []
    for part in books.competitions(league):
        if getattr(part, "KAMBI_PATH", None):
            try:
                out.append((getattr(part, "COMPETITION", None), kambi.games(part)))
            except (httpx.HTTPError, ValueError):
                pass
    if not out and any(getattr(part, "KAMBI_PATH", None) for part in books.competitions(league)):
        raise ValueError("no Kambi listing answered")
    return out


def _underdog_games(lines: list[dict]) -> list[dict]:
    """Underdog's games from its lines, each with its teams' full names where Underdog gives them."""
    games = {}
    for line in lines:
        date, codes = line["game"].split("|")
        away, home = codes.split("@")
        full = underdog.matchup(line.get("matchup") or "")
        if full:
            away, home = full
        g = games.setdefault(line["game"], {"key": line["game"], "date": date, "start": line["start"], "away": away,
                                            "home": home, "words": matchups.words(line.get("matchup") or codes),
                                            "lines": 0})
        g["lines"] += 1
    return list(games.values())


def _game_words(games: list[dict]) -> list:
    """The games as matchups.on_board reads them: each one's start and its teams' words."""
    return [(_when(g["start"]), matchups.words(f"{g['away']} {g['home']}")) for g in games]


def _odds_board(league, offline: bool, record: bool) -> Path:
    """A board with no game logs behind it (college football, soccer): Underdog's lines, each
    book's prices at them, the fair chance and EV, and no hit rates. Its rows are keyed by the
    player's name, and the books asked game by game ask only for its games (matchups.py)."""
    def say(message: str) -> None:
        print(f"build {league.KEY}: {message}")

    now, today = _utcnow(), slate_date()
    raw, fetched, error = _fetch_cached(f"{league.KEY}-underdog", lambda: underdog.fetch(league.UNDERDOG_SPORT), offline)
    lines, skipped = underdog.parse(raw, league) if raw else ([], Counter())
    past = len(lines)
    lines = [line for line in lines if _slate_date(line["start"]) >= today and not _started(line["start"], now)]
    past -= len(lines)
    for line in lines:
        away, home = (line["opponent"], line["team"]) if line["home"] else (line["team"], line["opponent"])
        line["game"] = f"{_slate_date(line['start'])}|{away}@{home}"
    # the games: every one Kambi lists on the slate, Underdog's matched onto them, then any it doesn't list
    listing, _, listing_error = _fetch_cached(f"{league.KEY}-games", lambda: _listings(league), offline)
    games = market.merge_games(market.listed(listing or [], today, league.SLATE_DAYS, now, _slate_date),
                               _underdog_games(lines))
    games = [g for g in games if not _started(g["start"], now)]

    # the books asked a request per game ask for the next NEAR_HOURS' games and Underdog's; the rest for all
    near = [g for g in games if g["lines"] or _when(g["start"]) <= now + dt.timedelta(hours=NEAR_HOURS)]
    every = types.SimpleNamespace(**{name: getattr(league, name) for name in dir(league) if name.isupper()},
                                  BOARD_GAMES=_game_words(games))
    nearby = types.SimpleNamespace(**{**vars(every), "BOARD_GAMES": _game_words(near)})
    carried = [book for book in books.BOOKS if books.carries(book, league)]
    asking = {}
    if games:   # no games: nothing to ask the books for
        with ThreadPoolExecutor(max_workers=len(carried)) as pool:
            def ask(book):
                board = nearby if getattr(book, "PER_GAME", False) else every
                last = _last_start(near if board is nearby else games, now)
                return books.fetch(book, board, **({"until": last} if getattr(book, "BOARD_GAMES_ONLY", False) else {}))
            asking = {book: pool.submit(_fetch_cached, f"{league.KEY}-{book.KEY}", lambda book=book: ask(book), offline)
                      for book in carried}
    rows = [{"id": None, "name": line["player"], "team": line["team"], "opp": line["opponent"], "home": line["home"],
             "pos": line["position"] or "", "start": line["start"], "game": line["game"], "stat": line["stat"],
             "line": line["line"], "ud": True, "over": line["over"], "under": line["under"]} for line in lines]
    answers, partial = [], []
    for book in carried:
        answer, at, failed = asking[book].result() if book in asking else (None, None, None)
        props, unmapped = book.parse(answer, league) if answer else ([], Counter())
        partial += [f"{book.NAME} {message}" for message in (answer or {}).get("errors", [])]
        answers.append((book, books.with_fixed(props, league), at, failed, answer is not None, unmapped))
    # a row at the market's line for every player and stat two books price that Underdog doesn't
    find = market.GameFinder(games)
    market.short_names(games, [(book.KEY, props) for book, props, *_ in answers], find)
    names = market.aliases(rows, [props for _, props, *_ in answers], find)
    answers = [(book, market.renamed(props, names), *rest) for book, props, *rest in answers]
    rows += market.rows(rows, [(book.KEY, props) for book, props, *_ in answers], find,
                        {book.KEY for book in carried if book.WEIGHT > 0}, set(league.PROPS))
    quotes = [(book, *books.prices(props, rows, getattr(league, "ANYTIME_STATS", ())), at, failed, saved, unmapped)
              for book, props, at, failed, saved, unmapped in answers]
    # a market row two books didn't both price in the end (a name another row shares) is left off
    priced = Counter(key for _, prices, *_ in quotes for key in prices)
    dropped = {f"{row['name']}|{row['stat']}" for row in rows if not row["ud"]
               and priced[f"{row['name']}|{row['stat']}"] < market.MIN_BOOKS}
    rows = [row for row in rows if row["ud"] or f"{row['name']}|{row['stat']}" not in dropped]
    quotes = [(book, {key: price for key, price in prices.items() if key not in dropped}, *rest)
              for book, prices, *rest in quotes]

    standard = Counter(offer["odds"] for row in rows for offer in (row["over"], row["under"])
                       if offer and offer["mult"] == 1 and offer["odds"])
    payload = {
        "league": league.KEY, "name": league.NAME, "nav": _nav(), "oddsOnly": True,
        "generated": dt.datetime.now(ET).isoformat(timespec="minutes"), "date": str(today),
        "season": league.season_id(today), "seasonLabel": league.season_label(league.season_id(today)),
        "prevSeason": None, "prevSeasonLabel": "", "lastGame": None,
        "props": [[key, label, short, league.TABS[key]] for key, (label, short) in league.PROPS.items()],
        "labels": [], "clock": [], "positions": {}, "kinds": {}, "groups": {}, "teams": 0, "rest": False,
        "typicalGames": 0, "games": games, "rows": rows, "recent": {}, "recentPo": {},
        "comps": [part.get("COMPETITION") for part in getattr(league, "COMPETITIONS", [])],
        "standardOdds": standard.most_common(1)[0][0] if standard else None, "udIcon": underdog.ICON,
        "players": [], "dvp": {},
        "lines": {"fetched": fetched.isoformat(timespec="minutes") if fetched else None, "error": error},
        "fair": _fair_payload({book.KEY for book in carried}),
        "books": _books_payload(quotes),
        "underdog": _underdog_payload(),
    }
    OUT.mkdir(exist_ok=True)
    out = OUT / league.PAGE
    out.write_text(TEMPLATE.read_text(encoding="utf-8").replace("__NAME__", league.NAME)
                   .replace("__DATA__", _dumps(payload).replace("</", "<\\/")), encoding="utf-8")
    viewer_messages = write_viewer(league, pl.DataFrame(schema={"player_id": pl.String, "name": pl.String}), offline)

    ud = sum(row["ud"] for row in rows)
    say(f"{ud} Underdog lines and {len(rows) - ud} market lines across {len(games)} games "
        f"({len(near)} asked of every book; an odds board: no game logs)")
    if listing_error:
        say(f"game listing fetch failed ({listing_error}); " + ("used the one cached" if listing else "games from Underdog only"))
    if past and not lines:
        say(f"WARNING: none of Underdog's {past} lines are for a game yet to start on the {today} slate or later")
    if error:
        say(f"Underdog fetch failed ({error}); "
            + (f"used lines cached at {fetched:%Y-%m-%d %H:%M}" if raw else "no lines on the board"))
    _report(say, quotes)
    for message in partial:
        say(f"fetch failed for {message}")
    if skipped:
        say(f"skipped Underdog lines for stats the board doesn't carry: {dict(skipped)}")
    for message in viewer_messages:
        say(message)
    if record:
        kept = snapshots.record(league, rows, [{"key": book.KEY, "prices": prices, "fetched": not failed}
                                               for book, prices, _, _, failed, _, _ in quotes],
                                now, underdog=not error)
        say(f"snapshot: {kept} prices new or moved since the last publish")
    say(f"wrote {out}")
    return out


def build(league, offline: bool = False, record: bool = False) -> Path:
    """Fetch the league's lines and prices and write its board. record: also keep this run's
    prices in the league's snapshots (publish does, for grading)."""
    if getattr(league, "ODDS_ONLY", False):
        return _odds_board(league, offline, record)
    games_store = store.Store(league)
    df = games_store.load()
    if df.is_empty():
        raise SystemExit(f"No {league.NAME} game logs in {games_store.box}. "
                         f"Run: python -m props backfill {league.KEY} <season> ...")
    fixed = getattr(league, "FIXED_LINES", {})
    df = df.with_columns(pl.col(column).alias(stat) for stat, (column, _) in fixed.items())

    def say(message: str) -> None:
        print(f"build {league.KEY}: {message}")

    # every source at once: the books and Underdog don't wait on the schedule or on each other
    # (net.LIMITS keeps each site to its own pace however many leagues are building)
    pool = ThreadPoolExecutor(max_workers=len(books.BOOKS) + 2)
    asking = {book: pool.submit(_fetch_cached, f"{league.KEY}-{book.KEY}", lambda book=book: book.fetch(league), offline)
              for book in books.BOOKS if not getattr(book, "BOARD_GAMES_ONLY", False)}
    asking_underdog = pool.submit(_fetch_cached, f"{league.KEY}-underdog",
                                  lambda: underdog.fetch(league.UNDERDOG_SPORT), offline)
    viewer_sports = pool.submit(_sports_list, offline)   # the viewer's list: the biggest single answer, so asked first

    # the schedule from the clock's slate, which says whether last night's games are all final
    clock = slate_date()
    schedule, _, schedule_error = _fetch_cached(f"{league.KEY}-schedule", lambda: league.schedule(clock), offline)
    today = board_date(schedule or [])
    if today != clock:
        schedule, _, schedule_error = _fetch_cached(f"{league.KEY}-schedule", lambda: league.schedule(today), offline)

    # the books asked game by game, for the board's games only: those on the schedule's slate at
    # once, and any later game Underdog has lines for (so on the board too) once they're read
    now = _utcnow()
    slate_last = _last_start(_games(schedule or [], [], today, league.SLATE_DAYS, now), now)
    board_last = Future()
    for book in books.BOOKS:
        if book not in asking:
            asking[book] = pool.submit(_fetch_cached, f"{league.KEY}-{book.KEY}",
                                       lambda book=book: _board_games(book, league, slate_last, board_last), offline)
    pool.shutdown(wait=False)

    try:
        season = league.season_id(today)
        previous = league.previous_season(season)
        if league.REST:
            df = metrics.with_rest(df)
        logs = league.samples(df).sort("date", "game_id", descending=True)
        index = _player_index(league, df)

        raw, fetched, error = asking_underdog.result()
        lines, skipped = underdog.parse(raw, league) if raw else ([], Counter())
        past = len(lines)
        lines = [line for line in lines if _slate_date(line["start"]) >= today and not _started(line["start"], now)]
        past -= len(lines)
        for line in lines:
            away, home = (line["opponent"], line["team"]) if line["home"] else (line["team"], line["opponent"])
            line["game"] = f"{_slate_date(line['start'])}|{away}@{home}"
        unmatched = underdog.match_players(lines, index)
        games = _games(schedule or [], lines, today, league.SLATE_DAYS, now)
        board_last.set_result(_last_start(games, slate_last))
    finally:   # never leave those fetches waiting, whatever went wrong
        if not board_last.done():
            board_last.set_result(slate_last)
    teams = sorted({team for g in games for team in (g["away"], g["home"])})
    rosters, _, roster_error = _fetch_cached(f"{league.KEY}-rosters", lambda: league.rosters(teams, today), offline)

    groups = {position: group for position, (_, _, group) in league.POSITIONS.items()}
    dvp = metrics.dvp(df, season, groups, _dvp_stats(league))
    fetched_books = []   # per book: (book, props, fetched at, error, saved copy?, unmapped stats)
    for book in books.BOOKS:
        answer, at, failed = asking[book].result()
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
        quotes.append((book, *books.prices(props, rows, getattr(league, "ANYTIME_STATS", ())), at, failed, saved, unmapped))

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
        "udIcon": underdog.ICON,   # Underdog's icon, for the EV toggle
        "players": recent.select("player_id", "name", "team", "position", "mug").rows(),
        "dvp": dvp_table,
        "lines": {
            "fetched": fetched.isoformat(timespec="minutes") if fetched else None,
            "error": error,
        },
        "fair": _fair_payload({book.KEY for book in books.BOOKS}),
        "books": _books_payload(quotes),
        "underdog": _underdog_payload(),
    }

    OUT.mkdir(exist_ok=True)
    # the player files come from the game logs alone: unchanged logs (and players, and code) mean unchanged files
    made_from = _player_files_key(league, games_store.box, recent["player_id"].to_list())
    made = CACHE / f"{league.KEY}-players.key"
    if not (made.exists() and made.read_text() == made_from and (OUT / "p" / league.KEY).is_dir()):
        _write_player_files(league, logs, recent,
                            metrics.teammates(logs, league.lineups(df), index, recent["player_id"].to_list()))
        made.parent.mkdir(parents=True, exist_ok=True)
        made.write_text(made_from)
    html = (TEMPLATE.read_text(encoding="utf-8")
            .replace("__NAME__", league.NAME)
            .replace("__DATA__", _dumps(payload).replace("</", "<\\/")))
    out = OUT / league.PAGE
    out.write_text(html, encoding="utf-8")
    viewer_messages = viewer_sports.result()[1] + write_viewer(league, recent, offline)

    matched = sum(row["id"] is not None for row in rows[:posted])
    say(f"{posted} Underdog lines ({matched} matched to game logs) and {len(rows) - posted} "
        f"typical lines across {len(games)} games; logs through {payload['lastGame']}")
    if past and not lines:
        say(f"WARNING: none of Underdog's {past} lines are for a game yet to start on the {today} slate or later; "
            "the board has no Underdog lines")
    if error:
        say(f"Underdog fetch failed ({error}); "
            + (f"used lines cached at {fetched:%Y-%m-%d %H:%M}" if raw else "no lines on the board"))
    _report(say, quotes)
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
    if record:
        kept = snapshots.record(league, rows, [{"key": book.KEY, "prices": prices, "fetched": not failed}
                                               for book, prices, _, _, failed, _, _ in quotes],
                                now, underdog=not error)
        say(f"snapshot: {kept} prices new or moved since the last publish")
    say(f"wrote {out}")
    return out
