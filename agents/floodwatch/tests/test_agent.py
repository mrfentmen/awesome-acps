"""Tests for the floodwatch agent: NWPS parsing, USGS name lookup, routing, live turns.

    python3 agents/floodwatch/tests/test_agent.py

Every feed answer is injected (no network). The NWPS fixture is shaped like the live payload
verified on 2026-09-22 for EADM7 (Mississippi River at St. Louis: 13.08 ft observed, 16.6 ft
forecast, minor at 30 ft), and the USGS fixture like its monitoring-locations GeoJSON.
"""

from __future__ import annotations

import queue
import re
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

from agent import FloodwatchAgent, route, _number  # noqa: E402
import data as data_mod  # noqa: E402
from data import (  # noqa: E402
    DATASET,
    FloodData,
    FloodError,
    gauge_id,
    gauge_id_from_text,
    number,
    river_query,
)

NWPS_EADM7 = {
    "lid": "EADM7", "usgsId": "07010000", "reachId": "3624735",
    "name": "Mississippi River at St. Louis", "description": "",
    "rfc": {"abbreviation": "NCRFC", "name": "North Central River Forecast Center"},
    "wfo": {"abbreviation": "LSX", "name": "St. Charles"},
    "state": {"abbreviation": "MO", "name": "Missouri"},
    "county": "St. Louis", "timeZone": "America/Chicago",
    "latitude": 38.628888888889, "longitude": -90.179722222222,
    "status": {
        "observed": {"primary": 13.08, "primaryUnit": "ft", "secondary": 234,
                     "secondaryUnit": "kcfs", "floodCategory": "no_flooding",
                     "validTime": "2026-09-23T00:00:00Z"},
        "forecast": {"primary": 16.6, "primaryUnit": "ft", "secondary": 280,
                     "secondaryUnit": "kcfs", "floodCategory": "no_flooding",
                     "validTime": "2026-09-27T00:00:00Z"},
    },
    "flood": {"stageUnits": "ft", "flowUnits": "cfs", "categories": {
        "major": {"stage": 40, "flow": -9999}, "moderate": {"stage": 35, "flow": -9999},
        "minor": {"stage": 30, "flow": -9999}, "action": {"stage": 28, "flow": -9999}}},
    "ObservedFloodCategory": "no_flooding", "ForecastFloodCategory": "no_flooding",
    "inService": True,
}

#: Same gauge in a flood: observed above the minor stage, with no categories published.
NWPS_MEMT1 = {
    "lid": "MEMT1", "usgsId": "07032000", "name": "Mississippi River at Memphis",
    "rfc": {"abbreviation": "LMRFC"}, "state": {"abbreviation": "TN"}, "county": "Shelby",
    "timeZone": "America/Chicago",
    "status": {
        "observed": {"primary": 31.5, "primaryUnit": "ft", "secondary": -9999,
                     "secondaryUnit": "kcfs", "floodCategory": "minor",
                     "validTime": "2026-09-23T00:00:00Z"},
        "forecast": {},
    },
    "flood": {"categories": {}},
    "ObservedFloodCategory": "minor",
}

USGS_FEATURES = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-90.18, 38.63]},
         "properties": {"monitoring_location_number": "07010000",
                        "monitoring_location_name": "Mississippi River at St. Louis, MO",
                        "state_name": "Missouri", "county_name": "St. Louis County"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-90.10, 38.39]},
         "properties": {"monitoring_location_number": "392709091025001",
                        "monitoring_location_name": "MISSISSIPPI RIVER AT LOUISIANA, MO (CORPS)",
                        "state_name": "Missouri", "county_name": "Pike County"}},
    ],
}

USGS_EMPTY = {"type": "FeatureCollection", "features": []}


def _first_like(cql: str) -> str:
    """The first name phrase a CQL2 filter searches for (what :class:`FakeFeed` keys on)."""
    match = re.search(r"LIKE '%(.*?)%'", cql or "")
    return match.group(1) if match else ""


