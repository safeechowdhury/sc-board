"""python -m props grade: how the books' prices and the board's Fair held up, from the snapshots.

Every publish keeps every book's price (snapshots.py) and the game logs have the results, so
grading needs no requests. For each league with game logs it measures:

    books     each book's devigged closing price (its last before the game started, both sides
              posted) against the result at that book's line: log loss and Brier score, lower is
              better. "vs_rest" compares it, on the same props, with Fair made from the other sharp
              books alone: a positive gain means the book knew something the rest didn't.
    fair      the board's Fair at close, at every line a sharp book posted both sides of: its log
              loss, Brier score and calibration (props bucketed by Fair's chance of the over, each
              bucket's average chance against how often the over landed). confident: only the
              props whose Fair the board would shade (books.CONFIDENT).
    targets   for each site to bet on (Underdog, Mise-o-jeu, Betano...), every side the board showed
              as +EV at a publish, graded at its first such publish: the EV it showed, its closing
              line value (that price against Fair at close), how often it won and the return per
              unit staked at that price.

Fair here is worked out as the board works it out (books.devig, log-odds averaged by WEIGHT, a
pair wider than MAX_OVERROUND left out), at the line itself: the board's estimates from an
exchange's other lines, and its cap at a one-way over, are not repeated. A prop whose player
didn't play (no game in his log that counts toward a prop: league.samples) is void, and a result
exactly on the line is a push; both are left out.

The summary goes to data/grades.json, rewritten from all the snapshots each time, and every
graded side to history.html (history.py), a local page that is never uploaded.
"""
import datetime as dt
import json
import math
from collections import defaultdict

import polars as pl

from . import books, history, snapshots, store

OUT = store.DATA / "grades.json"
WEIGHTS = {book.KEY: book.WEIGHT for book in books.BOOKS}
TARGETS = ["underdog"] + [book.KEY for book in books.BOOKS if book.TARGET]
BUCKETS = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0001]
EPS = 1e-6


def _clip(p: float) -> float:
    return min(max(p, EPS), 1 - EPS)


def _logit(p: float) -> float:
    p = _clip(p)
    return math.log(p / (1 - p))


def fair(quotes: dict, leave_out: tuple = ()) -> tuple[float | None, set]:
    """The over's fair chance from {book: (over chance, under chance)} with the vig in, and the
    books behind it: each sharp book posting both sides inside MAX_OVERROUND, devigged, averaged
    in log-odds by weight. leave_out: books not to count (the site EV is measured against, as on
    the board). (None, set()) without one."""
    parts = []
    for book, (over, under) in quotes.items():
        weight = WEIGHTS.get(book, 0)
        if weight <= 0 or book in leave_out or not over or not under:
            continue
        if over + under > 1 + books.MAX_OVERROUND + 1e-9:
            continue
        parts.append((book, weight, books.devig(over, under)[0]))
    if not parts:
        return None, set()
    total = sum(weight for _, weight, _ in parts)
    logit = sum(weight * _logit(p) for _, weight, p in parts) / total
    return 1 / (1 + math.exp(-logit)), {book for book, _, _ in parts}


def confident(used: set) -> bool:
    return any(set(sure) <= used for sure in books.CONFIDENT)


def _loss(p: float, hit: bool) -> tuple[float, float]:
    """(log loss, Brier score) of the chance p of something that did (hit) or didn't happen."""
    p = _clip(p)
    return -math.log(p if hit else 1 - p), (p - hit) ** 2


def _mean(values: list) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def names(league) -> dict:
    """{player id as text: name} from the game logs."""
    df = store.Store(league).load()
    if df.is_empty():
        return {}
    return dict(df.select(pl.col("player_id").cast(pl.String), "name").unique("player_id", keep="last").iter_rows())


def results(league) -> dict:
    """{(game date, player id as text, stat): value} for every game that counts toward a prop."""
    df = store.Store(league).load()
    if df.is_empty():
        return {}
    fixed = getattr(league, "FIXED_LINES", {})
    df = league.samples(df.with_columns(pl.col(column).alias(stat) for stat, (column, _) in fixed.items()))
    stats = [stat for stat in league.PROPS if stat in df.columns]
    out = {}
    for row in df.select("date", pl.col("player_id").cast(pl.String), *stats).iter_rows(named=True):
        for stat in stats:
            if row[stat] is not None:
                out[(str(row["date"]), row["player_id"], stat)] = row[stat]
    return out


