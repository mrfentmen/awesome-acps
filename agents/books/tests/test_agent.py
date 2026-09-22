"""Tests for the books agent: Open Library reader, routing, skills, permissions.

    python3 agents/books/tests/test_agent.py
"""

from __future__ import annotations

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

from agent import BooksAgent, author_from_text, query_from_text, route  # noqa: E402
from data import BooksData, BooksError  # noqa: E402

SEARCH_PAYLOAD = {
    "numFound": 48208,
    "docs": [
        {"key": "/works/OL893414W", "title": "Dune", "author_name": ["Frank Herbert"],
         "first_publish_year": 1965, "edition_count": 155, "language": ["fre", "spa"]},
        {"key": "/works/OL893461W", "title": "Dune Messiah", "author_name": ["Frank Herbert"],
         "first_publish_year": 1969, "edition_count": 101, "language": ["spa", "pol"]},
        {"key": "/authors/OL31353A", "title": "Not a work", "author_name": []},
    ],
}

WORK_PAYLOAD = {
    "key": "/works/OL893414W",
    "title": "Dune",
    "description": "Set on the desert planet Arrakis, Dune is the story of the boy Paul Atreides.",
    "subjects": ["Dune (Imaginary place)", "Fiction", "Fiction, science fiction, general"],
    "links": [{"title": "New York Times review", "url": "http://www.nytimes.com/1999/11/28/books/x.html"}],
}

WORK_PAYLOAD_OBJECT_DESCRIPTION = dict(WORK_PAYLOAD, description={
    "type": "/type/text", "value": "Set on the desert planet Arrakis.\n\nA second paragraph."})

AUTHOR_SEARCH = {"docs": [{"key": "OL31353A", "name": "Ursula K. Le Guin", "work_count": 253,
                           "top_work": "The Left Hand of Darkness"}]}
AUTHOR_DETAIL = {"key": "/authors/OL31353A", "name": "Ursula K. Le Guin",
                 "bio": {"type": "/type/text", "value": "Ursula Kroeber Le Guin (1929-2018) was an American author."},
                 "birth_date": "21 October 1929", "death_date": "22 January 2018"}
AUTHOR_WORKS = {"entries": [{"key": "/works/OL1W", "title": "The Left Hand of Darkness"},
                            {"key": "/works/OL2W", "title": "The Dispossessed"},
                            {"key": "/works/OL3W", "title": "A Wizard of Earthsea"}]}

TRENDING_PAYLOAD = {"works": [
    {"key": "/works/OL17930368W", "title": "Atomic Habits", "authors": [{"name": "James Clear"}],
     "first_publish_year": 2018},
    {"key": "/works/OL1968368W", "title": "The 48 Laws of Power", "authors": [{"name": "Robert Greene"}]},
]}


class FakeData(BooksData):
    """Same interface as the real reader, no network."""

    def __init__(self, search_payload=None, work_payload=None, raise_error: bool = False):
        self.search_payload = search_payload if search_payload is not None else SEARCH_PAYLOAD
        self.work_payload = work_payload if work_payload is not None else WORK_PAYLOAD
        self.raise_error = raise_error
        self.calls: list[dict] = []

    def search(self, query: str, limit: int = 5) -> dict:
        if self.raise_error:
            raise BooksError("Open Library is offline")
        self.calls.append({"kind": "search", "query": query, "limit": limit})
        books = [self._one_book(doc) for doc in self.search_payload.get("docs", [])
                 if str(doc.get("key", "")).startswith("/works/")]
        return {"dataset": "openlibrary.org/search.json", "query": query,
                "found": int(self.search_payload.get("numFound") or 0), "books": books[:limit]}

    def work(self, reference: str) -> dict:
        if self.raise_error:
            raise BooksError("Open Library is offline")
        self.calls.append({"kind": "work", "reference": reference})
        payload = self.work_payload
        return {"dataset": "openlibrary.org/works", "key": payload["key"], "title": payload["title"],
                "description": self._description(payload.get("description")),
                "subjects": payload.get("subjects") or [],
                "first_publish_date": payload.get("first_publish_date"),
                "links": [link.get("url") for link in (payload.get("links") or [])]}

    def author(self, name: str, works: int = 5) -> dict:
        if self.raise_error:
            raise BooksError("Open Library is offline")
        self.calls.append({"kind": "author", "name": name, "works": works})
        first = AUTHOR_SEARCH["docs"][0]
        return {"dataset": "openlibrary.org/authors", "key": "/authors/OL31353A",
                "name": AUTHOR_DETAIL["name"], "bio": self._description(AUTHOR_DETAIL["bio"]),
                "birth_date": AUTHOR_DETAIL["birth_date"], "death_date": AUTHOR_DETAIL["death_date"],
                "work_count": first["work_count"], "top_work": first["top_work"],
                "works": AUTHOR_WORKS["entries"][:works]}

    def trending(self, limit: int = 5) -> dict:
        if self.raise_error:
            raise BooksError("Open Library is offline")
        self.calls.append({"kind": "trending", "limit": limit})
        return {"dataset": "openlibrary.org/trending/daily.json",
                "books": [{"key": item.get("key"), "title": item.get("title"),
                           "authors": [author.get("name") for author in (item.get("authors") or [])],
                           "first_publish_year": item.get("first_publish_year")}
                          for item in TRENDING_PAYLOAD["works"][:limit]]}