class FakeFeed:
    """Dispatch on the water feed host; records every call so the chain can be checked."""

    def __init__(self, fail: bool = False, empty_search: bool = False, phrases=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail
        self.empty_search = empty_search
        #: filters that should come back empty, keyed by the first phrase in the LIKE clause
        self.phrases = {key.lower(): value for key, value in (phrases or {}).items()}

    def __call__(self, url: str, params: dict):
        if self.fail:
            raise FloodError("the water feeds are offline")
        self.calls.append((url, dict(params)))
        if "nwps" in url:
            wanted = url.rsplit("/", 1)[-1]
            if wanted == "EADM7":
                return NWPS_EADM7
            if wanted == "MEMT1":
                return NWPS_MEMT1
            if wanted == "07010000":
                return NWPS_EADM7
            if wanted == "07032000":
                return NWPS_MEMT1
            raise FloodError(f"NWPS request failed: HTTP Error 404: Not Found for {wanted}")
        if "waterdata.usgs.gov" in url:
            if self.empty_search:
                return USGS_EMPTY
            phrase = _first_like(params.get("filter", ""))
            if phrase in self.phrases:
                return {"type": "FeatureCollection", "features": self.phrases[phrase]}
            return USGS_FEATURES
        raise FloodError(f"unexpected url {url}")


def live_data(**kwargs) -> tuple[FloodData, FakeFeed]:
    feed = FakeFeed(**kwargs)
    return FloodData(fetch=feed), feed


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
    def test_missing_values_are_not_numbers(self):
        self.assertIsNone(number(-9999))
        self.assertIsNone(number(None))
        self.assertIsNone(number("nonsense"))
        self.assertEqual(number(13.08), 13.08)
        self.assertEqual(number(30), 30.0)

    def test_gauge_ids(self):
        self.assertEqual(gauge_id("eadm7"), "EADM7")
        self.assertEqual(gauge_id("07010000"), "07010000")
        self.assertEqual(gauge_id(" 07010000 "), "07010000")
        with self.assertRaises(ValueError):
            gauge_id("Mississippi River")
        with self.assertRaises(ValueError):
            gauge_id("ABCD")

    def test_gauge_id_from_text(self):
        self.assertEqual(gauge_id_from_text("how high is the water at gauge EADM7?"), "EADM7")
        self.assertEqual(gauge_id_from_text("level at 07010000 please"), "07010000")
        self.assertIsNone(gauge_id_from_text("how high is the Mississippi River?"))

    def test_river_query_keeps_the_middle_of_a_site_name(self):
        self.assertEqual(river_query("what is the Mississippi River at St. Louis doing?"),
                         "Mississippi River at St. Louis")
        self.assertEqual(river_query("is the Colorado River at Austin flooding?"),
                         "Colorado River at Austin")
        self.assertEqual(river_query("which gauges do you have for the Willamette River?"),
                         "Willamette River")
        self.assertIsNone(river_query("hello there"))

    def test_gauge_report_shape(self):
        data, _ = live_data()
        report = data.gauge("EADM7")
        self.assertEqual(report["name"], "Mississippi River at St. Louis")
        self.assertEqual(report["observed"]["stage"], 13.08)
        self.assertEqual(report["observed"]["flow"], 234.0)
        self.assertEqual(report["observed"]["category"], "no_flooding")
        self.assertEqual(report["forecast"]["stage"], 16.6)
        self.assertEqual(report["categories"]["minor"], 30.0)
        self.assertEqual(report["next_threshold"]["category"], "action")
        self.assertEqual(report["next_threshold"]["feet_to_go"], 14.92)
        self.assertEqual(report["state"], "MO")

    def test_a_forecast_peak_above_the_next_stage_is_reported(self):
        data, _ = live_data()
        report = data.gauge("MEMT1")
        self.assertEqual(report["observed"]["category"], "minor")
        self.assertIsNone(report["observed"]["flow"])
        self.assertIsNone(report["forecast"]["stage"])
        self.assertEqual(report["categories"], {})
        self.assertIsNone(report["next_threshold"])

    def test_an_unknown_gauge_id_is_refused(self):
        data, _ = live_data()
        with self.assertRaises(ValueError) as caught:
            data.gauge("ZZZZ9")
        self.assertIn("no gauge with the id", str(caught.exception))

    def test_find_gauges_flags_which_sites_are_usable(self):
        data, _ = live_data()
        rows = data.find_gauges("Mississippi River at St. Louis")
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["usable"])
        self.assertFalse(rows[1]["usable"])

    def test_the_name_search_ignores_case(self):
        # USGS stores names upper case, so a case-sensitive LIKE finds nothing real.
        data, feed = live_data()
        data.find_gauges("Willamette River")
        usgs_calls = [params for url, params in feed.calls if "usgs" in url]
        self.assertEqual(usgs_calls[0]["filter"],
                         "LOWER(monitoring_location_name) LIKE '%willamette river%'")

    def test_a_quote_in_a_river_name_cannot_break_the_filter(self):
        data, feed = live_data()
        data.find_gauges("O'Brien Creek")
        usgs_calls = [params for url, params in feed.calls if "usgs" in url]
        self.assertEqual(usgs_calls[0]["filter"],
                         "LOWER(monitoring_location_name) LIKE '%o''brien creek%'")

    def test_a_wildcard_in_a_river_name_is_stripped(self):
        data, feed = live_data()
        data.find_gauges("100% River")
        usgs_calls = [params for url, params in feed.calls if "usgs" in url]
        self.assertEqual(usgs_calls[0]["filter"],
                         "LOWER(monitoring_location_name) LIKE '%100 river%'")

    def test_the_search_widens_when_the_exact_phrase_has_no_gauges(self):
        # 'Colorado River at Austin' is not how USGS spells any site, but both words are in
        # real names, so the next filter has to be tried instead of giving up.
        data, feed = live_data(phrases={"colorado river at austin": []})
        rows = data.find_gauges("Colorado River at Austin")
        usgs_filters = [params["filter"] for url, params in feed.calls if "usgs" in url]
        self.assertGreater(len(usgs_filters), 1)
        self.assertIn(" AND ", usgs_filters[1])
        self.assertTrue(any(row["usable"] for row in rows))

    def test_a_tight_match_stops_the_search_from_widening(self):
        data, feed = live_data()
        data.find_gauges("Mississippi River at St. Louis")
        usgs_calls = [params for url, params in feed.calls if "usgs" in url]
        self.assertEqual(len(usgs_calls), 1)

    def test_readable_gauges_rank_above_tributaries(self):
        rows = [{"site": "14144800", "name": "MIDDLE FORK WILLAMETTE RIVER NR OAKRIDGE, OR",
                 "usable": True},
                {"site": "301239097364601", "name": "IT 7900 on Colorado River nr Austin TX",
                 "usable": False},
                {"site": "14157565", "name": "WILLAMETTE RIVER AT PORTLAND, OR", "usable": True}]
        rows.sort(key=lambda row: data_mod._match_rank(row, "Willamette River"))
        self.assertEqual([row["site"] for row in rows],
                         ["14157565", "14144800", "301239097364601"])

    def test_status_by_name_goes_through_usgs_then_nwps(self):
        data, feed = live_data()
        report = data.flood_status("Mississippi River at St. Louis")
        self.assertEqual(report["lid"], "EADM7")
        self.assertIn("name match", report["via"])
        hosts = ["nwps" if "nwps" in url else "usgs" for url, _params in feed.calls]
        self.assertEqual(hosts[0], "usgs")
        self.assertIn("nwps", hosts)

    def test_status_by_id_never_touches_usgs(self):
        data, feed = live_data()
        report = data.flood_status("07010000")
        self.assertEqual(report["lid"], "EADM7")
        self.assertEqual(report["via"], "gauge id")
        self.assertTrue(all("nwps" in url for url, _params in feed.calls))

    def test_an_unresolvable_river_is_refused_by_name(self):
        data, _ = live_data(empty_search=True)
        with self.assertRaises(ValueError) as caught:
            data.flood_status("Nowhere River")
        self.assertIn("no NWPS gauge", str(caught.exception))

    def test_an_offline_feed_raises_one_error_type(self):
        data, _ = live_data(fail=True)
        with self.assertRaises(FloodError):
            data.gauge("EADM7")

    def test_numbers_keep_whole_values_whole(self):
        self.assertEqual(_number(13.08), "13.08")
        self.assertEqual(_number(30.0), "30")
        self.assertEqual(_number(None), "not published")