def _quotes(prices: pl.DataFrame) -> dict:
    """{(game, player, stat): {line: {book: (over, under)}}} from snapshot records, chances as fractions."""
    out: dict = defaultdict(lambda: defaultdict(dict))
    for game, player, stat, book, line, over, under in prices.select(
            "game", "player_id", "stat", "book", "line", "over", "under").iter_rows():
        out[(game, player, stat)][line][book] = (over / 10000 if over else None, under / 10000 if under else None)
    return out


def _closes(records: pl.DataFrame) -> pl.DataFrame:
    """Each price's last record before its game started; a price taken down by then has none."""
    return (records.filter(pl.col("seen_at") < pl.col("start")).sort("seen_at")
            .group_by(snapshots.KEY, maintain_order=True).last()
            .filter(pl.col("line").is_not_null()))


def grade_league(league) -> dict | None:
    records = snapshots.load(league)
    outcome = results(league)
    if records.is_empty() or not outcome:
        return None
    started = records.filter(pl.col("start") <= dt.datetime.now(dt.timezone.utc))
    close = _quotes(_closes(started))

    def landed(prop, line):
        """True if the over landed, False if the under did, None if void or a push."""
        game, player, stat = prop
        value = outcome.get((game[:10], player, stat))
        if value is None or value == line:
            return None
        return value > line

    book_scores: dict = defaultdict(lambda: {"log_loss": [], "brier": [], "gain": []})
    fair_scores = {"all": {"log_loss": [], "brier": [], "cal": []}, "confident": {"log_loss": [], "brier": [], "cal": []}}
    for prop, lines in close.items():
        for line, quotes in lines.items():
            hit = landed(prop, line)
            if hit is None:
                continue
            p, used = fair(quotes)
            if p is not None:
                for kind in ("all", "confident") if confident(used) else ("all",):
                    ll, brier = _loss(p, hit)
                    fair_scores[kind]["log_loss"].append(ll)
                    fair_scores[kind]["brier"].append(brier)
                    fair_scores[kind]["cal"].append((p, hit))
            for book, (over, under) in quotes.items():
                if not over or not under or over + under > 1 + books.MAX_OVERROUND + 1e-9:
                    continue
                mine = books.devig(over, under)[0]
                ll, brier = _loss(mine, hit)
                book_scores[book]["log_loss"].append(ll)
                book_scores[book]["brier"].append(brier)
                rest, _ = fair(quotes, leave_out=(book,))
                if rest is not None:
                    book_scores[book]["gain"].append(_loss(rest, hit)[0] - ll)

    def calibration(pairs):
        out = []
        for lo, hi in zip(BUCKETS, BUCKETS[1:]):
            inside = [(p, hit) for p, hit in pairs if lo <= p < hi]
            if inside:
                out.append({"from": lo, "to": min(hi, 1.0), "n": len(inside),
                            "predicted": _mean([p for p, _ in inside]), "landed": _mean([float(h) for _, h in inside])})
        return out

    targets, picks = _targets(league, records, close, landed)
    return {
        "graded_through": str(started["start"].max()) if started.height else None,
        "books": {book: {"n": len(s["log_loss"]), "log_loss": _mean(s["log_loss"]), "brier": _mean(s["brier"]),
                         "vs_rest": {"n": len(s["gain"]), "log_loss_gain": _mean(s["gain"])}}
                  for book, s in sorted(book_scores.items())},
        "fair": {kind: {"n": len(s["log_loss"]), "log_loss": _mean(s["log_loss"]), "brier": _mean(s["brier"]),
                        "calibration": calibration(s["cal"])} for kind, s in fair_scores.items()},
        "targets": targets,
        "picks": picks,
    }


