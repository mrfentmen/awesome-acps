"""Tests for the alert agent: the condition checks, routing, bounds and the silent wait.

    python3 agents/alert/tests/test_agent.py

No network: the five checks are injected as dicts shaped like the live payloads verified on
2026-09-23. The subject here is the *silence*: an alert that fires must send exactly one alert,
and a watch that does not fire must send none - never a stream of "still 154" messages.
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

import render_values  # noqa: E402
from agent import MAX_ROUNDS, bounds, render_alert, route  # noqa: E402
from data import AlertData, AlertError  # noqa: E402

PLACE = {"results": [{"name": "Delhi", "latitude": 28.6519, "longitude": 77.2315,
                      "country": "India", "admin1": "Delhi"}]}


class FakeFeeds:
    """Serves the five condition feeds and records every (url, params) pair."""

    def __init__(self, aqi=210.0, temperature=41.5, quakes=(4.6, 5.1), debt="41112118151111.92",
                 category="no_flooding", stage=13.21):
        self.calls: list[tuple[str, dict]] = []
        self.aqi = aqi
        self.temperature = temperature
        self.quakes = quakes
        self.debt = debt
        self.category = category
        self.stage = stage

    def __call__(self, url: str, params: dict):
        self.calls.append((url, dict(params)))
        if "geocoding" in url:
            return dict(PLACE)
        if "air-quality" in url:
            return {"current": {"us_aqi": self.aqi}}
        if "open-meteo.com/v1/forecast" in url:
            return {"current": {"temperature_2m": self.temperature}}
        if "/nwps/v1/gauges/" in url:
            return {"name": "MISSISSIPPI RIVER AT ST. LOUIS", "usgsId": "07010000",
                    "status": {"observed": {"primary": self.stage, "floodCategory": self.category}},
                    "floodThresholds": {"action": 24.0, "minor": 28.0, "moderate": 34.0}}
        if "earthquake" in url:
            features = [{"id": f"q{index}", "properties": {"mag": magnitude,
                                                           "place": f"place {index}",
                                                           "time": 1_700_000_000_000 + index}}
                        for index, magnitude in enumerate(self.quakes)]
            return {"type": "FeatureCollection", "features": features}
        if "fiscaldata" in url:
            return {"data": [{"record_date": "2026-09-21", "tot_pub_debt_out_amt": self.debt}]}
        raise AssertionError(f"unexpected url {url}")


def make_data(**kw) -> tuple[AlertData, FakeFeeds]:
    feed = FakeFeeds(**kw)
    return AlertData(fetch=feed), feed


class CheckTests(unittest.TestCase):
    def test_air_quality_is_met_against_the_threshold(self):
        data, _ = make_data(aqi=210.0)
        reading = data.check("aqi", direction="above", threshold=200, place="Delhi")
        self.assertEqual(reading["value"], 210.0)
        self.assertTrue(reading["met"])
        self.assertEqual(reading["label"], "US AQI in Delhi")
        self.assertFalse(data.check("aqi", direction="above", threshold=250,
                                    place="Delhi")["met"])

    def test_a_below_threshold_is_honoured(self):
        data, _ = make_data(temperature=41.5)
        self.assertTrue(data.check("temperature", direction="above", threshold=40,
                                   place="Delhi")["met"])
        self.assertFalse(data.check("temperature", direction="above", threshold=45,
                                    place="Delhi")["met"])
        self.assertTrue(data.check("temperature", direction="below", threshold=42,
                                   place="Delhi")["met"])

    def test_debt_needs_a_threshold_and_reports_dollars(self):
        data, _ = make_data()
        reading = data.check("debt", threshold=41_000_000_000_000)
        self.assertEqual(reading["value"], 41112118151111.92)
        self.assertTrue(reading["met"])
        self.assertFalse(data.check("debt", threshold=42_000_000_000_000)["met"])
        # with no threshold there is nothing to be above, so a watch never fires on it
        self.assertFalse(data.check("debt")["met"])

    def test_a_gauge_is_met_only_when_noaa_says_it_is_flooding(self):
        quiet, _ = make_data(category="no_flooding")
        reading = quiet.check("flood", gauge="07010000")
        self.assertEqual(reading["value"], 13.21)
        self.assertFalse(reading["met"])
        rising, _ = make_data(category="minor")
        self.assertTrue(rising.check("flood", gauge="07010000")["met"])

    def test_a_gauge_id_must_look_like_one(self):
        data, _ = make_data()
        with self.assertRaises(ValueError):
            data.floods("the mississippi")
        with self.assertRaises(ValueError):
            data.check("flood")

    def test_a_new_quake_fires_once_and_never_again_for_the_same_event(self):
        data, _ = make_data(quakes=(4.6, 5.1))
        first = data.check("quakes", threshold=5.0)
        self.assertTrue(first["met"])
        self.assertEqual(first["value"], 5.1)
        self.assertEqual(first["event"]["place"], "place 1")
        second = data.check("quakes", threshold=5.0)
        self.assertFalse(second["met"])
        self.assertEqual(second["watched"], 1)

    def test_a_quake_below_the_threshold_is_ignored(self):
        data, _ = make_data(quakes=(2.1, 2.4))
        reading = data.check("quakes", threshold=5.0)
        self.assertFalse(reading["met"])
        self.assertEqual(reading["watched"], 0)
        self.assertIsNone(reading["value"])

    def test_an_unknown_place_and_an_unknown_condition_are_errors(self):
        data, _ = make_data()
        with self.assertRaises(ValueError):
            data.check("the price of cheese")
        data, _ = make_data()
        data._fetch = lambda url, params: (_ for _ in ()).throw(AlertError("down"))
        with self.assertRaises(AlertError):
            data.check("aqi", threshold=200, place="Delhi")


class ValueTests(unittest.TestCase):
    def test_big_numbers_read_like_words(self):
        self.assertEqual(render_values.number(41_112_118_151_111.92), "41.1 trillion")
        self.assertEqual(render_values.number(41_000_000_000_000), "41.0 trillion")
        self.assertEqual(render_values.number(4_500_000_000), "4.5 billion")
        self.assertEqual(render_values.number(2_300_000), "2.3 million")

    def test_small_numbers_keep_their_point(self):
        self.assertEqual(render_values.number(210.0), "210")
        self.assertEqual(render_values.number(13.21), "13.21")
        self.assertEqual(render_values.number(0.5), "0.5")
        self.assertEqual(render_values.number(1234), "1,234")

    def test_missing_values_are_said_not_guessed(self):
        self.assertEqual(render_values.number(None), "n/a")
        self.assertEqual(render_values.number("not a number"), "n/a")


class RenderTests(unittest.TestCase):
    def test_an_aqi_alert_names_the_reading_and_the_threshold(self):
        reading = {"condition": "aqi", "value": 213.0, "unit": " AQI", "label": "US AQI in Delhi",
                   "source": "Open-Meteo air quality"}
        text = render_alert(reading, {"threshold": 200.0, "direction": "above"}, 3, 60.0)
        self.assertIn("ALERT - US AQI in Delhi is 213 AQI, above your threshold of 200", text)
        self.assertIn("Checked 3 time(s) over 60s", text)
        self.assertIn("Open-Meteo air quality, read live", text)

    def test_a_flood_alert_prints_the_stages_instead_of_a_threshold(self):
        reading = {"condition": "flood", "value": 29.4, "unit": " ft",
                   "label": "MISSISSIPPI RIVER AT ST. LOUIS (gauge 07010000)",
                   "category": "minor", "thresholds": {"action": 24.0, "minor": 28.0},
                   "source": "NOAA National Water Prediction Service (api.water.noaa.gov)"}
        text = render_alert(reading, {"direction": "above"}, 1, 0.2)
        self.assertIn("is 29.4 ft", text)
        self.assertIn("NOAA's category for this gauge right now: minor", text)
        self.assertIn("action 24, minor 28", text)
        self.assertIn("Threshold: above any", text)

    def test_a_quake_alert_reports_the_event(self):
        reading = {"condition": "quakes", "value": 5.2, "unit": " M",
                   "label": "earthquakes at or above M5.0",
                   "event": {"magnitude": 5.2, "place": "off the coast", "url": "https://x/1"},
                   "when": "03:14 UTC", "first": False, "source": "USGS earthquakes"}
        text = render_alert(reading, {"threshold": 5.0, "direction": "above"}, 2, 30.0)
        self.assertIn("Event M5.2 - off the coast at 03:14 UTC", text)
        self.assertIn("https://x/1", text)
        self.assertNotIn("already under way", text)

    def test_an_event_already_in_progress_says_so(self):
        reading = {"condition": "quakes", "value": 5.2, "unit": " M", "label": "quakes",
                   "event": {"magnitude": 5.2, "place": "somewhere", "url": None},
                   "when": "", "first": True, "source": "USGS earthquakes"}
        self.assertIn("already under way", render_alert(reading, {"threshold": 5.0}, 1, 1.0))


class BoundsTests(unittest.TestCase):
    def test_defaults_are_bounded(self):
        self.assertEqual(bounds(None, None), (20, 30.0))

    def test_the_caps_hold(self):
        rounds, gap = bounds(100, 10)
        self.assertEqual(rounds, MAX_ROUNDS)
        self.assertEqual(gap, 10.0)
        rounds, gap = bounds(5, 10_000)
        self.assertEqual(gap, 300.0)
        self.assertEqual(rounds, 3)  # 5 * 300s would be 1500s, over the 900s ceiling

    def test_the_whole_watch_can_never_outrun_the_ceiling(self):
        rounds, gap = bounds(100, 1000)
        self.assertEqual(gap, 300.0)
        self.assertEqual(rounds, 3)  # 3 * 300s = 900s, the hard ceiling

    def test_nonsense_is_replaced_not_crashed_on(self):
        self.assertEqual(bounds("many", "soon"), (20, 30.0))
        self.assertEqual(bounds(0, 0), (1, 1.0))


class RouteTests(unittest.TestCase):
    def test_air_quality(self):
        skill, params = route("alert me when the air quality in Delhi passes 200")
        self.assertEqual(skill, "alert-watch")
        self.assertEqual(params["condition"], "aqi")
        self.assertEqual(params["place"], "Delhi")
        self.assertEqual(params["threshold"], 200.0)
        self.assertEqual(params["direction"], "above")

    def test_a_place_after_the_number(self):
        _skill, params = route("alert me when the air quality passes 200 in Delhi")
        self.assertEqual(params["place"], "Delhi")

    def test_temperature_both_ways(self):
        _skill, params = route("tell me when the temperature in Phoenix goes above 40")
        self.assertEqual(params["condition"], "temperature")
        self.assertEqual(params["place"], "Phoenix")
        self.assertEqual(params["direction"], "above")
        _skill, params = route("alert me when the temperature in Phoenix drops below 5")
        self.assertEqual(params["direction"], "below")

    def test_quakes(self):
        skill, params = route("alert me when quakes above 5 happen")
        self.assertEqual(skill, "alert-watch")
        self.assertEqual(params["condition"], "quakes")
        self.assertEqual(params["threshold"], 5.0)

    def test_flood_needs_a_gauge_id(self):
        _skill, params = route("tell me when gauge 07010000 reaches flood stage")
        self.assertEqual(params["condition"], "flood")
        self.assertEqual(params["gauge"], "07010000")
        self.assertEqual(route("tell me when the river floods")[0], "help")

    def test_debt_in_trillions(self):
        _skill, params = route("alert me when the national debt passes 41 trillion")
        self.assertEqual(params["condition"], "debt")
        self.assertEqual(params["threshold"], 41_000_000_000_000.0)

    def test_an_interval_and_a_number_of_checks(self):
        _skill, params = route("alert me when the air quality in Delhi passes 200 every 30 "
                               "seconds for 5 checks")
        self.assertEqual(params["interval"], 30.0)
        self.assertEqual(params["rounds"], 5)
        self.assertEqual(route("watch for quakes above 5 every 2 minutes")[1]["interval"], 120.0)

    def test_without_a_place_or_a_threshold_it_asks_instead_of_guessing(self):
        self.assertEqual(route("alert me when the air quality passes")[0], "help")
        self.assertEqual(route("alert me when the air quality in Delhi is 200")[0], "help")

    def test_help(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")
        self.assertEqual(route("tell me when the debt passes 41 trillion then book a flight")[0],
                         "alert-watch")


class FakeAlert:
    """Stands in for AlertData: fixed readings, and a record of every check."""

    def __init__(self, met=False, fail: str | None = None):
        self.met = met
        self.fail = fail
        self.checks: list[dict] = []

    def check(self, condition, direction="above", threshold=None, place=None, gauge=None):
        self.checks.append({"condition": condition, "direction": direction,
                            "threshold": threshold, "place": place, "gauge": gauge})
        if self.fail:
            raise AlertError(self.fail)
        return {"condition": condition, "value": 213.0, "unit": " AQI",
                "label": f"US AQI in {place or 'nowhere'}", "met": self.met,
                "source": "Open-Meteo air quality"}


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
        from agent import AlertAgent

        agent_conn, client_conn = connected_pair()
        data = FakeAlert(**kw)
        agent = AlertAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client, data
        finally:
            client.stop()

    def test_a_condition_that_holds_sends_exactly_one_alert(self):
        result, client, data = self.turn("alert me when the air quality in Delhi passes 200",
                                         met=True)
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertEqual(len(client.permission_requests), 1)
        self.assertEqual(len(data.checks), 1)
        self.assertEqual(data.checks[0]["place"], "Delhi")
        self.assertEqual(result["text"].count("ALERT -"), 1)
        self.assertIn("US AQI in Delhi is 213 AQI, above your threshold of 200", result["text"])
        completed = [update for update in result["updates"]
                     if update.get("sessionUpdate") == "tool_call_update"
                     and update.get("status") == "completed"]
        self.assertEqual(len(completed), 1)
        summary = "".join(block["content"]["text"] for block in completed[0]["content"])
        self.assertIn("Condition met after 1 check(s)", summary)

    def test_a_condition_that_does_not_hold_says_nothing_until_it_gives_up(self):
        result, _, data = self.turn("alert me when the air quality in Delhi passes 200 for 1 check",
                                    met=False)
        self.assertNotIn("ALERT -", result["text"])
        self.assertEqual(len(data.checks), 1)
        self.assertIn("Not met after 1 check(s)", result["text"])
        self.assertIn("last reading 213 AQI", result["text"])
        self.assertIn("Nothing is still checking now", result["text"])

    def test_the_bound_between_the_watch_and_the_check_reaches_the_reader(self):
        _result, _, data = self.turn("alert me when the air quality in Delhi passes 200 "
                                     "for 4 checks", met=True)
        self.assertEqual(data.checks[0]["condition"], "aqi")
        self.assertEqual(data.checks[0]["threshold"], 200.0)
        self.assertEqual(data.checks[0]["direction"], "above")

    def test_help_does_not_ask_or_check(self):
        result, client, data = self.turn("what can you do?")
        self.assertIn("stay quiet until a condition is met", result["text"])
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(data.checks, [])

    def test_permission_denied_checks_nothing(self):
        result, _, data = self.turn("alert me when the air quality in Delhi passes 200",
                                    permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])
        self.assertEqual(data.checks, [])

    def test_a_dead_source_stops_the_watch_and_says_why(self):
        result, _, _ = self.turn("alert me when the air quality in Delhi passes 200",
                                 fail="the source is unreachable")
        self.assertIn("I stopped: the source is unreachable", result["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
