"""Tests for the tides agent: NOAA station search, gauge readings, routing, turns.

    python3 agents/tides/tests/test_agent.py

No network: the two NOAA feeds are injected. The fixtures are shaped exactly like the live
payloads verified on 2026-09-23 - including the two traps this reader exists to survive:

  1. the station list spells longitude `lng`, and
  2. asked for `range` without `begin_date`, the predictions endpoint silently starts three
     days in the past. That is asserted below so it cannot come back.
"""

from __future__ import annotations

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

from agent import _number, coords_from_text, place_from_text, route  # noqa: E402
from data import (  # noqa: E402
    DATASET,
    TidesData,
    TidesError,
    haversine_km,
    score_name,
    station_id,
    state_from_words,
)

STATIONS = {"stations": [
    {"id": "8443970", "name": "BOSTON", "state": "MA", "lat": 42.354, "lng": -71.0503,
     "tideType": "", "timezonecorr": -5},
    {"id": "8518750", "name": "NEW YORK (The Battery)", "state": "NY", "lat": 40.7005,
     "lng": -74.0142, "tideType": "", "timezonecorr": -5},
    {"id": "8668092", "name": "Battery Creek, 4 mi. above entrance", "state": "SC", "lat": 32.4133,
     "lng": -80.6397, "tideType": "", "timezonecorr": -5},
    {"id": "8723214", "name": "MIAMI BEACH", "state": "FL", "lat": 25.7683, "lng": -80.1320,
     "tideType": "", "timezonecorr": -5},
    {"id": "8723170", "name": "Miami Harbor Entrance", "state": "FL", "lat": 25.7690,
     "lng": -80.1320, "tideType": "", "timezonecorr": -5},
    {"id": "8418150", "name": "PORTLAND", "state": "ME", "lat": 43.6567, "lng": -70.2467,
     "tideType": "", "timezonecorr": -5},
]}

#: Live shape: metadata with the human name, then one row of the newest reading in lst_ldt.
WATER = {"metadata": {"id": "8518750", "name": "The Battery", "lat": "40.7006", "lon": "-74.0142"},
         "data": [{"t": "2026-09-22 22:00", "v": "3.653", "s": "0.110", "f": "1,0,0,0", "q": "p"}]}

#: Eight hi/lo events starting at the station's own today, as NOAA returns them.
PREDICTIONS = {"predictions": [
    {"t": "2026-09-23 00:31", "v": "0.776", "type": "L"},
    {"t": "2026-09-23 06:38", "v": "4.450", "type": "H"},
    {"t": "2026-09-23 12:40", "v": "1.020", "type": "L"},
    {"t": "2026-09-23 18:52", "v": "4.900", "type": "H"},
    {"t": "2026-09-24 01:12", "v": "0.500", "type": "L"},
    {"t": "2026-09-24 07:20", "v": "4.700", "type": "H"},
]}

#: Two events that have already happened relative to the reading above.
STALE = {"predictions": [
    {"t": "2026-09-20 23:01", "v": "1.286", "type": "L"},
    {"t": "2026-09-21 05:32", "v": "7.000", "type": "H"},
]}


#: Distinguishes 'not given' from an explicit None (a station with no live gauge).
UNSET = object()


