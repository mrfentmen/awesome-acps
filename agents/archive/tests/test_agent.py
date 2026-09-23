"""Tests for the archive agent: search, opening an item, routing and turns.

    python3 agents/archive/tests/test_agent.py

No network: the payloads are injected, shaped like the live ones read on 2026-09-23
("apollo 11" -> 1,626 items, Apollo11Audio with 513 openable files and 425 derivatives left out).
"""

from __future__ import annotations

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

from agent import clean_query, item_from_text, query_from_text, route  # noqa: E402
from data import (ArchiveData, ArchiveError, human_size, mediatype_from_text,  # noqa: E402
                  plain_text, playable_file)

SEARCH = {"response": {
    "numFound": 1626,
    "start": 0,
    "docs": [
        {"identifier": "Apollo1116mmOnboardFilm", "title": "APOLLO 11 16MM ONBOARD FILM",
         "mediatype": "movies", "year": "1969", "downloads": 300997,
         "creator": "NASA/Johnson Space Center"},
        {"identifier": "Apollo11Audio", "title": "Apollo 11", "mediatype": "audio",
         "downloads": 295152, "creator": ["NASA", "Johnson Space Center"]},
        {"identifier": "MSFC-6900937", "title": "Apollo 11 Moon Landing", "mediatype": "image",
         "year": "1969", "downloads": 57285},
    ]}}

METADATA = {
    "metadata": {"identifier": "Apollo11Audio", "title": "Apollo 11", "creator": "NASA",
                 "date": "2010-12-07 20:18:08", "mediatype": "audio",
                 "collection": ["nasaaudiocollection", "nasa"],
                 "subject": "NASA; Apollo 11",
                 "description": "The Apollo 11 mission. <br /><br />Digitized by NASA."},
    "files": [
        {"name": "11-03301.flac", "format": "Flac", "size": "850849280"},
        {"name": "11-03301.mp3", "format": "VBR MP3", "size": "71197824"},
        {"name": "11-03301.png", "format": "PNG", "size": "26624"},
        {"name": "Apollo11Audio_meta.xml", "format": "Metadata", "size": "4096"},
        {"name": "Apollo11Audio_archive.torrent", "format": "Archive BitTorrent", "size": "1024"},
        {"name": "__ia_thumb.jpg", "format": "Item Tile", "size": "8192"},
        {"name": "11-03301.afpk", "format": "Columbia Peaks", "size": "543056"},
    ],
}


class FakeFeeds:
    def __init__(self, search=None, metadata=None, fail=None):
        self.calls: list[str] = []
        self.queries: list[str] = []
        self.search = SEARCH if search is None else search
        self.metadata = METADATA if metadata is None else metadata
        self.fail = fail

    def __call__(self, url: str, params):
        self.calls.append(url)
        #: A search sends repeated fl[] keys, so params arrives as a list of pairs there and as a
        #: plain dict for everything else.
        pairs = params.items() if isinstance(params, dict) else (params or [])
        for key, value in pairs:
            if key == "q":
                self.queries.append(value)
        if self.fail and self.fail in url:
            raise ArchiveError("the archive is unreachable")
        if "/metadata/" in url:
            return json.dumps(self.metadata)
        if "advancedsearch" in url:
            return json.dumps(self.search)
        raise AssertionError(f"unexpected url {url}")


def make_data(**kw):
    feed = FakeFeeds(**kw)
    return ArchiveData(fetch=feed), feed


