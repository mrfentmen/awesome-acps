"""Tests for the snowpack agent: station series, a state summary, routing and turns.

    python3 agents/snowpack/tests/test_agent.py

No network: the JSON is injected, shaped like the live payloads read on 2026-09-23 (station
301:CA:SNTL 'Adin Mtn', Modoc CA, 6170 ft; WTEQ 0.0 with median 0.0; a CO station holding 2.2 in
with its own median).
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

from agent import route, state_code_from_text  # noqa: E402
from data import (SnowData, SnowpackError, SnowpackNotFound, last_value,  # noqa: E402
                  percent_of_median)

STATIONS_ONE = [{"stationTriplet": "301:CA:SNTL", "name": "Adin Mtn", "stateCode": "CA",
                 "countyName": "Modoc", "elevation": 6170.0, "latitude": 41.2358333,
                 "longitude": -120.7919167, "networkCode": "SNTL"}]

STATIONS_MANY = [
    {"stationTriplet": "1031:CO:SNTL", "name": "Never Summer", "stateCode": "CO",
     "countyName": "Grand", "elevation": 10300.0, "latitude": 40.3, "longitude": -105.9},
    {"stationTriplet": "970:CO:SNTL", "name": "Jones Pass", "stateCode": "CO",
     "countyName": "Clear Creek", "elevation": 10430.0, "latitude": 39.7, "longitude": -105.8},
]


def values(*pairs):
    return [{"date": date, "value": value, "median": median} for date, value, median in pairs]


STATION_DATA = [{
    "stationTriplet": "301:CA:SNTL",
    #: The API returns snow depth before snow water equivalent; the reader must reorder.
    "data": [
        {"stationElement": {"elementCode": "SNWD"},
         "values": values(("2026-09-20", 1.0, 0.0), ("2026-09-21", 1.0, 0.0))},
        {"stationElement": {"elementCode": "WTEQ"},
         "values": values(("2026-09-19", None, None), ("2026-09-20", 0.0, 0.0),
                          ("2026-09-21", 0.5, 1.0))},
    ]}]

STATE_DATA = [
    {"stationTriplet": "1031:CO:SNTL", "data": [
        {"stationElement": {"elementCode": "WTEQ"},
         "values": values(("2026-09-21", 9.9, 5.5))}]},
    {"stationTriplet": "970:CO:SNTL", "data": [
        {"stationElement": {"elementCode": "WTEQ"},
         "values": values(("2026-09-21", 1.0, 4.0))}]},
    #: A station with nothing but zeros - off season, and it must not be called "with snow".
    {"stationTriplet": "1041:CO:SNTL", "data": [
        {"stationElement": {"elementCode": "WTEQ"},
         "values": values(("2026-09-21", 0.0, 0.0))}]},
]


class FakeFeeds:
    def __init__(self, stations=None, data=None, empty=False, fail=False):
        self.calls: list[str] = []
        self.params: list[dict] = []
        self.stations = STATIONS_ONE if stations is None else stations
        self.data = STATION_DATA if data is None else data
        self.empty = empty
        self.fail = fail

    def __call__(self, url: str, params: dict):
        self.calls.append(url)
        self.params.append(dict(params or {}))
        if self.fail:
            raise SnowpackError("the snow network is unreachable")
        if "stations" in url:
            return json.dumps(self.stations)
        if self.empty:
            return json.dumps([])
        return json.dumps(self.data)


def make_data(**kw):
    feed = FakeFeeds(**kw)
    return SnowData(fetch=feed), feed


class HelperTests(unittest.TestCase):
    def test_percent_of_normal_needs_a_real_median(self):
        self.assertEqual(percent_of_median(2.2, 5.5), 40.0)
        self.assertEqual(percent_of_median(0, 4.0), 0.0)
        self.assertIsNone(percent_of_median(1.0, 0.0))  # 0/0 is not "normal"
        self.assertIsNone(percent_of_median(None, 4.0))
        self.assertIsNone(percent_of_median("x", 4.0))

    def test_the_newest_reading_with_a_number_wins(self):
        self.assertEqual(last_value(values(("a", 1.0, 1.0), ("b", None, None)))["date"], "a")
        self.assertIsNone(last_value([]))
        self.assertIsNone(last_value(None))

    def test_a_state_by_name_or_by_a_code_written_as_one(self):
        self.assertEqual(state_code_from_text("how much water is in the California snowpack?"),
                         "CA")
        self.assertEqual(state_code_from_text("snow depth in Colorado right now"), "CO")
        self.assertEqual(state_code_from_text("snow in OR"), "OR")
        #: "or" is the commonest word in an English question, so it is not Oregon.
        self.assertIsNone(state_code_from_text("is there any snow or not"))


class ReadTests(unittest.TestCase):
    def test_a_station_puts_water_equivalent_first_and_carries_its_median(self):
        data, feed = make_data()
        station = data.station("301:CA:SNTL", days=10)
        self.assertEqual([series["code"] for series in station["series"]], ["WTEQ", "SNWD"])
        water = station["series"][0]
        self.assertEqual(water["label"], "snow water equivalent")
        self.assertEqual(water["unit"], "in")
        self.assertEqual(water["latest"]["value"], 0.5)
        self.assertEqual(water["latest"]["date"], "2026-09-21")
        self.assertEqual(water["percent_of_median"], 50.0)
        self.assertEqual(len(water["rows"]), 2)  # the row with no value is dropped
        self.assertEqual(station["station"]["name"], "Adin Mtn")
        self.assertEqual(station["station"]["elevation_ft"], 6170.0)
        self.assertEqual(feed.params[0]["centralTendencyType"], "MEDIAN")
        self.assertEqual(feed.params[0]["elements"], "WTEQ,SNWD")

    def test_a_station_that_reports_nothing_is_an_error(self):
        data, _feed = make_data(data=[{"stationTriplet": "301:CA:SNTL", "data": []}])
        with self.assertRaises(SnowpackNotFound):
            data.station("301:CA:SNTL")
        data, _feed = make_data(empty=True)
        with self.assertRaises(SnowpackNotFound):
            data.station("301:CA:SNTL")

    def test_a_state_summary_counts_what_has_snow_and_names_the_rest(self):
        data, feed = make_data(stations=STATIONS_MANY, data=STATE_DATA)
        summary = data.state("CO", days=7, limit=5)
        self.assertEqual(summary["stations"], 3)
        self.assertEqual(summary["with_snow"], 2)  # the all-zero station is not "with snow"
        self.assertEqual([row["triplet"] for row in summary["top"]],
                         ["1031:CO:SNTL", "970:CO:SNTL"])
        self.assertEqual(summary["top"][0]["name"], "Never Summer")
        self.assertEqual(summary["top"][0]["elevation_ft"], 10300.0)
        self.assertEqual(summary["top"][0]["percent_of_median"], 180.0)
        #: 180% and 25% - the all-zero station has no median to divide by, so it is left out.
        self.assertAlmostEqual(summary["average_percent_of_median"], 102.5)
        self.assertFalse(summary["medians_are_all_zero"])
        #: Every station worth naming is looked up in one call, not one call each.
        self.assertEqual(feed.params[-1]["stationTriplets"], "1031:CO:SNTL,970:CO:SNTL")

    def test_an_off_season_state_has_no_percent_of_normal(self):
        data, _feed = make_data(
            stations=[],
            data=[{"stationTriplet": "1041:CO:SNTL", "data": [
                {"stationElement": {"elementCode": "WTEQ"},
                 "values": values(("2026-09-21", 0.0, 0.0))}]}])
        summary = data.state("CO")
        self.assertEqual(summary["with_snow"], 0)
        self.assertIsNone(summary["average_percent_of_median"])
        self.assertTrue(summary["medians_are_all_zero"])
        self.assertEqual(summary["top"], [])

    def test_an_unknown_snow_state_and_an_empty_one(self):
        data, _feed = make_data()
        with self.assertRaises(ValueError):
            data.state("FL")
        data, _feed = make_data(empty=True)
        with self.assertRaises(SnowpackNotFound):
            data.state("CO")

    def test_a_dead_network(self):
        data, _feed = make_data(fail=True)
        with self.assertRaises(SnowpackError):
            data.station("301:CA:SNTL")
        with self.assertRaises(SnowpackError):
            data.state("CO")


class RouteTests(unittest.TestCase):
    def test_a_state_snow_question(self):
        self.assertEqual(route("how much water is in the California snowpack?"),
                         ("snowpack-state", {"state": "CA"}))
        self.assertEqual(route("snow depth in Colorado right now"),
                         ("snowpack-state", {"state": "CO", "element": "SNWD"}))

    def test_a_station_question_needs_a_snow_word(self):
        self.assertEqual(route("snow water equivalent at station 301:CA:SNTL"),
                         ("snowpack-station", {"triplet": "301:CA:SNTL"}))
        self.assertEqual(route("snow at 301:CA:SNTL over the last 60 days"),
                         ("snowpack-station", {"triplet": "301:CA:SNTL", "days": 60}))
        #: A triplet on its own is a reading of the id, not a snow question.
        self.assertEqual(route("301:CA:SNTL")[0], "help")

    def test_a_ski_question_is_not_a_snowpack_question(self):
        self.assertEqual(route("what are the ski conditions?")[0], "help")

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
        from agent import SnowpackAgent

        agent_conn, client_conn = connected_pair()
        data, feed = make_data(**kw)
        agent = SnowpackAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client, feed
        finally:
            client.stop()

    def test_a_station_answer(self):
        result, client, _ = self.turn("snow water equivalent at station 301:CA:SNTL")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertEqual(len(client.permission_requests), 1)
        self.assertIn("Adin Mtn (301:CA:SNTL)", result["text"])
        self.assertIn("location: Modoc, CA", result["text"])
        self.assertIn("elevation: 6170 ft", result["text"])
        self.assertIn("coordinates: 41.23583, -120.79192", result["text"])
        self.assertIn("snow water equivalent: 0.5 in on 2026-09-21", result["text"])
        self.assertIn("median for the day: 1 in, 50% of normal", result["text"])

    def test_a_state_answer_reports_the_spread_and_the_season(self):
        result, _, _ = self.turn("how much water is in the California snowpack?",
                                 stations=STATIONS_MANY, data=STATE_DATA)
        self.assertIn("CA - snow water equivalent (in)", result["text"])
        self.assertIn("3 station(s) reported a value; 2 of them have any", result["text"])
        self.assertIn("102% of normal on average", result["text"])
        self.assertIn("Never Summer (1031:CO:SNTL, 10300 ft): 9.9 in on 2026-09-21", result["text"])
        self.assertIn("180% of normal", result["text"])
        self.assertIn("snow water equivalent - inches of water", result["text"])

    def test_an_off_season_state_says_so_plainly(self):
        result, _, _ = self.turn(
            "how much water is in the Colorado snowpack?", stations=[],
            data=[{"stationTriplet": "1041:CO:SNTL", "data": [
                {"stationElement": {"elementCode": "WTEQ"},
                 "values": values(("2026-09-21", 0.0, 0.0))}]}])
        self.assertIn("0 of them have any", result["text"])
        self.assertIn("percent of normal does not exist off season", result["text"])

    def test_help_asks_nothing(self):
        result, client, feed = self.turn("what can you do?")
        self.assertIn("snow water equivalent", result["text"])
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(feed.calls, [])

    def test_permission_denied(self):
        result, _, _ = self.turn("how much water is in the California snowpack?",
                                 permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)

    def test_a_dead_network(self):
        result, _, _ = self.turn("how much water is in the California snowpack?", fail=True)
        self.assertIn("I could not read the snow network", result["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
