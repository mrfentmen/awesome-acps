"""Tests for the osm agent: Nominatim, Overpass, routing and turns.

    python3 agents/osm/tests/test_agent.py

No network: both services are injected. The fixtures are shaped like the live answers verified
on 2026-09-23, including the two traps this agent exists to survive:

  1. feature words must be read from the question *without* the place name, or 'Bryant Park'
     also asks for parks, and
  2. one real place is often mapped twice (node and way), which must not print twice.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import unittest
from pathlib import Path
from urllib import parse

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for path in (str(REPO_ROOT), str(AGENT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from acp_kit import AcpClient, Connection, STOP_END_TURN, STOP_REFUSAL  # noqa: E402

from agent import coords_from_text, place_from_text, route, without_place  # noqa: E402
from data import (  # noqa: E402
    DATASET,
    MAX_RADIUS_M,
    OsmData,
    OsmError,
    haversine_km,
    kinds_from_text,
    radius_from_text,
)

PLACES = [{"lat": "40.7537876", "lon": "-73.9834608", "name": "Bryant Park",
           "display_name": "Bryant Park, Manhattan Community Board 5, New York, United States",
           "type": "park", "category": "leisure"}]

#: Live shape of an Overpass answer: elements with tags, and `center` for ways and relations.
DRINKING_WATER = {"elements": [
    {"type": "node", "id": 1, "lat": 40.7541, "lon": -73.9835,
     "tags": {"amenity": "drinking_water", "wheelchair": "yes"}},
    {"type": "node", "id": 2, "lat": 40.7540, "lon": -73.9840,
     "tags": {"amenity": "drinking_water", "wheelchair": "yes", "fee": "no"}},
    {"type": "way", "id": 3, "center": {"lat": 40.7579, "lon": -73.9840},
     "tags": {"amenity": "drinking_water", "fee": "no"}},
]}

#: The same playground mapped twice, 20 m apart - the live duplicate this reader collapses.
PLAYGROUNDS = {"elements": [
    {"type": "node", "id": 10, "lat": 40.7812, "lon": -73.9665,
     "tags": {"leisure": "playground", "name": "Diana Ross Playground",
              "opening_hours": "dawn-dusk"}},
    {"type": "way", "id": 11, "center": {"lat": 40.7813, "lon": -73.9665},
     "tags": {"leisure": "playground", "name": "Diana Ross Playground"}},
    {"type": "node", "id": 12, "lat": 40.7819, "lon": -73.9670,
     "tags": {"leisure": "playground", "name": "Ancient Playground"}},
]}


class FakeFeed:
    """Serves Nominatim and Overpass, and records what it was asked for."""

    def __init__(self, places=None, overpass=None, fail_mirrors=()):
        self.calls: list[tuple[str, dict, bytes | None]] = []
        self.places = PLACES if places is None else places
        self.overpass = DRINKING_WATER if overpass is None else overpass
        self.fail_mirrors = set(fail_mirrors)

    def __call__(self, url: str, params: dict, data=None, content_type=None):
        self.calls.append((url, dict(params), data))
        if "nominatim" in url:
            if isinstance(self.places, Exception):
                raise self.places
            return json.dumps(self.places)
        for mirror in self.fail_mirrors:
            if mirror in url:
                raise OsmError("Overpass is busy (429)")
        if "overpass" in url:
            if isinstance(self.overpass, Exception):
                raise self.overpass
            return json.dumps(self.overpass)
        raise AssertionError(f"unexpected url {url}")

    def overpass_queries(self) -> list[str]:
        """The Overpass QL sent, decoded out of the form body."""
        queries = []
        for _url, _params, data in self.calls:
            if data is None:
                continue
            fields = parse.parse_qs(data.decode())
            queries.append(fields["data"][0])
        return queries


def make_data(**kwargs) -> tuple[OsmData, FakeFeed]:
    feed = FakeFeed(**kwargs)
    return OsmData(fetch=feed), feed


class KindTests(unittest.TestCase):
    def test_plain_kinds(self):
        self.assertEqual([label for _key, _value, label in kinds_from_text("nearest pharmacy")],
                         ["pharmacy"])
        self.assertEqual(kinds_from_text("nearest pharmacy"), [("amenity", "pharmacy", "pharmacy")])
        self.assertEqual(kinds_from_text("EV chargers within 2 km")[0][:2], ("amenity", "charging_station"))

    def test_a_place_name_does_not_smuggle_a_kind_in(self):
        # 'Bryant Park' asks for a park only if the word park is not part of the place name.
        self.assertIn(("leisure", "park", "park"), kinds_from_text("parks near Boston"))
        left = without_place("nearest drinking water to Bryant Park, New York")
        self.assertEqual(kinds_from_text(left), [("amenity", "drinking_water", "drinking water")])

    def test_parking_is_not_a_park(self):
        self.assertEqual(kinds_from_text("parking near here")[0][:2], ("amenity", "parking"))

    def test_radius_units(self):
        self.assertEqual(radius_from_text("within 500 m of here"), 500)
        self.assertEqual(radius_from_text("within 2 km"), 2000)
        self.assertEqual(radius_from_text("within 1 mile"), 1609)
        self.assertEqual(radius_from_text("within 100 km"), MAX_RADIUS_M)
        self.assertIsNone(radius_from_text("nearest cafe"))

    def test_place_and_coordinates(self):
        self.assertEqual(place_from_text("nearest cafe to Bryant Park, New York"),
                         "Bryant Park, New York")
        self.assertIsNone(place_from_text("EV chargers within 2 km of 40.7536,-73.9832"))
        self.assertEqual(coords_from_text("-33.87,151.21"), (-33.87, 151.21))
        self.assertIsNone(coords_from_text("no numbers here"))


class ReaderTests(unittest.TestCase):
    def test_geocoding_reads_the_first_match(self):
        data, _ = make_data()
        place = data.geocode("Bryant Park, New York")
        self.assertEqual(place["name"], "Bryant Park")
        self.assertAlmostEqual(place["latitude"], 40.7537876)
        self.assertAlmostEqual(place["longitude"], -73.9834608)
        self.assertEqual(place["kind"], "park")

    def test_an_unknown_place_is_refused(self):
        data, _ = make_data(places=[])
        with self.assertRaises(ValueError):
            data.geocode("Xyzzyville")
        with self.assertRaises(ValueError):
            data.geocode("")

    def test_pois_are_parsed_from_nodes_and_ways(self):
        data, _ = make_data()
        result = data.pois(40.7538, -73.9835, "amenity", "drinking_water", 800)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["radius_m"], 800)
        self.assertFalse(result["capped"])
        self.assertEqual(result["pois"][0]["tags"]["wheelchair"], "yes")
        self.assertEqual(result["pois"][2]["osm_id"], "way/3")
        self.assertLess(result["pois"][0]["distance_km"], result["pois"][2]["distance_km"])

    def test_the_overpass_query_names_the_tag_and_the_radius(self):
        data, feed = make_data()
        data.pois(40.7538, -73.9835, "amenity", "charging_station", 2000)
        query = feed.overpass_queries()[0]
        self.assertIn('["amenity"="charging_station"]', query)
        self.assertIn("around:2000,40.7538,-73.9835", query)

    def test_a_place_mapped_twice_is_reported_once(self):
        data, _ = make_data(overpass=PLAYGROUNDS)
        result = data.pois(40.7812, -73.9665, "leisure", "playground", 500)
        names = [poi["name"] for poi in result["pois"]]
        self.assertEqual(names.count("Diana Ross Playground"), 1)
        self.assertEqual(result["total"], 2)

    def test_the_mirror_is_used_when_the_first_overpass_is_busy(self):
        data, feed = make_data(fail_mirrors=("overpass-api.de",))
        result = data.pois(40.7538, -73.9835, "amenity", "drinking_water", 800)
        self.assertEqual(result["total"], 3)
        self.assertEqual(len(feed.overpass_queries()), 2)

    def test_both_mirrors_down_is_one_error(self):
        data, _ = make_data(fail_mirrors=("overpass-api.de", "kumi"))
        with self.assertRaises(OsmError):
            data.pois(40.7538, -73.9835, "amenity", "drinking_water", 800)

    def test_an_empty_place_payload_is_an_error_not_a_guess(self):
        data, _ = make_data(overpass={"unexpected": True})
        with self.assertRaises(OsmError):
            data.pois(40.7538, -73.9835, "amenity", "drinking_water", 800)

    def test_haversine_is_a_real_distance(self):
        self.assertAlmostEqual(haversine_km((40.7537, -73.9835), (40.7537, -73.9835)), 0.0)
        self.assertAlmostEqual(haversine_km((40.7537, -73.9835), (40.7812, -73.9665)), 3.2, delta=0.3)


class RouteTests(unittest.TestCase):
    def test_a_feature_near_a_place(self):
        skill, params = route("nearest drinking water to Bryant Park, New York")
        self.assertEqual(skill, "osm-near")
        self.assertEqual(params["query"], "Bryant Park, New York")
        self.assertEqual(params["kinds"], [("amenity", "drinking_water", "drinking water")])

    def test_a_feature_near_coordinates_keeps_no_place_name(self):
        skill, params = route("EV chargers within 2 km of 40.7536,-73.9832")
        self.assertEqual(skill, "osm-near")
        self.assertNotIn("query", params)
        self.assertEqual(params["radius_m"], 2000)
        self.assertEqual(params["latitude"], 40.7536)

    def test_a_place_alone_answers_with_the_place(self):
        self.assertEqual(route("Bryant Park, New York"), ("osm-place", {"query": "Bryant Park, New York"}))

    def test_a_feature_with_no_place_asks_for_one(self):
        self.assertEqual(route("nearest cafe"), ("help", {}))

    def test_help(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")


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
        from agent import OsmAgent

        agent_conn, client_conn = connected_pair()
        data, feed = make_data(**kw)
        agent = OsmAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client, feed
        finally:
            client.stop()

    def test_near_a_place_lists_what_osm_has(self):
        result, client, _ = self.turn("nearest drinking water to Bryant Park, New York")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("Drinking water near Bryant Park (40.7538, -73.9835) within 800 m:", result["text"])
        self.assertIn("OpenStreetMap has 3 mapped within 800 m", result["text"])
        self.assertIn("volunteer-mapped: missing means unmapped, not absent", result["text"])
        self.assertIn(DATASET, result["text"])
        self.assertEqual(len(client.permission_requests), 1)

    def test_an_empty_result_is_a_mapping_gap_not_a_denial(self):
        result, _, _ = self.turn("nearest pharmacy to Bryant Park, New York",
                                 overpass={"elements": []})
        self.assertIn("nothing mapped", result["text"])
        self.assertIn("does not prove there is none", result["text"])

    def test_a_place_alone_says_what_can_be_asked(self):
        result, _, _ = self.turn("Bryant Park, New York")
        self.assertIn("40.75379, -73.98346", result["text"])
        self.assertIn("Ask for one", result["text"])

    def test_an_unknown_place_is_refused(self):
        result, _, _ = self.turn("nearest cafe to Xyzzyville", places=[])
        self.assertIn("no place called", result["text"])

    def test_help_does_not_ask_permission(self):
        result, client, _ = self.turn("what can you do?")
        self.assertIn("OpenStreetMap", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _, _ = self.turn("nearest drinking water to Bryant Park", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_a_dead_service_is_reported(self):
        result, _, _ = self.turn("nearest drinking water to Bryant Park",
                                 fail_mirrors=("overpass-api.de", "kumi"))
        self.assertIn("could not read OpenStreetMap", result["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
