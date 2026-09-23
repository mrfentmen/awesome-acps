"""Tests for the buoys agent: NDBC parsing, station search, routing, live turns.

    python3 agents/buoys/tests/test_agent.py

Every feed answer is injected (no network). The NDBC realtime file is text, so the fake
transport returns the two fixtures below: the station XML and one realtime table, shaped
exactly like the live files verified on 2026-09-22.
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

from agent import BuoysAgent, compass, place_from_text, route, station_from_text  # noqa: E402
from data import DATASET, BuoysError, NdbcData, reading, station_id  # noqa: E402

STATIONS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<stations created="2026-09-22T23:00:00UTC" count="4">
  <station id="41025" lat="35.006" lon="-75.402" name="Diamond Shoals, NC" owner="NDBC" pgm="MOORED BUOY" type="buoy"/>
  <station id="46042" lat="36.787" lon="-122.408" name="MONTEREY - 27NM WNW of Monterey, CA" owner="NDBC" pgm="MOORED BUOY" type="buoy"/>
  <station id="46236" lat="36.759" lon="-121.95" name="Monterey Canyon Outer, CA  (156)" owner="NDBC" pgm="MOORED BUOY" type="buoy"/>
  <station id="meyc1" lat="36.605" lon="-121.889" name="9413450 - Monterey, CA" owner="NOS" pgm="TIDE GAUGE" type="fixed"/>
</stations>
"""

#: Real shape of a realtime2 file: names row, units row, then reports newest-first.
REALTIME_41025 = """#YY  MM DD hh mm WDIR WSPD GST  WVHT   DPD   APD MWD   PRES  ATMP  WTMP  DEWP  VIS PTDY  TIDE
#yr  mo dy hr mn degT m/s  m/s     m   sec   sec degT   hPa  degC  degC  degC  nmi  hPa
2026 09 22 23 50  20 11.0 13.0   1.6     9   5.7  35 1014.6  25.5    MM  23.6   MM   MM
2026 09 22 22 50  15 10.0 12.0   1.5     8   5.5  30 1014.0  25.0  24.0  23.0   MM   MM
2026 09 22 21 50  10  9.0 11.0   1.4     8   5.4  28 1013.8  24.8  24.1  22.9   MM   MM
"""

REALTIME_46042 = REALTIME_41025.replace("1.6", "2.4")
REALTIME_46236 = REALTIME_41025.replace("1.6", "1.0")