class HelperTests(unittest.TestCase):
    def test_a_mediatype_word_gives_the_filter_and_the_word(self):
        self.assertEqual(mediatype_from_text("find Apollo 11 recordings"),
                         ("audio", "recordings"))
        self.assertEqual(mediatype_from_text("how many films exist?"), ("movies", "films"))
        self.assertEqual(mediatype_from_text("old radio dramas"), ("audio", "radio"))
        self.assertEqual(mediatype_from_text("who won the game?"), (None, ""))
        self.assertEqual(mediatype_from_text("find old arcade games"), (None, ""))

    def test_sizes_are_human(self):
        self.assertEqual(human_size(850849280), "811.4 MB")
        self.assertEqual(human_size(26624), "26.0 KB")
        self.assertEqual(human_size(None), "unknown size")

    def test_html_in_a_description_is_stripped(self):
        self.assertEqual(plain_text("The mission. <br /><br />Digitized &amp; cataloged."),
                         "The mission. Digitized & cataloged.")

    def test_machinery_files_are_not_content(self):
        self.assertTrue(playable_file({"name": "11-03301.flac", "format": "Flac"}))
        self.assertTrue(playable_file({"name": "notes.pdf", "format": "Text PDF"}))
        for entry in ({"name": "x_meta.xml", "format": "Metadata"},
                      {"name": "x.torrent", "format": "Archive BitTorrent"},
                      {"name": "__ia_thumb.jpg", "format": "Item Tile"},
                      {"name": "x.afpk", "format": "Columbia Peaks"},
                      {"name": "sneaky.xml", "format": "Something New"}):
            self.assertFalse(playable_file(entry), entry)

    def test_a_query_drops_the_count_and_the_mediatype_and_the_glue(self):
        self.assertEqual(clean_query("how many films about the moon landing exist?",
                                     "films"), "moon landing")
        self.assertEqual(clean_query("top 4 books about beekeeping", "books"), "beekeeping")
        self.assertEqual(clean_query("Apollo 11 recordings", "recordings"), "Apollo 11")
        self.assertEqual(clean_query("old radio dramas", "radio"), "old radio dramas")

    def test_query_and_item_extraction(self):
        self.assertEqual(query_from_text("find Apollo 11 recordings"), "Apollo 11")
        self.assertEqual(query_from_text("is there anything about the 1918 flu in the archive?"),
                         "1918 flu")
        self.assertIsNone(query_from_text("what is in the item Apollo11Audio?"))
        self.assertEqual(item_from_text("what is in the item Apollo11Audio?"), "Apollo11Audio")
        self.assertEqual(item_from_text("list the files in MisharyRasyidPerJuz"),
                         "MisharyRasyidPerJuz")
        self.assertIsNone(item_from_text("find some films"))


class ReadTests(unittest.TestCase):
    def test_a_search_keeps_the_archive_wide_total_and_a_url(self):
        data, feed = make_data()
        found = data.search("Apollo 11", mediatype="audio", limit=3)
        self.assertEqual(found["total"], 1626)
        self.assertEqual(found["rows"][0]["identifier"], "Apollo1116mmOnboardFilm")
        self.assertEqual(found["rows"][0]["url"],
                         "https://archive.org/details/Apollo1116mmOnboardFilm")
        self.assertEqual(found["rows"][1]["creator"], "NASA")  # a list collapses to its first
        self.assertIn("title:(Apollo 11)", feed.queries[-1])
        self.assertIn("mediatype:(audio)", feed.queries[-1])
        #: A description fallback as well as the title, so a hit whose title does not say
        #: "Apollo 11" (very common in the archive) still comes back.
        self.assertIn("description:(Apollo 11)", feed.queries[-1])

    def test_a_search_with_nothing_matching_is_an_error(self):
        data, _feed = make_data(search={"response": {"numFound": 0, "docs": []}})
        with self.assertRaises(ArchiveError):
            data.search("nothing at all")
        data, _feed = make_data()
        with self.assertRaises(ValueError):
            data.search("   ")

    def test_an_item_lists_openable_files_and_says_what_it_left_out(self):
        data, _feed = make_data()
        item = data.item("Apollo11Audio", limit=5)
        self.assertEqual(item["title"], "Apollo 11")
        self.assertEqual(item["creator"], "NASA")
        self.assertEqual(item["date"], "2010-12-07")  # date only, not the timestamp
        self.assertEqual(item["description"], "The Apollo 11 mission. Digitized by NASA.")
        self.assertEqual([row["name"] for row in item["rows"]],
                         ["11-03301.flac", "11-03301.mp3", "11-03301.png"])
        self.assertEqual(item["files_kept"], 3)
        self.assertEqual(item["files_skipped"], 4)
        self.assertEqual(item["files_total"], 7)
        self.assertEqual(item["total_size_text"], "879.4 MB")

    def test_a_details_url_works_as_an_identifier(self):
        data, feed = make_data()
        item = data.item("https://archive.org/details/Apollo11Audio")
        self.assertEqual(item["identifier"], "Apollo11Audio")
        self.assertTrue(feed.calls[-1].endswith("/metadata/Apollo11Audio"))

    def test_a_missing_item_is_an_error(self):
        data, _feed = make_data(metadata={"metadata": {}, "files": []})
        with self.assertRaises(ArchiveError):
            data.item("no-such-item")
        data, _feed = make_data()
        with self.assertRaises(ValueError):
            data.item("  ")

    def test_a_dead_archive(self):
        data, _feed = make_data(fail="advancedsearch")
        with self.assertRaises(ArchiveError):
            data.search("Apollo 11")
        data, _feed = make_data(fail="/metadata/")
        with self.assertRaises(ArchiveError):
            data.item("Apollo11Audio")


