"""What every fetch in a run shares: one clock, one answer per request, and a polite cap per site.

The props boards and the lines page ask several books the same questions (Pinnacle's
league feeds, Bovada's game pages, Betano's league page, Mise-o-jeu's games). Inside a
`run()`, a request already answered is answered again from memory, so each is made once
however many pages need it, and every page sees the same "now", so their windows (and
so their requests) line up. Outside a run nothing is remembered: each call fetches.

Each site also gets a cap on requests in flight at once, and some a gap between them,
whichever page or league is asking, so building everything at once never hits a site
harder than building one league did.

And an answer a site says stays fresh (its Cache-Control max-age, less the Age its CDN has
held it) is kept on disk that long: a refresh inside that window gets the same answer the
site's CDN would have given, without asking. Pinnacle's feeds keep 15 minutes, Bovada's
pages 10. Outside a run nothing is kept or reused.
"""
import contextlib
import datetime as dt
import hashlib
import json
import re
import threading
import time
from concurrent.futures import Future

from . import store

# requests in flight at once per site, and the seconds to leave between them
LIMITS = {
    "bovada": (1, 1.0),        # refuses even three at once; answers a burst with 429
    "betano": (6, 0.0),        # as many as one league's fetch had at once before
    "betway": (1, 1.0),        # a league page a request, one at a time
    "draftkings": (3, 0.0),    # refuses a burst
    "fanduel": (6, 0.0),
    "kalshi": (2, 0.25),       # answered soccer's extra series, asked back to back, with 429
    "kambi": (4, 0.0),
    "mise": (3, 0.0),
    "novig": (3, 0.0),
    "pinnacle": (2, 0.0),
    "polymarket": (2, 0.25),   # a new site: two at a time, a pause between
    "stake": (6, 0.0),         # as many as one league's fetch had at once before
    "pick6": (3, 0.25),        # draftkings' little brother answers a burst like its big one
    "underdog": (2, 0.0),
}

FRESH = store.DATA / "cache" / "http"   # answers still fresh by their site's own word
MIN_FRESH = 5   # seconds: an answer fresh for less is not worth keeping

_lock = threading.Lock()
_run: dict | None = None
_sites: dict[str, tuple[threading.Semaphore, list]] = {}
asked: dict[str, int] = {}   # requests made per site, for the run's report


@contextlib.contextmanager
def run():
    """A refresh: one clock and one answer per request until it ends."""
    global _run
    with _lock:
        outer, _run = _run, {"now": dt.datetime.now(dt.timezone.utc), "answers": {}, "close": []}
    for path in FRESH.glob("*.json") if FRESH.exists() else []:   # clear out what has gone stale
        try:
            if json.loads(path.read_text())["until"] <= time.time():
                path.unlink()
        except (OSError, ValueError, KeyError):
            path.unlink(missing_ok=True)
    try:
        yield
    finally:
        with _lock:
            closing, _run = _run["close"], outer
        for close in closing:
            close()


def now() -> dt.datetime:
    """The run's clock, or the time if there is no run."""
    current = _run
    return current["now"] if current else dt.datetime.now(dt.timezone.utc)


def once(key, fetch):
    """fetch(), or within a run the answer it already gave for `key`. Callers asking for the
    same key at once wait for the first; a failure is shared too, not retried."""
    current = _run
    if current is None:
        return fetch()
    with _lock:
        future = current["answers"].get(key)
        mine = future is None
        if mine:
            future = current["answers"][key] = Future()
    if mine:
        try:
            future.set_result(fetch())
        except BaseException as e:
            future.set_exception(e)
    return future.result()


def once_each(keys: list, fetch) -> dict:
    """once() for several keys answered together: fetch(keys) -> {key: answer}, called with only the
    keys not yet asked for in the run, so a site that answers many games a request (Kambi, Mise-o-jeu)
    is asked for each game once however the pages wanting them batch them. A key fetch leaves out
    is answered None."""
    current = _run
    if current is None:
        found = fetch(list(keys))
        return {key: found.get(key) for key in keys}
    with _lock:
        mine = [key for key in dict.fromkeys(keys) if key not in current["answers"]]
        for key in mine:
            current["answers"][key] = Future()
    if mine:
        try:
            found = fetch(mine)
        except BaseException as e:
            for key in mine:
                current["answers"][key].set_exception(e)
        else:
            for key in mine:
                current["answers"][key].set_result(found.get(key))
    return {key: current["answers"][key].result() for key in keys}


@contextlib.contextmanager
def shared(key, make):
    """A client made once a run and shared by every fetch in it (so its cookies are set once),
    closed when the run ends; outside a run, made for this use and closed after it."""
    current = _run
    if current is None:
        client = make()
        try:
            yield client
        finally:
            client.close()
        return

    def keep():
        client = make()
        with _lock:
            current["close"].append(client.close)
        return client
    yield once(key, keep)


@contextlib.contextmanager
def site(name: str):
    """Hold one of the site's request slots for the length of a request, starting it no sooner
    than the site's gap after the last one ended."""
    limit, gap = LIMITS.get(name, (4, 0.0))
    with _lock:
        slots, ended = _sites.setdefault(name, (threading.Semaphore(limit), [0.0]))
    with slots:
        wait = ended[0] + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            yield
        finally:
            ended[0] = time.monotonic()
            with _lock:
                asked[name] = asked.get(name, 0) + 1


_MAX_AGE = re.compile(r"(?:^|[,\s])max-age=(\d+)")


def _fresh_for(headers) -> float:
    """Seconds the answer stays fresh, by its Cache-Control max-age less its Age; 0 if it says nothing
    or forbids keeping it."""
    control = (headers or {}).get("cache-control") or ""
    found = _MAX_AGE.search(control)
    if not found or re.search(r"no-store|no-cache|private", control):
        return 0.0
    try:
        age = float((headers or {}).get("age") or 0)
    except ValueError:
        age = 0.0
    return int(found.group(1)) - age


def fresh(key, ask):
    """ask() -> (answer, headers). Within a run: the answer kept from an earlier run while its site
    still calls it fresh, or ask() and keep its answer as long as the site says it stays fresh.
    Outside a run: ask()'s answer."""
    if _run is None:
        return ask()[0]
    path = FRESH / f"{hashlib.blake2b(repr(key).encode(), digest_size=16).hexdigest()}.json"
    try:
        kept = json.loads(path.read_text())
        if kept["until"] > time.time():
            return kept["answer"]
    except (OSError, ValueError, KeyError):
        pass
    answer, headers = ask()
    life = _fresh_for(headers)
    if life >= MIN_FRESH:
        FRESH.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(f".{threading.get_ident()}.part")
        part.write_text(json.dumps({"key": repr(key), "until": time.time() + life, "answer": answer}))
        part.replace(path)
    return answer
