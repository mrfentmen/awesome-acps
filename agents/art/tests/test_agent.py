"""Tests for the art agent: museum search, fallback chain, random picks, routing.

    python3 agents/art/tests/test_agent.py
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

from agent import ArtAgent, query_from_text, route  # noqa: E402
from data import (  # noqa: E402
    DATASET_AIC,
    DATASET_CLEVELAND,
    DATASET_MET,
    ArtData,
    ArtError,
)

AIC_PAYLOAD = {"pagination": {"total": 132828}, "data": [
    {"id": 16568, "title": "Water Lilies", "artist_display": "Claude Monet\nFrench, 1840-1926",
     "date_display": "1906", "medium_display": "Oil on canvas", "image_id": "abc-123"},
]}

CLEVELAND_PAYLOAD = {"info": {"total": 28}, "data": [
    {"id": 1234, "title": "Water Lilies", "creation_date": "1906",
     "creators": [{"description": "Claude Monet (French, 1840-1926)"}],
     "technique": "oil on canvas", "url": "https://www.clevelandart.org/art/1234",
     "images": {"web": {"url": "https://openaccess-cdn.clevelandart.org/1234.jpg"}}},
]}

MET_SEARCH_PAYLOAD = {"total": 328, "objectIDs": [438003]}
MET_OBJECT_PAYLOAD = {
    "objectID": 438003, "title": "Water Lilies", "artistDisplayName": "Claude Monet",
    "objectDate": "1906", "medium": "Oil on canvas", "department": "European Paintings",
    "objectURL": "https://www.metmuseum.org/art/collection/search/438003",
    "primaryImageSmall": "https://images.metmuseum.org/438003.jpg", "isPublicDomain": True,
}


def dispatch(url: str, params: dict):
    """Answer by museum. The Met object list is the bare objects URL."""
    if "artic.edu" in url:
        return AIC_PAYLOAD
    if "clevelandart" in url:
        return CLEVELAND_PAYLOAD
    if url.endswith("/objects"):
        return [438003, 438004, 438005]
    if "/objects/438003" in url:
        return MET_OBJECT_PAYLOAD
    if "/objects/" in url:
        return {"objectID": 1, "title": "Untitled", "isPublicDomain": False}
    return MET_SEARCH_PAYLOAD


class FakeData(ArtData):
    """Same interface as the real reader, no network."""

    def __init__(self, raise_error: bool = False, museum: str = DATASET_AIC):
        self.raise_error = raise_error
        self.museum = museum
        self.calls: list[dict] = []

    def search(self, query: str, limit: int = 3) -> dict:
        if self.raise_error:
            raise ArtError("no museum answered for that search")
        self.calls.append({"kind": "search", "query": query})
        rows = [{"title": "Water Lilies", "artist": "Claude Monet", "date": "1906",
                 "medium": "Oil on canvas", "museum": self.museum.split(".")[0],
                 "url": "https://www.artic.edu/artworks/16568",
                 "image": "https://www.artic.edu/iiif/2/abc-123/full/843,/0/default.jpg"}]
        return {"dataset": self.museum, "query": query, "rows": rows}

    def random_piece(self, seed: int | None = None) -> dict:
        if self.raise_error:
            raise ArtError("could not land on a public-domain object in a few tries")
        self.calls.append({"kind": "random"})
        piece = {"title": "Water Lilies", "artist": "Claude Monet", "date": "1906",
                 "medium": "Oil on canvas", "museum": "The Met",
                 "url": "https://www.metmuseum.org/art/collection/search/438003",
                 "image": None, "public_domain": True, "department": "European Paintings"}
        return {"dataset": DATASET_MET, "piece": piece, "pool": 470000}


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
    def test_query_validation(self):
        self.assertEqual(ArtData.check_query("  the   sea "), "the sea")
        with self.assertRaises(ValueError):
            ArtData.check_query("!")
        with self.assertRaises(ValueError):
            ArtData.check_query("word " * 20)

    def test_the_art_institute_answers_first(self):
        data = ArtData(fetch=dispatch)
        result = data.search("monet", limit=1)
        self.assertEqual(result["dataset"], DATASET_AIC)
        self.assertEqual(result["rows"][0]["title"], "Water Lilies")

    def test_the_chain_falls_through_to_cleveland(self):
        def fetch(url, params):
            if "artic.edu" in url:
                raise ArtError("AIC is down")
            return dispatch(url, params)

        data = ArtData(fetch=fetch)
        result = data.search("monet", limit=1)
        self.assertEqual(result["dataset"], DATASET_CLEVELAND)

    def test_the_chain_falls_through_to_the_met(self):
        def fetch(url, params):
            if "artic.edu" in url or "clevelandart" in url:
                raise ArtError("down")
            return dispatch(url, params)

        data = ArtData(fetch=fetch)
        result = data.search("monet", limit=1)
        self.assertEqual(result["dataset"], DATASET_MET)
        self.assertEqual(result["rows"][0]["artist"], "Claude Monet")

    def test_all_museums_down_is_an_error(self):
        data = ArtData(fetch=lambda url, params: (_ for _ in ()).throw(ArtError("down")))
        with self.assertRaises(ArtError):
            data.search("monet", limit=1)

    def test_a_random_pick_is_public_domain(self):
        data = ArtData(fetch=dispatch, seed=7)
        result = data.random_piece()
        self.assertTrue(result["piece"]["public_domain"])
        self.assertEqual(result["piece"]["title"], "Water Lilies")
        self.assertEqual(result["pool"], 3)


class RouteTests(unittest.TestCase):
    def test_help(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")

    def test_random(self):
        self.assertEqual(route("surprise me with a piece")[0], "art-random")

    def test_search_by_artist(self):
        skill, params = route("paintings by Monet")
        self.assertEqual(skill, "art-search")
        self.assertEqual(params["query"], "Monet")

    def test_search_by_subject(self):
        skill, params = route("find art about the sea")
        self.assertEqual(skill, "art-search")
        self.assertEqual(params["query"], "sea")

    def test_query_extraction_is_conservative(self):
        self.assertIsNone(query_from_text("what can you do?"))


class TurnTests(unittest.TestCase):
    def turn(self, text: str, data: FakeData | None = None, permission: str = "allow-once"):
        agent_conn, client_conn = connected_pair()
        agent = ArtAgent(agent_conn, data or FakeData())
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_search_names_the_work(self):
        result, client = self.turn("paintings by Monet")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("Works matching 'Monet'", result["text"])
        self.assertIn("Water Lilies - Claude Monet - 1906", result["text"])
        self.assertIn("Oil on canvas", result["text"])
        self.assertIn("https://www.artic.edu/artworks/16568", result["text"])
        tools = [update for update in result["updates"] if update.get("sessionUpdate") == "tool_call"]
        self.assertEqual(tools[0]["name"], "art-search")
        self.assertEqual(len(client.permission_requests), 1)

    def test_random_pick_comes_from_the_met(self):
        result, _ = self.turn("surprise me with a piece")
        self.assertIn("A random public-domain piece from The Met:", result["text"])
        self.assertIn("Water Lilies - Claude Monet - 1906", result["text"])
        self.assertIn("European Paintings", result["text"])
        self.assertIn("470,000 object ids", result["text"])

    def test_missing_query_asks_for_one(self):
        result, _ = self.turn("show me art")
        self.assertIn("What should I look for?", result["text"])

    def test_help_does_not_ask_permission(self):
        result, client = self.turn("what can you do?")
        self.assertIn("Art Institute of Chicago", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _ = self.turn("paintings by Monet", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_a_feed_failure_is_reported(self):
        result, _ = self.turn("paintings by Monet", data=FakeData(raise_error=True))
        self.assertIn("could not read the museum collections", result["text"])
        statuses = [update.get("status") for update in result["updates"]
                    if update.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", statuses)


if __name__ == "__main__":
    unittest.main()
