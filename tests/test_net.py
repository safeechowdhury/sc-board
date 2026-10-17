import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from props import net


class OnceTest(unittest.TestCase):
    def test_outside_a_run_every_call_fetches(self):
        calls = []
        net.once("k", lambda: calls.append(1))
        net.once("k", lambda: calls.append(1))
        self.assertEqual(len(calls), 2)

    def test_inside_a_run_a_request_is_made_once_even_when_asked_at_the_same_time(self):
        calls = []

        def fetch():
            calls.append(1)
            time.sleep(0.05)
            return {"answer": 1}

        answers = []
        with net.run():
            threads = [threading.Thread(target=lambda: answers.append(net.once("k", fetch))) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual((len(calls), answers), (1, [{"answer": 1}] * 5))
        net.once("k", fetch)   # the run is over: asked again
        self.assertEqual(len(calls), 2)

    def test_a_failure_is_shared_not_retried(self):
        calls = []

        def fetch():
            calls.append(1)
            raise ValueError("refused")

        with net.run():
            for _ in range(2):
                with self.assertRaises(ValueError):
                    net.once("k", fetch)
        self.assertEqual(len(calls), 1)

    def test_a_run_has_one_clock(self):
        with net.run():
            first = net.now()
            time.sleep(0.01)
            self.assertEqual(net.now(), first)


class FreshTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(net, "FRESH", Path(tempfile.mkdtemp()) / "http")
        patch.start()
        self.addCleanup(patch.stop)
        self.calls = []

    def ask(self, headers):
        def ask():
            self.calls.append(1)
            return {"n": len(self.calls)}, headers
        return ask

    def test_an_answer_is_kept_as_long_as_its_site_says_it_stays_fresh(self):
        cdn = {"cache-control": "public, max-age=600, s-maxage=600", "age": "452"}
        with net.run():
            self.assertEqual(net.fresh("page", self.ask(cdn)), {"n": 1})
        with net.run():   # a later refresh, inside the 148 seconds left
            self.assertEqual(net.fresh("page", self.ask(cdn)), {"n": 1})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(net.fresh("page", self.ask(cdn)), {"n": 2})   # outside a run: always asked

    def test_nothing_is_kept_when_the_site_forbids_it_or_it_is_about_to_go_stale(self):
        for headers in ({}, {"cache-control": "no-store"}, {"cache-control": "max-age=1"},
                        {"cache-control": "max-age=600", "age": "598"}):
            with net.run():
                net.fresh(repr(headers), self.ask(headers))
            with net.run():
                net.fresh(repr(headers), self.ask(headers))
        self.assertEqual(len(self.calls), 8)

    def test_a_kept_answer_past_its_time_is_asked_again_and_cleared_out(self):
        with net.run():
            net.fresh("page", self.ask({"cache-control": "max-age=60"}))
        (kept,) = net.FRESH.glob("*.json")
        kept.write_text(kept.read_text().replace('"until": ', '"until": 1, "was": '))
        with net.run():
            self.assertFalse(kept.exists())
            net.fresh("page", self.ask({}))
        self.assertEqual(len(self.calls), 2)


class SiteTest(unittest.TestCase):
    def test_a_site_gets_no_more_requests_at_once_than_its_limit_and_waits_its_gap(self):
        net.LIMITS["test-site"] = (2, 0.0)
        net.LIMITS["test-paced"] = (1, 0.05)
        busy, peak, lock = [0], [0], threading.Lock()

        def request():
            with net.site("test-site"):
                with lock:
                    busy[0] += 1
                    peak[0] = max(peak[0], busy[0])
                time.sleep(0.02)
                with lock:
                    busy[0] -= 1

        threads = [threading.Thread(target=request) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(peak[0], 2)

        started = []
        for _ in range(3):
            with net.site("test-paced"):
                started.append(time.monotonic())
        self.assertGreaterEqual(started[2] - started[0], 0.09)


if __name__ == "__main__":
    unittest.main()
