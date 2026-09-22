"""Tests for the iss agent: position reader, sky reader, routing, permissions.

    python3 agents/iss/tests/test_agent.py
"""

from __future__ import annotations

import queue
import sys
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for path in (str(REPO_ROOT), str(AGENT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from acp_kit import AcpClient, Connection, STOP_END_TURN, STOP_REFUSAL  # noqa: E402

from agent import IssAgent, band, route  # noqa: E402
from data import (  # noqa: E402
    DATASET_CLOUD,
    DATASET_ISS,
    DATASET_SUN,
    IssData,
    IssError,
    distance_km,
)

POSITION_PAYLOAD = {
    "timestamp": 1790107575,
    "message": "success",
    "iss_position": {"latitude": "-24.2244", "longitude": "-148.5018"},
}

SUN_PAYLOAD = {"results": {
    "sunrise": "2026-09-22T12:50:00+00:00",
    "sunset": "2026-09-23T01:05:00+00:00",
    "solar_noon": "2026-09-22T18:58:00+00:00",
    "day_length": "43488",
}}

CLOUD_PAYLOAD = {"current": {"time": "2026-09-22T21:00", "cloud_cover": 80}}


def dispatch(url: str, params: dict):
    """One fetch for the reader tests: answer by which feed was asked for."""
    if "open-notify" in url:
        return POSITION_PAYLOAD
    if "sunrise-sunset" in url:
        return SUN_PAYLOAD
    if "open-meteo" in url:
        return CLOUD_PAYLOAD
    raise AssertionError(f"unexpected url {url}")


class FakeData(IssData):
    """Same interface as the real reader, no network."""

    def __init__(self, raise_error: bool = False):
        self.raise_error = raise_error
        self.calls: list[dict] = []

    def position(self) -> dict:
        if self.raise_error:
            raise IssError("open-notify is offline")
        self.calls.append({"kind": "position"})
        return {"dataset": DATASET_ISS, "latitude": -24.2244, "longitude": -148.5018,
                "point": "-24.2244,-148.5018", "timestamp": 1790107575}

    def sky(self, point: str, now=None) -> dict:
        if self.raise_error:
            raise IssError("the sky feeds are offline")
        self.calls.append({"kind": "sky", "point": point})
        return {"dataset": DATASET_SUN, "point": point,
                "sunrise": "2026-09-22T12:50:00+00:00", "sunset": "2026-09-23T01:05:00+00:00",
                "solar_noon": "2026-09-22T18:58:00+00:00", "cloud_cover": 80,
                "cloud_dataset": DATASET_CLOUD, "cloud_time": "2026-09-22T21:00",
                "evaluated_at": "2026-09-22T21:00:00Z", "dark": True}


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
    def test_distance_between_points(self):
        self.assertEqual(distance_km("40.71,-74.01", "40.71,-74.01"), 0.0)
        gap = distance_km("40.71,-74.01", "51.51,-0.13")
        self.assertGreater(gap, 5500)
        self.assertLess(gap, 5650)

    def test_position_reads_the_feed(self):
        data = IssData(fetch=dispatch)
        position = data.position()
        self.assertEqual(position["latitude"], -24.2244)
        self.assertEqual(position["point"], "-24.2244,-148.5018")
        self.assertEqual(position["timestamp"], 1790107575)

    def test_position_rejects_a_bad_payload(self):
        data = IssData(fetch=lambda url, params: {"message": "success"})
        with self.assertRaises(IssError):
            data.position()

    def test_sky_is_daytime_before_sunset(self):
        data = IssData(fetch=dispatch)
        sky = data.sky("39.74,-104.99", now=datetime(2026, 9, 22, 21, 0, tzinfo=timezone.utc))
        self.assertFalse(sky["dark"])
        self.assertEqual(sky["cloud_cover"], 80)
        self.assertEqual(sky["sunset"], "2026-09-23T01:05:00+00:00")

    def test_sky_is_dark_after_sunset(self):
        data = IssData(fetch=dispatch)
        sky = data.sky("39.74,-104.99", now=datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc))
        self.assertTrue(sky["dark"])

    def test_sky_rejects_a_bad_payload(self):
        data = IssData(fetch=lambda url, params: {"results": {}} if "sunrise-sunset" in url
                       else {"current": {"cloud_cover": 10}})
        with self.assertRaises(IssError):
            data.sky("39.74,-104.99")

    def test_bands_are_labelled(self):
        self.assertIn("overhead", band(100))
        self.assertIn("your part of the sky", band(1500))
        self.assertIn("far side", band(9000))


class RouteTests(unittest.TestCase):
    def test_help(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")

    def test_position_default(self):
        skill, params = route("where is the ISS right now?")
        self.assertEqual(skill, "iss-now")
        self.assertEqual(params, {})

    def test_sky_question_with_a_city(self):
        skill, params = route("could I see the ISS from Denver tonight?")
        self.assertEqual(skill, "iss-sky")
        self.assertEqual(params["place"], "denver")
        self.assertEqual(params["point"], "39.74,-104.99")

    def test_sky_question_with_a_point(self):
        skill, params = route("is the ISS visible from 40.71,-74.01?")
        self.assertEqual(skill, "iss-sky")
        self.assertEqual(params["point"], "40.71,-74.01")

    def test_distance_question_with_a_point_is_position(self):
        skill, params = route("how far is the ISS from 40.71,-74.01?")
        self.assertEqual(skill, "iss-now")
        self.assertEqual(params["point"], "40.71,-74.01")


class TurnTests(unittest.TestCase):
    def turn(self, text: str, data: FakeData | None = None, permission: str = "allow-once"):
        agent_conn, client_conn = connected_pair()
        agent = IssAgent(agent_conn, data or FakeData())
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_position_names_the_ground_point(self):
        result, client = self.turn("where is the ISS right now?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("The ISS is at -24.2244 lat, -148.5018 lon", result["text"])
        self.assertIn(DATASET_ISS, result["text"])
        tools = [update for update in result["updates"] if update.get("sessionUpdate") == "tool_call"]
        self.assertEqual(tools[0]["name"], "iss-now")
        self.assertEqual(len(client.permission_requests), 1)

    def test_distance_from_a_point(self):
        result, _ = self.turn("how far is the ISS from 40.71,-74.01?")
        self.assertIn("From 40.71,-74.01 it is about", result["text"])
        self.assertIn("far side of the planet", result["text"])

    def test_sky_check_reports_the_conditions(self):
        result, _ = self.turn("could I see the ISS from Denver tonight?")
        self.assertIn("Sky check for 39.74,-104.99", result["text"])
        self.assertIn("cloud cover now 80%", result["text"])
        self.assertIn("it is dark where you are", result["text"])
        self.assertIn("clouds are heavy enough", result["text"])
        self.assertIn("not a pass forecast", result["text"])

    def test_help_does_not_ask_permission(self):
        result, client = self.turn("what can you do?")
        self.assertIn("do not predict passes", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _ = self.turn("where is the ISS right now?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_a_feed_failure_is_reported(self):
        result, _ = self.turn("where is the ISS right now?", data=FakeData(raise_error=True))
        self.assertIn("could not read the space-station feeds", result["text"])
        statuses = [update.get("status") for update in result["updates"]
                    if update.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", statuses)


if __name__ == "__main__":
    unittest.main()
