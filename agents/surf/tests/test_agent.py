"""Tests for the surf agent: point resolution, wave bands, wind read, routing, live turns.

    python3 agents/surf/tests/test_agent.py

Every feed answer is injected (no network). Both Open-Meteo endpoints are served from the
fixtures below, shaped like the live payloads verified on 2026-09-22 (Santa Cruz: 1.7 m
waves, 9.5 s period, 292 degrees; wind 11.7 km/h from 281 degrees).
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

from agent import SurfAgent, route, _number  # noqa: E402
from data import (  # noqa: E402
    DATASET,
    SURF_SPOTS,
    SurfData,
    SurfError,
    compass,
    wave_band,
    wind_relation,
)

MARINE_CURRENT = {
    "timezone": "America/Los_Angeles",
    "current_units": {"wave_height": "m", "wave_period": "s", "wave_direction": "°"},
    "current": {"time": "2026-09-22T17:30", "wave_height": 1.7, "wave_direction": 292,
                "wave_period": 9.5, "swell_wave_height": 1.04, "wind_wave_height": 0.74},
}

WIND_CURRENT = {
    "timezone": "America/Los_Angeles",
    "current": {"time": "2026-09-22T17:30", "wind_speed_10m": 11.7, "wind_direction_10m": 281,
                "wind_gusts_10m": 13.7, "temperature_2m": 18.1},
}

_HOURS = [f"2026-09-22T{h:02d}:00" for h in range(24)] + [f"2026-09-23T{h:02d}:00" for h in range(24)]
_HEIGHTS = [round(0.8 + (h % 12) * 0.1, 1) for h in range(48)]

MARINE_HOURLY = {
    "timezone": "America/Los_Angeles",
    "hourly": {
        "time": _HOURS,
        "wave_height": _HEIGHTS,
        "wave_period": [9.0] * 48,
        "wave_direction": [292] * 48,
        "swell_wave_height": [0.7] * 48,
        "wind_wave_height": [0.5] * 48,
    },
}


class FakeFeed:
    """Dispatch on the Open-Meteo host; records every call so the reader can be checked."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    def __call__(self, url: str, params: dict):
        if self.fail:
            raise SurfError("Open-Meteo is offline")
        self.calls.append((url, dict(params)))
        if "marine-api" in url:
            return MARINE_HOURLY if "hourly" in params else MARINE_CURRENT
        if "api.open-meteo.com" in url:
            return WIND_CURRENT
        raise SurfError(f"unexpected url {url}")


def live_data(fail: bool = False) -> tuple[SurfData, FakeFeed]:
    feed = FakeFeed(fail=fail)
    return SurfData(fetch=feed), feed


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


class ReaderTests(unittest.TestCase):
    def test_point_resolution(self):
        self.assertEqual(SurfData.check_point("Santa Cruz"), "36.95,-122.03")
        self.assertEqual(SurfData.check_point("santa cruz"), "36.95,-122.03")
        self.assertEqual(SurfData.check_point("36.95,-122.03"), "36.95,-122.03")
        self.assertEqual(SurfData.check_point("  -8.81 , 115.09 "), "-8.81,115.09")
        with self.assertRaises(ValueError):
            SurfData.check_point("Atlantis Reef")
        with self.assertRaises(ValueError):
            SurfData.check_point("91,-200")

    def test_spot_from_text_prefers_the_longest_name(self):
        self.assertEqual(SurfData.spot_from_text("how is it at Santa Cruz today?"), "santa cruz")
        self.assertEqual(SurfData.spot_from_text("waves at Uluwatu"), "uluwatu")
        self.assertIsNone(SurfData.spot_from_text("who are you?"))
        self.assertEqual(SurfData.spot_from_text("what about 21.66,-158.05"), "21.66,-158.05")

    def test_compass_labels(self):
        self.assertEqual(compass(292), "WNW")
        self.assertEqual(compass(0), "N")
        self.assertEqual(compass(112), "ESE")
        self.assertIsNone(compass(None))

    def test_wave_bands(self):
        self.assertEqual(wave_band(0.3)[0], "flat")
        self.assertEqual(wave_band(0.6)[0], "small")
        self.assertEqual(wave_band(1.7)[0], "fun")
        self.assertEqual(wave_band(3.0)[0], "solid")
        self.assertEqual(wave_band(6.0)[0], "big")
        self.assertEqual(wave_band(None)[0], "unknown")

    def test_wind_relation_reads_the_angle_between_wind_and_swell(self):
        label, _why, difference = wind_relation(281, 292)
        self.assertEqual(label, "onshore")
        self.assertAlmostEqual(difference, 11.0)
        self.assertEqual(wind_relation(112, 292)[0], "offshore")
        self.assertEqual(wind_relation(22, 292)[0], "cross-shore")
        self.assertEqual(wind_relation(None, 292)[0], "unknown")

    def test_conditions_pull_both_feeds(self):
        data, feed = live_data()
        result = data.conditions("Santa Cruz")
        self.assertEqual(result["point"], "36.95,-122.03")
        self.assertEqual(result["waves"]["height_m"], 1.7)
        self.assertEqual(result["band"]["label"], "fun")
        self.assertEqual(result["wind_relation"]["label"], "onshore")
        self.assertEqual(result["swell_type"], "windswell")
        self.assertEqual(result["timezone"], "America/Los_Angeles")
        urls = [url for url, _params in feed.calls]
        self.assertTrue(any("marine-api" in url for url in urls))
        self.assertTrue(any("api.open-meteo.com" in url for url in urls))

    def test_forecast_builds_hourly_rows_and_ranks_them(self):
        data, _ = live_data()
        result = data.forecast("Santa Cruz", 48)
        self.assertEqual(result["hours"], 48)
        self.assertEqual(len(result["rows"]), 48)
        self.assertEqual(result["rows"][0]["time"], "2026-09-22T00:00")
        heights = [row["height_m"] for row in result["biggest"]]
        self.assertEqual(heights, sorted(heights, reverse=True))

    def test_an_offline_feed_raises_one_error_type(self):
        data, _ = live_data(fail=True)
        with self.assertRaises(SurfError):
            data.conditions("Santa Cruz")

    def test_numbers_keep_whole_values_whole(self):
        self.assertEqual(_number(11.0), "11")
        self.assertEqual(_number(292, 0), "292")
        self.assertEqual(_number(None), "not reported")
        self.assertEqual(_number(1.25, 2), "1.25")


