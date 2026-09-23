"""Tests for the trending agent: the top list, per-article views, routing and turns.

    python3 agents/trending/tests/test_agent.py

No network: the metrics payloads are injected, shaped like the live ones read on 2026-09-23
(Main_Page first with 7,067,096 views, Lizzie Borden second with 1,022,634).
"""

from __future__ import annotations

import datetime
import json
import queue
import sys
import threading
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for path in (str(REPO_ROOT), str(AGENT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from acp_kit import AcpClient, Connection, STOP_END_TURN, STOP_REFUSAL  # noqa: E402

from agent import article_from_text, project_from_text, route  # noqa: E402
from data import TrendingData, TrendingError, readable_title, yesterday  # noqa: E402

TOP = {"items": [{"project": "en.wikipedia", "access": "all-access", "year": "2026",
                  "month": "09", "day": "20", "articles": [
                      {"article": "Main_Page", "views": 7067096, "rank": 1},
                      {"article": "Lizzie_Borden", "views": 1022634, "rank": 2},
                      {"article": "Special:Search", "views": 866333, "rank": 3},
                      {"article": "Wikipedia:Featured_pictures", "views": 605636, "rank": 4},
                      {"article": "Resident_Evil_(2026_film)", "views": 268382, "rank": 5},
                      {"article": "UFC_331", "views": 237626, "rank": 7},
                  ]}]}

PER_ARTICLE = {"items": [
    {"project": "en.wikipedia", "article": "Lizzie_Borden", "timestamp": "2026091500",
     "views": 34280},
    {"project": "en.wikipedia", "article": "Lizzie_Borden", "timestamp": "2026091600",
     "views": 164540},
    {"project": "en.wikipedia", "article": "Lizzie_Borden", "timestamp": "2026091700",
     "views": 557261},
]}


class FakeFeeds:
    def __init__(self, top=None, per_article=None, fail=None):
        self.calls: list[str] = []
        self.top = TOP if top is None else top
        self.per_article = PER_ARTICLE if per_article is None else per_article
        self.fail = fail

    def __call__(self, url: str, params: dict):
        self.calls.append(url)
        if self.fail and self.fail in url:
            raise TrendingError("the metrics are unreachable")
        if "/per-article/" in url:
            return json.dumps(self.per_article)
        if "/top/" in url:
            return json.dumps(self.top)
        raise AssertionError(f"unexpected url {url}")


def make_data(**kw):
    feed = FakeFeeds(**kw)
    return TrendingData(fetch=feed), feed


class HelperTests(unittest.TestCase):
    def test_titles_lose_their_underscores(self):
        self.assertEqual(readable_title("Lizzie_Borden"), "Lizzie Borden")

    def test_the_metrics_lag_by_a_day(self):
        self.assertEqual(yesterday(), datetime.date.today() - datetime.timedelta(days=1))


class ReadTests(unittest.TestCase):
    def test_the_top_list_drops_navigation_and_says_how_many(self):
        data, _feed = make_data()
        reading = data.top(limit=3)
        self.assertEqual([row["title"] for row in reading["rows"]],
                         ["Lizzie Borden", "Resident Evil (2026 film)", "UFC 331"])
        self.assertEqual(reading["navigation_skipped"], 3)  # front page, search, featured pictures
        self.assertEqual(reading["rows"][0]["rank"], 1)
        self.assertEqual(reading["rows"][0]["views"], 1022634)
        self.assertEqual(reading["date"], yesterday().isoformat())

    def test_the_day_is_asked_for_explicitly_and_the_url_carries_it(self):
        data, feed = make_data()
        reading = data.top(project="de", limit=2, when=datetime.date(2026, 9, 20))
        self.assertEqual(reading["project"], "de.wikipedia")
        self.assertIn("/top/de.wikipedia/all-access/2026/09/20", feed.calls[-1])

    def test_a_project_can_be_named_by_its_full_name_and_an_unknown_one_is_refused(self):
        data, _feed = make_data()
        self.assertEqual(data.project("fr.wikipedia"), "fr.wikipedia")
        with self.assertRaises(ValueError):
            data.project("klingon")

    def test_an_empty_day_is_an_error(self):
        data, _feed = make_data(top={"items": []})
        with self.assertRaises(TrendingError):
            data.top()

    def test_pageviews_for_one_article(self):
        data, feed = make_data()
        reading = data.article("Lizzie Borden", days=3)
        self.assertEqual(reading["total"], 756081)
        self.assertEqual(reading["best"]["date"], "2026-09-17")
        self.assertEqual(reading["rows"][0]["date"], "2026-09-15")
        self.assertIn("/per-article/en.wikipedia/all-access/user/Lizzie_Borden/daily/",
                      feed.calls[-1])

    def test_a_window_with_no_recorded_views_is_an_error(self):
        data, _feed = make_data(per_article={"items": []})
        with self.assertRaises(TrendingError):
            data.article("Nothing Here")
        data, _feed = make_data()
        with self.assertRaises(ValueError):
            data.article("")

    def test_a_dead_metrics_service(self):
        data, _feed = make_data(fail="/top/")
        with self.assertRaises(TrendingError):
            data.top()


class RouteTests(unittest.TestCase):
    def test_the_trending_question(self):
        skill, params = route("what is trending on Wikipedia today?")
        self.assertEqual(skill, "trending-top")
        self.assertEqual(params, {})

    def test_a_count_and_a_language(self):
        skill, params = route("top 5 articles in German")
        self.assertEqual(skill, "trending-top")
        self.assertEqual(params["limit"], 5)
        self.assertEqual(params["project"], "de.wikipedia")

    def test_pageviews_for_an_article_with_a_window(self):
        skill, params = route("how many people read the Lizzie Borden article this week?")
        self.assertEqual(skill, "trending-article")
        self.assertEqual(params["article"], "Lizzie Borden")
        self.assertEqual(params["days"], 7)

    def test_an_explicit_day_count(self):
        skill, params = route("pageviews for Ada Lovelace over the last 30 days")
        self.assertEqual(skill, "trending-article")
        self.assertEqual(params["article"], "Ada Lovelace")
        self.assertEqual(params["days"], 30)

    def test_help_and_nonsense(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")
        self.assertEqual(route("book a flight")[0], "help")

    def test_helpers(self):
        self.assertEqual(project_from_text("top 5 articles in German"), "de.wikipedia")
        self.assertIsNone(project_from_text("what is trending?"))
        self.assertEqual(article_from_text("pageviews for Ada Lovelace over the last 30 days"),
                         "Ada Lovelace")
        self.assertIsNone(article_from_text("what is trending today?"))


class QueueReader:
    def __init__(self):
        self._items: queue.Queue = queue.Queue()

    def push(self, text):
        self._items.put(text)

    def close(self):
        self._items.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        item = self._items.get()
        if item is None:
            raise StopIteration
        return item


class WiredWriter:
    def __init__(self, reader):
        self.reader = reader

    def write(self, text):
        self.reader.push(text)

    def flush(self):
        pass

    def close(self):
        self.reader.close()


def connected_pair():
    to_agent, to_client = QueueReader(), QueueReader()
    return (
        Connection(to_agent, WiredWriter(to_client), name="agent"),
        Connection(to_client, WiredWriter(to_agent), name="client"),
    )


class TurnTests(unittest.TestCase):
    def turn(self, text: str, permission: str = "allow-once", **kw):
        from agent import TrendingAgent

        agent_conn, client_conn = connected_pair()
        data, feed = make_data(**kw)
        agent = TrendingAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client, feed
        finally:
            client.stop()

    def test_a_trending_answer_names_the_day_and_what_was_skipped(self):
        result, client, _ = self.turn("what is trending on Wikipedia today?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertEqual(len(client.permission_requests), 1)
        self.assertIn(f"for {yesterday().isoformat()}", result["text"])
        self.assertIn("1,022,634 views", result["text"])
        self.assertIn("left out 3 navigation title(s)", result["text"])
        self.assertIn("lag by a day", result["text"])

    def test_a_pageviews_answer(self):
        result, _, _ = self.turn("pageviews for Lizzie Borden over the last 3 days")
        self.assertIn("756,081 views over 3 day(s)", result["text"])
        self.assertIn("best day 2026-09-17 with 557,261", result["text"])

    def test_help_asks_nothing(self):
        result, client, feed = self.turn("what can you do?")
        self.assertIn("pageview", result["text"])
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(feed.calls, [])

    def test_permission_denied(self):
        result, _, _ = self.turn("what is trending?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)

    def test_a_dead_metrics_service(self):
        result, _, _ = self.turn("what is trending?", fail="/top/")
        self.assertIn("I could not read the metrics", result["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