class QueueReader:
    """One side's inbox: an iterator of lines, plus push to add one."""

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


class ReaderTests(unittest.TestCase):
    def test_work_ids_are_normalised(self):
        self.assertEqual(BooksData.check_work("OL893414W"), "/works/OL893414W")
        self.assertEqual(BooksData.check_work("ol893414w"), "/works/OL893414W")
        self.assertEqual(BooksData.check_work("https://openlibrary.org/works/OL893414W"), "/works/OL893414W")
        with self.assertRaises(ValueError):
            BooksData.check_work("Dune")

    def test_author_keys_are_normalised(self):
        self.assertEqual(BooksData.check_author_key("OL31353A"), "/authors/OL31353A")
        with self.assertRaises(ValueError):
            BooksData.check_author_key("OL893414W")

    def test_limits_and_queries_are_validated(self):
        self.assertEqual(BooksData.check_limit(5), 5)
        with self.assertRaises(ValueError):
            BooksData.check_limit(0)
        with self.assertRaises(ValueError):
            BooksData.check_limit(100)
        self.assertEqual(BooksData.check_query("  dune  "), "dune")
        with self.assertRaises(ValueError):
            BooksData.check_query("a")
        with self.assertRaises(ValueError):
            BooksData.check_query("x" * 201)

    def test_search_maps_the_docs_and_drops_non_works(self):
        data = BooksData(fetch=lambda url, params: SEARCH_PAYLOAD)
        result = data.search("dune", limit=5)
        self.assertEqual(result["found"], 48208)
        self.assertEqual(len(result["books"]), 2)
        first = result["books"][0]
        self.assertEqual(first["title"], "Dune")
        self.assertEqual(first["authors"], ["Frank Herbert"])
        self.assertEqual(first["first_publish_year"], 1965)
        self.assertEqual(first["url"], "https://openlibrary.org/works/OL893414W")

    def test_a_search_payload_without_docs_is_an_error(self):
        data = BooksData(fetch=lambda url, params: {"numFound": 0})
        with self.assertRaises(BooksError):
            data.search("dune")

    def test_work_reads_a_string_description(self):
        data = BooksData(fetch=lambda url, params: WORK_PAYLOAD)
        work = data.work("OL893414W")
        self.assertEqual(work["title"], "Dune")
        self.assertTrue(work["description"].startswith("Set on the desert planet Arrakis"))
        self.assertEqual(work["subjects"][0], "Dune (Imaginary place)")
        self.assertEqual(work["links"], ["http://www.nytimes.com/1999/11/28/books/x.html"])

    def test_work_reads_an_object_description_and_collapses_whitespace(self):
        data = BooksData(fetch=lambda url, params: WORK_PAYLOAD_OBJECT_DESCRIPTION)
        work = data.work("OL893414W")
        self.assertEqual(work["description"], "Set on the desert planet Arrakis. A second paragraph.")

    def test_a_work_without_a_title_is_not_reported_as_found(self):
        data = BooksData(fetch=lambda url, params: {"key": "/works/OL1W"})
        with self.assertRaises(BooksError):
            data.work("OL1W")

    def test_author_reads_the_detail_and_the_works(self):
        payloads = {
            "/search/authors.json": AUTHOR_SEARCH,
            "/authors/OL31353A.json": AUTHOR_DETAIL,
            "/authors/OL31353A/works.json": AUTHOR_WORKS,
        }
        data = BooksData(fetch=lambda url, params: payloads[url.replace("https://openlibrary.org", "")])
        author = data.author("ursula k. le guin", works=2)
        self.assertEqual(author["name"], "Ursula K. Le Guin")
        self.assertEqual(author["work_count"], 253)
        self.assertEqual(author["birth_date"], "21 October 1929")
        self.assertTrue(author["bio"].startswith("Ursula Kroeber Le Guin"))
        self.assertEqual([item["title"] for item in author["works"]],
                         ["The Left Hand of Darkness", "The Dispossessed"])

    def test_an_unknown_author_is_an_error(self):
        data = BooksData(fetch=lambda url, params: {"docs": []})
        with self.assertRaises(BooksError):
            data.author("nobody at all")

    def test_trending_maps_the_feed(self):
        data = BooksData(fetch=lambda url, params: TRENDING_PAYLOAD)
        trending = data.trending(limit=2)
        self.assertEqual(trending["books"][0]["title"], "Atomic Habits")
        self.assertEqual(trending["books"][0]["authors"], ["James Clear"])
        self.assertEqual(trending["books"][1]["authors"], ["Robert Greene"])


