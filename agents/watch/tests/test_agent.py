"""Tests for the watch agent: feed snapshots, routing, the push loop and cancellation.

    python3 agents/watch/tests/test_agent.py

No network: the three feeds are injected. The fixtures are shaped like the live payloads
verified on 2026-09-23 (USGS all_hour with 4 events, SWPC 1-minute Kp, open-notify iss-now).
"""

from __future__ import annotations

import queue
import sys
import threading
import time
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for path in (str(REPO_ROOT), str(AGENT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from acp_kit import AcpClient, Connection, STOP_CANCELLED, STOP_END_TURN, STOP_REFUSAL  # noqa: E402

from agent import HELP, WatchAgent, _change, render_round, route  # noqa: E402
from data import (  # noqa: E402
    DATASET,
    MAX_INTERVAL,
    MAX_ROUNDS,
    MAX_SECONDS,
    WatchData,
    WatchError,
    bounds,
    haversine_km,
    storm_level,
    utc,
)

#: Four real events from the USGS all_hour feed, deliberately out of order.
USGS = {
    "type": "FeatureCollection",
    "metadata": {"title": "USGS All Earthquakes, Past Hour"},
    "features": [
        {"type": "Feature", "id": "ci41337343",
         "properties": {"mag": 0.96, "place": "4 km SW of Idyllwild, CA",
                        "time": 1790125724430, "url": "https://example/ci41337343"}},
        {"type": "Feature", "id": "ak0199",
         "properties": {"mag": 4.5, "place": "62 km S of Sand Point, Alaska",
                        "time": 1790125910000, "url": "https://example/ak0199"}},
        {"type": "Feature", "id": "us6000",
         "properties": {"mag": 2.1, "place": "10 km NW of Pahala, Hawaii",
                        "time": 1790125800000, "url": "https://example/us6000"}},
        {"type": "Feature", "id": "nc7500",
         "properties": {"mag": None, "place": "somewhere quiet", "time": None, "url": None}},
    ],
}

KP = [
    {"time_tag": "2026-09-23T01:05:00", "kp_index": 0, "estimated_kp": 0.0, "kp": "0Z"},
    {"time_tag": "2026-09-23T01:06:00", "kp_index": 1, "estimated_kp": 0.33, "kp": "0Z"},
    {"time_tag": "2026-09-23T01:07:00", "kp_index": 0, "estimated_kp": 0.0, "kp": "0Z"},
    {"time_tag": "2026-09-23T01:08:00", "kp_index": 5, "estimated_kp": 5.67, "kp": "6-"},
]

ISS = {"message": "success", "iss_position": {"latitude": "48.9591", "longitude": "-139.7758"},
       "timestamp": 1790125912}


class FakeFeed:
    """Serves the three real feed shapes and counts how often each was read."""

    def __init__(self, usgs=None, kp=None, iss=None, fail: str | None = None) -> None:
        self.calls: list[str] = []
        self.usgs = usgs if usgs is not None else USGS
        self.kp = kp if kp is not None else KP
        self.iss = iss if iss is not None else ISS
        self.fail = fail

    def __call__(self, url: str, params: dict):
        self.calls.append(url)
        if self.fail and self.fail in url:
            raise WatchError(f"feed request failed: <urlopen error timed out> for {url}")
        if "earthquake.usgs.gov" in url:
            return self.usgs
        if "swpc.noaa.gov" in url:
            return self.kp
        if "open-notify.org" in url:
            return self.iss
        raise AssertionError(f"unexpected url {url}")


def make_data(feed: FakeFeed | None = None) -> tuple[WatchData, FakeFeed]:
    feed = feed or FakeFeed()
    return WatchData(fetch=feed), feed


class GeometryTests(unittest.TestCase):
    def test_haversine_matches_a_known_distance(self):
        # London to Paris is about 344 km.
        self.assertAlmostEqual(haversine_km((51.5074, -0.1278), (48.8566, 2.3522)), 344, delta=3)

    def test_haversine_of_the_same_point_is_zero(self):
        self.assertEqual(haversine_km((10.0, 20.0), (10.0, 20.0)), 0.0)

    def test_utc_formats_an_epoch(self):
        self.assertEqual(utc(1790125912), "2026-09-23T01:11:52Z")
        self.assertIsNone(utc(None))

    def test_storm_scale(self):
        self.assertIsNone(storm_level(4.0))
        self.assertEqual(storm_level(5.67), "G1 (minor)")
        self.assertEqual(storm_level(9.0), "G5 (extreme)")


class BoundsTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(bounds(), (3, 15.0))

    def test_rounds_are_capped(self):
        self.assertEqual(bounds(rounds=999)[0], MAX_ROUNDS)

    def test_interval_is_capped(self):
        self.assertEqual(bounds(rounds=1, interval=100000)[1], MAX_INTERVAL)

    def test_junk_falls_back_to_the_default(self):
        self.assertEqual(bounds(rounds="lots", interval="soon"), (3, 15.0))

    def test_the_total_time_stays_inside_the_cap(self):
        rounds, gap = bounds(rounds=20, interval=120)
        self.assertLessEqual(rounds * gap, MAX_SECONDS)


class SnapshotTests(unittest.TestCase):
    def test_quake_snapshot_finds_the_newest_event_not_the_first(self):
        data, _ = make_data()
        snap = data.quakes()
        self.assertEqual(snap["data"]["newest"]["id"], "ak0199")
        self.assertEqual(snap["line"], "4 quake(s) in the past hour - newest M4.5 "
                                       "62 km S of Sand Point, Alaska")
        self.assertEqual(snap["at"], "2026-09-23T01:11:50Z")

    def test_quake_snapshot_can_filter_by_magnitude(self):
        data, _ = make_data()
        snap = data.quakes(min_magnitude=4.0)
        self.assertEqual(snap["data"]["count"], 1)
        self.assertIn("at or above M4.0", snap["line"])
        self.assertEqual(snap["data"]["biggest"]["id"], "ak0199")

    def test_quake_snapshot_survives_an_event_with_no_magnitude(self):
        data, _ = make_data()
        self.assertEqual(data.quakes()["data"]["total"], 4)

    def test_aurora_snapshot_reads_the_latest_reading(self):
        data, _ = make_data()
        snap = data.aurora()
        self.assertEqual(snap["value"], 5.67)
        self.assertEqual(snap["line"], "Kp 5.67 - G1 (minor)")
        self.assertEqual(snap["at"], "2026-09-23T01:08:00Z")

    def test_aurora_snapshot_orders_by_timestamp_not_file_order(self):
        data, _ = make_data(FakeFeed(kp=list(reversed(KP))))
        self.assertEqual(data.aurora()["at"], "2026-09-23T01:08:00Z")

    def test_iss_snapshot_names_the_hemisphere(self):
        data, _ = make_data()
        snap = data.iss()
        self.assertEqual(snap["line"], "station at 48.96 N, 139.78 W")
        self.assertEqual(snap["at"], "2026-09-23T01:11:52Z")

    def test_snapshot_dispatches_by_name(self):
        data, _ = make_data()
        self.assertEqual(data.snapshot("iss")["feed"], "iss")
        self.assertEqual(data.snapshot("aurora")["feed"], "aurora")
        self.assertEqual(data.snapshot("quakes")["feed"], "quakes")

    def test_an_unknown_feed_is_refused(self):
        data, _ = make_data()
        with self.assertRaises(ValueError) as caught:
            data.snapshot("the news")
        self.assertIn("I can watch", str(caught.exception))

    def test_a_dead_feed_raises_one_error_type(self):
        data, _ = make_data(FakeFeed(fail="earthquake"))
        with self.assertRaises(WatchError):
            data.quakes()

    def test_a_payload_that_is_not_a_feed_is_rejected(self):
        data, _ = make_data(FakeFeed(usgs={"unexpected": True}))
        with self.assertRaises(WatchError):
            data.quakes()

    def test_every_read_is_fresh_so_a_watch_sees_changes(self):
        feed = FakeFeed()
        data = WatchData(fetch=feed)
        data.quakes()
        data.quakes()
        self.assertEqual(len(feed.calls), 2)


class RenderTests(unittest.TestCase):
    def test_the_first_round_says_it_is_the_first(self):
        data, _ = make_data()
        snap = data.iss()
        line = render_round(1, 3, snap, None)
        self.assertIn("Round 1/3 [2026-09-23T01:11:52Z]", line)
        self.assertIn("first reading", line)

    def test_an_unchanged_snapshot_says_no_change(self):
        data, _ = make_data()
        snap = data.iss()
        self.assertEqual(_change(snap, snap), "no change since the last round")

    def test_a_rising_kp_says_rising(self):
        rising = {"feed": "aurora", "value": 5.67}
        self.assertEqual(_change(rising, {"feed": "aurora", "value": 3.0}),
                         "rising from Kp 3.00 to Kp 5.67")

    def test_a_new_quake_is_reported_as_new(self):
        self.assertEqual(_change({"feed": "quakes", "value": "b"}, {"feed": "quakes", "value": "a"}),
                         "a new event has appeared")

    def test_iss_movement_between_rounds_is_measured(self):
        before = {"feed": "iss", "value": (48.0, -139.0), "data": {"latitude": 48.0, "longitude": -139.0,
                                                                   "timestamp": 1790125912}}
        after = {"feed": "iss", "value": (48.66, -139.0), "data": {"latitude": 48.66, "longitude": -139.0,
                                                                    "timestamp": 1790126008}}
        change = _change(after, before)
        self.assertIn("moved 73 km in 96s", change)
        self.assertIn("km/s", change)


class RouteTests(unittest.TestCase):
    def test_watch_the_earthquakes(self):
        skill, params = route("watch the earthquakes")
        self.assertEqual(skill, "watch-quakes")
        self.assertEqual(params["feed"], "quakes")

    def test_watch_the_aurora_with_rounds_and_interval(self):
        skill, params = route("watch the aurora for 4 rounds every 30 seconds")
        self.assertEqual(skill, "watch-aurora")
        self.assertEqual(params["rounds"], 4)
        self.assertEqual(params["interval"], 30.0)

    def test_interval_in_minutes(self):
        self.assertEqual(route("watch the iss every 2 minutes")[1]["interval"], 120.0)

    def test_the_space_station_is_the_iss_feed(self):
        self.assertEqual(route("watch the space station every 5 seconds")[0], "watch-iss")

    def test_a_magnitude_filter(self):
        self.assertEqual(route("watch earthquakes over magnitude 4")[1]["min_magnitude"], 4.0)

    def test_magnitude_is_ignored_for_other_feeds(self):
        self.assertNotIn("min_magnitude", route("watch the aurora over 4")[1])

    def test_an_unknown_feed_falls_back_to_help(self):
        self.assertEqual(route("watch the news")[0], "help")
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


def chunks(result: dict) -> list[dict]:
    return [update for update in result["updates"]
            if update.get("sessionUpdate") == "agent_message_chunk"]


class TurnTests(unittest.TestCase):
    def turn(self, text: str, feed=None, permission: str = "allow-once"):
        agent_conn, client_conn = connected_pair()
        data, fake = make_data(feed)
        agent = WatchAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client, fake
        finally:
            client.stop()

    def test_a_watch_pushes_one_line_per_round(self):
        result, client, fake = self.turn("watch the space station for 3 rounds every 1 second")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("Watching where the International Space Station is for 3 round(s)", result["text"])
        self.assertIn("Round 1/3", result["text"])
        self.assertIn("Round 2/3", result["text"])
        self.assertIn("Round 3/3", result["text"])
        self.assertIn("Watched 3 round(s)", result["text"])
        self.assertEqual(len([call for call in fake.calls if "open-notify" in call]), 3)
        self.assertEqual(len(client.permission_requests), 1)

    def test_each_round_is_its_own_message_so_the_editor_can_render_it_live(self):
        result, _, _ = self.turn("watch the aurora for 3 rounds every 1 second")
        rounds = [chunk for chunk in chunks(result) if chunk["content"]["text"].startswith("Round ")]
        self.assertEqual(len(rounds), 3)
        self.assertEqual(len({chunk["messageId"] for chunk in rounds}), 3)

    def test_a_quiet_feed_says_nothing_changed(self):
        result, _, _ = self.turn("watch the aurora for 3 rounds every 1 second")
        self.assertIn("no change since the last round", result["text"])

    def test_a_single_round_still_answers(self):
        result, _, fake = self.turn("watch the earthquakes for 1 round")
        self.assertIn("Round 1/1", result["text"])
        self.assertIn("Watched 1 round(s)", result["text"])
        self.assertEqual(len([call for call in fake.calls if "usgs" in call or "earthquake" in call]), 1)

    def test_the_magnitude_filter_reaches_the_reader(self):
        result, _, _ = self.turn("watch earthquakes over magnitude 4 for 2 rounds every 1 second")
        self.assertIn("at or above M4.0", result["text"])
        self.assertIn("1 quake(s)", result["text"])

    def test_the_answer_says_the_limits_and_that_nothing_keeps_running(self):
        result, _, _ = self.turn("watch the aurora for 1 round")
        self.assertIn("polled live once per round", result["text"])
        self.assertIn("nothing is still running", result["text"].lower())
        self.assertIn(DATASET, result["text"])

    def test_a_feed_that_dies_is_reported_and_the_turn_ends(self):
        result, _, _ = self.turn("watch the aurora for 2 rounds every 1 second",
                                 feed=FakeFeed(fail="swpc"))
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("I could not read the feed", result["text"])

    def test_help_does_not_ask_permission(self):
        result, client, _ = self.turn("what can you do?")
        self.assertIn("I poll", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _, _ = self.turn("watch the aurora for 2 rounds", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_cancelling_the_turn_stops_the_watch_at_once(self):
        agent_conn, client_conn = connected_pair()
        data, fake = make_data()
        agent = WatchAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        outcome: dict = {}

        def run():
            outcome.update(client.prompt("watch the space station for 20 rounds every 2 seconds",
                                         session_id))

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                if len([chunk for chunk in chunks({"updates": client.updates})
                        if chunk["content"]["text"].startswith("Round ")]) >= 2:
                    break
                time.sleep(0.01)
            self.assertTrue(fake.calls, "the watch never read the feed")
            client.cancel(session_id)
            worker.join(timeout=5)
        finally:
            client.stop()
        self.assertEqual(outcome["stopReason"], STOP_CANCELLED)
        # Cancelled long before the 20 rounds it asked for.
        self.assertLess(len([call for call in fake.calls if "open-notify" in call]), 20)

    def test_the_help_text_is_the_agent_help_constant(self):
        result, _, _ = self.turn("help")
        self.assertIn(HELP.splitlines()[0], result["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
