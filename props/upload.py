"""Upload site/ to the web host over FTPS: only the files that changed since the last upload.

The host (whc.ca shared hosting) can't build the site itself, and the books refuse cloud
servers, so the site is built here and sent up. Settings come from the environment or the
repo's .env file (never committed):

    FTP_SERVER     the host, e.g. ftp.example.com
    FTP_USERNAME   the cPanel FTP account
    FTP_PASSWORD
    FTP_DIR        the folder the site goes in (default public_html)

What was sent, and its content hash, is kept in data/cache/uploaded.json, so a run sends only
new or changed files and deletes the ones site/ no longer has (a player gone from the board).
Player files go up first and the pages last, so a page never points at a player file that isn't
there yet. Each file is written under a temporary name and renamed into place, so a visitor
never gets a half-sent page. `--full` forgets what was sent and sends everything.
"""
import ftplib
import hashlib
import json
import os
import ssl
from pathlib import Path

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
    hosts' FTP servers require ("522 SSL connection failed: session reuse required")."""

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
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
    ftp = _FTPS(conf["FTP_SERVER"], timeout=60, context=ssl.create_default_context())
    ftp.login(conf["FTP_USERNAME"], conf["FTP_PASSWORD"])
    ftp.prot_p()   # encrypt the files too, not just the login
    ftp.cwd(conf["FTP_DIR"])
    return ftp


def upload(root: Path | None = None, full: bool = False) -> list[str]:
    """Send what changed in site/ to the host. Returns messages for the log."""
    root = root or site.OUT
    conf = settings()
    now = _hashes(root)
    sent = {} if full or not SENT.exists() else json.loads(SENT.read_text())
    send, delete = plan(now, sent)
    if not send and not delete:
        return ["upload: nothing changed since the last upload"]
    ftp = _connect(conf)
    made: set[str] = set()
    try:
        for name in send:
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
            if len(sent) % 200 == 0:   # a long first upload that breaks off resumes where it was
                _save(sent)
        for name in delete:
            try:
                ftp.delete(name)
            except ftplib.error_perm:
                pass   # already gone
            sent.pop(name, None)
    finally:
        _save(sent)
        try:
            ftp.quit()
        except (OSError, EOFError, ftplib.Error):
            ftp.close()
    return [f"upload: sent {len(send)} files" + (f", deleted {len(delete)}" if delete else "")
            + f" to {conf['FTP_SERVER']}/{conf['FTP_DIR']}"]


def _save(sent: dict) -> None:
    SENT.parent.mkdir(parents=True, exist_ok=True)
    part = SENT.with_suffix(".part")
    part.write_text(json.dumps(sent))
    part.replace(SENT)
