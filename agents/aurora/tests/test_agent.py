"""Tests for the aurora agent: SWPC reader, routing, skills, permissions.

    python3 agents/aurora/tests/test_agent.py
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

from agent import AuroraAgent, place_from_text, route  # noqa: E402
from data import (  # noqa: E402
    CITY_COORDS,
    DATASET_ALERTS,
    DATASET_FORECAST,
    DATASET_KP_1M,
    DATASET_OVATION,
    SpaceWeatherData,
    SpaceWeatherError,
    is_storm,
    kp_band,
    parse_message,
)

WATCH_TEXT = (
    "Space Weather Message Code: WATA20\r\n"
    "Serial Number: 1126\r\n"
    "Issue Time: 2026 Sep 21 1832 UTC\r\n"
    "\r\n"
    "WATCH: Geomagnetic Storm Category G1 Predicted \r\n"
    "Highest Storm Level Predicted by Day:\r\n"
    "Sep 22:  None (Below G1)   Sep 23:  None (Below G1)   Sep 24:  G1 (Minor)   \r\n"
)

#: Shapes copied from the live SWPC products on 2026-09-22.
RAW_ALERTS = [
    {"product_id": "ALTEF3", "issue_datetime": "2026-09-16 09:00:00.000",
     "message": "Space Weather Message Code: ALTEF3\r\nSerial Number: 3700\r\nIssue Time: 2026 Sep 16 0900 UTC\r\n"
                "\r\nCONTINUED ALERT: Electron 2MeV Integral Flux exceeded 1,000pfu\r\n"},
    {"product_id": "A20F", "issue_datetime": "2026-09-21 18:32:26.023", "message": WATCH_TEXT},
]

KP_NOW = {
    "dataset": DATASET_KP_1M,
    "time_tag": "2026-09-22T17:53:00",
    "estimated_kp": 0.33,
    "band": "quiet",
    "storm": False,
    "three_hourly": {"dataset": "swpc.noaa.gov/planetary-k-index", "time_tag": "2026-09-22T12:00:00",
                     "kp": 0.33, "a_running": 2, "station_count": 8},
}

RECENT = {
    "now": dict(KP_NOW),
    "window_hours": 24,
    "peak_kp": 2.0,
    "peak_band": "quiet",
    "peak_time_tag": "2026-09-22T00:00:00",
    "rows": [],
    "dataset": "swpc.noaa.gov/planetary-k-index",
}

FORECAST = {
    "dataset": DATASET_FORECAST,
    "days": [
        {"date": "2026-09-23", "max_kp": 4.33, "band": "active", "storm": False,
         "rows": [{"time_tag": "2026-09-23T03:00:00", "kp": 4.33}]},
        {"date": "2026-09-24", "max_kp": 5.33, "band": "G1 (minor)", "storm": True,
         "rows": [{"time_tag": "2026-09-24T00:00:00", "kp": 5.33}]},
    ],
    "predicted_rows": 16,
    "observed_rows": 65,
    "peak_kp": 5.33,
    "peak_band": "G1 (minor)",
}

PROBABILITY = {
    "dataset": DATASET_OVATION,
    "observation_time": "2026-09-22T17:49:00Z",
    "forecast_time": "2026-09-22T19:20:00Z",
    "probability": 5,
    "nearest_cell": {"latitude": 65, "longitude": 212},
    "radius_degrees": 2.0,
    "max_probability_nearby": 7,
    "cells_read": 65160,
}


def alerts_payload(alerts, limit: int, contains: str | None = None) -> dict:
    items = [item for item in (parse_message(row) for row in alerts) if item]
    items.sort(key=lambda item: str(item["issue_datetime"]), reverse=True)
    if contains:
        needle = contains.lower()
        items = [item for item in items
                 if needle in item["text"].lower() or needle in (item["headline"] or "").lower()]
    return {"dataset": DATASET_ALERTS, "count": len(items), "messages": items[:limit]}


class FakeData(SpaceWeatherData):
    """Same interface as the real reader, no network."""

    def __init__(self, kp: float = 0.33, alerts=None, raise_error: bool = False):
        self.kp = kp
        self.alerts = list(alerts if alerts is not None else RAW_ALERTS)
        self.raise_error = raise_error
        self.calls: list[dict] = []

    def kp_now(self) -> dict:
        if self.raise_error:
            raise SpaceWeatherError("SWPC is offline")
        self.calls.append({"kind": "kp_now"})
        payload = dict(KP_NOW)
        payload["estimated_kp"] = self.kp
        payload["band"] = kp_band(self.kp)
        payload["storm"] = is_storm(self.kp)
        payload["three_hourly"] = dict(KP_NOW["three_hourly"])
        return payload

    def recent(self, hours: int = 24) -> dict:
        self.calls.append({"kind": "recent", "hours": hours})
        payload = dict(RECENT)
        payload["now"] = self.kp_now()
        payload["peak_kp"] = 6.0 if is_storm(self.kp) else 2.0
        payload["peak_band"] = kp_band(payload["peak_kp"])
        return payload

    def forecast(self, days: int = 3) -> dict:
        self.calls.append({"kind": "forecast", "days": days})
        return dict(FORECAST, days=FORECAST["days"][:days])

    def messages(self, limit: int = 5, contains: str | None = None) -> dict:
        self.calls.append({"kind": "messages", "limit": limit, "contains": contains})
        return alerts_payload(self.alerts, limit, contains)

    def aurora_probability(self, lat: float, lon: float, radius: float = 2.0) -> dict:
        self.calls.append({"kind": "probability", "lat": lat, "lon": lon})
        return dict(PROBABILITY)


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
    def test_check_point_and_city(self):
        self.assertEqual(SpaceWeatherData.check_point("64.84, -147.72"), (64.84, -147.72))
        with self.assertRaises(ValueError):
            SpaceWeatherData.check_point("64.84")
        with self.assertRaises(ValueError):
            SpaceWeatherData.check_point("91,-74")
        self.assertEqual(SpaceWeatherData.city("Fairbanks"), (64.84, -147.72, "Fairbanks"))
        self.assertIsNone(SpaceWeatherData.city("Atlantis"))
        self.assertEqual(len(CITY_COORDS), len(set(CITY_COORDS)))

    def test_bands_and_storm_scale(self):
        self.assertEqual(kp_band(0.33), "quiet")
        self.assertEqual(kp_band(3.0), "unsettled")
        self.assertEqual(kp_band(4.0), "active")
        self.assertEqual(kp_band(5.0), "G1 (minor)")
        self.assertEqual(kp_band(6.67), "G3 (strong)")
        self.assertTrue(is_storm(5.0))
        self.assertFalse(is_storm(4.67))

    def test_parse_message_reads_fields(self):
        parsed = parse_message({"product_id": "A20F", "issue_datetime": "2026-09-21 18:32:26.023",
                                "message": WATCH_TEXT})
        self.assertEqual(parsed["code"], "WATA20")
        self.assertEqual(parsed["serial"], "1126")
        self.assertEqual(parsed["kind"], "WATCH")
        self.assertEqual(parsed["g_scale"], "G1")
        self.assertIn("Geomagnetic Storm Category G1 Predicted", parsed["headline"])

    def test_extended_warning_wins_over_warning(self):
        text = ("Space Weather Message Code: WARK04\r\nSerial Number: 5418\r\n"
                "Issue Time: 2026 Sep 16 0538 UTC\r\n\r\nEXTENDED WARNING: Geomagnetic K-index of 4 expected\r\n")
        parsed = parse_message({"product_id": "WARK04", "issue_datetime": "2026-09-16 05:38:03.320",
                                "message": text})
        self.assertEqual(parsed["kind"], "EXTENDED WARNING")

    def test_empty_rows_are_ignored(self):
        self.assertIsNone(parse_message({"product_id": "X", "message": ""}))

    def test_messages_are_newest_first(self):
        data = SpaceWeatherData(fetch=lambda path, params: RAW_ALERTS)
        result = data.messages(limit=2)
        self.assertEqual([item["code"] for item in result["messages"]], ["WATA20", "ALTEF3"])

    def test_a_payload_that_is_not_a_list_is_an_error(self):
        data = SpaceWeatherData(fetch=lambda path, params: {"nope": True})
        with self.assertRaises(SpaceWeatherError):
            data.messages()

    def test_the_ovation_grid_is_read_around_a_point(self):
        grid = {"Observation Time": "2026-09-22T17:49:00Z", "Forecast Time": "2026-09-22T19:20:00Z",
                "coordinates": [[212, 65, 5], [212, 64, 3], [212, 66, 9], [0, -90, 0], [213, 65, 20]]}
        data = SpaceWeatherData(fetch=lambda path, params: grid)
        result = data.aurora_probability(64.84, -147.72)
        self.assertEqual(result["nearest_cell"], {"latitude": 65, "longitude": 212})
        self.assertEqual(result["probability"], 5)
        self.assertEqual(result["max_probability_nearby"], 20)
        self.assertEqual(result["cells_read"], 5)


class RouteTests(unittest.TestCase):
    def test_now_is_the_default(self):
        self.assertEqual(route("how are the geomagnetic conditions right now?")[0], "aurora-now")

    def test_forecast_words(self):
        skill, params = route("what is the aurora forecast for the next 3 days?")
        self.assertEqual(skill, "aurora-forecast")
        self.assertEqual(params["days"], 3)

    def test_visibility_by_city_and_point(self):
        by_city = route("what are the odds of seeing the aurora in Fairbanks?")
        self.assertEqual(by_city[0], "aurora-visibility")
        self.assertEqual(by_city[1]["place"], "fairbanks")
        by_point = route("will I see the northern lights at 64.84,-147.72?")
        self.assertEqual(by_point[1]["point"], "64.84,-147.72")

    def test_visibility_without_a_place_is_still_visibility(self):
        self.assertEqual(route("what are the odds of seeing the aurora?")[0], "aurora-visibility")

    def test_an_unknown_place_is_routed_to_visibility_by_name(self):
        skill, params = route("what are the odds of seeing the aurora in Narnia?")
        self.assertEqual(skill, "aurora-visibility")
        self.assertEqual(params["place"], "narnia")

    def test_messages_with_a_keyword(self):
        skill, params = route("what has NOAA said about flares lately?")
        self.assertEqual(skill, "aurora-messages")
        self.assertEqual(params["contains"], "flare")
        self.assertEqual(route("any space weather alerts?")[0], "aurora-messages")

    def test_helpers(self):
        self.assertEqual(place_from_text("aurora over New York tonight?"), "new york")
        self.assertIsNone(place_from_text("aurora tonight?"))

    def test_help(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")


class TurnTests(unittest.TestCase):
    def turn(self, text: str, data: FakeData | None = None, permission: str = "allow-once"):
        agent_conn, client_conn = connected_pair()
        agent = AuroraAgent(agent_conn, data or FakeData())
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_now_cites_the_product_and_the_peak(self):
        result, client = self.turn("how are the geomagnetic conditions right now?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("estimated Kp", result["text"])
        self.assertIn("Peak over the last 24 hours", result["text"])
        self.assertIn("swpc.noaa.gov", result["text"])
        tools = [update for update in result["updates"] if update.get("sessionUpdate") == "tool_call"]
        self.assertEqual(tools[0]["name"], "aurora-now")
        self.assertEqual(len(client.permission_requests), 1)

    def test_streaming_is_chunked_under_one_message_id(self):
        result, _ = self.turn("how are the geomagnetic conditions right now?")
        chunks = [update for update in result["updates"] if update.get("sessionUpdate") == "agent_message_chunk"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len({chunk["messageId"] for chunk in chunks}), 1)

    def test_forecast_lists_days_and_the_storm_watch(self):
        result, _ = self.turn("what is the aurora forecast for the next 3 days?")
        self.assertIn("2026-09-23: peak Kp 4.33 (active)", result["text"])
        self.assertIn("G1 (minor)", result["text"])
        self.assertIn("WATA20", result["text"])

    def test_visibility_reports_odds_and_the_cloud_caveat(self):
        result, _ = self.turn("what are the odds of seeing the aurora in Tromso?")
        self.assertIn("5%", result["text"])
        self.assertIn("up to 7% within 2 degrees", result["text"])
        self.assertIn("clouds", result["text"])
        self.assertIn(DATASET_OVATION, result["text"])

    def test_visibility_without_a_place_asks_for_one(self):
        result, _ = self.turn("what are the odds of seeing the aurora?")
        self.assertIn("I do not know the place", result["text"])
        self.assertIn("Give me a latitude/longitude point", result["text"])

    def test_an_unknown_place_is_refused_by_name(self):
        result, _ = self.turn("what are the odds of seeing the aurora in Narnia?")
        self.assertIn("I do not know the place 'narnia'", result["text"])

    def test_messages_filter(self):
        result, _ = self.turn("what has NOAA said about geomagnetic storms lately?")
        self.assertIn("WATA20", result["text"])
        self.assertIn("WATCH", result["text"])

    def test_help_does_not_ask_permission(self):
        result, client = self.turn("what can you do?")
        self.assertIn("Space Weather Prediction Center", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _ = self.turn("how are the geomagnetic conditions?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_a_feed_failure_is_reported(self):
        result, _ = self.turn("how are the geomagnetic conditions?", data=FakeData(raise_error=True))
        self.assertIn("could not read the NOAA product", result["text"])
        statuses = [update.get("status") for update in result["updates"]
                    if update.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", statuses)

    def test_a_storm_is_reported_plainly(self):
        result, _ = self.turn("how are the geomagnetic conditions right now?", data=FakeData(kp=6.0))
        self.assertIn("G2 (moderate)", result["text"])
        self.assertIn("storm is in progress", result["text"])


if __name__ == "__main__":
    unittest.main()