class FakeFeed:
    """Dispatch on the NDBC URL; records every call so the reader can be checked."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    def __call__(self, url: str, params: dict):
        if self.fail:
            raise BuoysError("NDBC is offline")
        self.calls.append(url)
        if url.endswith("activestations.xml"):
            return STATIONS_XML
        if url.endswith("41025.TXT") or url.endswith("41025.txt"):
            return REALTIME_41025
        if url.endswith("46042.TXT") or url.endswith("46042.txt"):
            return REALTIME_46042
        if url.endswith("46236.TXT") or url.endswith("46236.txt"):
            return REALTIME_46236
        raise BuoysError(f"unexpected NDBC url {url}")


def live_data(fail: bool = False) -> tuple[NdbcData, FakeFeed]:
    feed = FakeFeed(fail=fail)
    return NdbcData(fetch=feed), feed


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
    def test_missing_readings_are_not_zero(self):
        self.assertIsNone(reading("MM"))
        self.assertIsNone(reading(""))
        self.assertEqual(reading("1.6"), 1.6)
        self.assertEqual(reading("1014.6"), 1014.6)
        self.assertIsNone(reading("nonsense"))

    def test_station_ids(self):
        self.assertEqual(station_id("41025"), "41025")
        self.assertEqual(station_id("MEYC1"), "meyc1")
        self.assertEqual(station_id("buoy 46042 please"), "46042")
        with self.assertRaises(ValueError):
            station_id("the pier")

    def test_compass_labels(self):
        self.assertEqual(compass(0), "N")
        self.assertEqual(compass(20), "NNE")
        self.assertEqual(compass(292), "WNW")
        self.assertEqual(compass(180), "S")
        self.assertIsNone(compass(None))

    def test_stations_are_parsed_with_coordinates(self):
        data, _ = live_data()
        rows = data.stations()
        self.assertEqual(len(rows), 4)
        diamond = data.station("41025")
        self.assertEqual(diamond["name"], "Diamond Shoals, NC")
        self.assertAlmostEqual(diamond["latitude"], 35.006)
        self.assertAlmostEqual(diamond["longitude"], -75.402)
        self.assertEqual(diamond["type"], "buoy")

    def test_an_unknown_station_is_refused(self):
        data, _ = live_data()
        with self.assertRaises(ValueError):
            data.station("99999")

    def test_find_stations_ranks_by_words_matched_then_type(self):
        data, _ = live_data()
        matches = data.find_stations("Monterey")
        self.assertEqual(len(matches), 3)
        self.assertEqual(matches[0]["type"], "buoy")
        self.assertIn("monterey", matches[0]["name"].lower())
        self.assertEqual(data.find_stations("nowhere at all"), [])

    def test_latest_keeps_columns_units_and_time(self):
        data, _ = live_data()
        result = data.latest("41025", rows=3)
        self.assertEqual(result["station"], "41025")
        self.assertEqual(result["reports"][0]["time_utc"], "2026-09-22T23:50Z")
        self.assertEqual(result["reports"][0]["values"]["WVHT"], 1.6)
        self.assertEqual(result["reports"][1]["time_utc"], "2026-09-22T22:50Z")
        self.assertIn("TIDE", result["columns"])
        self.assertEqual(result["reports"][0]["raw"]["WTMP"], "MM")
        self.assertIsNone(result["reports"][0]["values"]["WTMP"])
        self.assertEqual(result["reports"][0]["units"]["WVHT"], "m")

    def test_conditions_summarise_the_newest_report(self):
        data, _ = live_data()
        result = data.conditions("41025")
        self.assertEqual(result["readings"]["WVHT"], 1.6)
        self.assertEqual(result["readings"]["WSPD"], 11.0)
        self.assertIsNone(result["readings"]["WTMP"])
        self.assertEqual(result["station"]["id"], "41025")

    def test_an_offline_feed_raises_one_error_type(self):
        data, _ = live_data(fail=True)
        with self.assertRaises(BuoysError):
            data.stations()


class RouteTests(unittest.TestCase):
    def test_station_id_in_a_question(self):
        skill, params = route("what are the conditions at buoy 41025?")
        self.assertEqual(skill, "buoy-conditions")
        self.assertEqual(params["station"], "41025")

    def test_waves_question_with_a_place(self):
        skill, params = route("how big are the waves at the Monterey buoy?")
        self.assertEqual(skill, "buoy-conditions")
        self.assertEqual(params["query"], "Monterey")
        self.assertNotIn("station", params)

    def test_find_question(self):
        skill, params = route("what buoys are near Monterey?")
        self.assertEqual(skill, "buoy-find")
        self.assertEqual(params["query"], "Monterey")

    def test_recent_question(self):
        skill, params = route("last few reports from 46042")
        self.assertEqual(skill, "buoy-recent")
        self.assertEqual(params["station"], "46042")

    def test_helpers(self):
        self.assertIsNone(station_from_text("how rough is it?"))
        self.assertEqual(station_from_text("station meyc1"), "meyc1")
        self.assertIsNone(place_from_text("what can you do?"))

    def test_help(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")


class TurnTests(unittest.TestCase):
    def turn(self, text: str, fail: bool = False, permission: str = "allow-once"):
        agent_conn, client_conn = connected_pair()
        data, _ = live_data(fail=fail)
        agent = BuoysAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_conditions_answer(self):
        result, client = self.turn("what are the conditions at buoy 41025?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("Diamond Shoals, NC (41025, buoy)", result["text"])
        self.assertIn("Waves: 1.6 m significant height with a dominant period of 9 s", result["text"])
        self.assertIn("Wind: 11 m/s from NNE (20 degrees), gusting 13 m/s", result["text"])
        self.assertIn("Water temperature: not reported", result["text"])
        self.assertIn(DATASET, result["text"])
        self.assertEqual(len(client.permission_requests), 1)

    def test_place_question_finds_a_station(self):
        result, _ = self.turn("how big are the waves at the Monterey buoy?")
        self.assertIn("found by named", result["text"])
        self.assertIn("Monterey", result["text"])
        self.assertIn("Waves:", result["text"])

    def test_find_lists_stations(self):
        result, _ = self.turn("what buoys are near Monterey?")
        self.assertIn("46042", result["text"])
        self.assertIn("meyc1", result["text"])

    def test_recent_lists_a_series(self):
        result, _ = self.turn("last few reports from 46042")
        self.assertIn("2026-09-22T23:50Z: waves 2.4 m", result["text"])

    def test_unknown_station_is_refused_not_guessed(self):
        result, client = self.turn("how are the conditions at buoy 99999?")
        self.assertIn("no active station", result["text"])
        self.assertEqual(len(client.permission_requests), 1)

    def test_streaming_is_chunked_under_one_message_id(self):
        result, _ = self.turn("what are the conditions at buoy 41025?")
        chunks = [u for u in result["updates"] if u.get("sessionUpdate") == "agent_message_chunk"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len({chunk["messageId"] for chunk in chunks}), 1)

    def test_help_does_not_ask_permission(self):
        result, client = self.turn("what can you do?")
        self.assertIn("NDBC", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _ = self.turn("what are the conditions at buoy 41025?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_a_feed_failure_is_reported(self):
        result, _ = self.turn("what are the conditions at buoy 41025?", fail=True)
        self.assertIn("could not read the NDBC feed", result["text"])
        statuses = [u.get("status") for u in result["updates"] if u.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", statuses)


if __name__ == "__main__":
    unittest.main()