class FakeFeed:
    """Serves the NOAA shapes and records every (url, params) pair it was asked for."""

    def __init__(self, predictions=UNSET, water=UNSET, fail: str | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.predictions = PREDICTIONS if predictions is UNSET else predictions
        self.water = WATER if water is UNSET else water
        self.fail = fail

    def __call__(self, url: str, params: dict):
        self.calls.append((url, dict(params)))
        if self.fail and self.fail in url:
            raise TidesError("NOAA is unreachable")
        if "stations.json" in url:
            return json.dumps(STATIONS)
        if "datagetter" in url:
            if params.get("product") == "water_level":
                if self.water is None:
                    return json.dumps({"error": {"message": "No data was found. Please check your "
                                                            "request and try again."}})
                return json.dumps(self.water)
            if params.get("product") == "predictions":
                if self.fail == "no-predictions":
                    return json.dumps({})
                return json.dumps(self.predictions)
        raise AssertionError(f"unexpected NOAA url {url}")

    def prediction_params(self) -> list[dict]:
        return [params for url, params in self.calls if params.get("product") == "predictions"]


def make_data(**kwargs) -> tuple[TidesData, FakeFeed]:
    feed = FakeFeed(**kwargs)
    return TidesData(fetch=feed), feed


class ReaderTests(unittest.TestCase):
    def test_longitude_is_read_from_the_lng_key(self):
        data, _ = make_data()
        battery = data.station("8518750")
        self.assertAlmostEqual(battery["longitude"], -74.0142)
        self.assertAlmostEqual(battery["latitude"], 40.7005)
        self.assertEqual(battery["state"], "NY")

    def test_station_ids(self):
        self.assertEqual(station_id("8518750"), "8518750")
        self.assertEqual(station_id("station 8443970 please"), "8443970")
        with self.assertRaises(ValueError):
            station_id("the battery")

    def test_an_unknown_station_is_refused(self):
        data, _ = make_data()
        with self.assertRaises(ValueError):
            data.station("9999999")

    def test_a_name_with_unexplained_words_loses_to_a_tight_match(self):
        # 'the battery' must reach New York, not Battery Creek in South Carolina.
        self.assertGreater(score_name(["battery"], "NEW YORK (The Battery)"),
                           score_name(["battery"], "Battery Creek, 4 mi. above entrance"))
        data, _ = make_data()
        self.assertEqual(data.find("The Battery")[0]["id"], "8518750")

    def test_exact_names_beat_prefixes(self):
        self.assertGreater(score_name(["portland"], "PORTLAND"),
                           score_name(["portland"], "Portland Head Light"))

    def test_state_words_are_extracted(self):
        self.assertEqual(state_from_words(["Miami", "Florida"]), ("FL", ["Miami"]))
        self.assertEqual(state_from_words(["Florida"]), ("FL", []))
        self.assertEqual(state_from_words(["Boston"]), (None, ["Boston"]))

    def test_find_by_state_and_by_name(self):
        data, _ = make_data()
        self.assertEqual(len(data.by_state("FL")), 2)
        self.assertEqual([row["name"] for row in data.by_state("FL")][0], "MIAMI BEACH")
        self.assertEqual(data.find("Miami")[0]["id"], "8723214")

    def test_nearest_uses_real_haversine_distances(self):
        data, _ = make_data()
        rows = data.nearest(25.77, -80.13, limit=3)
        self.assertEqual([row["state"] for row in rows][:2], ["FL", "FL"])
        self.assertGreater(rows[2]["distance_km"], rows[1]["distance_km"])
        self.assertLess(rows[0]["distance_km"], 1.0)
        self.assertEqual([row["distance_km"] for row in rows],
                         sorted(row["distance_km"] for row in rows))
        # Portland, Maine is roughly 2,100 km from Miami Beach, so it must not lead.
        self.assertGreater(haversine_km((25.77, -80.13), (43.6567, -70.2467)), 1500)
        self.assertAlmostEqual(haversine_km((40.7005, -74.0142), (40.7005, -74.0142)), 0.0)

    def test_the_reading_is_parsed_with_its_quality_flag(self):
        data, _ = make_data()
        reading = data.observed("8518750")
        self.assertEqual(reading["time_local"], "2026-09-22 22:00")
        self.assertAlmostEqual(reading["height_ft"], 3.653)
        self.assertEqual(reading["quality"], "p")
        self.assertEqual(reading["station_name"], "The Battery")

    def test_a_station_without_a_live_gauge_returns_none_not_an_error(self):
        data, _ = make_data(water=None)
        self.assertIsNone(data.observed("8518750"))

    def test_predictions_are_always_asked_for_from_today(self):
        # Regression: with `range` alone NOAA starts three days back and 'next' becomes a lie.
        data, feed = make_data()
        data.events("8518750", hours=72)
        params = feed.prediction_params()[0]
        self.assertIn("begin_date", params)
        self.assertEqual(len(params["begin_date"]), 8)
        self.assertEqual(params["interval"], "hilo")
        self.assertEqual(params["station"], "8518750")

    def test_events_come_back_in_time_order_with_their_kind(self):
        data, _ = make_data()
        events = data.events("8518750")
        self.assertEqual([event["kind"] for event in events][:3], ["low", "high", "low"])
        self.assertEqual(events[0]["time_local"], "2026-09-23 00:31")
        self.assertEqual([event["time_local"] for event in events],
                         sorted(event["time_local"] for event in events))

    def test_the_tide_answer_is_anchored_to_the_gauge(self):
        data, _ = make_data()
        result = data.tide("8518750", count=4)
        self.assertTrue(result["anchored"])
        # The reading is 2026-09-22 22:00, so the 09-23 00:31 low is next and nothing stale shows.
        self.assertEqual(result["upcoming"][0]["time_local"], "2026-09-23 00:31")
        self.assertTrue(all(event["time_local"] > "2026-09-22 22:00" for event in result["upcoming"]))
        self.assertEqual(result["total_events"], len(PREDICTIONS["predictions"]))

    def test_a_stale_window_is_never_reported_as_next(self):
        data, _ = make_data(predictions=STALE)
        result = data.tide("8518750")
        self.assertEqual(result["upcoming"], [])

    def test_without_a_gauge_the_answer_says_it_is_not_anchored(self):
        data, _ = make_data(water=None)
        result = data.tide("8518750")
        self.assertFalse(result["anchored"])
        self.assertEqual(len(result["upcoming"]), 4)

    def test_a_dead_feed_raises_one_error_type(self):
        data, _ = make_data(fail="stations")
        with self.assertRaises(TidesError):
            data.stations()

    def test_an_empty_payload_is_rejected(self):
        data, _ = make_data(fail="no-predictions")
        with self.assertRaises(TidesError):
            data.events("8518750")


class RouteTests(unittest.TestCase):
    def test_next_high_tide(self):
        skill, params = route("when is the next high tide in Boston?")
        self.assertEqual(skill, "tide-next")
        self.assertEqual(params["query"], "Boston")
        self.assertEqual(params["kind"], "high")

    def test_low_tide(self):
        self.assertEqual(route("when is the next low tide in Portland?")[1]["kind"], "low")

    def test_now_is_a_reading_not_a_hi_lo_request(self):
        skill, params = route("what is the tide at The Battery right now?")
        self.assertEqual(skill, "tide-now")
        self.assertEqual(params["query"], "Battery")
        self.assertNotIn("kind", params)

    def test_the_battery_loses_its_leading_article(self):
        self.assertEqual(place_from_text("what is the tide at The Battery right now?"), "Battery")

    def test_station_lookup(self):
        skill, params = route("what tide stations are near Miami?")
        self.assertEqual(skill, "tide-stations")
        self.assertEqual(params["query"], "Miami")

    def test_a_state_alone_is_a_lookup(self):
        self.assertEqual(route("tides in Florida"), ("tide-stations", {"state": "FL"}))

    def test_a_station_id_wins(self):
        self.assertEqual(route("8518750")[1]["station"], "8518750")

    def test_coordinates(self):
        self.assertEqual(route("high tide at 25.77,-80.13")[1]["latitude"], 25.77)
        self.assertEqual(coords_from_text("-33.87,151.21"), (-33.87, 151.21))
        self.assertIsNone(coords_from_text("no numbers here"))

    def test_help(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")
        self.assertEqual(route("tell me a joke")[0], "help")

    def test_number_display(self):
        self.assertEqual(_number(9.22), "9.22")
        self.assertEqual(_number(7.0), "7")
        self.assertEqual(_number(None), "not available")


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
        agent_conn, client_conn = connected_pair()
        data, feed = make_data(**kw)
        from agent import TidesAgent

        agent = TidesAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client, feed
        finally:
            client.stop()

    def test_the_next_high_tide_is_a_high_tide(self):
        result, client, _ = self.turn("when is the next high tide in Boston?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("The next high tide for BOSTON, MA (8443970):", result["text"])
        self.assertIn("next HIGH 4.45 ft at 2026-09-23 06:38", result["text"])
        # Only highs: the interleaved lows are not what was asked for.
        self.assertNotIn("LOW", result["text"])
        self.assertIn("harmonic predictions", result["text"])
        self.assertIn(DATASET, result["text"])
        self.assertEqual(len(client.permission_requests), 1)

    def test_the_reading_is_reported_with_its_quality_flag(self):
        result, _, _ = self.turn("what is the tide at The Battery right now?")
        self.assertIn("NEW YORK (The Battery), NY (8518750)", result["text"])
        self.assertIn("the gauge reads 3.65 ft above MLLW at 2026-09-22 22:00", result["text"])
        self.assertIn("preliminary", result["text"])

    def test_a_state_lists_its_stations_instead_of_guessing_one(self):
        result, _, _ = self.turn("tides in Florida")
        self.assertIn("NOAA lists 2 tide stations in Florida", result["text"])
        self.assertIn("8723214 MIAMI BEACH", result["text"])

    def test_an_unknown_place_is_refused_with_a_way_forward(self):
        result, _, _ = self.turn("when is the next high tide in Xyzzyville?")
        self.assertIn("no NOAA tide station matching", result["text"])
        self.assertIn("8518750", result["text"])

    def test_a_station_without_a_gauge_says_so(self):
        result, _, _ = self.turn("when is the next high tide at The Battery?", water=None)
        self.assertIn("no live water level", result["text"])
        self.assertIn("cannot be anchored to now", result["text"])

    def test_help_does_not_ask_permission(self):
        result, client, _ = self.turn("what can you do?")
        self.assertIn("NOAA Tides and Currents", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _, _ = self.turn("when is the next high tide in Boston?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_a_dead_feed_is_reported(self):
        result, _, _ = self.turn("when is the next high tide in Boston?", fail="stations")
        self.assertIn("could not read the NOAA feed", result["text"])
        statuses = [u.get("status") for u in result["updates"] if u.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", statuses)


if __name__ == "__main__":
    unittest.main(verbosity=2)
