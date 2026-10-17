"""Upload site/ to the web host over FTPS: only the files that changed since the last upload.

The host (whc.ca shared hosting) can't build the site itself, and the books refuse cloud
servers, so the site is built here and sent up. Settings come from the environment or the
repo's .env file (never committed):

    FTP_SERVER     the host, e.g. ftp.yourdomain.ca
    FTP_CERT_NAME  optional: the name the host's certificate is for, where it isn't FTP_SERVER (on
                   shared hosting the certificate names the server, e.g. srv2.swhc.ca)
    FTP_USERNAME   the cPanel FTP account
    FTP_PASSWORD
    FTP_DIR        the folder the site goes in (default public_html)

What was sent, and its content hash, is kept in data/cache/uploaded.json, so a run sends only
new or changed files and deletes the ones site/ no longer has (a player gone from the board).
Player files go up first and the pages last, so a page never points at a player file that isn't
there yet. Each file is written under a temporary name and renamed into place, so a visitor
never gets a half-sent page. `--full` forgets what was sent and sends everything. `--clean` sends
everything and then deletes whatever else is in the folder (but KEEP), leaving exactly site/.
"""
import ftplib
import hashlib
import json
import os
import ssl
from pathlib import Path

import certifi

from . import site, store

SENT = store.DATA / "cache" / "uploaded.json"
ENV = store.ROOT / ".env"


def settings() -> dict:
    """FTP_* from the environment, else from .env."""
    found = {}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.startswith("FTP_"):
                found[key] = value.strip().strip("'\"")
    found.update({key: value for key, value in os.environ.items() if key.startswith("FTP_")})
    missing = [key for key in ("FTP_SERVER", "FTP_USERNAME", "FTP_PASSWORD") if not found.get(key)]
    if missing:
        raise SystemExit(f"upload: set {', '.join(missing)} in {ENV} or the environment")
    found.setdefault("FTP_DIR", "public_html")
    return found


class _FTPS(ftplib.FTP_TLS):
    """FTP_TLS whose data connections reuse the control connection's TLS session, which many
    hosts' FTP servers require ("522 SSL connection failed: session reuse required"), and whose
    certificate is checked against cert_name: on shared hosting the server's certificate names
    the server (srv2.swhc.ca), not the domain it is reached by."""

    cert_name: str | None = None

    def auth(self):
        if self.cert_name is None:
            return super().auth()
        host, self.host = self.host, self.cert_name   # FTP_TLS.auth checks the certificate against self.host
        try:
            return super().auth()
        finally:
            self.host = host

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.cert_name or self.host,
                                            session=self.sock.session)
        return conn, size


def _hashes(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): hashlib.blake2b(path.read_bytes(), digest_size=16).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file() and path.name != ".DS_Store"}


def plan(now: dict[str, str], sent: dict[str, str]) -> tuple[list[str], list[str]]:
    """(files to send, player files first and pages last; remote files to delete)."""
    send = [name for name, digest in now.items() if sent.get(name) != digest]
    send.sort(key=lambda name: (not name.startswith("p/"), name.endswith(".html"), name))
    return send, sorted(set(sent) - set(now))


def _connect(conf: dict) -> _FTPS:
    # certifi's list of certificate authorities: python.org's Python for macOS ships without one
    # ("unable to get local issuer certificate") until its Install Certificates script is run
    ftp = _FTPS(timeout=60, context=ssl.create_default_context(cafile=certifi.where()))
    ftp.cert_name = conf.get("FTP_CERT_NAME") or None
    ftp.connect(conf["FTP_SERVER"])
    ftp.login(conf["FTP_USERNAME"], conf["FTP_PASSWORD"])
    ftp.prot_p()   # encrypt the files too, not just the login
    ftp.cwd(conf["FTP_DIR"])
    return ftp


# never deleted by a clean: the host's own (AutoSSL's certificate checks, cPanel's scripts and quota file),
# plus any top-level names listed in FTP_KEEP ("blog,old-site")
KEEP = {".well-known", "cgi-bin", ".ftpquota"}


