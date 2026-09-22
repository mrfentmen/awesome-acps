"""Tests for the rivers agent: USGS reader, routing, skills, permissions.

    python3 agents/rivers/tests/test_agent.py
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

from agent import RiversAgent, hours_from_text, route, site_from_text  # noqa: E402
from data import (  # noqa: E402
    DATASET_LATEST,
    DATASET_SERIES,
    DATASET_SITES,
    PARAMETERS,
    RiversData,
    RiversError,
)

#: Shapes copied from the live USGS OGC API on 2026-09-22 (USGS-06730500).
LATEST_PAYLOAD = {
    "numberReturned": 2,
    "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-105.22, 40.01]},
         "properties": {"monitoring_location_id": "USGS-06730500",
                        "monitoring_location_name": "BOULDER CREEK AT MOUTH NEAR LONGMONT, CO",
                        "parameter_code": "00060", "statistic_id": "00011",
                        "time": "2026-09-22T18:00:00+00:00", "value": "0.30", "unit_of_measure": "ft^3/s"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-105.22, 40.01]},
         "properties": {"monitoring_location_id": "USGS-06730500",
                        "monitoring_location_name": "BOULDER CREEK AT MOUTH NEAR LONGMONT, CO",
                        "parameter_code": "00065", "statistic_id": "00001",
                        "time": "2026-09-22T18:00:00+00:00", "value": "8.97", "unit_of_measure": "ft"}},
    ],
}

SERIES_PAYLOAD = {
    "numberReturned": 3,
    "features": [
        {"type": "Feature", "properties": {"monitoring_location_id": "USGS-06730500",
                                           "parameter_code": "00060", "time": f"2026-09-22T{h:02d}:00:00+00:00",
                                           "value": value, "unit_of_measure": "ft^3/s"}}
        for h, value in ((16, "0.20"), (17, "0.35"), (18, "0.30"))
    ],
}

EMPTY_PAYLOAD = {"numberReturned": 0, "features": []}

SITES_PAYLOAD = {
    "numberReturned": 2,
    "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-74.0, 40.7]},
         "properties": {"id": "USGS-01302030", "monitoring_location_name": "EAST RIVER AT ROOSEVELT ISLAND",
                        "site_type_code": "ST", "county_name": "New York", "state_name": "New York"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-74.01, 40.72]},
         "properties": {"id": "USGS-01376515", "monitoring_location_name": "HUDSON RIVER AT PIER 84",
                        "site_type_code": "ST", "county_name": "New York", "state_name": "New York"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-74.01, 40.72]},
         "properties": {"id": "AL012-90100100001", "monitoring_location_name": "NOT A USGS SITE",
                        "site_type_code": "FA-WDS"}},
    ],
}


class PayloadData(RiversData):
    """Same interface as the real reader, but every read returns one canned payload."""

    def __init__(self, payload, raise_error: bool = False):
        self.payload = payload
        self.raise_error = raise_error
        self.calls: list[dict] = []

    def latest(self, site: str) -> dict:
        if self.raise_error:
            raise RiversError("USGS is offline")
        self.calls.append({"kind": "latest", "site": site})
        observations = []
        for feature in self.payload.get("features", []):
            props = feature["properties"]
            observations.append({
                "time": props.get("time"), "value": float(props.get("value")),
                "unit": props.get("unit_of_measure"), "parameter_code": props.get("parameter_code"),
                "statistic_id": props.get("statistic_id"), "site": props.get("monitoring_location_id"),
                "parameter_name": PARAMETERS.get(props.get("parameter_code"), "other"),
            })
        if not observations:
            raise RiversError(f"USGS has no current readings for {site}")
        return {
            "dataset": DATASET_LATEST, "site": site,
            "site_name": self.payload["features"][0]["properties"].get("monitoring_location_name"),
            "observations": observations,
            "observed_at": max(item["time"] for item in observations),
        }

    def series(self, site: str, hours: int = 24, parameter: str = "00060") -> dict:
        if self.raise_error:
            raise RiversError("USGS is offline")
        self.calls.append({"kind": "series", "site": site, "hours": hours, "parameter": parameter})
        rows = [{"time": feature["properties"]["time"], "value": float(feature["properties"]["value"]),
                 "unit": feature["properties"]["unit_of_measure"], "parameter_code": parameter,
                 "statistic_id": "00011", "site": site}
                for feature in self.payload.get("features", [])]
        if not rows:
            raise RiversError(f"USGS has no series for {site}")
        rows.sort(key=lambda row: row["time"])
        numbers = [row["value"] for row in rows]
        change = round(numbers[-1] - numbers[0], 4)
        return {
            "dataset": DATASET_SERIES, "site": site, "site_name": None, "parameter_code": parameter,
            "parameter_name": PARAMETERS[parameter], "unit": rows[0]["unit"], "rows": rows,
            "count": len(rows), "window_hours": hours,
            "first": {"time": rows[0]["time"], "value": rows[0]["value"]},
            "last": {"time": rows[-1]["time"], "value": rows[-1]["value"]},
            "min": min(numbers), "max": max(numbers), "mean": round(sum(numbers) / len(numbers), 4),
            "change": change, "rising": change > 0,
        }

    def sites_near(self, point: str, radius_km: float = 25, limit: int = 5) -> dict:
        if self.raise_error:
            raise RiversError("USGS is offline")
        self.calls.append({"kind": "near", "point": point, "radius_km": radius_km})
        sites = []
        for feature in self.payload.get("features", []):
            props = feature["properties"]
            if not str(props.get("id", "")).startswith("USGS-"):
                continue
            coordinates = feature["geometry"]["coordinates"]
            sites.append({
                "site": props["id"], "name": props.get("monitoring_location_name"),
                "site_type": props.get("site_type_code"), "county": props.get("county_name"),
                "state": props.get("state_name"),
                "distance_km": round(RiversData._haversine_km(40.71, -74.01, coordinates[1], coordinates[0]), 2),
            })
        sites.sort(key=lambda item: item["distance_km"])
        return {"dataset": DATASET_SITES, "point": point, "radius_km": radius_km,
                "sites": sites[:limit], "matched": len(sites)}


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
    def test_site_ids_are_normalised(self):
        self.assertEqual(RiversData.check_site("06730500"), "USGS-06730500")
        self.assertEqual(RiversData.check_site("USGS-06730500"), "USGS-06730500")
        self.assertEqual(RiversData.check_site("https://waterdata.usgs.gov/monitoring-location/USGS-06730500"),
                         "USGS-06730500")
        with self.assertRaises(ValueError):
            RiversData.check_site("boulder creek")

    def test_points_radii_and_parameters_are_validated(self):
        self.assertEqual(RiversData.check_point("40.71,-74.01"), "40.71,-74.01")
        with self.assertRaises(ValueError):
            RiversData.check_point("-95,0")
        self.assertEqual(RiversData.check_radius_km(25), 25.0)
        with self.assertRaises(ValueError):
            RiversData.check_radius_km(900)
        self.assertEqual(RiversData.check_parameter("00065"), "00065")
        with self.assertRaises(ValueError):
            RiversData.check_parameter("99999")
        with self.assertRaises(ValueError):
            RiversData.check_hours(0)

    def test_latest_keeps_usgs_units_and_names_the_parameters(self):
        data = RiversData(fetch=lambda url, params: LATEST_PAYLOAD)
        latest = data.latest("06730500")
        self.assertEqual(latest["site"], "USGS-06730500")
        self.assertEqual(latest["site_name"], "BOULDER CREEK AT MOUTH NEAR LONGMONT, CO")
        self.assertEqual(latest["observed_at"], "2026-09-22T18:00:00+00:00")
        by_code = {item["parameter_code"]: item for item in latest["observations"]}
        self.assertEqual(by_code["00060"]["value"], 0.30)
        self.assertEqual(by_code["00060"]["unit"], "ft^3/s")
        self.assertEqual(by_code["00060"]["parameter_name"], "discharge (streamflow)")
        self.assertEqual(by_code["00065"]["unit"], "ft")

    def test_latest_refuses_an_empty_gauge(self):
        data = RiversData(fetch=lambda url, params: EMPTY_PAYLOAD)
        with self.assertRaises(RiversError):
            data.latest("06730500")

    def test_series_computes_the_range_and_the_change(self):
        data = RiversData(fetch=lambda url, params: SERIES_PAYLOAD)
        series = data.series("06730500", hours=24, parameter="00060")
        self.assertEqual(series["count"], 3)
        self.assertEqual(series["first"]["value"], 0.20)
        self.assertEqual(series["last"]["value"], 0.30)
        self.assertEqual(series["min"], 0.20)
        self.assertEqual(series["max"], 0.35)
        self.assertAlmostEqual(series["mean"], 0.2833, places=4)
        self.assertAlmostEqual(series["change"], 0.10, places=6)
        self.assertTrue(series["rising"])
        self.assertEqual(series["parameter_name"], "discharge (streamflow)")

    def test_a_missing_series_is_an_error_not_a_zero(self):
        data = RiversData(fetch=lambda url, params: EMPTY_PAYLOAD)
        with self.assertRaises(RiversError):
            data.series("06730500")

    def test_sites_near_filters_to_usgs_and_sorts_by_distance(self):
        data = RiversData(fetch=lambda url, params: SITES_PAYLOAD)
        result = data.sites_near("40.71,-74.01", radius_km=25, limit=5)
        self.assertEqual([site["site"] for site in result["sites"]],
                         ["USGS-01376515", "USGS-01302030"])
        for site in result["sites"]:
            self.assertLess(site["distance_km"], 2.0)
        self.assertEqual(result["matched"], 2)


class RouteTests(unittest.TestCase):
    def test_a_gauge_and_no_time_is_the_latest_reading(self):
        skill, params = route("what is gauge 09380000 doing right now?")
        self.assertEqual(skill, "river-stage")
        self.assertEqual(params["site"], "USGS-09380000")

    def test_a_gauge_and_a_window_is_the_series(self):
        skill, params = route("has gauge 06730500 been rising in the last 24 hours?")
        self.assertEqual(skill, "river-recent")
        self.assertEqual(params["hours"], 24)
        self.assertEqual(params["parameter"], "00060")

    def test_days_are_converted_to_hours(self):
        skill, params = route("gauge 06730500 over the last 7 days?")
        self.assertEqual(skill, "river-recent")
        self.assertEqual(params["hours"], 168)
        self.assertEqual(hours_from_text("take a look at the last 7 days please"), 168)

    def test_a_point_alone_finds_gauges(self):
        skill, params = route("what gauges are near 40.71,-74.01?")
        self.assertEqual(skill, "river-near")
        self.assertEqual(params["point"], "40.71,-74.01")

    def test_temperature_words_pick_the_parameter(self):
        skill, params = route("water temperature at gauge 06730500 in the last 12 hours?")
        self.assertEqual(skill, "river-recent")
        self.assertEqual(params["parameter"], "00010")

    def test_no_gauge_no_point_still_routes_to_stage_so_it_can_ask(self):
        skill, params = route("how high is the river?")
        self.assertEqual(skill, "river-stage")
        self.assertEqual(params, {})

    def test_helpers(self):
        self.assertEqual(site_from_text("USGS-06730500 please"), "USGS-06730500")
        self.assertIsNone(site_from_text("boulder creek"))

    def test_help(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")


class TurnTests(unittest.TestCase):
    def turn(self, text: str, payload=None, permission: str = "allow-once", raise_error: bool = False):
        agent_conn, client_conn = connected_pair()
        data = PayloadData(payload if payload is not None else LATEST_PAYLOAD, raise_error=raise_error)
        agent = RiversAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_latest_readings_cite_the_parameter_and_the_unit(self):
        result, client = self.turn("what is gauge 06730500 doing right now?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("discharge (streamflow) (00060): 0.3 ft^3/s at 2026-09-22T18:00:00+00:00", result["text"])
        self.assertIn("BOULDER CREEK AT MOUTH NEAR LONGMONT, CO", result["text"])
        self.assertIn(DATASET_LATEST, result["text"])
        tools = [update for update in result["updates"] if update.get("sessionUpdate") == "tool_call"]
        self.assertEqual(tools[0]["name"], "river-stage")
        self.assertEqual(len(client.permission_requests), 1)

    def test_streaming_is_chunked_under_one_message_id(self):
        result, _ = self.turn("what is gauge 06730500 doing right now?")
        chunks = [update for update in result["updates"] if update.get("sessionUpdate") == "agent_message_chunk"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len({chunk["messageId"] for chunk in chunks}), 1)

    def test_series_reports_the_trend(self):
        result, _ = self.turn("has gauge 06730500 been rising in the last 24 hours?", payload=SERIES_PAYLOAD)
        self.assertIn("Change over the window: +0.1 ft^3/s (rising)", result["text"])
        self.assertIn("Range 0.2 to 0.35 ft^3/s, mean 0.2833", result["text"])

    def test_near_lists_the_stations_with_distances(self):
        result, _ = self.turn("what gauges are near 40.71,-74.01?", payload=SITES_PAYLOAD)
        self.assertIn("USGS-01302030 - EAST RIVER AT ROOSEVELT ISLAND", result["text"])
        self.assertIn("km", result["text"])
        self.assertIn(DATASET_SITES, result["text"])

    def test_no_gauge_asks_for_one(self):
        result, _ = self.turn("how high is the river?")
        self.assertIn("I need a USGS gauge number", result["text"])

    def test_no_point_asks_for_one(self):
        result, _ = self.turn("which gauges are near me?")
        self.assertIn("I need a latitude/longitude point", result["text"])
        self.assertEqual(route("which gauges are near me?")[0], "river-near")
        self.assertEqual(route("how high is the river?")[0], "river-stage")

    def test_help_does_not_ask_permission(self):
        result, client = self.turn("what can you do?")
        self.assertIn("USGS stream gauges", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _ = self.turn("gauge 06730500?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_a_feed_failure_is_reported(self):
        result, _ = self.turn("gauge 06730500?", raise_error=True)
        self.assertIn("could not read the USGS water feed", result["text"])
        statuses = [update.get("status") for update in result["updates"]
                    if update.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", statuses)


if __name__ == "__main__":
    unittest.main()