class RouteTests(unittest.TestCase):
    def test_river_name_question(self):
        skill, params = route("what is the Mississippi River at St. Louis doing?")
        self.assertEqual(skill, "flood-status")
        self.assertEqual(params["river"], "Mississippi River at St. Louis")

    def test_gauge_id_question(self):
        skill, params = route("how high is the water at gauge EADM7?")
        self.assertEqual(skill, "flood-status")
        self.assertEqual(params["gauge"], "EADM7")

    def test_usgs_number_question(self):
        skill, params = route("what is the stage at 07010000 right now?")
        self.assertEqual(skill, "flood-status")
        self.assertEqual(params["gauge"], "07010000")

    def test_find_question(self):
        skill, params = route("which gauges do you have for the Willamette River?")
        self.assertEqual(skill, "flood-find")
        self.assertEqual(params["river"], "Willamette River")

    def test_help(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")


class TurnTests(unittest.TestCase):
    def turn(self, text: str, fail: bool = False, permission: str = "allow-once", **kwargs):
        agent_conn, client_conn = connected_pair()
        data, _ = live_data(fail=fail, **kwargs)
        agent = FloodwatchAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_river_question_names_the_gauge_it_used(self):
        result, client = self.turn("what is the Mississippi River at St. Louis doing?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("EADM7 - Mississippi River at St. Louis (MO, St. Louis)", result["text"])
        self.assertIn("Observed: 13.08 ft, flow 234 kcfs", result["text"])
        self.assertIn("Forecast: 16.6 ft, flow 280 kcfs by 2026-09-27T00:00:00Z - no flooding",
                      result["text"])
        self.assertIn("Flood stages here: action 28 ft, minor 30 ft, moderate 35 ft, major 40 ft",
                      result["text"])
        self.assertIn("Next: action at 28 ft, 14.92 ft above the observed stage", result["text"])
        self.assertIn("Gauge chosen by name match", result["text"])
        self.assertIn(DATASET, result["text"])
        self.assertEqual(len(client.permission_requests), 1)

    def test_gauge_id_question(self):
        result, _ = self.turn("how high is the water at gauge EADM7?")
        self.assertIn("EADM7", result["text"])
        self.assertIn("no flooding", result["text"])
        self.assertNotIn("Gauge chosen by", result["text"])

    def test_flooding_gauge_says_so_and_handles_the_missing_flow(self):
        result, _ = self.turn("what is the stage at 07032000 right now?")
        self.assertIn("Observed: 31.5 ft, flow not published", result["text"])
        self.assertIn("minor flooding", result["text"])
        self.assertIn("Forecast: none published for this gauge", result["text"])

    def test_find_lists_sites_and_marks_the_unusable_one(self):
        result, _ = self.turn("which gauges do you have for the Willamette River?")
        self.assertIn("07010000 - Mississippi River at St. Louis, MO", result["text"])
        self.assertIn("(no NWPS forecast point)", result["text"])

    def test_unresolvable_river_is_refused(self):
        result, _ = self.turn("what is Nowhere River doing?", empty_search=True)
        self.assertIn("I found no NWPS gauge for 'Nowhere River'", result["text"])

    def test_streaming_is_chunked_under_one_message_id(self):
        result, _ = self.turn("what is the Mississippi River at St. Louis doing?")
        chunks = [u for u in result["updates"] if u.get("sessionUpdate") == "agent_message_chunk"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len({chunk["messageId"] for chunk in chunks}), 1)

    def test_help_does_not_ask_permission(self):
        result, client = self.turn("what can you do?")
        self.assertIn("NWPS", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _ = self.turn("how high is the water at gauge EADM7?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_a_feed_failure_is_reported(self):
        result, _ = self.turn("how high is the water at gauge EADM7?", fail=True)
        self.assertIn("could not read the river feeds", result["text"])
        statuses = [u.get("status") for u in result["updates"] if u.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", statuses)


if __name__ == "__main__":
    unittest.main()
