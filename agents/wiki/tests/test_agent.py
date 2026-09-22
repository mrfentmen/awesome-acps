"""Tests for the wiki agent: summary, entity and on-this-day readers, routing, permissions.

    python3 agents/wiki/tests/test_agent.py
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

from agent import WikiAgent, route  # noqa: E402
from data import (  # noqa: E402
    DATASET_ONTHISDAY,
    DATASET_WIKI,
    DATASET_WIKIDATA,
    WikiData,
    WikiError,
    WikiNotFound,
)

SUMMARY_PAYLOAD = {
    "title": "Ada Lovelace",
    "description": "English mathematician (1815-1852)",
    "extract": "Ada Lovelace was an English mathematician and writer, chiefly known for her "
               "work on Charles Babbage's proposed mechanical general-purpose computer.",
    "wikibase_item": "Q7259",
    "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Ada_Lovelace"}},
    "timestamp": "2026-09-22T00:00:00Z",
}

SEARCH_PAYLOAD = {"search": [
    {"id": "Q42", "label": "Douglas Adams", "description": "English writer and humorist",
     "concepturi": "http://www.wikidata.org/entity/Q42"},
]}

ENTITY_PAYLOAD = {"entities": {"Q42": {
    "labels": {"en": {"value": "Douglas Adams"}},
    "descriptions": {"en": {"value": "English writer and humorist"}},
    "sitelinks": {"enwiki": {"title": "Douglas Adams"}},
}}}

ONTHISDAY_PAYLOAD = {"selected": [
    {"year": 2014, "text": "The NASA spacecraft MAVEN entered into orbit around Mars.",
     "pages": [{"title": "MAVEN"}]},
    {"year": 1862, "text": "The Emancipation Proclamation was issued.",
     "pages": [{"title": "Emancipation Proclamation"}]},
]}


def dispatch(url: str, params: dict):
    """One fetch for the reader tests: answer by which feed was asked for."""
    if "onthisday" in url:
        return ONTHISDAY_PAYLOAD
    if "wikipedia.org" in url:
        return SUMMARY_PAYLOAD
    if "wikidata.org" in url:
        return ENTITY_PAYLOAD if params.get("action") == "wbgetentities" else SEARCH_PAYLOAD
    raise AssertionError(f"unexpected url {url}")


class FakeData(WikiData):
    """Same interface as the real reader, no network."""

    def __init__(self, raise_error: bool = False, not_found: bool = False):
        self.raise_error = raise_error
        self.not_found = not_found
        self.calls: list[dict] = []

    def page(self, title: str) -> dict:
        if self.not_found:
            raise WikiNotFound("no Wikipedia page for 'atlantis'")
        if self.raise_error:
            raise WikiError("Wikipedia is offline")
        self.calls.append({"kind": "page", "title": title})
        return {"dataset": DATASET_WIKI, "title": "Ada Lovelace",
                "description": "English mathematician (1815-1852)",
                "extract": "Ada Lovelace was an English mathematician and writer, chiefly known "
                           "for her work on Charles Babbage's proposed mechanical general-purpose "
                           "computer.",
                "wikibase_id": "Q7259", "url": "https://en.wikipedia.org/wiki/Ada_Lovelace",
                "timestamp": "2026-09-22T00:00:00Z"}

    def entity(self, query: str, limit: int = 3) -> dict:
        if self.raise_error:
            raise WikiError("Wikidata is offline")
        self.calls.append({"kind": "entity", "query": query})
        return {"dataset": DATASET_WIKIDATA, "query": query, "rows": [
            {"id": "Q42", "label": "Douglas Adams", "description": "English writer and humorist",
             "url": "http://www.wikidata.org/entity/Q42"},
        ]}

    def get_entity(self, qid: str) -> dict:
        if self.raise_error:
            raise WikiError("Wikidata is offline")
        self.calls.append({"kind": "get_entity", "qid": qid})
        return {"dataset": DATASET_WIKIDATA, "id": qid.upper(), "label": "Douglas Adams",
                "description": "English writer and humorist", "article": "Douglas Adams"}

    def on_this_day(self, month: int, day: int) -> dict:
        if self.raise_error:
            raise WikiError("the Wikimedia feed is offline")
        self.calls.append({"kind": "on_this_day", "month": month, "day": day})
        return {"dataset": DATASET_ONTHISDAY, "date": "09/22", "rows": [
            {"year": 2014, "text": "The NASA spacecraft MAVEN entered into orbit around Mars.",
             "page": "MAVEN"},
            {"year": 1862, "text": "The Emancipation Proclamation was issued.",
             "page": "Emancipation Proclamation"},
        ]}


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
    def test_title_validation(self):
        self.assertEqual(WikiData.check_title("  Ada   Lovelace "), "Ada Lovelace")
        with self.assertRaises(ValueError):
            WikiData.check_title("x")

    def test_page_reads_the_summary(self):
        data = WikiData(fetch=dispatch)
        page = data.page("Ada Lovelace")
        self.assertEqual(page["title"], "Ada Lovelace")
        self.assertEqual(page["wikibase_id"], "Q7259")
        self.assertIn("English mathematician", page["extract"])

    def test_entity_searches_wikidata(self):
        data = WikiData(fetch=dispatch)
        result = data.entity("Douglas Adams")
        self.assertEqual(result["rows"][0]["id"], "Q42")

    def test_get_entity_reads_labels_and_sitelinks(self):
        data = WikiData(fetch=dispatch)
        entity = data.get_entity("q42")
        self.assertEqual(entity["id"], "Q42")
        self.assertEqual(entity["label"], "Douglas Adams")
        self.assertEqual(entity["article"], "Douglas Adams")

    def test_on_this_day_reads_the_feed(self):
        data = WikiData(fetch=dispatch)
        result = data.on_this_day(9, 22)
        self.assertEqual(result["date"], "09/22")
        self.assertEqual(result["rows"][0]["year"], 2014)
        self.assertEqual(result["rows"][0]["page"], "MAVEN")

    def test_a_404_becomes_not_found(self):
        import urllib.error
        from unittest import mock

        data = WikiData()
        boom = urllib.error.HTTPError("https://example.test", 404, "not found", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=boom):
            with self.assertRaises(WikiNotFound):
                data.page("Atlantis")

    def test_not_found_reaches_the_turn(self):
        class NotFoundData(WikiData):
            def page(self, title: str) -> dict:
                raise WikiNotFound("no Wikipedia page for 'atlantis'")

        data = NotFoundData()
        with self.assertRaises(WikiNotFound):
            data.page("Atlantis")

    def test_a_payload_without_a_summary_is_an_error(self):
        data = WikiData(fetch=lambda url, params: {"title": "Ada Lovelace"})
        with self.assertRaises(WikiError):
            data.page("Ada Lovelace")


class RouteTests(unittest.TestCase):
    def test_help(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")

    def test_summary_keeps_the_case(self):
        skill, params = route("who was Ada Lovelace?")
        self.assertEqual(skill, "wiki-summary")
        self.assertEqual(params["title"], "Ada Lovelace")

    def test_on_this_day(self):
        skill, params = route("what happened on this day?")
        self.assertEqual(skill, "wiki-onthisday")
        self.assertEqual(params, {})

    def test_qid_goes_to_the_entity_lookup(self):
        skill, params = route("tell me about Q42")
        self.assertEqual(skill, "wiki-entity")
        self.assertEqual(params["query"], "Q42")

    def test_entity_by_name(self):
        skill, params = route("what is the wikidata entity for Douglas Adams?")
        self.assertEqual(skill, "wiki-entity")
        self.assertEqual(params["query"], "Douglas Adams")


class TurnTests(unittest.TestCase):
    def turn(self, text: str, data: FakeData | None = None, permission: str = "allow-once"):
        agent_conn, client_conn = connected_pair()
        agent = WikiAgent(agent_conn, data or FakeData())
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_summary_quotes_the_article(self):
        result, client = self.turn("who was Ada Lovelace?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("Ada Lovelace - English mathematician (1815-1852)", result["text"])
        self.assertIn("Ada Lovelace was an English mathematician and writer", result["text"])
        self.assertIn("CC BY-SA", result["text"])
        tools = [update for update in result["updates"] if update.get("sessionUpdate") == "tool_call"]
        self.assertEqual(tools[0]["name"], "wiki-summary")
        self.assertEqual(len(client.permission_requests), 1)

    def test_on_this_day_lists_the_events(self):
        result, _ = self.turn("what happened on this day?")
        self.assertIn("On this day (09/22)", result["text"])
        self.assertIn("2014 - The NASA spacecraft MAVEN entered into orbit around Mars.", result["text"])

    def test_entity_page_includes_the_label_and_the_article(self):
        result, _ = self.turn("tell me about Q42")
        self.assertIn("Q42 is: Douglas Adams", result["text"])
        self.assertIn("English writer and humorist", result["text"])
        self.assertIn("Ada Lovelace was an English mathematician", result["text"])

    def test_a_missing_page_says_so(self):
        result, _ = self.turn("who was Atlantis?", data=FakeData(not_found=True))
        self.assertIn("found no page for that title", result["text"])

    def test_help_does_not_ask_permission(self):
        result, client = self.turn("what can you do?")
        self.assertIn("Wikipedia", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _ = self.turn("who was Ada Lovelace?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_a_feed_failure_is_reported(self):
        result, _ = self.turn("who was Ada Lovelace?", data=FakeData(raise_error=True))
        self.assertIn("could not read the Wikimedia feed", result["text"])
        statuses = [update.get("status") for update in result["updates"]
                    if update.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", statuses)


if __name__ == "__main__":
    unittest.main()