class RouteTests(unittest.TestCase):
    def test_now_question(self):
        skill, params = route("how big are the waves at Santa Cruz right now?")
        self.assertEqual(skill, "surf-now")
        self.assertEqual(params["spot"], "santa cruz")

    def test_forecast_question(self):
        skill, params = route("when is it worth surfing at Nazare this weekend?")
        self.assertEqual(skill, "surf-forecast")
        self.assertEqual(params["spot"], "nazare")

    def test_forecast_hours_are_read(self):
        skill, params = route("what is the swell doing at Pipeline in the next 24 hours?")
        self.assertEqual(skill, "surf-forecast")
        self.assertEqual(params["hours"], 24)
        self.assertEqual(params["spot"], "pipeline")

    def test_spots_question(self):
        skill, _params = route("which spots do you know?")
        self.assertEqual(skill, "surf-spots")

    def test_coordinates_work_without_a_name(self):
        skill, params = route("how is it at 21.66,-158.05 today?")
        self.assertEqual(skill, "surf-now")
        self.assertEqual(params["spot"], "21.66,-158.05")

    def test_help(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")


class TurnTests(unittest.TestCase):
    def turn(self, text: str, fail: bool = False, permission: str = "allow-once"):
        agent_conn, client_conn = connected_pair()
        data, _ = live_data(fail=fail)
        agent = SurfAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_conditions_answer_names_the_inference(self):
        result, client = self.turn("how big are the waves at Santa Cruz right now?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("santa cruz (36.95,-122.03)", result["text"])
        self.assertIn("Waves: 1.7 m significant height (fun, waist to chest high)", result["text"])
        self.assertIn("9.5 s period from WNW (292 degrees)", result["text"])
        self.assertIn("Wind: 11.7 km/h from W (281 degrees), gusting 13.7 km/h, air 18.1 degC",
                      result["text"])
        self.assertIn("Read: onshore", result["text"])
        self.assertIn("I do not know which way this beach faces", result["text"])
        self.assertIn(DATASET, result["text"])
        self.assertEqual(len(client.permission_requests), 1)

    def test_forecast_lists_days_and_windows(self):
        result, _ = self.turn("what is the swell doing at Pipeline in the next 24 hours?")
        self.assertIn("Wave height at pipeline", result["text"])
        self.assertIn("Biggest windows in that stretch:", result["text"])
        self.assertIn("peak", result["text"])

    def test_spots_answer(self):
        result, _ = self.turn("which spots do you know?")
        self.assertIn("santa cruz", result["text"])
        self.assertIn("pipeline", result["text"])
        self.assertEqual(len(SURF_SPOTS), len(set(SURF_SPOTS)))

    def test_unknown_spot_is_refused_not_guessed(self):
        result, _ = self.turn("how big are the waves at Atlantis Reef right now?")
        self.assertIn("I do not know the spot", result["text"])

    def test_streaming_is_chunked_under_one_message_id(self):
        result, _ = self.turn("how big are the waves at Santa Cruz right now?")
        chunks = [u for u in result["updates"] if u.get("sessionUpdate") == "agent_message_chunk"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len({chunk["messageId"] for chunk in chunks}), 1)

    def test_help_does_not_ask_permission(self):
        result, client = self.turn("what can you do?")
        self.assertIn("Open-Meteo", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _ = self.turn("how big are the waves at Santa Cruz?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_a_feed_failure_is_reported(self):
        result, _ = self.turn("how big are the waves at Santa Cruz?", fail=True)
        self.assertIn("could not read the marine model", result["text"])
        statuses = [u.get("status") for u in result["updates"] if u.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", statuses)


if __name__ == "__main__":
    unittest.main()
