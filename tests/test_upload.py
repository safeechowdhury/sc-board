import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from props import publish, upload


class FakeFTP:
    """Records what an upload does, as a host's FTP server would see it."""

    def __init__(self):
        self.files, self.calls = {}, []

    def mkd(self, path):
        self.calls.append(("mkd", path))

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
