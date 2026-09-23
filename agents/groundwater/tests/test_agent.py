"""Tests for the groundwater agent: wells, series, a state overview, routing and turns.

    python3 agents/groundwater/tests/test_agent.py

No network: the JSON is injected, shaped like the live payloads read on 2026-09-23 (site
OH015-395847084085500 = 'MI-3A OH', Miami County Ohio, well depth 130 ft; depths 2026-09-20
10.31 ft, 2026-09-21 10.19 ft, 2026-09-22 9.95 ft).
"""

from __future__ import annotations

import datetime
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

from agent import route  # noqa: E402
from data import (GroundwaterData, GroundwaterError, GroundwaterNotFound,  # noqa: E402
                  identifier_from_text, median, state_from_text)

SITE = {"features": [{"properties": {
    "id": "OH015-395847084085500", "monitoring_location_number": "395847084085500",
    "monitoring_location_name": "MI-3A OH", "state_name": "Ohio", "county_name": "Miami County",
    "site_type": "Well", "well_constructed_depth": 130.0, "aquifer_code": "112OTSH"},
    "geometry": {"coordinates": [-84.1485277777778, 39.9798888888889]}}]}

DAILY = {"features": [
    {"properties": {"monitoring_location_id": "OH015-395847084085500", "parameter_code": "72019",
                    "time": "2026-09-22", "value": "9.95", "unit_of_measure": "ft",
                    "approval_status": "Provisional"}},
    {"properties": {"monitoring_location_id": "OH015-395847084085500", "parameter_code": "72019",
                    "time": "2026-09-20", "value": "10.31", "unit_of_measure": "ft",
                    "approval_status": "Provisional"}},
    {"properties": {"monitoring_location_id": "OH015-395847084085500", "parameter_code": "72019",
                    "time": "2026-09-21", "value": "10.19", "unit_of_measure": "ft",
                    "approval_status": "Approved"}},
    #: A row with no value at all - USGS does send these.
    {"properties": {"monitoring_location_id": "OH015-395847084085500", "parameter_code": "72019",
                    "time": "2026-09-19", "value": ""}},
]}

LATEST = {"features": [
    {"properties": {"monitoring_location_id": "USGS-395316083593100", "time": "2026-09-22",
                    "value": "-0.19"}},
    {"properties": {"monitoring_location_id": "OH015-395847084085500", "time": "2026-09-22",
                    "value": "9.95"}},
    {"properties": {"monitoring_location_id": "USGS-394442084111600", "time": "2026-09-22",
                    "value": "121.67"}},
]}


class FakeFeeds:
    def __init__(self, site=None, daily=None, latest=None, empty=False, fail=False):
        self.calls: list[str] = []
        self.params: list[dict] = []
        self.site = SITE if site is None else site
        self.daily = DAILY if daily is None else daily
        self.latest = LATEST if latest is None else latest
        self.empty = empty
        self.fail = fail

    def __call__(self, url: str, params: dict):
        self.calls.append(url)
        self.params.append(dict(params or {}))
        if self.fail:
            raise GroundwaterError("the USGS API is unreachable")
        if self.empty:
            return json.dumps({"features": []})
        if "monitoring-locations" in url:
            return json.dumps(self.site)
        if "latest-daily" in url:
            return json.dumps(self.latest)
        return json.dumps(self.daily)


def make_data(**kw):
    feed = FakeFeeds(**kw)
    return GroundwaterData(fetch=feed), feed


class HelperTests(unittest.TestCase):
    def test_a_site_number_or_a_location_id(self):
        self.assertEqual(identifier_from_text("water level at site 395847084085500"),
                         "395847084085500")
        self.assertEqual(identifier_from_text("well OH015-395847084085500 over 30 days"),
                         "OH015-395847084085500")
        self.assertIsNone(identifier_from_text("how deep is the water table in Kansas?"))

    def test_a_state_only_by_full_name(self):
        self.assertEqual(state_from_text("how deep is the water table in Kansas?"), "Kansas")
        self.assertEqual(state_from_text("what about New Mexico?"), "New Mexico")
        #: Two-letter codes collide with ordinary words, so they are not accepted.
        self.assertIsNone(state_from_text("no i want ca"))
        self.assertIsNone(state_from_text("how deep is the water table in France?"))

    def test_a_median(self):
        self.assertEqual(median([1, 2, 3]), 2)
        self.assertEqual(median([1, 2, 3, 4]), 2.5)
        self.assertIsNone(median([]))


