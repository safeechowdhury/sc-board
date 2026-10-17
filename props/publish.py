"""python -m props publish: refresh every board and the lines page at once, then zip the site.

Two zips land at the repo root, each with the site's files at its top level:

    sc-board-pages.zip   the pages: every board, the lines page, the Underdog viewer
    sc-board-site.zip    the whole site, the player files (site/p/) too

The game logs are refreshed first, once a slate day (last night's games are final by
then; `--logs` refreshes them again). The player files change only when the game logs
do, so between those the pages zip is all a site needs. The run says which to upload:
it remembers the player files of the last full zip it pointed to and compares.
"""
import hashlib
import json
import shutil
import time
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import gamelines, net, site, store
from .slate import slate_date

PAGES_ZIP = store.ROOT / "sc-board-pages.zip"
SITE_ZIP = store.ROOT / "sc-board-site.zip"
UPLOADED = store.DATA / "cache" / "published-players.json"   # the player files of the last full zip pointed to
PLAYERS_ZIP = store.DATA / "cache" / "players.zip"            # the player files compressed, reused while they last
PLAYERS_KEY = store.DATA / "cache" / "players-zip.json"       # ... and what they were
LOGS_DAY = store.DATA / "cache" / "logs-refreshed"            # the slate day the game logs were last refreshed


def refresh_logs(leagues: dict, force: bool = False) -> None:
    """Every league's game logs, refreshed at once (each from its own league's site), once a slate day."""
    today = str(slate_date())
    if not force and LOGS_DAY.exists() and LOGS_DAY.read_text() == today:
        return
    leagues = {key: league for key, league in leagues.items() if not getattr(league, "ODDS_ONLY", False)}   # no logs to refresh

    def refresh(league):
        return [f"refresh {league.KEY}: {message}" for message in league.refresh(store.Store(league), 3)]

    with ThreadPoolExecutor(max_workers=len(leagues)) as pool:
        for messages in pool.map(refresh, leagues.values()):
            for message in messages:
                print(message)
    LOGS_DAY.parent.mkdir(parents=True, exist_ok=True)
    LOGS_DAY.write_text(today)


def build_all(leagues: dict, offline: bool = False, lines: bool = True, record: bool = False) -> list[Path]:
    """Every league's board and (unless `lines` is False) the lines page, all at once, on one
    clock and sharing answers. The boards' pages come first, in the leagues' order. record: keep
    each board's prices in its snapshots."""
    with net.run(), ThreadPoolExecutor(max_workers=len(leagues) + 1) as pool:
        jobs = [(league.KEY, pool.submit(site.build, league, offline, record)) for league in leagues.values()]
        if lines:
            jobs.append(("lines page", pool.submit(gamelines.build, leagues, offline)))
        pages, failed = [], []
        for name, job in jobs:   # one page failing leaves the others (and the upload) standing
            try:
                pages.append(job.result())
            except (Exception, SystemExit) as e:
                failed.append(name)
                print(f"build {name}: FAILED, its page was not updated: {type(e).__name__}: {e}")
                traceback.print_exception(e)
    if failed and not pages:
        raise SystemExit(f"every page failed to build: {', '.join(failed)}")
    return pages


def _files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file() and path.name != ".DS_Store")


def _zip(out: Path, files: list[Path], root: Path) -> None:
    part = out.with_suffix(".zip.part")
    with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in files:
            z.write(path, path.relative_to(root).as_posix())
    part.replace(out)


def _fingerprints(files: list[Path], root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): hashlib.blake2b(path.read_bytes(), digest_size=16).hexdigest()
            for path in files}


def _site_zip(players: list[Path], pages: list[Path], now: dict, root: Path) -> None:
    """The whole site: the player files, compressed once while they stay the same, plus the pages."""
    if not (PLAYERS_ZIP.exists() and PLAYERS_KEY.exists() and json.loads(PLAYERS_KEY.read_text()) == now):
        PLAYERS_ZIP.parent.mkdir(parents=True, exist_ok=True)
        _zip(PLAYERS_ZIP, players, root)
        PLAYERS_KEY.write_text(json.dumps(now))
    part = SITE_ZIP.with_suffix(".zip.part")
    shutil.copyfile(PLAYERS_ZIP, part)
    with zipfile.ZipFile(part, "a", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in pages:
            z.write(path, path.relative_to(root).as_posix())
    part.replace(SITE_ZIP)


def zip_site(root: Path | None = None) -> list[str]:
    """Write both zips; returns what to tell the person uploading."""
    root = root or site.OUT
    files = _files(root)
    players = [path for path in files if path.relative_to(root).parts[0] == "p"]
    pages = [path for path in files if path.relative_to(root).parts[0] != "p"]
    now = _fingerprints(players, root)
    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(_zip, PAGES_ZIP, pages, root), pool.submit(_site_zip, players, pages, now, root)]:
            future.result()
    before = json.loads(UPLOADED.read_text()) if UPLOADED.exists() else None
    size = lambda path: f"{path.stat().st_size / 1e6:.1f} MB"
    out = [f"wrote {PAGES_ZIP} ({len(pages)} files, {size(PAGES_ZIP)})",
           f"wrote {SITE_ZIP} ({len(files)} files, {size(SITE_ZIP)})"]
    if before == now:
        out.append(f"upload {PAGES_ZIP.name}: the player files are the same as in the last full zip")
    else:
        changed = sum(before.get(name) != print_ for name, print_ in now.items()) if before else len(now)
        gone = len(before.keys() - now.keys()) if before else 0
        out.append(f"upload {SITE_ZIP.name}: {changed} player files changed"
                   + (f" and {gone} are gone" if gone else "") + " since the last full zip")
        UPLOADED.parent.mkdir(parents=True, exist_ok=True)
        UPLOADED.write_text(json.dumps(now))
    return out


def publish(leagues: dict, offline: bool = False, logs: bool = False) -> None:
    started = time.monotonic()
    if not offline:
        refresh_logs(leagues, force=logs)
    build_all(leagues, offline, record=not offline)   # a saved fetch's prices aren't new: nothing to keep
    built = time.monotonic()
    for message in zip_site():
        print(f"publish: {message}")
    print(f"publish: built in {built - started:.1f}s, zipped in {time.monotonic() - built:.1f}s; requests: "
          + ", ".join(f"{name} {n}" for name, n in sorted(net.asked.items())))