class RouteTests(unittest.TestCase):
    def test_a_work_id_routes_to_the_work(self):
        skill, params = route("what is OL893414W?")
        self.assertEqual(skill, "books-work")
        self.assertEqual(params["work"], "OL893414W")

    def test_trending_words(self):
        self.assertEqual(route("what is popular on Open Library?")[0], "books-trending")
        self.assertEqual(route("what is trending today?")[0], "books-trending")

    def test_books_by_an_author(self):
        skill, params = route("what has Ursula K. Le Guin written?")
        self.assertEqual(skill, "books-author")
        self.assertEqual(params["author"], "Ursula K. Le Guin")

    def test_by_phrase(self):
        skill, params = route("books by Frank Herbert")
        self.assertEqual(skill, "books-author")
        self.assertEqual(params["author"], "Frank Herbert")

    def test_a_subject_search(self):
        skill, params = route("find books about urban foxes")
        self.assertEqual(skill, "books-search")
        self.assertEqual(params["query"], "urban foxes")

    def test_a_plain_title_search(self):
        skill, params = route("Dune")
        self.assertEqual(skill, "books-search")
        self.assertEqual(params["query"], "Dune")

    def test_helpers(self):
        self.assertEqual(query_from_text("find books about urban foxes"), "urban foxes")
        self.assertEqual(query_from_text("who wrote Dune?"), "Dune")
        self.assertEqual(author_from_text("what has Ursula K. Le Guin written?"), "Ursula K. Le Guin")
        self.assertEqual(author_from_text("books by Frank Herbert"), "Frank Herbert")
        self.assertIsNone(author_from_text("who wrote Dune?"))
        self.assertIsNone(author_from_text("find books about foxes"))

    def test_who_wrote_is_a_book_question_not_an_author_question(self):
        skill, params = route("who wrote Dune?")
        self.assertEqual(skill, "books-search")
        self.assertEqual(params["query"], "Dune")

    def test_help(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")


class TurnTests(unittest.TestCase):
    def turn(self, text: str, data: FakeData | None = None, permission: str = "allow-once"):
        agent_conn, client_conn = connected_pair()
        agent = BooksAgent(agent_conn, data or FakeData())
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_search_lists_the_records_with_ids(self):
        result, client = self.turn("find books about dune")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("48208 record(s) match 'dune'", result["text"])
        self.assertIn("Dune - Frank Herbert (1965), 155 edition(s), /works/OL893414W", result["text"])
        self.assertIn("openlibrary.org/search.json", result["text"])
        tools = [update for update in result["updates"] if update.get("sessionUpdate") == "tool_call"]
        self.assertEqual(tools[0]["name"], "books-search")
        self.assertEqual(len(client.permission_requests), 1)

    def test_streaming_is_chunked_under_one_message_id(self):
        result, _ = self.turn("find books about dune")
        chunks = [update for update in result["updates"] if update.get("sessionUpdate") == "agent_message_chunk"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len({chunk["messageId"] for chunk in chunks}), 1)

    def test_work_reads_the_description(self):
        result, _ = self.turn("what is OL893414W?")
        self.assertIn("Dune - /works/OL893414W", result["text"])
        self.assertIn("Set on the desert planet Arrakis", result["text"])
        self.assertIn("Subjects: Dune (Imaginary place), Fiction", result["text"])

    def test_author_lists_the_works_and_says_the_order_is_not_ranking(self):
        result, _ = self.turn("what has Ursula K. Le Guin written?")
        self.assertIn("253 work(s) in the catalog", result["text"])
        self.assertIn("best known for The Left Hand of Darkness", result["text"])
        self.assertIn("its own order, not ranked", result["text"])
        self.assertIn("A Wizard of Earthsea", result["text"])

    def test_trending_says_what_trending_means(self):
        result, _ = self.turn("what is popular on Open Library today?")
        self.assertIn("Atomic Habits - James Clear (2018)", result["text"])
        self.assertIn("not a sales chart", result["text"])

    def test_help_does_not_ask_permission(self):
        result, client = self.turn("what can you do?")
        self.assertIn("Open Library", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _ = self.turn("find dune", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_a_feed_failure_is_reported(self):
        result, _ = self.turn("find dune", data=FakeData(raise_error=True))
        self.assertIn("could not read Open Library", result["text"])
        statuses = [update.get("status") for update in result["updates"]
                    if update.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", statuses)

    def test_an_empty_search_says_so(self):
        result, _ = self.turn("find zzzznotathing", data=FakeData(search_payload={"numFound": 0, "docs": []}))
        self.assertIn("no record matching 'zzzznotathing'", result["text"])


if __name__ == "__main__":
    unittest.main()
