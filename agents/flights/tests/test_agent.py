"""Tests for the flights agent: the state array, boxes, distances, routing and the turns.

    python3 agents/flights/tests/test_agent.py

No network: OpenSky's snapshot and Nominatim's place are injected. The fixture row is a real one
read live on 2026-09-23, kept in the API's array form so that the one thing that has to be right -
the order of those 17 unnamed fields - is checked against real data rather than against a guess.

`states: null` is also pinned here: OpenSky answers an empty box with null, not with an empty
list, and the two must not be confused.
"""

from __future__ import annotations

import io
import json
import queue
import sys
import threading
import unittest
import urllib.error
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for path in (str(REPO_ROOT), str(AGENT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from acp_kit import AcpClient, Connection, STOP_END_TURN, STOP_REFUSAL  # noqa: E402

from agent import (  # noqa: E402
    HELP,
    address_from_text,
    age,
    box_text,
    meters,
    place_from_text,
    radius_from_text,
    rate,
    render_aircraft,
    route,
    speed,
)
from data import (  # noqa: E402
    FIELDS,
    MAX_AIRCRAFT,
    FlightsData,
    FlightsError,
    bbox_around,
    compass,
    haversine_km,
    parse_state,
)

#: A real row: EJA391 over New Jersey, climbing, squawking 1574.
ROW = ["a487ef", "EJA391  ", "United States", 1790182852, 1790182852, -75.1705, 40.4794,
       7726.68, False, 172.25, 233.75, 6.83, None, 8046.72, "1574", False, 0]
SNAPSHOT_TIME = 1790182852

#: A second row near Bryant Park, on the ground, at 0 knots.
GROUND_ROW = ["ad2138", "N945RF  ", "United States", 1790182770, 1790182790, -73.9835, 40.7560,
              -145.0, True, 0.0, 0.0, None, None, None, "1200", False, 0]

PLACE = {"lat": "40.7537509", "lon": "-73.9835428", "name": "Bryant Park",
         "display_name": "Bryant Park, Manhattan, New York, United States"}


class FakeSky:
    """OpenSky's snapshot and Nominatim's place by url. Records what was asked for."""

    def __init__(self, rows=None, place=PLACE, overpass_error=None) -> None:
        self.calls: list[str] = []
        self.rows = [ROW] if rows is None else rows
        self.place = place
        self.overpass_error = overpass_error

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        if self.overpass_error and "opensky" in url:
            raise self.overpass_error
        if "nominatim" in url:
            return json.dumps([] if self.place is None else [self.place])
        return json.dumps({"time": SNAPSHOT_TIME, "states": self.rows})


class GeoTests(unittest.TestCase):
    def test_distance_between_two_known_points(self):
        self.assertEqual(haversine_km(40.0, -73.0, 40.0, -73.0), 0.0)
        london_to_new_york = haversine_km(51.5074, -0.1278, 40.7128, -74.0060)
        self.assertTrue(5560 < london_to_new_york < 5590, london_to_new_york)
        self.assertTrue(haversine_km(-33.87, 151.21, 40.71, -74.0) > 15000)

    def test_compass_names(self):
        self.assertEqual(compass(0), "north")
        self.assertEqual(compass(45), "northeast")
        self.assertEqual(compass(90), "east")
        self.assertEqual(compass(233.75), "southwest")
        self.assertEqual(compass(359), "north")
        self.assertEqual(compass(None), "")

    def test_a_box_is_a_square_of_sky(self):
        box = bbox_around(40.7538, -73.9835, 25.0)
        self.assertAlmostEqual(box["lamin"], 40.5285, places=3)
        self.assertAlmostEqual(box["lomax"], -73.6862, places=3)
        self.assertLess(box["lamin"], 40.7538)
        self.assertGreater(box["lamax"], 40.7538)
        self.assertLess(box["lomin"], -73.9835)

    def test_a_box_near_the_pole_does_not_divide_by_zero(self):
        box = bbox_around(89.9, 0.0, 50.0)
        self.assertLessEqual(box["lomax"], 180.0)
        self.assertGreaterEqual(box["lomin"], -180.0)
        self.assertLessEqual(box["lamax"], 90.0)

    def test_a_box_never_leaves_the_globe(self):
        box = bbox_around(0.0, 179.999, 100.0)
        self.assertLessEqual(box["lomax"], 180.0)
        self.assertGreaterEqual(box["lomin"], -180.0)


class StateTests(unittest.TestCase):
    def test_a_real_row_parses_into_named_fields(self):
        state = parse_state(ROW, SNAPSHOT_TIME)
        self.assertEqual(state["icao24"], "a487ef")
        self.assertEqual(state["callsign"], "EJA391")
        self.assertEqual(state["country"], "United States")
        # Longitude is index 5 and latitude index 6. Getting these the wrong way round would put
        # this aircraft in the Southern Ocean, so they are asserted by name and by value.
        self.assertEqual(state["longitude"], -75.1705)
        self.assertEqual(state["latitude"], 40.4794)
        self.assertEqual(state["altitude_m"], 7726.68)
        self.assertFalse(state["on_ground"])
        self.assertEqual(state["speed_ms"], 172.25)
        self.assertEqual(state["track"], 233.75)
        self.assertEqual(state["vertical_rate_ms"], 6.83)
        self.assertEqual(state["squawk"], "1574")

    def test_position_age_is_relative_to_the_snapshot(self):
        state = parse_state(GROUND_ROW, SNAPSHOT_TIME)
        self.assertEqual(state["time_position_age_s"], 82.0)
        self.assertEqual(state["last_contact_age_s"], 62.0)
        self.assertIsNone(parse_state(ROW)["time_position_age_s"])

    def test_a_centre_adds_the_distance(self):
        state = parse_state(GROUND_ROW, SNAPSHOT_TIME, (40.7537509, -73.9835428))
        self.assertIsNotNone(state["distance_km"])
        self.assertTrue(0.0 <= state["distance_km"] < 1.0, state["distance_km"])
        self.assertIsNone(parse_state(ROW, SNAPSHOT_TIME)["distance_km"])

    def test_a_row_with_missing_fields_is_an_error_not_a_guess(self):
        with self.assertRaises(FlightsError) as caught:
            parse_state(ROW[:6], SNAPSHOT_TIME)
        self.assertIn("17 were expected", str(caught.exception))

    def test_a_null_states_field_is_no_aircraft(self):
        data = FlightsData(fetch=FakeSky(rows=None))
        data.fetch = lambda url: json.dumps({"time": SNAPSHOT_TIME, "states": None})
        snapshot = data.states(lamin=-0.5, lomin=-30.5, lamax=-0.2, lomax=-30.2)
        self.assertEqual(snapshot["aircraft"], [])
        self.assertEqual(snapshot["time"], SNAPSHOT_TIME)

    def test_an_empty_list_is_also_no_aircraft(self):
        data = FlightsData(fetch=lambda url: json.dumps({"time": 1, "states": []}))
        self.assertEqual(data.states(lamin=0, lomin=0, lamax=1, lomax=1)["aircraft"], [])

    def test_a_snapshot_that_is_not_a_snapshot_is_an_error(self):
        data = FlightsData(fetch=lambda url: json.dumps([1, 2, 3]))
        with self.assertRaises(FlightsError) as caught:
            data.states(lamin=0, lomin=0, lamax=1, lomax=1)
        self.assertIn("other than a snapshot", str(caught.exception))

    def test_the_box_is_put_in_the_query_by_its_proper_names(self):
        feed = FakeSky()
        data = FlightsData(fetch=feed)
        data.states(lamin=1.5, lomin=-2.0, lamax=3.0, lomax=4.25)
        self.assertIn("lamin=1.5", feed.calls[0])
        self.assertIn("lomax=4.25", feed.calls[0])


class OverheadTests(unittest.TestCase):
    def test_airborne_aircraft_come_before_the_ones_on_the_ground(self):
        feed = FakeSky(rows=[GROUND_ROW, ROW])
        data = FlightsData(fetch=feed)
        reading = data.overhead("Bryant Park", radius_km=25.0)
        self.assertEqual([item["icao24"] for item in reading["aircraft"]], ["a487ef", "ad2138"])
        self.assertEqual(reading["airborne"], 1)
        self.assertEqual(reading["on_ground"], 1)
        self.assertEqual(reading["found"], 2)
        self.assertEqual(reading["highest_m"], 7726.68)

    def test_the_place_is_geocoded_once_and_the_box_centres_on_it(self):
        feed = FakeSky()
        data = FlightsData(fetch=feed)
        reading = data.overhead("Bryant Park", radius_km=10.0)
        self.assertEqual(reading["place"]["name"], "Bryant Park")
        self.assertEqual(reading["place"]["radius_km"], 10.0)
        self.assertEqual(len([call for call in feed.calls if "nominatim" in call]), 1)
        self.assertIn("lamin=40.66", feed.calls[-1])

    def test_only_the_asked_for_number_of_aircraft_comes_back(self):
        rows = [list(ROW) for _ in range(MAX_AIRCRAFT + 5)]
        for index, row in enumerate(rows):
            row[0] = f"a{index:05x}"
        data = FlightsData(fetch=FakeSky(rows=rows))
        reading = data.overhead("Bryant Park")
        self.assertEqual(len(reading["aircraft"]), MAX_AIRCRAFT)
        self.assertEqual(reading["found"], MAX_AIRCRAFT + 5)

    def test_an_empty_box_is_reported_as_no_aircraft(self):
        data = FlightsData(fetch=FakeSky(rows=[]))
        reading = data.overhead("Bryant Park")
        self.assertEqual(reading["found"], 0)
        self.assertEqual(reading["aircraft"], [])
        self.assertIsNone(reading["nearest_km"])

    def test_a_place_that_does_not_exist_is_an_error(self):
        data = FlightsData(fetch=FakeSky(place=None))
        with self.assertRaises(FlightsError) as caught:
            data.overhead("Zzzz Nowhere")
        self.assertIn("no place called", str(caught.exception))

    def test_a_place_is_cached_so_two_questions_do_not_geocode_twice(self):
        feed = FakeSky()
        data = FlightsData(fetch=feed)
        data.overhead("Bryant Park")
        data.overhead("Bryant Park")
        self.assertEqual(len([call for call in feed.calls if "nominatim" in call]), 1)

    def test_no_place_at_all_is_an_error_with_an_example(self):
        data = FlightsData(fetch=FakeSky())
        with self.assertRaises(FlightsError) as caught:
            data.geocode("")
        self.assertIn("Bryant Park", str(caught.exception))


class AircraftTests(unittest.TestCase):
    def test_one_aircraft_by_address(self):
        feed = FakeSky()
        reading = FlightsData(fetch=feed).aircraft("a487ef")
        self.assertEqual(reading["address"], "a487ef")
        self.assertEqual(reading["aircraft"]["callsign"], "EJA391")
        self.assertIn("icao24=a487ef", feed.calls[0])

    def test_an_address_that_is_not_broadcasting_says_just_that(self):
        data = FlightsData(fetch=FakeSky(rows=[]))
        reading = data.aircraft("a487ef")
        self.assertIsNone(reading["aircraft"])
        self.assertEqual(reading["address"], "a487ef")

    def test_a_callsign_is_refused_because_this_api_cannot_look_one_up(self):
        with self.assertRaises(FlightsError) as caught:
            FlightsData(fetch=FakeSky()).aircraft("EJA391")
        self.assertIn("24-bit address", str(caught.exception))

    def test_a_short_or_odd_address_is_refused(self):
        for bad in ("a487e", "zzzzzz", "a487ef0", ""):
            with self.assertRaises(FlightsError):
                FlightsData(fetch=FakeSky()).aircraft(bad)

    def test_a_refusal_from_opensky_says_what_it_means(self):
        error = urllib.error.HTTPError("https://opensky-network.org/api/states/all", 429,
                                       "Too Many Requests", {}, io.BytesIO(b""))
        said = FlightsData._say(error)
        self.assertIn("429", said)
        self.assertIn("400 credits", said)
        self.assertIn("403", FlightsData._say(urllib.error.HTTPError("x", 403, "", {}, None)))


class RouteTests(unittest.TestCase):
    def test_a_place_after_over(self):
        self.assertEqual(route("what planes are over Bryant Park, New York right now?"),
                         ("flights-overhead", {"place": "Bryant Park, New York"}))
        self.assertEqual(place_from_text("who is flying over Denver?"), "Denver")
        self.assertEqual(place_from_text("what aircraft are near Heathrow?"), "Heathrow")

    def test_a_radius_is_read_from_km_and_miles(self):
        self.assertEqual(route("planes within 50 km of Heathrow"),
                         ("flights-overhead", {"place": "Heathrow", "radius_km": 50.0}))
        self.assertAlmostEqual(radius_from_text("anything within 10 miles"), 16.0934, places=3)
        self.assertAlmostEqual(radius_from_text("planes within 5km"), 5.0)
        self.assertIsNone(radius_from_text("planes overhead"))

    def test_a_radius_is_capped_so_one_question_cannot_ask_for_the_world(self):
        self.assertEqual(radius_from_text("within 90000 km"), 500.0)

    def test_a_radius_question_with_the_place_first(self):
        skill, params = route("how many aircraft are within 50 km of Heathrow?")
        self.assertEqual(skill, "flights-overhead")
        self.assertEqual(params["place"], "Heathrow")
        self.assertEqual(params["radius_km"], 50.0)

    def test_an_address_lookup(self):
        self.assertEqual(route("what is aircraft a487ef?"),
                         ("flights-aircraft", {"address": "a487ef"}))
        self.assertEqual(route("where is a487ef?"), ("flights-aircraft", {"address": "a487ef"}))
        self.assertEqual(address_from_text("aircraft a487ef"), "a487ef")
        self.assertEqual(address_from_text("what planes are over Denver?"), None)

    def test_over_me_is_not_a_place(self):
        self.assertIsNone(place_from_text("what planes are over me right now?"))
        self.assertIsNone(place_from_text("who is flying above us?"))
        self.assertEqual(route("what planes are over me right now?")[0], "flights-help")

    def test_nothing_to_look_up_gets_help(self):
        self.assertEqual(route("")[0], "flights-help")
        self.assertEqual(route("hello there")[0], "flights-help")
        self.assertEqual(route("what can you do?")[0], "flights-help")
        self.assertIn("ADS-B", HELP)


class RenderTests(unittest.TestCase):
    def state(self, row=ROW, centre=False):
        return parse_state(row, SNAPSHOT_TIME, (40.7537, -73.9835) if centre else None)

    def test_altitude_speed_and_rate(self):
        self.assertEqual(meters(7726.68), "7,727 m (25,350 ft)")
        self.assertEqual(meters(None), "not reported")
        self.assertEqual(speed(172.25), "620 km/h (335 kt)")
        self.assertEqual(speed(None), "not reported")
        self.assertEqual(rate(6.83), "climbing 6.8 m/s")
        self.assertEqual(rate(-1.2), "descending 1.2 m/s")
        self.assertEqual(rate(0.1), "level")
        self.assertEqual(rate(None), "not reported")
        self.assertEqual(age(82.0), "82s old")
        self.assertEqual(age(None), "unknown")

    def test_one_aircraft_reads_as_lines(self):
        lines = render_aircraft(self.state())
        text = "\n".join(lines)
        self.assertIn("EJA391  [a487ef]  United States", text)
        self.assertIn("altitude 7,727 m (25,350 ft) barometric", text)
        self.assertIn("climbing 6.8 m/s", text)
        self.assertIn("speed 620 km/h (335 kt), heading 234 deg (southwest)", text)
        self.assertIn("squawk 1574", text)

    def test_an_aircraft_on_the_ground_says_so_instead_of_a_height(self):
        text = "\n".join(render_aircraft(self.state(GROUND_ROW, centre=True)))
        self.assertIn("on the ground", text)
        self.assertIn("km away", text)
        self.assertNotIn("altitude", text)

    def test_a_missing_callsign_is_named_as_missing(self):
        row = list(ROW)
        row[1] = None
        self.assertIn("(no callsign broadcast)", "\n".join(render_aircraft(self.state(row))))

    def test_a_box_reads_without_hemisphere_labels(self):
        text = box_text(bbox_around(-33.87, 151.21, 25.0))
        self.assertIn("lat -34.09", text)
        self.assertIn("lon 150.9", text)
        self.assertNotIn("N", text.replace("lat", "").replace("lon", ""))


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
    def turn(self, text, permission="allow-once", rows=None, place=PLACE):
        from agent import FlightsAgent

        agent_conn, client_conn = connected_pair()
        agent = FlightsAgent(agent_conn, FlightsData(fetch=FakeSky(rows=rows, place=place)))
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_an_overhead_turn(self):
        result, client = self.turn("what planes are over Bryant Park, New York right now?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertEqual(len(client.permission_requests), 1)
        self.assertIn("Aircraft within 25 km of Bryant Park", result["text"])
        self.assertIn("EJA391", result["text"])
        self.assertIn("an empty answer is not an empty sky", result["text"])
        self.assertIn("lat 40.5", result["text"])

    def test_a_radius_question_says_the_radius_it_used(self):
        result, _ = self.turn("how many aircraft are within 50 km of Heathrow?")
        self.assertIn("Aircraft within 50 km of", result["text"])

    def test_an_address_turn(self):
        result, _ = self.turn("what is aircraft a487ef?")
        self.assertIn("24-bit address a487ef", result["text"])
        self.assertIn("EJA391", result["text"])
        self.assertNotIn("km away", result["text"])

    def test_an_empty_box_is_an_answer_not_an_error(self):
        result, _ = self.turn("what planes are over Bryant Park?", rows=[])
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("no aircraft are broadcasting positions in this box", result["text"])

    def test_an_unknown_place_is_reported(self):
        result, _ = self.turn("what planes are over Zzzz Nowhere?", place=None)
        self.assertIn("I could not read that", result["text"])
        self.assertIn("no place called", result["text"])

    def test_a_rate_limit_is_reported_as_a_rate_limit(self):
        class Limited(FakeSky):
            def __call__(self, url):
                if "opensky" in url:
                    raise FlightsError("OpenSky answered 429: this address has used its daily "
                                       "anonymous allowance")
                return super().__call__(url)

        from agent import FlightsAgent

        agent_conn, client_conn = connected_pair()
        agent = FlightsAgent(agent_conn, FlightsData(fetch=Limited()))
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            result = client.prompt("what planes are over Bryant Park?", session_id)
        finally:
            client.stop()
        self.assertIn("429", result["text"])
        self.assertIn("daily anonymous allowance", result["text"])

    def test_permission_denied_stops_with_a_refusal(self):
        result, _ = self.turn("what planes are over Denver?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("I need permission", result["text"])

    def test_help_reads_nothing_and_asks_nothing(self):
        result, client = self.turn("what can you do?")
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(client.tool_calls(), [])
        self.assertIn("I read OpenSky's live aircraft positions", result["text"])

    def test_the_tool_call_completes_with_the_count(self):
        _, client = self.turn("what planes are over Denver?")
        done = [update for update in client.updates
                if update.get("sessionUpdate") == "tool_call_update"
                and update.get("status") == "completed"]
        self.assertEqual(len(done), 1)
        self.assertIn("aircraft in 25 km", done[0]["content"][0]["content"]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
