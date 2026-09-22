"""Tests for the air agent: feed reader, routing, skills, permissions.

    python3 agents/air/tests/test_agent.py
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

from agent import AirAgent, route  # noqa: E402
from data import (  # noqa: E402
    DATASET,
    AirQualityData,
    AirQualityError,
    aqi_band,
    pm25_band,
)

CURRENT_PAYLOAD = {
    "latitude": 28.5, "longitude": 77.25, "timezone": "UTC",
    "current": {
        "time": "2026-09-22T18:00", "interval": 3600, "pm2_5": 92.4, "pm10": 140.1,
        "us_aqi": 158, "european_aqi": 88, "ozone": 61.0, "nitrogen_dioxide": 44.2,
        "sulphur_dioxide": 12.1, "carbon_monoxide": 610.0, "uv_index": 0.15,
    },
}

HOURLY_PAYLOAD = {
    "latitude": 28.5, "longitude": 77.25, "timezone": "UTC",
    "hourly": {
        "time": [f"2026-09-22T{h:02d}:00" for h in range(24)],
        "pm2_5": [40 + h for h in range(24)],
        "pm10": [70 + h for h in range(24)],
        "us_aqi": [80, 90, 105, 120, 60, 55, 50, 45, 40, 95, 110, 130,
                   70, 65, 60, 58, 52, 48, 44, 42, 40, 38, 36, 34],
    },
}


class FakeData(AirQualityData):
    """Same interface as the real reader, no network."""

    def __init__(self, us_aqi: float = 158, raise_error: bool = False):
        self.us_aqi = us_aqi
        self.raise_error = raise_error
        self.calls: list[dict] = []

    def current(self, point: str) -> dict:
        if self.raise_error:
            raise AirQualityError("Open-Meteo is offline")
        self.calls.append({"kind": "current", "point": point})
        location = AirQualityData.check_point(point)
        reading = dict(CURRENT_PAYLOAD["current"])
        reading["us_aqi"] = self.us_aqi
        reading["pm2_5"] = 92.4 if self.us_aqi > 100 else 8.0
        return {
            "dataset": DATASET,
            "point": location,
            "time": reading["time"],
            "current": reading,
            "us_aqi": reading["us_aqi"],
            "us_aqi_band": aqi_band(reading["us_aqi"]),
            "european_aqi": reading["european_aqi"],
            "pm2_5_band": pm25_band(reading["pm2_5"]),
        }

    def hourly(self, point: str, hours: int = 24) -> dict:
        if self.raise_error:
            raise AirQualityError("Open-Meteo is offline")
        self.calls.append({"kind": "hourly", "point": point, "hours": hours})
        location = AirQualityData.check_point(point)
        rows = [{"time": stamp, "pm2_5": HOURLY_PAYLOAD["hourly"]["pm2_5"][index],
                 "pm10": HOURLY_PAYLOAD["hourly"]["pm10"][index],
                 "us_aqi": HOURLY_PAYLOAD["hourly"]["us_aqi"][index]}
                for index, stamp in enumerate(HOURLY_PAYLOAD["hourly"]["time"])][:hours]
        peak = max(rows, key=lambda row: row["pm2_5"])
        dirty = [row for row in rows if (row["us_aqi"] or 0) > 100]
        return {
            "dataset": DATASET,
            "point": location,
            "window_hours": len(rows),
            "rows": rows,
            "peak_pm2_5": peak["pm2_5"],
            "peak_pm2_5_time": peak["time"],
            "worst_aqi": max(row["us_aqi"] for row in rows),
            "hours_above_aqi_100": len(dirty),
            "first_dirty_hour": dirty[0]["time"] if dirty else None,
        }


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
    def test_aqi_bands(self):
        self.assertEqual(aqi_band(0), "Good")
        self.assertEqual(aqi_band(50), "Good")
        self.assertEqual(aqi_band(51), "Moderate")
        self.assertEqual(aqi_band(101), "Unhealthy for Sensitive Groups")
        self.assertEqual(aqi_band(158), "Unhealthy")
        self.assertEqual(aqi_band(250), "Very Unhealthy")
        self.assertEqual(aqi_band(400), "Hazardous")
        self.assertEqual(aqi_band(None), "not reported")

    def test_pm25_bands(self):
        self.assertEqual(pm25_band(8), "within the WHO guideline")
        self.assertEqual(pm25_band(20), "above the WHO guideline")
        self.assertEqual(pm25_band(92.4), "very high")
        self.assertEqual(pm25_band(None), "not reported")

    def test_points_and_places(self):
        self.assertEqual(AirQualityData.check_point("28.61,77.21"), "28.61,77.21")
        with self.assertRaises(ValueError):
            AirQualityData.check_point("28.61")
        with self.assertRaises(ValueError):
            AirQualityData.check_point("95,0")
        self.assertEqual(AirQualityData.point_from_place("Delhi"), "28.61,77.21")
        self.assertIsNone(AirQualityData.point_from_place("Atlantis"))
        self.assertEqual(AirQualityData.place_from_text("air in New York today"), "new york")
        self.assertIsNone(AirQualityData.place_from_text("air today"))

    def test_current_uses_the_providers_numbers(self):
        data = AirQualityData(fetch=lambda url, params: CURRENT_PAYLOAD)
        reading = data.current("28.61,77.21")
        self.assertEqual(reading["us_aqi"], 158)
        self.assertEqual(reading["us_aqi_band"], "Unhealthy")
        self.assertEqual(reading["current"]["pm10"], 140.1)
        self.assertEqual(reading["dataset"], DATASET)

    def test_current_rejects_a_payload_without_a_current_block(self):
        data = AirQualityData(fetch=lambda url, params: {"hourly": {}})
        with self.assertRaises(AirQualityError):
            data.current("28.61,77.21")

    def test_hourly_finds_the_peak_and_the_dirty_hours(self):
        data = AirQualityData(fetch=lambda url, params: HOURLY_PAYLOAD)
        result = data.hourly("28.61,77.21", 12)
        self.assertEqual(result["window_hours"], 12)
        self.assertEqual(result["peak_pm2_5"], 51)
        self.assertEqual(result["peak_pm2_5_time"], "2026-09-22T11:00")
        self.assertEqual(result["hours_above_aqi_100"], 4)
        self.assertEqual(result["first_dirty_hour"], "2026-09-22T02:00")
        self.assertEqual(result["worst_aqi"], 130)

    def test_hours_are_validated(self):
        with self.assertRaises(ValueError):
            AirQualityData.check_hours(0)
        with self.assertRaises(ValueError):
            AirQualityData.check_hours(100)


class RouteTests(unittest.TestCase):
    def test_now_is_the_default(self):
        self.assertEqual(route("how is the air quality in Delhi?")[0], "air-now")

    def test_hours_questions(self):
        skill, params = route("when does the air get worse today in Delhi?")
        self.assertEqual(skill, "air-hours")
        self.assertEqual(params["place"], "delhi")
        self.assertEqual(params["point"], "28.61,77.21")
        self.assertEqual(params["hours"], 24)

    def test_explicit_hour_window(self):
        skill, params = route("air quality at 40.71,-74.01 for the next 6 hours?")
        self.assertEqual(skill, "air-hours")
        self.assertEqual(params["hours"], 6)
        self.assertEqual(params["point"], "40.71,-74.01")

    def test_help(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")


class TurnTests(unittest.TestCase):
    def turn(self, text: str, data: FakeData | None = None, permission: str = "allow-once"):
        agent_conn, client_conn = connected_pair()
        agent = AirAgent(agent_conn, data or FakeData())
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_now_names_the_band_and_the_source(self):
        result, client = self.turn("how is the air quality in Delhi?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("US AQI 158 - Unhealthy", result["text"])
        self.assertIn("PM2.5 92.4 ug/m3", result["text"])
        self.assertIn(DATASET, result["text"])
        tools = [update for update in result["updates"] if update.get("sessionUpdate") == "tool_call"]
        self.assertEqual(tools[0]["name"], "air-now")
        self.assertEqual(len(client.permission_requests), 1)

    def test_streaming_is_chunked_under_one_message_id(self):
        result, _ = self.turn("how is the air quality in Delhi?")
        chunks = [update for update in result["updates"] if update.get("sessionUpdate") == "agent_message_chunk"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len({chunk["messageId"] for chunk in chunks}), 1)

    def test_hours_lists_the_peak_and_the_dirty_window(self):
        result, _ = self.turn("when does the air get worse today in Delhi?")
        self.assertIn("Peak PM2.5 in the window: 63 ug/m3 at 2026-09-22T23:00", result["text"])
        self.assertIn("4 hour(s) above AQI 100, first at 2026-09-22T02:00", result["text"])

    def test_unknown_place_is_refused_not_guessed(self):
        result, _ = self.turn("how is the air quality in Atlantis?")
        self.assertIn("I do not know the place 'atlantis'", result["text"])

    def test_unknown_place_routing(self):
        skill, params = route("how is the air quality in Atlantis?")
        self.assertEqual(skill, "air-now")
        self.assertEqual(params["place"], "atlantis")
        self.assertIsNone(route("how is the air quality right now?")[1].get("place"))

    def test_a_missing_place_asks_for_one(self):
        result, _ = self.turn("how is the air quality right now?")
        self.assertIn("I need a place", result["text"])

    def test_help_does_not_ask_permission(self):
        result, client = self.turn("what can you do?")
        self.assertIn("Open-Meteo", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _ = self.turn("air in Delhi?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_a_feed_failure_is_reported(self):
        result, _ = self.turn("air in Delhi?", data=FakeData(raise_error=True))
        self.assertIn("could not read the air quality feed", result["text"])
        statuses = [update.get("status") for update in result["updates"]
                    if update.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", statuses)

    def test_clean_air_is_called_clean(self):
        result, _ = self.turn("air in Delhi?", data=FakeData(us_aqi=32))
        self.assertIn("US AQI 32 - Good", result["text"])
        self.assertIn("within the WHO guideline", result["text"])


if __name__ == "__main__":
    unittest.main()
