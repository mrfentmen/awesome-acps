"""Tests for the pollen agent: the reader, the bands, routing, and the honest not-covered path.

    python3 agents/pollen/tests/test_agent.py

No network: the geocoder and the pollen payload are injected, shaped exactly like the live
ones read on 2026-09-23 - including the case that matters most, a place outside the CAMS model
where every species comes back `null`.
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

from agent import (  # noqa: E402
    group_from_text,
    place_from_text,
    render_current,
    route,
    species_from_text,
)
from data import (  # noqa: E402
    SPECIES,
    PollenData,
    PollenError,
    band,
    local_hour,
    species_keys,
)

PLACE = {"results": [{"name": "Berlin", "latitude": 52.52, "longitude": 13.41,
                      "country": "Germany", "admin1": "Berlin"}]}

SPECIES_KEYS = [key for key, _label in SPECIES]

#: Berlin, 2026-09-23: real numbers, grass is the only one up.
BERLIN = {"alder_pollen": 0.0, "birch_pollen": 0.0, "grass_pollen": 0.1,
          "mugwort_pollen": 0.0, "ragweed_pollen": 0.0, "olive_pollen": 0.0}


def current_payload(values=None, offset=7200):
    return {"current": {"time": "2026-09-23T05:00", "interval": 3600,
                        **(BERLIN if values is None else values)},
            "utc_offset_seconds": offset}


def hourly_payload(offset=7200, days=3, grass=(0.0, 0.2, 12.0), birch=(0.0, 0.0, 30.0)):
    """Hourly pollen over whole days, starting at the place's own midnight."""
    start = local_hour(offset).date()
    times = [(datetime.datetime.combine(start, datetime.time()) +
              datetime.timedelta(hours=index)).strftime("%Y-%m-%dT%H:%M")
             for index in range(days * 24)]
    values = {}
    for key in SPECIES_KEYS:
        series = [0.0] * len(times)
        if key == "grass_pollen":
            series[24:24 + len(grass)] = list(grass)
        if key == "birch_pollen":
            series[48:48 + len(birch)] = list(birch)
        values[key] = series
    return {"hourly": {"time": times, **values}, "utc_offset_seconds": offset}


