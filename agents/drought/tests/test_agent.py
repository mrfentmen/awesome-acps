"""Tests for the drought agent: CSV parsing, state and county reads, routing and turns.

    python3 agents/drought/tests/test_agent.py

No network: the CSV is injected, shaped like the live one read on 2026-09-23 (California
2026-09-15: None 34.77, D0 65.23, D1 22.32, D2 0.04; Harris County TX: D0 100.00, D1 16.48).
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

from agent import place_from_text, route  # noqa: E402
from data import DroughtData, DroughtError, fips_for, number  # noqa: E402

HEADER = ("MapDate,StateAbbreviation,None,D0,D1,D2,D3,D4,ValidStart,ValidEnd,"
          "StatisticFormatID\r\n")
STATE_CSV = (HEADER
             + "20260915,CA,34.77,65.23,22.32,0.04,0.00,0.00,2026-09-15,2026-09-21,1\r\n"
             + "20260908,CA,34.77,65.23,18.79,1.30,0.00,0.00,2026-09-08,2026-09-14,1\r\n"
             + "20260901,CA,34.77,65.23,22.34,0.04,0.00,0.00,2026-09-01,2026-09-07,1\r\n")

COUNTY_CSV = ("MapDate,FIPS,County,State,None,D0,D1,D2,D3,D4,ValidStart,ValidEnd,"
              "StatisticFormatID\r\n"
              "20260915,48201,Harris County,TX,0.00,100.00,16.48,0.00,0.00,0.00,"
              "2026-09-15,2026-09-21,1\r\n"
              "20260908,48201,Harris County,TX,0.00,100.00,0.00,0.00,0.00,0.00,"
              "2026-09-08,2026-09-14,1\r\n")

NO_DATA_CSV = (HEADER
               + "20260915,CA,34.77,-99,--,,,0.00,2026-09-15,2026-09-21,1\r\n")


class FakeFeeds:
    def __init__(self, text=STATE_CSV, county_text=COUNTY_CSV, fail=False):
        self.calls: list[str] = []
        self.params: list[dict] = []
        self.text = text
        self.county_text = county_text
        self.fail = fail

    def __call__(self, url: str, params: dict):
        self.calls.append(url)
        self.params.append(dict(params or {}))
        if self.fail:
            raise DroughtError("the monitor is unreachable")
        return self.county_text if "CountyStatistics" in url else self.text


def make_data(**kw):
    feed = FakeFeeds(**kw)
    return DroughtData(fetch=feed), feed


class HelperTests(unittest.TestCase):
    def test_a_state_by_name_code_or_county_fips(self):
        self.assertEqual(fips_for("California"), "06")
        self.assertEqual(fips_for("CALIFORNIA"), "06")
        self.assertEqual(fips_for("CA"), "06")
        self.assertEqual(fips_for("ca"), "06")
        self.assertEqual(fips_for("48201"), "48201")
        self.assertIsNone(fips_for("France"))
        self.assertIsNone(fips_for(""))

    def test_the_no_data_markers(self):
        self.assertIsNone(number(""))
        self.assertIsNone(number("-99"))
        self.assertIsNone(number("None"))
        self.assertIsNone(number(None))
        self.assertEqual(number("65.23"), 65.23)
        self.assertEqual(number("0.00"), 0.0)


class ReadTests(unittest.TestCase):
    def test_a_state_read_sorts_oldest_first_and_keeps_the_newest_as_latest(self):
        data, feed = make_data()
        reading = data.state("California", weeks=4)
        self.assertEqual(reading["place"], "CA")
        self.assertEqual([row["date"] for row in reading["rows"]],
                         ["2026-09-01", "2026-09-08", "2026-09-15"])
        self.assertEqual(reading["latest"]["date"], "2026-09-15")
        self.assertEqual(reading["latest"]["d0"], 65.23)
        self.assertEqual(reading["latest"]["none"], 34.77)
        self.assertEqual(reading["latest"]["week_ends"], "2026-09-21")
        self.assertEqual(feed.params[-1]["aoi"], "06")
        self.assertEqual(feed.params[-1]["statisticsType"], "1")

    def test_only_the_requested_weeks_are_kept(self):
        data, _feed = make_data()
        self.assertEqual(len(data.state("CA", weeks=2)["rows"]), 2)

    def test_a_county_read_names_the_county(self):
        data, feed = make_data()
        reading = data.county("48201", weeks=2)
        self.assertEqual(reading["place"], "Harris County, TX")
        self.assertEqual(reading["kind"], "county")
        self.assertEqual(reading["latest"]["d1"], 16.48)
        self.assertIn("CountyStatistics", feed.calls[-1])
        self.assertEqual(feed.params[-1]["aoi"], "48201")

    def test_read_picks_county_or_state_by_the_shape_of_the_code(self):
        data, _feed = make_data()
        self.assertEqual(data.read("48201")["kind"], "county")
        self.assertEqual(data.read("California")["kind"], "state")

    def test_an_unknown_place_and_an_unknown_county(self):
        data, _feed = make_data()
        with self.assertRaises(ValueError):
            data.state("France")
        with self.assertRaises(ValueError):
            data.county("06")  # a state code is not a county

    def test_a_week_the_monitor_has_no_figure_for(self):
        data, _feed = make_data(text=NO_DATA_CSV)
        latest = data.state("CA")["latest"]
        self.assertIsNone(latest["d0"])
        self.assertIsNone(latest["d1"])
        self.assertIsNone(latest["d2"])

    def test_empty_and_dead_responses(self):
        data, _feed = make_data(text=HEADER)
        with self.assertRaises(DroughtError):
            data.state("CA")
        data, _feed = make_data(fail=True)
        with self.assertRaises(DroughtError):
            data.state("CA")


class RouteTests(unittest.TestCase):
    def test_a_drought_question_names_the_state(self):
        self.assertEqual(route("how dry is California?"),
                         ("drought-place", {"place": "california"}))
        self.assertEqual(route("is Texas in drought right now?"),
                         ("drought-place", {"place": "texas"}))

    def test_a_window(self):
        self.assertEqual(route("drought in CA over the last 8 weeks"),
                         ("drought-place", {"place": "ca", "weeks": 8}))

    def test_a_county_fips(self):
        self.assertEqual(route("how dry is county 48201?"),
                         ("drought-place", {"place": "48201"}))

    def test_a_bare_state_is_still_a_drought_question(self):
        self.assertEqual(route("California"), ("drought-place", {"place": "california"}))

    def test_another_kind_of_weather_question_is_not_answered_with_a_drought_table(self):
        self.assertEqual(route("what is the weather in Texas?")[0], "help")
        self.assertEqual(route("rain in Indiana")[0], "help")

    def test_the_preposition_in_is_not_indiana(self):
        self.assertEqual(place_from_text("what is the weather in Paris?"), None)
        self.assertEqual(place_from_text("drought in IN"), "in")

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
        from agent import DroughtAgent

        agent_conn, client_conn = connected_pair()
        data, feed = make_data(**kw)
        agent = DroughtAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client, feed
        finally:
            client.stop()

    def test_a_drought_answer_explains_the_categories_and_the_week(self):
        result, client, _ = self.turn("how dry is California?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertEqual(len(client.permission_requests), 1)
        self.assertIn("CA - drought as of 2026-09-15", result["text"])
        self.assertIn("week ending 2026-09-21", result["text"])
        self.assertIn("65.23% of the area is at least abnormally dry", result["text"])
        self.assertIn("22.32% at least moderate drought (D1+)", result["text"])
        self.assertIn("34.77% has no drought at all", result["text"])
        self.assertIn("cumulative", result["text"])

    def test_a_trend_across_weeks(self):
        result, _, _ = self.turn("drought in CA over the last 4 weeks")
        self.assertIn("previous weeks (D0 or worse): 2026-09-01 65.23%, "
                      "2026-09-08 65.23%", result["text"])
        self.assertIn("versus 2026-09-01: +0.00 points", result["text"])

    def test_a_county_answer(self):
        result, _, _ = self.turn("how dry is county 48201?")
        self.assertIn("Harris County, TX", result["text"])
        self.assertIn("100.00% of the area is at least abnormally dry", result["text"])
        self.assertIn("16.48% at least moderate drought", result["text"])

    def test_help_asks_nothing(self):
        result, client, feed = self.turn("what can you do?")
        self.assertIn("Drought Monitor", result["text"])
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(feed.calls, [])

    def test_permission_denied(self):
        result, _, _ = self.turn("how dry is California?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)

    def test_a_dead_monitor(self):
        result, _, _ = self.turn("how dry is California?", fail=True)
        self.assertIn("I could not read the Drought Monitor", result["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