class ReadTests(unittest.TestCase):
    def test_a_site_record(self):
        data, feed = make_data()
        site = data.site("OH015-395847084085500")
        self.assertEqual(site["name"], "MI-3A OH")
        self.assertEqual(site["county"], "Miami County")
        self.assertEqual(site["state"], "Ohio")
        self.assertEqual(site["well_depth"], 130.0)
        self.assertEqual(site["aquifer"], "112OTSH")
        #: GeoJSON coordinates are [longitude, latitude] - easy to swap by accident.
        self.assertAlmostEqual(site["latitude"], 39.9798888888889)
        self.assertAlmostEqual(site["longitude"], -84.1485277777778)
        self.assertEqual(feed.params[-1]["id"], "OH015-395847084085500")

    def test_a_site_number_is_looked_up_by_number_not_by_id(self):
        data, feed = make_data()
        data.site("395847084085500")
        self.assertEqual(feed.params[-1], {"monitoring_location_number": "395847084085500",
                                          "limit": "1"})

    def test_a_depth_series_is_sorted_and_rows_without_a_value_are_dropped(self):
        data, feed = make_data()
        readings = data.readings("OH015-395847084085500", days=14)
        self.assertEqual([row["date"] for row in readings["rows"]],
                         ["2026-09-20", "2026-09-21", "2026-09-22"])
        self.assertEqual(readings["latest"]["depth_ft"], 9.95)
        self.assertEqual(readings["oldest"]["depth_ft"], 10.31)
        self.assertEqual(readings["latest"]["status"], "Provisional")
        self.assertEqual(feed.params[-1]["parameter_code"], "72019")
        self.assertEqual(feed.params[-1]["sortby"], "-time")

    def test_a_well_is_the_record_plus_its_depths(self):
        data, _feed = make_data()
        well = data.well("OH015-395847084085500", days=14)
        self.assertEqual(well["name"], "MI-3A OH")
        self.assertEqual(well["days"], 3)
        self.assertEqual(well["latest"]["depth_ft"], 9.95)
        self.assertEqual(len(well["readings"]), 3)

    def test_a_well_with_no_readings_is_an_error(self):
        data, _feed = make_data(daily={"features": []})
        with self.assertRaises(GroundwaterNotFound):
            data.readings("OH015-395847084085500")
        data, _feed = make_data(empty=True)
        with self.assertRaises(GroundwaterNotFound):
            data.site("395847084085500")
        data, _feed = make_data()
        with self.assertRaises(ValueError):
            data.site("   ")

    def test_a_state_overview_counts_and_spreads_the_wells(self):
        data, feed = make_data()
        overview = data.state("Ohio", days=7)
        self.assertEqual(overview["wells"], 3)
        self.assertEqual(overview["median_ft"], 9.95)
        self.assertEqual(overview["shallowest"]["depth_ft"], -0.19)
        self.assertEqual(overview["deepest"]["depth_ft"], 121.67)
        self.assertFalse(overview["truncated"])
        self.assertEqual(feed.params[-1]["state_name"], "Ohio")
        self.assertEqual(feed.params[-1]["parameter_code"], "72019")
        today = datetime.date.today()
        start = today - datetime.timedelta(days=7)
        self.assertEqual(feed.params[-1]["datetime"], f"{start.isoformat()}/{today.isoformat()}")

    def test_a_state_with_no_reporting_wells(self):
        data, _feed = make_data(latest={"features": []})
        with self.assertRaises(GroundwaterNotFound):
            data.state("Ohio")

    def test_a_dead_api(self):
        data, _feed = make_data(fail=True)
        with self.assertRaises(GroundwaterError):
            data.site("OH015-395847084085500")
        with self.assertRaises(GroundwaterError):
            data.state("Ohio")


class RouteTests(unittest.TestCase):
    def test_a_state_question(self):
        self.assertEqual(route("how deep is the water table in Kansas?"),
                         ("groundwater-state", {"state": "Kansas"}))
        self.assertEqual(route("is the water table dropping in Arizona?"),
                         ("groundwater-state", {"state": "Arizona"}))

    def test_a_well_question(self):
        self.assertEqual(route("water level at site 395847084085500"),
                         ("groundwater-well", {"identifier": "395847084085500"}))
        self.assertEqual(route("well OH015-395847084085500 over the last 30 days"),
                         ("groundwater-well", {"identifier": "OH015-395847084085500", "days": 30}))

    def test_a_weeks_window_becomes_days(self):
        self.assertEqual(route("site 395847084085500 over the last 2 weeks"),
                         ("groundwater-well", {"identifier": "395847084085500", "days": 14}))

    def test_another_kind_of_water_question_is_not_answered_here(self):
        self.assertEqual(route("what is the weather in Ohio?")[0], "help")
        self.assertEqual(route("how high is the river stage in Ohio?")[0], "help")

    def test_help_and_nonsense(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")
        self.assertEqual(route("tell me a joke")[0], "help")


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
        from agent import GroundwaterAgent

        agent_conn, client_conn = connected_pair()
        data, feed = make_data(**kw)
        agent = GroundwaterAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client, feed
        finally:
            client.stop()

    def test_a_well_answer(self):
        result, client, _ = self.turn("water level at site 395847084085500")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertEqual(len(client.permission_requests), 1)
        self.assertIn("MI-3A OH (395847084085500)", result["text"])
        self.assertIn("location: Miami County, Ohio", result["text"])
        self.assertIn("well depth: 130.0 ft", result["text"])
        self.assertIn("coordinates: 39.97989, -84.14853", result["text"])
        self.assertIn("depth to water: 9.95 ft below land surface on 2026-09-22", result["text"])
        self.assertIn("(Provisional)", result["text"])
        self.assertIn("3 reading(s) from 2026-09-20 to 2026-09-22: -0.36 ft (rising)",
                      result["text"])
        self.assertIn("deeper* water", result["text"])

    def test_a_state_answer_reports_the_spread(self):
        result, _, _ = self.turn("how deep is the water table in Kansas?")
        #: The rows carry no state name at all, so the label has to be the one that was asked
        #: for - which the API's own filter is what makes true.
        self.assertIn("Kansas - depth to water in the last 7 day(s)", result["text"])
        self.assertIn("3 well(s) reported", result["text"])
        self.assertIn("median: 9.95 ft below land surface", result["text"])
        self.assertIn("shallowest: -0.19 ft", result["text"])
        self.assertIn("deepest: 121.67 ft", result["text"])
        self.assertIn("a negative depth means the water stood above the land surface",
                      result["text"])

    def test_help_asks_nothing(self):
        result, client, feed = self.turn("what can you do?")
        self.assertIn("depth to water", result["text"])
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(feed.calls, [])

    def test_permission_denied(self):
        result, _, _ = self.turn("how deep is the water table in Kansas?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)

    def test_a_dead_api(self):
        result, _, _ = self.turn("how deep is the water table in Kansas?", fail=True)
        self.assertIn("I could not read USGS's water data", result["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
