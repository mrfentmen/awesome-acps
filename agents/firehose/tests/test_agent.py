"""Tests for the firehose agent: the SSE reader, the filters, routing and the pushed turn.

    python3 agents/firehose/tests/test_agent.py

No network: the stream is injected as a list of already-decoded SSE lines, shaped like the ones
Wikimedia actually sends (verified live on 2026-09-23). The point of these tests is that the
agent can only ever *push* what the reader yields, so the reader's filtering and its "how many
did I see versus show" bookkeeping are tested directly.
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

from agent import render_event, route  # noqa: E402
from data import (  # noqa: E402
    MAX_EVENTS,
    FirehoseData,
    FirehoseError,
    matches,
    normalise,
    parse_sse_line,
)


def recent_change(title="Ada Lovelace", user="Editor", bot=False, kind="edit", minor=False,
                  server="en.wikipedia.org", wiki="enwiki", comment="fixed a typo",
                  old=100, new=180):
    """One raw recentchange object, like the stream sends."""
    return {
        "title": title, "user": user, "bot": bot, "type": kind, "minor": minor,
        "server_name": server, "wiki": wiki, "comment": comment,
        "revision": {"old": 555, "new": 556}, "length": {"old": old, "new": new},
        "id": 42, "title_url": f"https://{server}/wiki/{title.replace(' ', '_')}",
    }


def sse(event=None, **kw):
    """One line of the stream. No argument means a keep-alive comment line."""
    if event is None:
        return ": keep-alive\n"
    return "data: " + json.dumps(event if event is not None else recent_change(**kw)) + "\n"


def stream_of(*events):
    """Lines for a handful of events, with the comment/blank noise the real stream has."""
    lines = [": ok\n", "\n"]
    for event in events:
        lines.append(sse(event))
        lines.append("id: 1\n")
    return lines


class FakeStream:
    """Stands in for FirehoseData: records the filters and yields fixed events."""

    def __init__(self, events=(), fail: str | None = None, seen: int | None = None):
        self.rows = [normalise(event) for event in events]
        self.fail = fail
        self.total_seen = len(self.rows) if seen is None else seen
        self.calls: list[dict] = []
        self.stats = {"seen": self.total_seen, "matched": 0, "stopped": "count"}

    def events(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise FirehoseError(self.fail)
        for index, row in enumerate(self.rows):
            self.stats["matched"] = index + 1
            yield row


class ParseTests(unittest.TestCase):
    def test_a_data_line_decodes_to_an_object(self):
        line = sse(recent_change(title="Cat"))
        self.assertEqual(parse_sse_line(line)["title"], "Cat")

    def test_everything_that_is_not_a_data_line_is_ignored(self):
        self.assertIsNone(parse_sse_line(": keep-alive"))
        self.assertIsNone(parse_sse_line(""))
        self.assertIsNone(parse_sse_line("id: 5"))
        self.assertIsNone(parse_sse_line("data: "))
        self.assertIsNone(parse_sse_line("data: not json"))
        self.assertIsNone(parse_sse_line("data: [1, 2]"))  # a list, not an event object

    def test_normalise_keeps_the_fields_a_reader_needs(self):
        row = normalise(recent_change(title="Cat", user="Ann", old=100, new=180))
        self.assertEqual(row["title"], "Cat")
        self.assertEqual(row["user"], "Ann")
        self.assertEqual(row["delta"], 80)
        self.assertEqual(row["server"], "en.wikipedia.org")
        self.assertEqual(row["revision"], 556)
        self.assertIn("/wiki/Cat", row["url"])

    def test_normalise_survives_a_missing_length(self):
        event = recent_change()
        del event["length"]
        self.assertIsNone(normalise(event)["delta"])
        self.assertEqual(normalise({})["title"], "(untitled)")
        self.assertEqual(normalise({})["user"], "(unknown)")


class FilterTests(unittest.TestCase):
    def test_bots_are_excluded_by_default_and_kept_on_request(self):
        bot = normalise(recent_change(bot=True))
        self.assertFalse(matches(bot))
        self.assertTrue(matches(bot, skip_bots=False))

    def test_only_new_keeps_page_creations(self):
        edit = normalise(recent_change(kind="edit"))
        created = normalise(recent_change(kind="new"))
        self.assertFalse(matches(edit, only_new=True))
        self.assertTrue(matches(created, only_new=True))
        self.assertTrue(matches(edit, only_new=False))

    def test_a_size_floor_ignores_small_edits_and_unknown_sizes(self):
        small = normalise(recent_change(old=100, new=120))
        big = normalise(recent_change(old=100, new=6000))
        unknown = recent_change()
        del unknown["length"]
        self.assertFalse(matches(small, min_delta=1000))
        self.assertTrue(matches(big, min_delta=1000))
        self.assertFalse(matches(normalise(unknown), min_delta=1000))

    def test_a_language_filter_reads_the_server_name(self):
        german = normalise(recent_change(server="de.wikipedia.org", wiki="dewiki"))
        english = normalise(recent_change())
        self.assertTrue(matches(german, language="de"))
        self.assertFalse(matches(english, language="de"))
        self.assertTrue(matches(german, language="dewiki"))  # both spellings are accepted

    def test_a_wiki_filter_is_exact(self):
        german = normalise(recent_change(server="de.wikipedia.org", wiki="dewiki"))
        self.assertTrue(matches(german, wiki="dewiki"))
        self.assertFalse(matches(german, wiki="enwiki"))


class ReaderTests(unittest.TestCase):
    def test_the_reader_yields_only_matching_events_and_counts_the_rest(self):
        lines = stream_of(recent_change(title="One"), recent_change(title="Bot", bot=True),
                          recent_change(title="Two"))
        data = FirehoseData(fetch=lambda url, timeout: lines)
        rows = list(data.events(count=5, seconds=5))
        self.assertEqual([row["title"] for row in rows], ["One", "Two"])
        self.assertEqual(data.stats["seen"], 3)
        self.assertEqual(data.stats["matched"], 2)

    def test_the_count_stops_the_stream(self):
        lines = stream_of(*[recent_change(title=f"Page {index}") for index in range(10)])
        data = FirehoseData(fetch=lambda url, timeout: lines)
        rows = list(data.events(count=3, seconds=5))
        self.assertEqual(len(rows), 3)
        self.assertEqual(data.stats["stopped"], "count")

    def test_the_count_can_never_exceed_the_hard_cap(self):
        lines = stream_of(*[recent_change(title=f"Page {index}") for index in range(MAX_EVENTS + 10)])
        data = FirehoseData(fetch=lambda url, timeout: lines)
        rows = list(data.events(count=10_000, seconds=5))
        self.assertEqual(len(rows), MAX_EVENTS)

    def test_a_dead_stream_is_one_error_type(self):
        def boom(url, timeout):
            raise FirehoseError("could not connect")

        with self.assertRaises(FirehoseError):
            list(FirehoseData(fetch=boom).events(count=1, seconds=1))

    def test_a_stream_that_breaks_mid_read_is_reported(self):
        def half(url, timeout):
            yield sse(recent_change(title="One"))
            raise OSError("connection reset")

        with self.assertRaises(FirehoseError):
            list(FirehoseData(fetch=half).events(count=5, seconds=5))


class RouteTests(unittest.TestCase):
    def test_a_count_and_a_plain_request(self):
        skill, params = route("watch the wiki firehose for 10 edits")
        self.assertEqual(skill, "firehose-edits")
        self.assertEqual(params["count"], 10)
        self.assertTrue(params["skip_bots"])

    def test_seconds_and_minutes(self):
        self.assertEqual(route("live edits for 20 seconds")[1]["seconds"], 20.0)
        self.assertEqual(route("live edits for 2 minutes")[1]["seconds"], 120.0)

    def test_new_pages_only(self):
        skill, params = route("live edits that are new articles")
        self.assertEqual(skill, "firehose-edits")
        self.assertTrue(params["only_new"])

    def test_a_size_floor_in_bytes_and_kilobytes(self):
        self.assertEqual(route("edits over 5000 bytes")[1]["min_delta"], 5000)
        self.assertEqual(route("edits over 5 kb")[1]["min_delta"], 5000)
        self.assertEqual(route("big changes on wikipedia")[1]["min_delta"], 1000)

    def test_bots_can_be_asked_for(self):
        self.assertFalse(route("live edits including bots")[1]["skip_bots"])
        self.assertTrue(route("live edits")[1]["skip_bots"])

    def test_a_language_or_a_full_wiki_name(self):
        self.assertEqual(route("live edits on de.wikipedia.org")[1]["wiki"], "dewiki")
        self.assertEqual(route("live edits on de.wikipedia.org")[1]["language"], "de")
        self.assertEqual(route("live edits on de")[1]["language"], "de")

    def test_a_word_that_looks_like_a_language_is_not_one(self):
        params = route("watch for new articles")[1]
        self.assertNotIn("language", params)

    def test_help_and_nonsense(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")
        self.assertEqual(route("book me a flight to Rome")[0], "help")


class RenderTests(unittest.TestCase):
    def test_an_edit_reads_as_one_line_with_the_author(self):
        line = render_event(1, normalise(recent_change(title="Cat", user="Ann", old=100, new=180)))
        self.assertIn("1. en.wikipedia.org - Cat (edit, +80 bytes) by Ann", line)
        self.assertIn('"fixed a typo"', line)
        self.assertIn("/wiki/Cat", line)

    def test_a_new_page_and_a_bot_are_labelled(self):
        line = render_event(2, normalise(recent_change(kind="new", bot=True)))
        self.assertIn("NEW PAGE [bot]", line)

    def test_a_minor_and_a_negative_change(self):
        line = render_event(3, normalise(recent_change(minor=True, old=500, new=100)))
        self.assertIn("(minor edit, -400 bytes)", line)

    def test_an_unknown_size_is_said_not_guessed(self):
        event = recent_change()
        del event["length"]
        self.assertIn("size unknown", render_event(4, normalise(event)))

    def test_a_long_comment_is_cut_and_flattened(self):
        event = recent_change(comment="line one\n" + "x" * 400)
        line = render_event(5, normalise(event))
        self.assertIn('"line one x', line)
        self.assertNotIn("\n\n", line.split('"')[1])


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
        from agent import FirehoseAgent

        agent_conn, client_conn = connected_pair()
        data = FakeStream(**kw)
        agent = FirehoseAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client, data
        finally:
            client.stop()

    def test_every_event_is_pushed_as_its_own_message(self):
        result, client, _ = self.turn(
            "watch the wiki firehose for 2 edits",
            events=[recent_change(title="One"), recent_change(title="Two")])
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        chunks = [update for update in result["updates"]
                  if update.get("sessionUpdate") == "agent_message_chunk"]
        self.assertEqual(len(chunks), 4)  # the header, two pushed events, and the summary
        self.assertIn("One", chunks[1]["content"]["text"])
        self.assertIn("Two", chunks[2]["content"]["text"])
        self.assertIn("Streamed 2 event(s) from 2 seen", result["text"])
        self.assertEqual(len(client.permission_requests), 1)

    def test_the_filters_the_reader_was_given_are_the_ones_asked_for(self):
        _result, _client, data = self.turn(
            "watch the firehose for 1 edit on de.wikipedia.org at least 500 bytes including bots",
            events=[recent_change(server="de.wikipedia.org", wiki="dewiki", old=1, new=900)])
        call = data.calls[0]
        self.assertEqual(call["count"], 1)
        self.assertEqual(call["wiki"], "dewiki")
        self.assertEqual(call["language"], "de")
        self.assertEqual(call["min_delta"], 500)
        self.assertFalse(call["skip_bots"])

    def test_a_quiet_result_says_the_stream_was_busy_not_quiet(self):
        result, _, _ = self.turn("watch the firehose for 3 edits", events=[],
                                 seen=240)
        self.assertIn("No events matched", result["text"])
        self.assertIn("not a quiet wiki", result["text"])

    def test_help_does_not_ask_permission(self):
        result, client, data = self.turn("what can you do?")
        self.assertIn("push stream", result["text"])
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(data.calls, [])

    def test_permission_denied_streams_nothing(self):
        result, _, data = self.turn("watch the firehose for 5 edits",
                                    permission="reject", events=[recent_change()])
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])
        self.assertEqual(data.calls, [])

    def test_a_dead_stream_is_reported_once(self):
        result, _, _ = self.turn("watch the firehose for 2 edits", fail="could not connect")
        self.assertIn("I could not read the stream", result["text"])
        self.assertIn("could not connect", result["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
