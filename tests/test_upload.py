import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from props import publish, upload


class FakeFTP:
    """Records what an upload does, as a host's FTP server would see it."""

    def __init__(self):
        self.files, self.calls, self.folders = {}, [], set()

    def mkd(self, path):
        self.folders.add(path)
        self.calls.append(("mkd", path))

    def rmd(self, path):
        if any(name.startswith(path + "/") for name in self.files):
            raise __import__("ftplib").error_perm("550 not empty")
        self.folders.discard(path)
        self.calls.append(("rmd", path))

    def mlsd(self, path=".", facts=()):
        prefix = "" if path == "." else path + "/"
        names = {name[len(prefix):].split("/")[0] for name in list(self.files) + list(self.folders)
                 if name.startswith(prefix) and name != path}
        for name in sorted(names):
            kind = "dir" if prefix + name in self.folders else "file"
            yield name, {"type": kind}

    def storbinary(self, cmd, f):
        name = cmd.removeprefix("STOR ")
        self.files[name] = f.read()
        self.calls.append(("stor", name))

    def rename(self, old, new):
        self.files[new] = self.files.pop(old)
        self.calls.append(("rename", new))

    def delete(self, name):
        self.files.pop(name, None)
        self.calls.append(("delete", name))

    def quit(self):
        pass


class UploadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "site"
        (self.root / "p" / "nhl").mkdir(parents=True)
        (self.root / "index.html").write_text("board")
        (self.root / "p" / "nhl" / "1.js").write_text("player 1")
        (self.root / "p" / "nhl" / "2.js").write_text("player 2")
        self.ftp = FakeFTP()
        for patch in (mock.patch.object(upload, "SENT", Path(self.tmp.name) / "uploaded.json"),
                      mock.patch.object(upload, "settings", lambda: {"FTP_SERVER": "ftp.test", "FTP_DIR": "public_html"}),
                      mock.patch.object(upload, "_connect", lambda conf: self.ftp)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_player_files_go_first_pages_last_each_renamed_into_place(self):
        upload.upload(self.root)
        sent = [name for kind, name in self.ftp.calls if kind == "rename"]
        self.assertEqual(sent, ["p/nhl/1.js", "p/nhl/2.js", "index.html"])
        self.assertEqual(self.ftp.files["index.html"], b"board")
        self.assertNotIn("index.html.part", self.ftp.files)

    def test_only_what_changed_goes_up_and_whats_gone_is_deleted(self):
        upload.upload(self.root)
        self.ftp.calls.clear()
        (self.root / "index.html").write_text("board, later")
        (self.root / "p" / "nhl" / "2.js").unlink()
        upload.upload(self.root)
        self.assertEqual(self.ftp.calls, [("stor", "index.html.part"), ("rename", "index.html"), ("delete", "p/nhl/2.js")])
        self.assertEqual(sorted(json.loads(upload.SENT.read_text())), ["index.html", "p/nhl/1.js"])
        self.assertEqual(upload.upload(self.root), ["upload: nothing changed since the last upload"])


class CleanTest(UploadTest):
    def setUp(self):
        super().setUp()
        self.ftp.folders |= {"old", ".well-known", ".well-known/acme", "p", "p/nhl"}
        self.ftp.files.update({"old.html": b"", "old/x.txt": b"", ".well-known/acme/token": b"",
                               "p/nhl/9.js": b"gone", "index.html": b"stale"})

    def test_clean_leaves_exactly_the_site_and_what_the_host_needs(self):
        asked = []
        upload.upload(self.root, clean=True, confirm=lambda files, folders: asked.append((files, folders)) or True)
        self.assertEqual(asked, [(["old.html", "old/x.txt", "p/nhl/9.js"], ["old"])])
        self.assertEqual(sorted(self.ftp.files), [".well-known/acme/token", "index.html", "p/nhl/1.js", "p/nhl/2.js"])
        self.assertEqual(self.ftp.files["index.html"], b"board")
        self.assertNotIn("old", self.ftp.folders)
        deletes = [i for i, (kind, _) in enumerate(self.ftp.calls) if kind == "delete"]
        self.assertGreater(min(deletes), max(i for i, (kind, _) in enumerate(self.ftp.calls) if kind == "rename"))

    def test_a_no_changes_nothing_and_a_dry_run_changes_nothing(self):
        before = dict(self.ftp.files)
        self.assertEqual(upload.upload(self.root, clean=True, confirm=lambda *_: False),
                         ["upload: stopped, nothing sent or deleted"])
        out = upload.upload(self.root, clean=True, dry_run=True)
        self.assertEqual(self.ftp.files, before)
        self.assertIn("upload (dry run): would delete 4:", out)
        self.assertFalse(upload.SENT.exists())


class SettingsTest(unittest.TestCase):
    def test_read_from_env_file_with_the_environment_winning(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("OTHER=1\nFTP_SERVER=ftp.example.com\nFTP_USERNAME='me'\nFTP_PASSWORD=from-file\n")
            with mock.patch.object(upload, "ENV", env), mock.patch.dict("os.environ", {"FTP_PASSWORD": "from-env"}):
                conf = upload.settings()
        self.assertEqual((conf["FTP_SERVER"], conf["FTP_USERNAME"], conf["FTP_PASSWORD"], conf["FTP_DIR"]),
                         ("ftp.example.com", "me", "from-env", "public_html"))


class BrokenRunTest(unittest.TestCase):
    def test_a_run_where_most_books_failed_is_not_uploaded(self):
        self.assertIsNone(publish.broken({"nhl": {"novig": True, "draftkings": False, "kalshi": True}}))
        why = publish.broken({"nhl": {"novig": False, "draftkings": False, "kalshi": True}})
        self.assertIn("2 of 3", why)
        self.assertIn("VPN", why)