class RouteTests(unittest.TestCase):
    def test_finding_recordings_keeps_the_11_and_sets_the_filter(self):
        skill, params = route("find Apollo 11 recordings")
        self.assertEqual(skill, "archive-search")
        self.assertEqual(params, {"query": "Apollo 11", "mediatype": "audio"})

    def test_counting_asks_for_a_few(self):
        skill, params = route("how many films about the moon landing exist?")
        self.assertEqual(skill, "archive-search")
        self.assertEqual(params["query"], "moon landing")
        self.assertEqual(params["mediatype"], "movies")
        self.assertEqual(params["limit"], 3)

    def test_a_bare_noun_search(self):
        skill, params = route("top 4 books about beekeeping")
        self.assertEqual(skill, "archive-search")
        self.assertEqual(params, {"query": "beekeeping", "mediatype": "texts", "limit": 4})

    def test_an_item_question(self):
        skill, params = route("what is in the item Apollo11Audio?")
        self.assertEqual(skill, "archive-item")
        self.assertEqual(params, {"identifier": "Apollo11Audio"})
        skill, params = route("list 5 files in Apollo11Audio")
        self.assertEqual((skill, params["limit"]), ("archive-item", 5))

    def test_help_and_nonsense(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")
        self.assertEqual(route("asdfqwer zxcv")[0], "help")


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
        from agent import ArchiveAgent

        agent_conn, client_conn = connected_pair()
        data, feed = make_data(**kw)
        agent = ArchiveAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client, feed
        finally:
            client.stop()

    def test_a_search_answer_names_the_total_and_the_hits(self):
        result, client, _ = self.turn("find Apollo 11 recordings")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertEqual(len(client.permission_requests), 1)
        self.assertIn("1,626 item(s) match 'Apollo 11' (audio)", result["text"])
        self.assertIn("300,997 downloads", result["text"])
        self.assertIn("Apollo1116mmOnboardFilm", result["text"])
        self.assertIn("I showed the 3 most downloaded of 1,626", result["text"])

    def test_an_item_answer_lists_files_and_what_was_skipped(self):
        result, _, _ = self.turn("what is in the item Apollo11Audio?")
        self.assertIn("files: 3 you can open, 879.4 MB in total", result["text"])
        self.assertIn("11-03301.flac  [Flac] 811.4 MB", result["text"])
        self.assertIn("left out 4 derivative file(s)", result["text"])
        self.assertIn("https://archive.org/details/Apollo11Audio", result["text"])

    def test_help_asks_nothing(self):
        result, client, feed = self.turn("what can you do?")
        self.assertIn("Internet Archive", result["text"])
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(feed.calls, [])

    def test_permission_denied(self):
        result, _, _ = self.turn("find Apollo 11 recordings", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)

    def test_a_dead_archive(self):
        result, _, _ = self.turn("find Apollo 11 recordings", fail="advancedsearch")
        self.assertIn("I could not read the archive", result["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
