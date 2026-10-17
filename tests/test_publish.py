import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from props import publish


class ZipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.site = self.tmp / "site"
        for name, text in {"index.html": "board", "lines.html": "lines", "ud/nhl.js": "links",
                           "p/nhl/1.js": "one", "p/nhl/2.js": "two", ".DS_Store": "x"}.items():
            (self.site / name).parent.mkdir(parents=True, exist_ok=True)
            (self.site / name).write_text(text)
        self.patches = [mock.patch.object(publish, "PAGES_ZIP", self.tmp / "pages.zip"),
                        mock.patch.object(publish, "SITE_ZIP", self.tmp / "site.zip"),
                        mock.patch.object(publish, "UPLOADED", self.tmp / "uploaded.json"),
                        mock.patch.object(publish, "PLAYERS_ZIP", self.tmp / "players.zip"),
                        mock.patch.object(publish, "PLAYERS_KEY", self.tmp / "players.json")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def names(self, path):
        with zipfile.ZipFile(path) as z:
            return sorted(z.namelist())

    def test_the_pages_zip_leaves_out_the_player_files_and_says_which_to_upload(self):
        first = publish.zip_site(self.site)
        self.assertEqual(self.names(self.tmp / "pages.zip"), ["index.html", "lines.html", "ud/nhl.js"])
        self.assertEqual(self.names(self.tmp / "site.zip"),
                         ["index.html", "lines.html", "p/nhl/1.js", "p/nhl/2.js", "ud/nhl.js"])
        self.assertIn("upload site.zip: 2 player files changed", first[-1])

        (self.site / "index.html").write_text("board, later")
        self.assertIn("upload pages.zip", publish.zip_site(self.site)[-1])   # only the pages changed

        with zipfile.ZipFile(self.tmp / "site.zip") as z:   # the reused player files, the new page
            self.assertEqual((z.read("index.html"), z.read("p/nhl/1.js")), (b"board, later", b"one"))

        (self.site / "p/nhl/2.js").write_text("two, after a game")
        (self.site / "p/nhl/1.js").unlink()
        self.assertIn("upload site.zip: 1 player files changed and 1 are gone", publish.zip_site(self.site)[-1])
        self.assertEqual(self.names(self.tmp / "site.zip"), ["index.html", "lines.html", "p/nhl/2.js", "ud/nhl.js"])
        with zipfile.ZipFile(self.tmp / "site.zip") as z:
            self.assertEqual(z.read("p/nhl/2.js"), b"two, after a game")


class LogsTest(unittest.TestCase):
    def test_game_logs_are_refreshed_once_a_slate_day_unless_asked(self):
        tmp = Path(tempfile.mkdtemp())
        calls = []

        class League:
            KEY = "nhl"

            @staticmethod
            def refresh(games_store, days):
                calls.append(days)
                return ["5 daily files"]

        with mock.patch.object(publish, "LOGS_DAY", tmp / "logs"), mock.patch.object(publish.store, "DATA", tmp), \
             mock.patch("builtins.print"):
            publish.refresh_logs({"nhl": League})
            publish.refresh_logs({"nhl": League})
            self.assertEqual(calls, [3])
            publish.refresh_logs({"nhl": League}, force=True)
            self.assertEqual(calls, [3, 3])
            (tmp / "logs").write_text("2000-01-01")   # a day ago, as it were
            publish.refresh_logs({"nhl": League})
            self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()


class BuildAllTest(unittest.TestCase):
    def test_one_league_failing_leaves_the_others_built(self):
        from props import publish, site

        class League:
            def __init__(self, key):
                self.KEY = key

        def build(league, offline, record):
            if league.KEY == "bad":
                raise KeyError("renamed")
            return Path(f"{league.KEY}.html")

        with mock.patch.object(site, "build", build), mock.patch("builtins.print"), \
                mock.patch("traceback.print_exception"):
            pages = publish.build_all({"good": League("good"), "bad": League("bad")}, lines=False)
        self.assertEqual(pages, [Path("good.html")])
