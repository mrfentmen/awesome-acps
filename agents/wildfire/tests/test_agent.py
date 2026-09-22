"""Tests for the wildfire agent: NIFC reader, routing, skills, permissions.

    python3 agents/wildfire/tests/test_agent.py
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

from agent import (  # noqa: E402
    WildfireAgent,
    name_from_text,
    place_from_text,
    route,
    state_from_text,
)
from data import (  # noqa: E402
    DATASET,
    WildfireData,
    WildfireError,
    escape_literal,
    haversine_miles,
    to_iso,
)

#: Shapes copied from the live NIFC layer on 2026-09-22.
FEATURES = [
    {"attributes": {"IncidentName": "Plaskett", "IncidentSize": 29993, "PercentContained": 97,
                    "FireDiscoveryDateTime": 1756225560000, "IncidentTypeCategory": "WF",
                    "POOState": "US-CA", "POOCounty": "Monterey", "FireCause": "Undetermined",
                    "GACC": "OSCC", "IncidentManagementOrganization": "Complex Incident Management Team",
                    "UniqueFireIdentifier": "2026-CAOSC-002993", "ModifiedOnDateTime_dt": 1758491324000},
     "geometry": {"x": -121.7175, "y": 36.2159}},
    {"attributes": {"IncidentName": "Timber", "IncidentSize": 25436, "PercentContained": 59,
                    "FireDiscoveryDateTime": 1754700900000, "IncidentTypeCategory": "WF",
                    "POOState": "US-CA", "POOCounty": "Monterey", "FireCause": "Undetermined",
                    "GACC": "OSCC", "IncidentManagementOrganization": "Type 2 Team",
                    "UniqueFireIdentifier": "2026-CAOSC-002543", "ModifiedOnDateTime_dt": 1758491324000},
     "geometry": {"x": -121.9, "y": 36.4}},
    {"attributes": {"IncidentName": "DOME", "IncidentSize": 3420, "PercentContained": 15,
                    "FireDiscoveryDateTime": 1757950080000, "IncidentTypeCategory": "WF",
                    "POOState": "US-CA", "POOCounty": "Mariposa", "FireCause": "Human",
                    "GACC": "OSCC", "IncidentManagementOrganization": "Type 3 Team",
                    "UniqueFireIdentifier": "2026-CAOSC-000342", "ModifiedOnDateTime_dt": 1758491324000},
     "geometry": {"x": -119.8, "y": 37.5}},
]

WILLOW = {
    "attributes": {"IncidentName": "Willow", "IncidentSize": 7389, "PercentContained": 80,
                   "FireDiscoveryDateTime": 1751142420000, "IncidentTypeCategory": "WF",
                   "POOState": "US-CO", "POOCounty": "Lake", "FireCause": "Natural",
                   "GACC": "RMCC", "IncidentManagementOrganization": "Type 3 Team",
                   "UniqueFireIdentifier": "2026-COGCC-000738", "ModifiedOnDateTime_dt": 1758491324000},
    "geometry": {"x": -106.3, "y": 39.2},
}


def layer_fetch(features=None, calls=None):
    """A WildfireData `fetch` that applies what the real ArcGIS layer would."""

    def fetch(path, params):
        if calls is not None:
            calls.append(dict(params))
        where = str(params.get("where") or "1=1")
        rows = list(features if features is not None else FEATURES + [WILLOW])
        match = re.search(r"POOState = '([^']+)'", where)
        if match:
            rows = [row for row in rows if row["attributes"]["POOState"] == match.group(1)]
        match = re.search(r"IncidentSize >= ([\d.]+)", where)
        if match:
            rows = [row for row in rows if row["attributes"]["IncidentSize"] >= float(match.group(1))]
        match = re.search(r"PercentContained <= ([\d.]+)", where)
        if match:
            rows = [row for row in rows if row["attributes"]["PercentContained"] <= float(match.group(1))]
        match = re.search(r"UPPER\(IncidentName\) LIKE '([^']+)'", where)
        if match:
            needle = match.group(1).strip("%").upper()
            rows = [row for row in rows if needle in row["attributes"]["IncidentName"].upper()]
        if params.get("geometry"):
            lon, lat = (float(part) for part in params["geometry"].split(","))
            radius = float(params.get("distance") or 0)
            rows = [row for row in rows
                    if haversine_miles(lat, lon, row["geometry"]["y"], row["geometry"]["x"]) <= radius]
        if "IncidentSize DESC" in str(params.get("orderByFields") or ""):
            rows.sort(key=lambda row: -row["attributes"]["IncidentSize"])
        if params.get("resultRecordCount"):
            rows = rows[: int(params["resultRecordCount"])]
        return {"features": rows}

    return fetch


class FakeData(WildfireData):
    """The real reader over the fake layer, or a failing reader on demand."""

    def __init__(self, features=None, calls=None, raise_error: bool = False):
        self.raise_error = raise_error
        super().__init__(fetch=layer_fetch(features, calls))

    def _query(self, params, ttl=None):
        if self.raise_error:
            raise WildfireError("the wildfire layer is offline")
        return super()._query(params, ttl=ttl)


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
    def test_escape_literal_doubles_quotes(self):
        self.assertEqual(escape_literal("O'Brien"), "'O''Brien'")
        self.assertEqual(escape_literal("CA"), "'CA'")

    def test_to_iso_converts_epoch_milliseconds(self):
        self.assertEqual(to_iso(1756225560000), "2025-08-26T16:26:00Z")
        self.assertIsNone(to_iso(None))
        self.assertIsNone(to_iso("not a date"))

    def test_haversine_is_zero_at_one_point_and_about_69_miles_per_degree(self):
        self.assertEqual(haversine_miles(39.74, -104.99, 39.74, -104.99), 0.0)
        self.assertTrue(68.5 <= haversine_miles(39.0, -105.0, 40.0, -105.0) <= 69.5)

    def test_validation_helpers(self):
        self.assertEqual(WildfireData.check_state("US-CA"), "CA")
        self.assertEqual(WildfireData.check_state("or"), "OR")
        with self.assertRaises(ValueError):
            WildfireData.check_state("Oregon")
        self.assertEqual(WildfireData.check_point("39.74, -104.99"), (39.74, -104.99))
        with self.assertRaises(ValueError):
            WildfireData.check_point("39.74")
        with self.assertRaises(ValueError):
            WildfireData.check_positive("0")
        self.assertEqual(WildfireData.check_acres("1,500"), 1500.0)

    def test_city_lookup(self):
        self.assertEqual(WildfireData.city("Denver"), (39.74, -104.99, "Denver"))
        self.assertIsNone(WildfireData.city("Atlantis"))

    def test_incidents_builds_a_filtered_sorted_query(self):
        calls = []
        data = WildfireData(fetch=layer_fetch(calls=calls))
        result = data.incidents(state="CA", min_acres=5000, limit=5)
        self.assertEqual(calls[-1]["where"], "POOState = 'US-CA' AND IncidentSize >= 5000.0")
        self.assertEqual(calls[-1]["orderByFields"], "IncidentSize DESC")
        self.assertEqual(calls[-1]["resultRecordCount"], "5")
        self.assertEqual([row["name"] for row in result["incidents"]], ["Plaskett", "Timber"])
        self.assertEqual(result["incidents"][0]["state"], "CA")
        self.assertEqual(result["incidents"][0]["latitude"], 36.2159)
        self.assertEqual(result["incidents"][0]["type_name"], "wildfire")
        self.assertEqual(result["acres"], 55429.0)
        self.assertEqual(result["dataset"], DATASET)

    def test_uncontained_filter(self):
        result = WildfireData(fetch=layer_fetch()).incidents(uncontained=True, limit=10)
        self.assertEqual([row["name"] for row in result["incidents"]], ["DOME"])

    def test_near_sorts_by_distance(self):
        result = WildfireData(fetch=layer_fetch()).near(39.74, -104.99, radius_miles=150)
        self.assertEqual([row["name"] for row in result["incidents"]], ["Willow"])
        self.assertTrue(50 <= result["incidents"][0]["distance_miles"] <= 100)
        self.assertEqual(result["origin"], {"latitude": 39.74, "longitude": -104.99})

    def test_summary_groups_by_state(self):
        data = WildfireData(fetch=layer_fetch())
        national = data.summary()
        self.assertEqual(national["count"], 4)
        self.assertEqual(national["by_state"][0]["state"], "CA")
        self.assertEqual(national["uncontained"], 1)
        self.assertEqual(data.summary(state="CA")["count"], 3)

    def test_lookup_wildcards(self):
        calls = []
        result = WildfireData(fetch=layer_fetch(calls=calls)).lookup("tim")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["incidents"][0]["name"], "Timber")
        self.assertIn("UPPER(IncidentName) LIKE '%TIM%'", calls[-1]["where"])
        with self.assertRaises(ValueError):
            WildfireData(fetch=layer_fetch()).lookup("x")

    def test_an_arcgis_error_body_becomes_a_validation_error(self):
        client = WildfireData(fetch=lambda path, params: {"error": {"message": "Invalid where clause"}})
        with self.assertRaises(ValueError):
            client.incidents()

    def test_a_payload_without_features_is_an_error(self):
        client = WildfireData(fetch=lambda path, params: {"nope": True})
        with self.assertRaises(WildfireError):
            client.incidents()


class RouteTests(unittest.TestCase):
    def test_active_by_default(self):
        self.assertEqual(route("what wildfires are burning right now?")[0], "wildfire-active")

    def test_state_is_read_from_text(self):
        self.assertEqual(state_from_text("fires in CA?"), "CA")
        self.assertEqual(state_from_text("fires in Oregon?"), "OR")
        self.assertIsNone(state_from_text("is it in or out"))
        self.assertIsNone(state_from_text("ok thanks"))
        parsed = route("show me wildfires over 10,000 acres in CA")
        self.assertEqual(parsed[1]["min_acres"], 10000.0)
        self.assertEqual(parsed[1]["state"], "CA")

    def test_uncontained_flag(self):
        self.assertTrue(route("any uncontained fires in OR?")[1].get("uncontained"))

    def test_near_by_point_and_city(self):
        by_point = route("any fires near 39.74,-104.99?")
        self.assertEqual(by_point[0], "wildfire-near")
        self.assertEqual(by_point[1]["point"], "39.74,-104.99")
        by_city = route("fires within 50 miles of Denver?")
        self.assertEqual(by_city[1]["place"], "denver")
        self.assertEqual(by_city[1]["radius_miles"], 50.0)
        self.assertEqual(place_from_text("near Missoula tonight"), "missoula")
        self.assertEqual(route("any fires near me?")[0], "wildfire-near")

    def test_summary_and_lookup(self):
        self.assertEqual(route("how many wildfires are burning nationwide?")[0], "wildfire-summary")
        lookup = route("tell me about the Timber fire")
        self.assertEqual(lookup[0], "wildfire-lookup")
        self.assertEqual(lookup[1]["name"], "Timber")
        self.assertEqual(name_from_text("details on the Plaskett fire"), "Plaskett")
        self.assertIsNone(name_from_text("how many fires are there"))

    def test_help_and_unknown(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")


class TurnTests(unittest.TestCase):
    def turn(self, text: str, data: FakeData | None = None, permission: str = "allow-once"):
        agent_conn, client_conn = connected_pair()
        agent = WildfireAgent(agent_conn, data or FakeData())
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_active_lists_fires_with_containment_and_cites_the_layer(self):
        result, client = self.turn("what wildfires are burning in CA?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("Plaskett", result["text"])
        self.assertIn("97% contained", result["text"])
        self.assertIn(DATASET, result["text"])
        tools = [update for update in result["updates"] if update.get("sessionUpdate") == "tool_call"]
        self.assertEqual(tools[0]["name"], "wildfire-active")
        self.assertEqual(tools[0]["kind"], "fetch")
        self.assertEqual(len(client.permission_requests), 1)

    def test_streaming_is_chunked_under_one_message_id(self):
        result, _ = self.turn("what wildfires are burning in CA?")
        chunks = [update for update in result["updates"] if update.get("sessionUpdate") == "agent_message_chunk"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len({chunk["messageId"] for chunk in chunks}), 1)

    def test_near_reports_distance_and_the_not_an_evacuation_notice(self):
        result, _ = self.turn("any fires within 150 miles of Denver?")
        self.assertIn("within 150 miles of Denver", result["text"])
        self.assertIn("miles away", result["text"])
        self.assertIn("not an evacuation notice", result["text"])

    def test_an_unknown_place_is_refused_honestly(self):
        result, _ = self.turn("any fires near Atlantis?")
        self.assertIn("will not guess coordinates", result["text"])

    def test_summary_totals(self):
        result, _ = self.turn("how much fire is burning in the country right now?")
        self.assertIn("active wildfire(s) on the interagency list", result["text"])
        self.assertIn("Still under half contained", result["text"])

    def test_lookup_shows_details(self):
        result, _ = self.turn("tell me about the Timber fire")
        self.assertIn("25,436 acres", result["text"])
        self.assertIn("coordination center: OSCC", result["text"])

    def test_lookup_without_a_name_asks(self):
        result, _ = self.turn("look up a fire")
        self.assertIn("Which incident?", result["text"])

    def test_no_matches_is_stated_honestly(self):
        result, _ = self.turn("tell me about the ZZZ fire")
        self.assertIn("No active incident whose name contains", result["text"])

    def test_help_does_not_ask_permission(self):
        result, client = self.turn("what can you do?")
        self.assertIn("wildfire", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _ = self.turn("what fires are burning in CA?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_layer_failure_is_reported(self):
        result, _ = self.turn("what fires are burning in CA?", data=FakeData(raise_error=True))
        self.assertIn("could not read the wildfire layer", result["text"])
        statuses = [update.get("status") for update in result["updates"]
                    if update.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", statuses)

    def test_an_unknown_state_word_is_not_treated_as_a_state(self):
        result, _ = self.turn("what fires are burning in Atlantis?")
        self.assertIn("every active incident", result["text"])  # no state filter was invented


if __name__ == "__main__":
    unittest.main()