def _remote(ftp, path: str = "") -> tuple[list[str], list[str]]:
    """Every file and folder under the upload folder on the server, as paths relative to it."""
    files, folders = [], []
    try:
        entries = [(name, facts.get("type", "")) for name, facts in ftp.mlsd(path or ".", facts=["type"])]
    except ftplib.error_perm:   # a server without MLSD: tell folders from files by trying to enter them
        home, entries = ftp.pwd(), []
        for name in ftp.nlst(path or "."):
            name = name.rpartition("/")[2]
            try:
                ftp.cwd(f"{home}/{path}/{name}" if path else f"{home}/{name}")
                entries.append((name, "dir"))
            except ftplib.error_perm:
                entries.append((name, "file"))
            finally:
                ftp.cwd(home)
    for name, kind in entries:
        if name in (".", "..") or kind in ("cdir", "pdir"):
            continue
        full = f"{path}/{name}" if path else name
        if kind == "dir":
            folders.append(full)
            more_files, more_folders = _remote(ftp, full)
            files += more_files
            folders += more_folders
        else:
            files.append(full)
    return files, folders


def strays(remote_files: list[str], remote_folders: list[str], local: dict[str, str], keep: set[str]):
    """(files, folders deepest first) on the server that aren't part of the site and aren't kept."""
    local_folders = {name.rsplit("/", i)[0] for name in local for i in range(1, name.count("/") + 1)}
    kept = lambda path: path.split("/")[0] in keep
    return (sorted(f for f in remote_files if f not in local and not kept(f)),
            sorted((d for d in remote_folders if d not in local_folders and not kept(d)), key=lambda d: -d.count("/")))


def upload(root: Path | None = None, full: bool = False, clean: bool = False, dry_run: bool = False,
           confirm=None) -> list[str]:
    """Send what changed in site/ to the host. Returns messages for the log.

    clean: send every file, then delete everything else in the upload folder (except KEEP), so it
    holds exactly site/, as if it had been emptied first, but with the old site up until the new
    one is in place. confirm(files, folders) is asked before anything is deleted; False stops there.
    dry_run: connect and say what would happen, changing nothing."""
    root = root or site.OUT
    conf = settings()
    now = _hashes(root)
    sent = {} if full or clean or not SENT.exists() else json.loads(SENT.read_text())
    send, delete = plan(now, sent)
    if not send and not delete and not clean and not dry_run:
        return ["upload: nothing changed since the last upload"]
    ftp = _connect(conf)
    where = f"{conf['FTP_SERVER']}/{conf['FTP_DIR']}"
    made: set[str] = set()
    started = False   # the record of what was sent is saved only once sending has begun
    try:
        extra_files, extra_folders = [], []
        if clean:
            keep = KEEP | {name.strip() for name in conf.get("FTP_KEEP", "").split(",") if name.strip()}
            extra_files, extra_folders = strays(*_remote(ftp), now, keep)
            delete = []
        if dry_run:
            out = [f"upload (dry run): connected to {where}", f"upload (dry run): would send {len(send)} files"]
            gone = extra_files + extra_folders if clean else delete
            out.append(f"upload (dry run): would delete {len(gone)}" + (":" if gone else ""))
            out += [f"    {name}" for name in gone[:50]] + ([f"    ... and {len(gone) - 50} more"] if len(gone) > 50 else [])
            return out
        if clean and (extra_files or extra_folders) and confirm and not confirm(extra_files, extra_folders):
            return ["upload: stopped, nothing sent or deleted"]
        started = True
        for count, name in enumerate(send, 1):
            folder = name.rpartition("/")[0]
            if folder and folder not in made:
                path = ""
                for part in folder.split("/"):
                    path = f"{path}/{part}" if path else part
                    try:
                        ftp.mkd(path)
                    except ftplib.error_perm:
                        pass   # already there
                made.add(folder)
            with open(root / name, "rb") as f:
                ftp.storbinary(f"STOR {name}.part", f)
            ftp.rename(f"{name}.part", name)
            sent[name] = now[name]
            if count % 200 == 0:   # a long upload that breaks off resumes where it was
                _save(sent)
        for name in delete + extra_files:
            try:
                ftp.delete(name)
            except ftplib.error_perm:
                pass   # already gone
            sent.pop(name, None)
        for folder in extra_folders:
            try:
                ftp.rmd(folder)
            except ftplib.error_perm:
                pass   # not empty: something kept is inside
    finally:
        if started:
            _save(sent)
        try:
            ftp.quit()
        except (OSError, EOFError, ftplib.Error):
            ftp.close()
    removed = len(delete) + len(extra_files)
    return [f"upload: sent {len(send)} files" + (f", deleted {removed}" if removed else "")
            + (f" and {len(extra_folders)} folders" if extra_folders else "") + f" to {where}"]


def _save(sent: dict) -> None:
    SENT.parent.mkdir(parents=True, exist_ok=True)
    part = SENT.with_suffix(".part")
    part.write_text(json.dumps(sent))
    part.replace(SENT)
