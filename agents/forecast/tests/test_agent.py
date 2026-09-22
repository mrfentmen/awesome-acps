"""Tests for the forecast agent: feed reader, rain windows, routing, permissions.

    python3 agents/forecast/tests/test_agent.py
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

from agent import ForecastAgent, route  # noqa: E402
from data import DATASET, WMO_CODES, ForecastData, ForecastError  # noqa: E402

CURRENT_PAYLOAD = {
    "latitude": 51.5, "longitude": -0.125, "timezone": "UTC",
    "current": {
        "time": "2026-09-22T18:00", "interval": 900, "temperature_2m": 14.3,
        "apparent_temperature": 13.1, "relative_humidity_2m": 78, "precipitation": 0.0,
        "weather_code": 3, "wind_speed_10m": 11.5, "wind_direction_10m": 240, "is_day": 0,
    },
}

HOURS = [f"2026-09-22T{hour:02d}:00" for hour in range(24)]
#: Dry, then a wet block 14:00-17:00 (peak 70%), then dry.
PROBABILITY = [5, 5, 5, 10, 10, 10, 5, 5, 5, 5, 20, 20, 30, 35,
               70, 65, 60, 45, 20, 10, 5, 5, 5, 5]
PRECIPITATION = [0.0] * 14 + [1.2, 0.8, 0.4, 0.2] + [0.0] * 6
TEMPERATURE = [12 + hour * 0.2 for hour in range(24)]

HOURLY_PAYLOAD = {
    "latitude": 51.5, "longitude": -0.125, "timezone": "UTC",
    "hourly": {"time": HOURS, "temperature_2m": TEMPERATURE, "precipitation": PRECIPITATION,
               "precipitation_probability": PROBABILITY, "wind_speed_10m": [10.0] * 24},
}

DAILY_PAYLOAD = {
    "latitude": 51.5, "longitude": -0.125, "timezone": "UTC",
    "daily": {
        "time": ["2026-09-22", "2026-09-23", "2026-09-24"],
        "temperature_2m_max": [18.4, 19.1, 17.2],
        "temperature_2m_min": [9.8, 10.2, 8.9],
        "precipitation_sum": [2.6, 0.0, 4.1],
        "precipitation_probability_max": [70, 10, 85],
        "wind_speed_10m_max": [22.0, 18.5, 31.0],
        "weather_code": [61, 1, 95],
    },
}


class FakeData(ForecastData):
    """Same interface as the real reader, no network."""

    def __init__(self, raise_error: bool = False):
        self.raise_error = raise_error
        self.calls: list[dict] = []

    def current(self, point: str) -> dict:
        if self.raise_error:
            raise ForecastError("Open-Meteo is offline")
        self.calls.append({"kind": "current", "point": point})
        reading = dict(CURRENT_PAYLOAD["current"])
        return {
            "dataset": DATASET,
            "point": ForecastData.check_point(point),
            "time": reading["time"],
            "current": reading,
            "weather": WMO_CODES[reading["weather_code"]],
        }

    def hourly(self, point: str, hours: int = 24) -> dict:
        if self.raise_error:
            raise ForecastError("Open-Meteo is offline")
        self.calls.append({"kind": "hourly", "point": point, "hours": hours})
        rows = [{"time": HOURS[index], "temperature_2m": TEMPERATURE[index],
                 "precipitation": PRECIPITATION[index],
                 "precipitation_probability": PROBABILITY[index], "wind_speed_10m": 10.0}
                for index in range(min(hours, 24))]
        return {"dataset": DATASET, "point": ForecastData.check_point(point),
                "window_hours": len(rows), "rows": rows}

    def daily(self, point: str, days: int = 3) -> dict:
        if self.raise_error:
            raise ForecastError("Open-Meteo is offline")
        self.calls.append({"kind": "daily", "point": point, "days": days})
        daily = DAILY_PAYLOAD["daily"]
        rows = []
        for index in range(min(days, 3)):
            row = {field: daily[field][index] for field in daily}
            row["date"] = daily["time"][index]
            row["weather"] = WMO_CODES[row["weather_code"]]
            rows.append(row)
        return {"dataset": DATASET, "point": ForecastData.check_point(point), "days": rows}


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
    def test_points_and_places(self):
        self.assertEqual(ForecastData.check_point("51.51,-0.13"), "51.51,-0.13")
        with self.assertRaises(ValueError):
            ForecastData.check_point("51.51")
        self.assertEqual(ForecastData.point_from_place("London"), "51.51,-0.13")
        self.assertIsNone(ForecastData.point_from_place("Atlantis"))
        self.assertEqual(ForecastData.place_from_text("rain in New York?"), "new york")

    def test_windows_are_validated(self):
        self.assertEqual(ForecastData.check_hours(24), 24)
        with self.assertRaises(ValueError):
            ForecastData.check_hours(0)
        with self.assertRaises(ValueError):
            ForecastData.check_hours(100)
        self.assertEqual(ForecastData.check_days(3), 3)
        with self.assertRaises(ValueError):
            ForecastData.check_days(30)

    def test_wmo_codes_have_words(self):
        self.assertEqual(WMO_CODES[0], "clear sky")
        self.assertEqual(WMO_CODES[95], "thunderstorm")
        self.assertEqual(WMO_CODES[65], "heavy rain")
        self.assertIn(61, WMO_CODES)
        self.assertIn(96, WMO_CODES)

    def test_current_reports_words_not_codes(self):
        data = ForecastData(fetch=lambda url, params: CURRENT_PAYLOAD)
        reading = data.current("51.51,-0.13")
        self.assertEqual(reading["weather"], "overcast")
        self.assertEqual(reading["current"]["temperature_2m"], 14.3)

    def test_current_rejects_a_payload_without_a_current_block(self):
        data = ForecastData(fetch=lambda url, params: {"hourly": {}})
        with self.assertRaises(ForecastError):
            data.current("51.51,-0.13")

    def test_rain_windows_group_consecutive_wet_hours(self):
        data = ForecastData(fetch=lambda url, params: HOURLY_PAYLOAD)
        hourly = data.hourly("51.51,-0.13", 24)
        windows = ForecastData.rain_windows(hourly["rows"])
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["start"], "2026-09-22T14:00")
        self.assertEqual(windows[0]["end"], "2026-09-22T17:00")
        self.assertEqual(windows[0]["hours"], 4)
        self.assertEqual(windows[0]["peak_probability"], 70)
        self.assertAlmostEqual(windows[0]["total_mm"], 2.6, places=2)

    def test_rain_windows_split_on_a_dry_hour(self):
        rows = [{"time": "t0", "precipitation_probability": 80, "precipitation": 1.0},
                {"time": "t1", "precipitation_probability": 5, "precipitation": 0.0},
                {"time": "t2", "precipitation_probability": 60, "precipitation": 0.5}]
        windows = ForecastData.rain_windows(rows)
        self.assertEqual([window["hours"] for window in windows], [1, 1])
        self.assertEqual(windows[0]["start"], "t0")
        self.assertEqual(windows[1]["start"], "t2")

    def test_a_measurable_but_unlikely_hour_still_counts(self):
        measurable = [{"time": "t0", "precipitation_probability": 10, "precipitation": 0.4}]
        barely = [{"time": "t0", "precipitation_probability": 39, "precipitation": 0.05}]
        self.assertEqual(len(ForecastData.rain_windows(measurable)), 1)
        self.assertEqual(ForecastData.rain_windows(barely), [])

    def test_the_threshold_is_validated(self):
        with self.assertRaises(ValueError):
            ForecastData.rain_windows([], threshold=0)
        with self.assertRaises(ValueError):
            ForecastData.rain_windows([], threshold=101)

    def test_daily_rows_carry_words(self):
        data = ForecastData(fetch=lambda url, params: DAILY_PAYLOAD)
        daily = data.daily("51.51,-0.13", 3)
        self.assertEqual(len(daily["days"]), 3)
        self.assertEqual(daily["days"][0]["weather"], "slight rain")
        self.assertEqual(daily["days"][2]["weather"], "thunderstorm")
        self.assertEqual(daily["days"][1]["precipitation_sum"], 0.0)


class RouteTests(unittest.TestCase):
    def test_now_is_the_default(self):
        self.assertEqual(route("what is the weather like in London?")[0], "weather-now")

    def test_rain_questions_win_over_the_day(self):
        skill, params = route("when will it rain in Seattle today?")
        self.assertEqual(skill, "weather-rain")
        self.assertEqual(params["place"], "seattle")
        self.assertEqual(params["point"], "47.61,-122.33")
        self.assertEqual(params["hours"], 24)

    def test_explicit_hour_window(self):
        skill, params = route("rain at 51.51,-0.13 over the next 6 hours?")
        self.assertEqual(skill, "weather-rain")
        self.assertEqual(params["hours"], 6)
        self.assertEqual(params["point"], "51.51,-0.13")

    def test_daily_questions(self):
        skill, params = route("what does the week look like in Nairobi?")
        self.assertEqual(skill, "weather-daily")
        self.assertEqual(params["days"], 3)

    def test_explicit_day_window(self):
        skill, params = route("forecast for the next 5 days in Tokyo?")
        self.assertEqual(skill, "weather-daily")
        self.assertEqual(params["days"], 5)

    def test_unknown_place_routing(self):
        skill, params = route("when will it rain in Narnia?")
        self.assertEqual(skill, "weather-rain")
        self.assertEqual(params["place"], "narnia")
        self.assertIsNone(route("when will it rain today?")[1].get("place"))

    def test_help(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")


class TurnTests(unittest.TestCase):
    def turn(self, text: str, data: FakeData | None = None, permission: str = "allow-once"):
        agent_conn, client_conn = connected_pair()
        agent = ForecastAgent(agent_conn, data or FakeData())
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_now_reads_out_the_conditions(self):
        result, client = self.turn("what is the weather like in London?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("Right now at 51.51,-0.13", result["text"])
        self.assertIn("overcast", result["text"])
        self.assertIn("Wind 11.5 km/h", result["text"])
        self.assertIn(DATASET, result["text"])
        tools = [update for update in result["updates"] if update.get("sessionUpdate") == "tool_call"]
        self.assertEqual(tools[0]["name"], "weather-now")
        self.assertEqual(len(client.permission_requests), 1)

    def test_streaming_is_chunked_under_one_message_id(self):
        result, _ = self.turn("what is the weather like in London?")
        chunks = [update for update in result["updates"] if update.get("sessionUpdate") == "agent_message_chunk"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len({chunk["messageId"] for chunk in chunks}), 1)

    def test_rain_names_the_hours(self):
        result, _ = self.turn("when will it rain in Seattle today?")
        self.assertIn("2026-09-22T14:00 to 2026-09-22T17:00 - 4 hour(s), peak 70% chance", result["text"])
        self.assertIn("Wettest: 2026-09-22T14:00 to 2026-09-22T17:00", result["text"])

    def test_daily_lists_every_day(self):
        result, _ = self.turn("what does the week look like in Nairobi?")
        self.assertIn("2026-09-22: slight rain, 9.8 to 18.4 C", result["text"])
        self.assertIn("2026-09-24: thunderstorm", result["text"])

    def test_unknown_place_is_refused_by_name(self):
        result, _ = self.turn("what is the weather in Narnia?")
        self.assertIn("I do not know the place 'narnia'", result["text"])

    def test_a_missing_place_asks_for_one(self):
        result, _ = self.turn("what is the weather like?")
        self.assertIn("I need a place for the forecast", result["text"])

    def test_help_does_not_ask_permission(self):
        result, client = self.turn("what can you do?")
        self.assertIn("Open-Meteo", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _ = self.turn("weather in London?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_a_feed_failure_is_reported(self):
        result, _ = self.turn("weather in London?", data=FakeData(raise_error=True))
        self.assertIn("could not read the weather feed", result["text"])
        statuses = [update.get("status") for update in result["updates"]
                    if update.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", statuses)

    def test_a_dry_window_says_so(self):
        class DryData(FakeData):
            def hourly(self, point: str, hours: int = 24) -> dict:
                result = super().hourly(point, hours)
                for row in result["rows"]:
                    row["precipitation"] = 0.0
                    row["precipitation_probability"] = 5
                return result

        result, _ = self.turn("when will it rain in Seattle?", data=DryData())
        self.assertIn("No rain is expected", result["text"])


if __name__ == "__main__":
    unittest.main()
