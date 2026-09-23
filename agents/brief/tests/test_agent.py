"""Tests for the brief agent: the section rails, routing, the permission ask and the write.

    python3 agents/brief/tests/test_agent.py

No network: the five feeds are injected as dicts shaped like the live payloads verified on
2026-09-23. The file write runs against a real temporary directory through the kit client's own
`fs/write_text_file`, so the tests prove the file lands on disk and not just that a request was
sent.
"""

from __future__ import annotations

import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for path in (str(REPO_ROOT), str(AGENT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from acp_kit import AcpClient, Connection, STOP_END_TURN, STOP_REFUSAL  # noqa: E402

from agent import FILENAME, place_from_text, route  # noqa: E402
from data import SECTIONS, BriefData, BriefError  # noqa: E402

PLACE = {"results": [{"name": "Denver", "latitude": 39.7392, "longitude": -104.9903,
                      "country": "United States", "admin1": "Colorado",
                      "country_code": "US", "timezone": "America/Denver"}]}

WEATHER = {"daily": {"temperature_2m_max": [25.4], "temperature_2m_min": [9.8],
                     "precipitation_probability_max": [20]},
           "current": {"temperature_2m": 18.5, "wind_speed_10m": 9.0}}

AIR = {"current": {"us_aqi": 45, "pm2_5": 8.5}}

ALERTS = {"features": [{"properties": {"event": "Fire Weather Warning",
                                       "areaDesc": "Denver County, CO"}}]}

QUAKES = {"type": "FeatureCollection", "features": [
    {"properties": {"mag": 5.2, "place": "somewhere in the ocean", "time": 1_700_000_000_000}}]}

STATION = {"iss_position": {"latitude": "10.5", "longitude": "-20.25"}}


class FakeFeeds:
    """Serves the five payloads and records every (url, params) pair."""

    def __init__(self, place=None, dead=(), fail_geocode=False):
        self.calls: list[tuple[str, dict]] = []
        self.place = PLACE if place is None else place
        self.dead = set(dead)
        self.fail_geocode = fail_geocode

    def __call__(self, url: str, params: dict):
        self.calls.append((url, dict(params)))
        if "geocoding" in url:
            if self.fail_geocode:
                raise BriefError("the geocoder is unreachable")
            return dict(self.place)
        for name in ("forecast", "air-quality", "alerts", "earthquake", "iss-now"):
            if name in url:
                if name in self.dead:
                    raise BriefError("the source is unreachable")
                return {"forecast": WEATHER, "air-quality": AIR, "alerts": ALERTS,
                        "earthquake": QUAKES, "iss-now": STATION}[name]
        raise AssertionError(f"unexpected url {url}")


def make_data(**kw) -> BriefData:
    return BriefData(fetch=FakeFeeds(**kw))


class SectionTests(unittest.TestCase):
    def test_every_section_renders_from_its_own_feed(self):
        briefing = make_data().briefing("Denver")
        self.assertEqual(briefing["ok"], len(SECTIONS))
        self.assertEqual([section["name"] for section in briefing["sections"]], list(SECTIONS))
        text = "\n".join(line for section in briefing["sections"] for line in section["lines"])
        self.assertIn("today's high 25.4 degC, low 9.8 degC, rain chance 20%", text)
        self.assertIn("US AQI 45 (good)", text)
        self.assertIn("Fire Weather Warning", text)
        self.assertIn("biggest M5.2", text)
        self.assertIn("10.50 deg N, 20.25 deg W", text)

    def test_one_dead_source_does_not_lose_the_others(self):
        briefing = make_data(dead=("air-quality",)).briefing("Denver")
        self.assertEqual(briefing["ok"], len(SECTIONS) - 1)
        air = [section for section in briefing["sections"] if section["name"] == "air"][0]
        self.assertIn("unavailable", air["error"])
        self.assertEqual(air["lines"], [])
        # and the briefing still knows where it is
        self.assertEqual(briefing["place"]["name"], "Denver")

    def test_every_source_dead_still_produces_a_briefing(self):
        briefing = make_data(dead=("forecast", "air-quality", "alerts", "earthquake", "iss-now")
                             ).briefing("Denver")
        self.assertEqual(briefing["ok"], 0)
        self.assertEqual(len(briefing["sections"]), len(SECTIONS))

    def test_an_unknown_place_is_refused(self):
        with self.assertRaises(ValueError):
            make_data(place={"results": []}).briefing("Xyzzyville")
        with self.assertRaises(ValueError):
            make_data().briefing("")

    def test_a_dead_geocoder_is_one_error_type(self):
        with self.assertRaises(BriefError):
            make_data(fail_geocode=True).briefing("Denver")

    def test_the_markdown_names_the_place_and_flags_what_was_missing(self):
        briefing = make_data(dead=("alerts",)).briefing("Denver")
        markdown = make_data().markdown(briefing)
        self.assertIn("# Briefing - Denver, Colorado, United States", markdown)
        self.assertIn(f"{len(SECTIONS) - 1} of {len(SECTIONS)} sections available", markdown)
        self.assertIn("unavailable", markdown)
        self.assertIn("- source: ", markdown)


class RouteTests(unittest.TestCase):
    def test_the_place_a_briefing_is_for(self):
        self.assertEqual(place_from_text("brief me on Denver"), "Denver")
        self.assertEqual(place_from_text("write a briefing for Tokyo"), "Tokyo")
        self.assertEqual(place_from_text("morning briefing for Miami"), "Miami")
        self.assertEqual(place_from_text("brief me on Denver today"), "Denver")

    def test_an_instruction_after_the_place_is_not_part_of_the_place(self):
        self.assertEqual(place_from_text("brief me on Denver, don't write a file"), "Denver")
        self.assertEqual(place_from_text("brief me on Denver, just tell me in the chat"), "Denver")
        self.assertEqual(place_from_text("brief me on Denver in the chat"), "Denver")

    def test_no_place_is_help(self):
        self.assertIsNone(place_from_text("what can you do?"))
        self.assertIsNone(place_from_text("brief me on the whole of the west coast of america"))

    def test_writing_is_the_default_and_printing_can_be_asked_for(self):
        skill, params = route("brief me on Denver")
        self.assertEqual(skill, "brief-write")
        self.assertEqual(params["place"], "Denver")
        self.assertEqual(route("write a briefing for Denver today")[0], "brief-write")
        self.assertEqual(route("brief me on Denver, just tell me in the chat")[0], "brief-print")

    def test_help(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")


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
    def turn(self, text: str, permission: str = "allow-once", cwd: str = "/tmp", **kw):
        from agent import BriefAgent

        agent_conn, client_conn = connected_pair()
        agent = BriefAgent(agent_conn, make_data(**kw))
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission,
                           fs_root=cwd if cwd != "/tmp" else None)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd=cwd)
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_the_file_is_written_into_the_workspace_after_the_ask(self):
        with tempfile.TemporaryDirectory() as workspace:
            result, client = self.turn("brief me on Denver", cwd=workspace)
            self.assertEqual(result["stopReason"], STOP_END_TURN)
            self.assertEqual(len(client.permission_requests), 1)
            self.assertEqual(len(client.written_files), 1)
            target = Path(workspace) / FILENAME
            self.assertTrue(target.is_file())
            body = target.read_text(encoding="utf-8")
            # resolve() on both sides: macOS hands out /var/... paths that are really /private/var
            self.assertEqual(Path(client.written_files[0]["path"]).resolve(), target.resolve())
            self.assertEqual(client.written_files[0]["content"], body)
            self.assertIn("# Briefing - Denver, Colorado, United States", body)
            self.assertIn("Wrote", result["text"])
            self.assertIn(f"{len(SECTIONS)} of {len(SECTIONS)} sections live", result["text"])

    def test_a_permission_refusal_writes_nothing_and_still_shows_the_briefing(self):
        with tempfile.TemporaryDirectory() as workspace:
            result, client = self.turn("brief me on Denver", permission="reject", cwd=workspace)
            self.assertEqual(result["stopReason"], STOP_REFUSAL)
            self.assertEqual(client.written_files, [])
            self.assertFalse((Path(workspace) / FILENAME).exists())
            self.assertIn("Nothing was written", result["text"])
            self.assertIn("# Briefing - Denver", result["text"])

    def test_a_client_without_the_filesystem_capability_is_told_to_print_instead(self):
        result, client = self.turn("brief me on Denver")  # no fs_root, so no write capability
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(client.written_files, [])
        self.assertIn("does not advertise fs.writeTextFile", result["text"])
        self.assertIn("# Briefing - Denver", result["text"])

    def test_asking_for_the_chat_writes_nothing(self):
        with tempfile.TemporaryDirectory() as workspace:
            result, client = self.turn("brief me on Denver, just tell me in the chat",
                                       cwd=workspace)
            self.assertEqual(client.permission_requests, [])
            self.assertEqual(client.written_files, [])
            self.assertIn("you asked for it in the chat", result["text"])
            self.assertIn("# Briefing - Denver", result["text"])

    def test_a_dead_source_is_marked_in_the_file_and_the_summary(self):
        with tempfile.TemporaryDirectory() as workspace:
            result, client = self.turn("brief me on Denver", cwd=workspace, dead=("alerts",))
            self.assertEqual(len(client.written_files), 1)
            self.assertIn(f"{len(SECTIONS) - 1} of {len(SECTIONS)} sections live", result["text"])
            self.assertIn("unavailable", client.written_files[0]["content"])

    def test_an_unknown_place_is_reported_with_no_file(self):
        with tempfile.TemporaryDirectory() as workspace:
            result, client = self.turn("brief me on Xyzzyville", cwd=workspace,
                                       place={"results": []})
            self.assertIn("could not put a briefing together", result["text"])
            self.assertIn("no place called", result["text"])
            self.assertEqual(client.written_files, [])
            self.assertFalse((Path(workspace) / FILENAME).exists())

    def test_help_does_not_read_a_feed_or_ask(self):
        result, client = self.turn("what can you do?")
        self.assertIn("ask permission to write", result["text"])
        self.assertEqual(client.permission_requests, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