class FakeFeeds:
    """Serves the geocoder and the pollen payload, recording every (url, params) pair."""

    def __init__(self, place=None, current=None, hourly=None, fail: str | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.place = PLACE if place is None else place
        self.current = current if current is not None else current_payload()
        self.hourly = hourly if hourly is not None else hourly_payload()
        self.fail = fail

    def __call__(self, url: str, params: dict):
        self.calls.append((url, dict(params)))
        if self.fail and self.fail in url:
            raise PollenError("the model is unreachable")
        if "geocoding" in url:
            return json.dumps(self.place)
        if "air-quality" in url:
            if "hourly" in params:
                return json.dumps(self.hourly)
            return json.dumps(self.current)
        raise AssertionError(f"unexpected url {url}")


def make_data(**kw) -> tuple[PollenData, FakeFeeds]:
    feed = FakeFeeds(**kw)
    return PollenData(fetch=feed), feed


class BandTests(unittest.TestCase):
    def test_the_published_bands(self):
        self.assertEqual(band(0.0), "none")
        self.assertEqual(band(0.1), "low")
        self.assertEqual(band(10.0), "low")
        self.assertEqual(band(10.1), "moderate")
        self.assertEqual(band(50.0), "moderate")
        self.assertEqual(band(99.0), "high")
        self.assertEqual(band(150.0), "very high")

    def test_a_missing_value_is_not_a_zero(self):
        self.assertEqual(band(None), "not available")


class SpeciesTests(unittest.TestCase):
    def test_all_six_by_default(self):
        self.assertEqual(species_keys(), list(SPECIES))

    def test_one_species(self):
        self.assertEqual([label for _key, label in species_keys("birch")], ["birch"])

    def test_a_group(self):
        self.assertEqual([label for _key, label in species_keys(group="weed")],
                         ["mugwort", "ragweed"])

    def test_unknown_names_are_refused(self):
        with self.assertRaises(ValueError):
            species_keys("sunflower")
        with self.assertRaises(ValueError):
            species_keys(group="vegetable")


class ReadTests(unittest.TestCase):
    def test_the_current_counts_keep_the_places_clock_and_the_units(self):
        data, _feed = make_data()
        reading = data.current("Berlin")
        self.assertTrue(reading["covered"])
        self.assertEqual(reading["at"], "2026-09-23T05:00")
        rows = {row["label"]: row for row in reading["readings"]}
        self.assertEqual(rows["grass"]["value"], 0.1)
        self.assertEqual(rows["grass"]["band"], "low")
        self.assertEqual(rows["alder"]["band"], "none")
        self.assertEqual(reading["worst"]["label"], "grass")
        self.assertEqual(reading["total"], 0.1)

    def test_only_the_species_asked_about_are_read(self):
        data, feed = make_data()
        reading = data.current("Berlin", species="birch")
        self.assertEqual([row["label"] for row in reading["readings"]], ["birch"])
        self.assertEqual(feed.calls[-1][1]["current"], "birch_pollen")

    def test_a_place_outside_the_model_is_reported_as_uncovered(self):
        data, _feed = make_data(current=current_payload(
            {key: None for key in SPECIES_KEYS}, offset=-14400))
        reading = data.current("New York")
        self.assertFalse(reading["covered"])
        self.assertIsNone(reading["total"])
        self.assertIsNone(reading["worst"])
        text = render_current(reading)
        self.assertIn("covers Europe", text)
        self.assertIn("not a zero reading", text)

    def test_a_real_zero_is_not_reported_as_uncovered(self):
        data, _feed = make_data(current=current_payload({key: 0.0 for key in SPECIES_KEYS}))
        reading = data.current("Berlin")
        self.assertTrue(reading["covered"])
        self.assertEqual(reading["total"], 0.0)
        self.assertEqual(reading["worst"]["band"], "none")
        self.assertIn("0.0 grains/m3", render_current(reading))

    def test_the_forecast_peaks_per_day_and_skips_the_days_already_over(self):
        data, _feed = make_data()
        reading = data.forecast("Berlin", days=3)
        self.assertTrue(reading["covered"])
        self.assertEqual(len(reading["days"]), 3)
        dates = [day["date"] for day in reading["days"]]
        self.assertEqual(dates, sorted(dates))
        first, second, third = reading["days"]
        self.assertTrue(first["covered"])
        self.assertEqual(first["peaks"], [])  # 0.0 all day is not a peak, and it is not "uncovered"
        self.assertEqual([row["label"] for row in second["peaks"]], ["grass"])
        self.assertEqual(second["worst"]["value"], 12.0)
        self.assertEqual(third["worst"]["label"], "birch")
        self.assertEqual(third["worst"]["value"], 30.0)

    def test_a_single_species_forecast_only_carries_that_species(self):
        data, feed = make_data()
        reading = data.forecast("Berlin", days=3, species="birch")
        self.assertEqual(reading["species"], ["birch"])
        for day in reading["days"]:
            self.assertTrue(all(row["species"] == "birch_pollen" for row in day["peaks"]))
        self.assertEqual(feed.calls[-1][1]["hourly"], "birch_pollen")

    def test_a_place_outside_the_model_has_no_forecast_either(self):
        payload = hourly_payload()
        for key in SPECIES_KEYS:
            payload["hourly"][key] = [None] * len(payload["hourly"]["time"])
        data, _feed = make_data(hourly=payload)
        reading = data.forecast("New York", days=3)
        self.assertFalse(reading["covered"])
        self.assertIn("covers Europe and this place is outside it", render_forecast_text(reading))
        self.assertNotIn("0 grains/m3", render_forecast_text(reading))

    def test_an_unknown_place_and_a_dead_model_are_errors(self):
        data, _feed = make_data(place={"results": []})
        with self.assertRaises(ValueError):
            data.current("Xyzzyville")
        data, _feed = make_data(fail="geocoding")
        with self.assertRaises(PollenError):
            data.current("Berlin")


def render_forecast_text(reading: dict) -> str:
    from agent import render_forecast

    return render_forecast(reading, {})


class RouteTests(unittest.TestCase):
    def test_a_plain_allergy_question(self):
        skill, params = route("is the pollen bad in Berlin today?")
        self.assertEqual(skill, "pollen-now")
        self.assertEqual(params["place"], "Berlin")

    def test_a_species_and_a_forecast(self):
        skill, params = route("when is birch pollen worst in Oslo this week?")
        self.assertEqual(skill, "pollen-forecast")
        self.assertEqual(params["place"], "Oslo")
        self.assertEqual(params["species"], "birch")

    def test_tomorrow_is_a_forecast_not_a_now(self):
        self.assertEqual(route("grass pollen in London tomorrow")[0], "pollen-forecast")
        self.assertEqual(route("grass pollen in London")[0], "pollen-now")

    def test_a_group_is_carried(self):
        _skill, params = route("tree pollen in Munich")
        self.assertEqual(params["group"], "tree")

    def test_the_days_asked_for(self):
        self.assertEqual(route("pollen forecast for Berlin over the next 3 days")[1]["days"], 3)

    def test_a_place_with_no_question_is_help(self):
        self.assertEqual(route("pollen")[0], "help")
        self.assertEqual(route("how bad is the pollen?")[0], "help")

    def test_nonsense(self):
        self.assertEqual(route("book me a flight")[0], "help")
        self.assertEqual(route("")[0], "help")

    def test_helpers(self):
        self.assertEqual(species_from_text("birch pollen please"), "birch")
        self.assertIsNone(species_from_text("is the pollen bad"))
        self.assertEqual(group_from_text("tree pollen"), "tree")
        self.assertEqual(place_from_text("pollen in Berlin today"), "Berlin")
        self.assertIsNone(place_from_text("how bad is the pollen today?"))


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
        from agent import PollenAgent

        agent_conn, client_conn = connected_pair()
        data, feed = make_data(**kw)
        agent = PollenAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client, feed
        finally:
            client.stop()

    def test_a_count_answer_names_every_species_and_the_worst(self):
        result, client, _ = self.turn("is the pollen bad in Berlin today?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertEqual(len(client.permission_requests), 1)
        self.assertIn("Pollen in Berlin, Berlin, Germany at 2026-09-23T05:00", result["text"])
        self.assertIn("grass    0.1 grains/m3", result["text"])
        self.assertIn("Worst right now: grass (0.1 grains/m3, low)", result["text"])
        self.assertIn("CAMS pollen model", result["text"])

    def test_the_not_covered_path_says_why_instead_of_printing_zeros(self):
        result, _, _ = self.turn("pollen in New York today",
                                 place={"results": [{"name": "New York", "latitude": 40.71,
                                                     "longitude": -74.0}]},
                                 current=current_payload({key: None for key in SPECIES_KEYS}))
        self.assertIn("No pollen has been modelled for New York", result["text"])
        self.assertIn("not a zero reading", result["text"])
        self.assertNotIn("0.0 grains/m3", result["text"])

    def test_a_forecast_lists_the_days(self):
        result, _, _ = self.turn("birch pollen forecast for Berlin over the next 3 days")
        self.assertIn("pollen peaks for Berlin", result["text"])
        self.assertIn("birch 30.0", result["text"])
        self.assertIn("(worst moderate)", result["text"])
        self.assertIn("every species at 0 grains/m3", result["text"])

    def test_help_does_not_ask_permission(self):
        result, client, feed = self.turn("what can you do?")
        self.assertIn("CAMS pollen model", result["text"])
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(feed.calls, [])

    def test_permission_denied(self):
        result, _, _ = self.turn("pollen in Berlin", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_a_dead_source_is_reported(self):
        result, _, _ = self.turn("pollen in Berlin", fail="air-quality")
        self.assertIn("I could not read the pollen model", result["text"])

    def test_an_unknown_place_is_reported(self):
        result, _, _ = self.turn("pollen in Xyzzyville", place={"results": []})
        self.assertIn("no place called", result["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