def _targets(league, records: pl.DataFrame, close: dict, landed) -> tuple[dict, list]:
    """Every side each betting site offered that the board showed as +EV (Fair confident, at the
    site's own line), graded at the first publish that showed it. Also every graded side at any EV,
    for the history page: one record a side, at its first +EV publish if it ever had one, else at
    the first publish that priced it with a confident Fair."""
    seen = sorted(snapshots.publishes(league)["seen_at"].unique().to_list())
    first: dict = {}   # (target, prop, line, side) -> (odds, ev then)
    sighted: dict = {}   # the same, at any EV: (odds, ev, fair chance of the side, when)
    for when in seen:
        state = snapshots.as_of(records, when).filter(pl.col("start") > when)
        for prop, lines in _quotes(state).items():
            for line, quotes in lines.items():
                for target in TARGETS:
                    over, under = quotes.get(target, (None, None))
                    if not over and not under:
                        continue
                    p, used = fair(quotes, leave_out=(target,))   # a site is never measured against itself
                    if p is None or not confident(used):
                        continue
                    for side, chance, fair_side in (("over", over, p), ("under", under, 1 - p)):
                        key = (target, prop, line, side)
                        if chance and key not in first:
                            odds = 1 / chance
                            ev = fair_side * odds - 1
                            if ev > 0:
                                first[key] = (odds, ev)
                            if ev > 0 or key not in sighted:
                                sighted[key] = (odds, ev, fair_side, when)
    out: dict = defaultdict(lambda: {"ev": [], "clv": [], "won": [], "profit": []})
    for (target, prop, line, side), (odds, ev) in first.items():
        hit = landed(prop, line)
        if hit is None:
            continue
        won = hit if side == "over" else not hit
        s = out[target]
        s["ev"].append(ev)
        s["won"].append(float(won))
        s["profit"].append(odds - 1 if won else -1.0)
        p_close, _ = fair(close.get(prop, {}).get(line, {}), leave_out=(target,))
        if p_close is not None:
            s["clv"].append((p_close if side == "over" else 1 - p_close) * odds - 1)
    picks = []
    who = names(league)
    for (target, prop, line, side), (odds, ev, fair_side, when) in sighted.items():
        hit = landed(prop, line)
        if hit is None:
            continue
        game, player, stat = prop
        p_close, _ = fair(close.get(prop, {}).get(line, {}), leave_out=(target,))
        picks.append({"league": league.KEY, "site": target, "game": game, "player": who.get(player, player),
                      "stat": league.PROPS.get(stat, (stat,))[0], "line": line, "side": side,
                      "odds": round(odds, 3), "ev": round(ev, 4), "fair": round(fair_side, 4),
                      "won": int(hit if side == "over" else not hit), "seen": when.isoformat(timespec="minutes"),
                      "clv": None if p_close is None else round((p_close if side == "over" else 1 - p_close) * odds - 1, 4)})
    return ({target: {"picks": len(s["ev"]), "ev_shown": _mean(s["ev"]), "clv": _mean(s["clv"]),
                      "clv_n": len(s["clv"]), "won": _mean(s["won"]), "roi": _mean(s["profit"])}
             for target, s in sorted(out.items())}, picks)


def grade(leagues: dict) -> dict:
    """Grade every league with game logs and write data/grades.json."""
    summary = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"), "leagues": {}}
    picks: list = []
    for key, league in leagues.items():
        if getattr(league, "ODDS_ONLY", False):
            continue   # no game logs to grade against
        found = grade_league(league)
        if found:
            picks += found.pop("picks")
            summary["leagues"][key] = found
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(summary, indent=1) + "\n")
    history.write(picks, summary["generated"])
    return summary


def report(summary: dict) -> list[str]:
    """A few lines a league for the terminal."""
    lines = []
    fmt = lambda v, pct=False: "-" if v is None else (f"{v:+.1%}" if pct else f"{v:.4f}")
    for key, g in summary["leagues"].items():
        fair_all, sure = g["fair"]["all"], g["fair"]["confident"]
        lines.append(f"{key}: graded through {g['graded_through']}; Fair log loss {fmt(fair_all['log_loss'])} "
                     f"over {fair_all['n']} props ({fmt(sure['log_loss'])} over the {sure['n']} confident)")
        ranked = sorted(g["books"].items(), key=lambda kv: -(kv[1]["vs_rest"]["log_loss_gain"] or -9))
        lines.append("  books, log loss (n) and gain over the rest: " + ", ".join(
            f"{book} {fmt(s['log_loss'])} ({s['n']}) {s['vs_rest']['log_loss_gain'] or 0:+.4f}" for book, s in ranked))
        for target, s in g["targets"].items():
            lines.append(f"  {target}: {s['picks']} +EV sides, EV shown {fmt(s['ev_shown'], True)}, "
                         f"CLV {fmt(s['clv'], True)}, won {fmt(s['won'], True).lstrip('+')}, ROI {fmt(s['roi'], True)}")
    return lines
