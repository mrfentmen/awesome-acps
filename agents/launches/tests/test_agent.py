"""Tests for the launches agent: Launch Library reader, routing, skills, permissions.

    python3 agents/launches/tests/test_agent.py
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

from agent import LaunchesAgent, days_from_text, route, search_from_text  # noqa: E402
from data import LaunchesData, LaunchesError  # noqa: E402

UPCOMING_PAYLOAD = {
    "count": 369,
    "results": [
        {"id": "63063b9d-ade9-4448-8717-6f910aa81188", "name": "Long March 8A | Unknown Payload",
         "net": "2026-09-23T13:30:00Z", "net_precision": {"name": "Minute"},
         "status": {"abbrev": "Go", "name": "Go for Launch"},
         "launch_service_provider": {"name": "China Aerospace Science and Technology Corporation"},
         "rocket": {"configuration": {"full_name": "Long March 8A", "name": "Long March 8A"}},
         "mission": {"name": "Unknown Payload", "type": "Unknown", "orbit": {"name": "Unknown"},
                     "description": "Details of the payload are unknown."},
         "pad": {"name": "Commercial LC-1",
                 "location": {"name": "Wenchang Space Launch Site, People's Republic of China"}},
         "window_start": "2026-09-23T13:30:00Z", "window_end": "2026-09-23T16:00:00Z",
         "url": "https://thespacedevs.com/launch/63063b9d"},
        {"id": "11111111-2222-3333-4444-555555555555", "name": "Starship | Starlink Group 31-1",
         "net": "2026-12-01T12:15:00Z", "status": {"abbrev": "TBD", "name": "To Be Determined"},
         "launch_service_provider": {"name": "SpaceX"},
         "rocket": {"configuration": {"full_name": "Starship"}},
         "mission": {"name": "Starlink Group 31-1", "type": "Communications",
                     "orbit": {"name": "Low Earth Orbit"}},
         "pad": {"name": "Orbital Launch Mount A", "location": {"name": "Starbase, Texas"}},
         "url": "https://thespacedevs.com/launch/11111111"},
    ],
}

PREVIOUS_PAYLOAD = {
    "count": 8200,
    "results": [
        {"id": "99999999-8888-7777-6666-555555555555", "name": "Kinetica 1 | 9 satellites",
         "net": "2026-09-20T04:03:00Z", "status": {"abbrev": "Success", "name": "Launch Successful"},
         "launch_service_provider": {"name": "CASIC"}, "rocket": {"configuration": {"full_name": "Kinetica 1"}},
         "mission": {}, "pad": {}, "url": "https://thespacedevs.com/launch/99999999"},
    ],
}

DETAIL_PAYLOAD = dict(
    UPCOMING_PAYLOAD["results"][0],
    failreason=None,
    probability=80,
    webcast_live=False,
    agency_launch_attempt_count=512,
)


class FakeData(LaunchesData):
    """Same interface as the real reader, no network."""

    def __init__(self, raise_error: bool = False):
        self.raise_error = raise_error
        self.calls: list[dict] = []

    def upcoming(self, limit: int = 5, search: str | None = None, days: int | None = None) -> dict:
        if self.raise_error:
            raise LaunchesError("Launch Library is offline")
        self.calls.append({"kind": "upcoming", "limit": limit, "search": search, "days": days})
        launches = [self._one_launch(row) for row in UPCOMING_PAYLOAD["results"]]
        if search:
            launches = [item for item in launches if search.lower() in (item["name"] or "").lower()]
        if days:
            launches = [item for item in launches if (item["net"] or "")[:10] <= "2026-09-25"]
        return {"dataset": "ll.thespacedevs.com/2.3.0/launches/upcoming",
                "count": UPCOMING_PAYLOAD["count"], "search": search, "days": days,
                "launches": launches[:limit]}

    def recent(self, limit: int = 5, search: str | None = None) -> dict:
        if self.raise_error:
            raise LaunchesError("Launch Library is offline")
        self.calls.append({"kind": "recent", "limit": limit, "search": search})
        launches = [self._one_launch(row) for row in PREVIOUS_PAYLOAD["results"]]
        if search:
            launches = [item for item in launches if search.lower() in (item["name"] or "").lower()]
        return {"dataset": "ll.thespacedevs.com/2.3.0/launches/previous",
                "count": PREVIOUS_PAYLOAD["count"], "search": search, "launches": launches[:limit]}

    def detail(self, launch_id: str) -> dict:
        if self.raise_error:
            raise LaunchesError("Launch Library is offline")
        self.calls.append({"kind": "detail", "id": launch_id})
        launch = self._one_launch(DETAIL_PAYLOAD)
        launch.update({"dataset": "ll.thespacedevs.com/2.3.0/launches",
                       "mission_description": "Details of the payload are unknown.",
                       "failreason": None, "probability": 80, "webcast_live": False,
                       "agency_launch_attempt_count": 512})
        return launch


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
    def test_limits_days_and_ids_are_validated(self):
        self.assertEqual(LaunchesData.check_limit(5), 5)
        with self.assertRaises(ValueError):
            LaunchesData.check_limit(0)
        with self.assertRaises(ValueError):
            LaunchesData.check_limit(500)
        self.assertEqual(LaunchesData.check_days(7), 7)
        with self.assertRaises(ValueError):
            LaunchesData.check_days(400)
        self.assertEqual(LaunchesData.check_id("63063B9D-ADE9-4448-8717-6F910AA81188"),
                         "63063b9d-ade9-4448-8717-6f910aa81188")
        with self.assertRaises(ValueError):
            LaunchesData.check_id("starlink")
        self.assertEqual(LaunchesData.check_search("  Starlink  "), "Starlink")
        with self.assertRaises(ValueError):
            LaunchesData.check_search("a")

    def test_upcoming_maps_the_records(self):
        data = LaunchesData(fetch=lambda url, params: UPCOMING_PAYLOAD)
        result = data.upcoming(limit=2)
        self.assertEqual(result["count"], 369)
        first = result["launches"][0]
        self.assertEqual(first["name"], "Long March 8A | Unknown Payload")
        self.assertEqual(first["net"], "2026-09-23T13:30:00Z")
        self.assertEqual(first["status"], "Go")
        self.assertEqual(first["provider"], "China Aerospace Science and Technology Corporation")
        self.assertEqual(first["rocket"], "Long March 8A")
        self.assertEqual(first["pad"], "Commercial LC-1")
        self.assertIn("Wenchang", first["location"])
        self.assertEqual(first["window_end"], "2026-09-23T16:00:00Z")
        self.assertEqual(result["launches"][1]["status"], "TBD")

    def test_a_payload_without_results_is_an_error(self):
        data = LaunchesData(fetch=lambda url, params: {"count": 0})
        with self.assertRaises(LaunchesError):
            data.upcoming()

    def test_recent_maps_the_outcome(self):
        data = LaunchesData(fetch=lambda url, params: PREVIOUS_PAYLOAD)
        result = data.recent(limit=1)
        self.assertEqual(result["launches"][0]["status"], "Success")
        self.assertEqual(result["launches"][0]["provider"], "CASIC")

    def test_detail_keeps_the_mission_description_and_the_extras(self):
        data = LaunchesData(fetch=lambda url, params: DETAIL_PAYLOAD)
        launch = data.detail("63063b9d-ade9-4448-8717-6f910aa81188")
        self.assertEqual(launch["mission_description"], "Details of the payload are unknown.")
        self.assertEqual(launch["probability"], 80)
        self.assertIsNone(launch["failreason"])
        self.assertEqual(launch["dataset"], "ll.thespacedevs.com/2.3.0/launches")


class RouteTests(unittest.TestCase):
    def test_a_uuid_routes_to_the_detail(self):
        skill, params = route("what is 63063b9d-ade9-4448-8717-6f910aa81188?")
        self.assertEqual(skill, "launches-detail")
        self.assertEqual(params["id"], "63063b9d-ade9-4448-8717-6f910aa81188")

    def test_the_next_launch(self):
        skill, params = route("what is the next launch?")
        self.assertEqual(skill, "launches-upcoming")
        self.assertIsNone(params.get("search"))

    def test_a_named_launch_becomes_a_search(self):
        skill, params = route("when is the next Starlink launch?")
        self.assertEqual(skill, "launches-upcoming")
        self.assertEqual(params["search"], "Starlink")

    def test_a_window_becomes_days(self):
        skill, params = route("what launches in the next 2 weeks?")
        self.assertEqual(skill, "launches-upcoming")
        self.assertEqual(params["days"], 14)
        self.assertEqual(days_from_text("anything in the next 3 days?"), 3)

    def test_past_launches(self):
        skill, params = route("what launched in the last few days?")
        self.assertEqual(skill, "launches-recent")
        self.assertIsNone(days_from_text("what launched in the last few days?"))

    def test_helpers(self):
        self.assertEqual(search_from_text("when is the next Starlink launch?"), "Starlink")
        self.assertEqual(search_from_text("what is the next launch?"), None)

    def test_help(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")


class TurnTests(unittest.TestCase):
    def turn(self, text: str, data: FakeData | None = None, permission: str = "allow-once"):
        agent_conn, client_conn = connected_pair()
        agent = LaunchesAgent(agent_conn, data or FakeData())
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_the_next_launch_names_the_window_and_the_pad(self):
        result, client = self.turn("what is the next launch?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("Long March 8A | Unknown Payload - 2026-09-23T13:30:00Z [Go]", result["text"])
        self.assertIn("Commercial LC-1", result["text"])
        self.assertIn("369 launch(es) on the upcoming list in total", result["text"])
        self.assertIn("ll.thespacedevs.com/2.3.0/launches/upcoming", result["text"])
        tools = [update for update in result["updates"] if update.get("sessionUpdate") == "tool_call"]
        self.assertEqual(tools[0]["name"], "launches-upcoming")
        self.assertEqual(len(client.permission_requests), 1)

    def test_streaming_is_chunked_under_one_message_id(self):
        result, _ = self.turn("what is the next launch?")
        chunks = [update for update in result["updates"] if update.get("sessionUpdate") == "agent_message_chunk"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len({chunk["messageId"] for chunk in chunks}), 1)

    def test_a_search_filters_the_list(self):
        result, _ = self.turn("when is the next Starlink launch?")
        self.assertIn("matching 'Starlink'", result["text"])
        self.assertIn("Starship | Starlink Group 31-1", result["text"])
        self.assertNotIn("Long March", result["text"])

    def test_a_window_is_reported(self):
        result, _ = self.turn("what launches in the next 2 days?")
        self.assertIn("within 2 days", result["text"])

    def test_past_launches_report_the_outcome(self):
        result, _ = self.turn("what launched in the last few days?")
        self.assertIn("Kinetica 1 | 9 satellites - 2026-09-20T04:03:00Z - Success (CASIC)", result["text"])

    def test_a_detail_read_carries_the_mission_and_the_weather_chance(self):
        result, _ = self.turn("what is 63063b9d-ade9-4448-8717-6f910aa81188?")
        self.assertIn("Vehicle Long March 8A", result["text"])
        self.assertIn("Weather probability 80%", result["text"])
        self.assertIn("Details of the payload are unknown.", result["text"])

    def test_help_does_not_ask_permission(self):
        result, client = self.turn("what can you do?")
        self.assertIn("Launch Library 2", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _ = self.turn("what is the next launch?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_a_feed_failure_is_reported(self):
        result, _ = self.turn("what is the next launch?", data=FakeData(raise_error=True))
        self.assertIn("could not read the launch schedule", result["text"])
        statuses = [update.get("status") for update in result["updates"]
                    if update.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", statuses)


if __name__ == "__main__":
    unittest.main()
