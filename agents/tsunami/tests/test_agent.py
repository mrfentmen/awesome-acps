"""Tests for the tsunami agent: CAP parsing, in-force logic, place matching, routing and turns.

    python3 agents/tsunami/tests/test_agent.py

No network: the CAP XML is injected, shaped like the live files read on 2026-09-23 (PAAQ
'Tsunami Information' Minor/Unknown/Unlikely, magnitude 6.3 Mwp, expired 2026-09-17T15:23:45;
PHEB 'Tsunami Information' for the VICINITY OF PUERTO RICO, expired 2026-09-18T14:25:30). The
clock is injected too, because "is it in force?" is a question about the current time.
"""

from __future__ import annotations

import datetime
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
from data import TsunamiData, TsunamiError, hours_between, kind_of, parse_time  # noqa: E402

PAAQ = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>PAAQ-1-tliih5</identifier>
  <sender>ntwc@noaa.gov</sender>
  <sent>2026-09-17T14:23:45-00:00</sent>
  <status>Actual</status><msgType>Alert</msgType><source>NTWC</source><scope>Public</scope>
  <info>
    <category>Geo</category>
    <event>Tsunami Information</event>
    <responseType>None</responseType>
    <urgency>Unknown</urgency>
    <severity>Minor</severity>
    <certainty>Unlikely</certainty>
    <expires>2026-09-17T15:23:45-00:00</expires>
    <senderName>NWS National Tsunami Warning Center Palmer AK</senderName>
    <headline>This is a Tsunami Information Statement.</headline>
    <description>This is a Tsunami Information Statement. - Event details: Preliminary
      magnitude 6.3 (Mwp) earthquake</description>
    <instruction>An earthquake has occurred; a tsunami is not expected.</instruction>
    <web>http://www.tsunami.gov/events/PAAQ/2026/09/17/tliih5/1/WEAK53/WEAK53.txt</web>
    <area><areaDesc>45 miles NE of Amukta Pass, Alaska</areaDesc>
      <circle>52.81,-171.341 0.0</circle></area>
    <parameter><valueName>EventPreliminaryMagnitude</valueName><value>6.3</value></parameter>
    <parameter><valueName>EventPreliminaryMagnitudeType</valueName><value>Mwp</value></parameter>
    <parameter><valueName>EventOriginTime</valueName><value>2026-09-17T14:19:53-00:00</value></parameter>
    <parameter><valueName>EventDepth</valueName><value>83 kilometers</value></parameter>
    <parameter><valueName>EventLocationName</valueName><value>45 miles NE of Amukta Pass, Alaska</value></parameter>
    <parameter><valueName>EventLatLon</valueName><value>52.810,-171.341 0.000</value></parameter>
  </info>
</alert>"""

PHEB = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>PHEB-1-26261050</identifier>
  <sender>ptwc@noaa.gov</sender>
  <sent>2026-09-18T13:25:30-00:00</sent>
  <status>Actual</status><msgType>Alert</msgType><source>PTWC</source><scope>Public</scope>
  <info>
    <category>Geo</category>
    <event>Tsunami Information</event>
    <urgency>Unknown</urgency><severity>Minor</severity><certainty>Unlikely</certainty>
    <expires>2026-09-18T14:25:30-00:00</expires>
    <senderName>NWS PACIFIC TSUNAMI WARNING CENTER HONOLULU HI</senderName>
    <headline>This is a Tsunami Information Statement.</headline>
    <description>Event details: Preliminary magnitude 4.6 (Ml) earthquake</description>
    <instruction>N/A</instruction>
    <web>http://www.tsunami.gov/events/PHEB/2026/09/18/26261050/1/WECA42/WECA42.txt</web>
    <area><areaDesc>VICINITY OF PUERTO RICO</areaDesc></area>
    <parameter><valueName>EventPreliminaryMagnitude</valueName><value>4.6</value></parameter>
  </info>
</alert>"""

#: The same shape, but still in force at the injected clock.
WARNING = PAAQ.replace("<event>Tsunami Information</event>", "<event>Tsunami Warning</event>") \
              .replace("<severity>Minor</severity>", "<severity>Extreme</severity>") \
              .replace("<expires>2026-09-17T15:23:45-00:00</expires>",
                       "<expires>2026-09-24T15:23:45-00:00</expires>") \
              .replace("<certainty>Unlikely</certainty>", "<certainty>Likely</certainty>")

NOW = datetime.datetime(2026, 9, 23, 6, 0, tzinfo=datetime.timezone.utc)


class FakeFeeds:
    def __init__(self, paaq=PAAQ, pheb=PHEB, missing=()):
        self.calls: list[str] = []
        self.paaq = paaq
        self.pheb = pheb
        self.missing = set(missing)

    def __call__(self, url: str, params=None):
        self.calls.append(url)
        code = url.rsplit("/", 1)[-1].replace("CAP.xml", "")
        if code in self.missing:
            raise TsunamiError(f"request failed: HTTP Error 500 for {code}")
        return self.paaq if code == "PAAQ" else self.pheb


def make_data(**kw):
    feed = FakeFeeds(**kw)
    return TsunamiData(fetch=feed, now=NOW), feed


class HelperTests(unittest.TestCase):
    def test_cap_timestamps(self):
        self.assertEqual(parse_time("2026-09-17T14:23:45-00:00"),
                         datetime.datetime(2026, 9, 17, 14, 23, 45, tzinfo=datetime.timezone.utc))
        self.assertEqual(parse_time("2026-09-17T14:23:45Z"),
                         datetime.datetime(2026, 9, 17, 14, 23, 45, tzinfo=datetime.timezone.utc))
        self.assertIsNone(parse_time(""))
        self.assertIsNone(parse_time("last Tuesday"))

    def test_hours_between(self):
        self.assertEqual(hours_between(NOW, NOW - datetime.timedelta(hours=12)), 12.0)

    def test_the_kind_of_bulletin(self):
        self.assertEqual(kind_of("Tsunami Warning"), "warning")
        self.assertEqual(kind_of("Tsunami Information Statement"), "information")
        self.assertEqual(kind_of("Tsunami Advisory"), "advisory")
        self.assertEqual(kind_of("Something New"), "something new")

    def test_a_place_in_the_question(self):
        self.assertEqual(place_from_text("is there a tsunami warning for Hawaii?"), "hawaii")
        self.assertEqual(place_from_text("tsunami near Amukta Pass?"), "Amukta Pass")
        #: "for the west coast" keeps the region but loses the article.
        self.assertEqual(place_from_text("any tsunami advisory for the west coast?"), "west coast")
        self.assertIsNone(place_from_text("is there a tsunami warning right now?"))


class ReadTests(unittest.TestCase):
    def test_both_centres_are_read_and_expired_bulletins_are_not_called_active(self):
        data, feed = make_data()
        found = data.alerts()
        self.assertEqual(len(found["alerts"]), 2)
        self.assertEqual(found["active"], [])  # both lapsed days before the injected clock
        self.assertEqual(found["newest"]["centre"], "PHEB")  # newest sent first
        self.assertEqual(found["newest"]["expires"], "2026-09-18T14:25:30+00:00")
        self.assertEqual(found["newest"]["expired_hours_ago"], 111.6)
        self.assertTrue(all(row["has_end_time"] for row in found["alerts"]))
        self.assertEqual(len(feed.calls), 2)

    def test_a_bulletin_still_in_force_is_active(self):
        data, _feed = make_data(paaq=WARNING)
        found = data.alerts()
        self.assertEqual([row["centre"] for row in found["active"]], ["PAAQ"])
        self.assertEqual(found["active"][0]["kind"], "warning")
        self.assertEqual(found["active"][0]["severity"], "Extreme")
        self.assertIsNone(found["active"][0]["expired_hours_ago"])

    def test_the_earthquake_parameters_are_pulled_out(self):
        data, _feed = make_data()
        newest = data.alerts()["alerts"][1]  # PAAQ, the older one
        self.assertEqual(newest["parameters"]["magnitude"], "6.3")
        self.assertEqual(newest["parameters"]["scale"], "Mwp")
        self.assertEqual(newest["parameters"]["depth"], "83 kilometers")
        self.assertEqual(newest["parameters"]["origin_time"], "2026-09-17T14:19:53-00:00")
        self.assertEqual(newest["areas"], ["45 miles NE of Amukta Pass, Alaska"])
        self.assertEqual(newest["circles"], ["52.81,-171.341 0.0"])
        self.assertEqual(newest["sent_hours_ago"], 135.6)

    def test_one_centre_down_still_answers_and_says_which_was_missing(self):
        data, _feed = make_data(missing=("PHEB",))
        found = data.alerts()
        self.assertEqual([row["centre"] for row in found["alerts"]], ["PAAQ"])
        self.assertEqual([row["centre"] for row in found["unavailable"]], ["PHEB"])

    def test_both_centres_down_is_an_error(self):
        data, _feed = make_data(missing=("PAAQ", "PHEB"))
        with self.assertRaises(TsunamiError):
            data.alerts()

    def test_a_broken_bulletin(self):
        data, _feed = make_data(paaq="<alert>not closed", pheb="<nothing/>")
        with self.assertRaises(TsunamiError):
            data.alerts()

    def test_a_place_named_in_the_area(self):
        data, _feed = make_data()
        found = data.place("Puerto Rico")
        self.assertEqual(len(found["matches"]), 1)
        self.assertEqual(found["matches"][0]["matched_in"], "area")
        self.assertFalse(found["matches"][0]["active"])

    def test_a_place_named_only_in_the_prose(self):
        data, _feed = make_data(paaq=WARNING.replace("<instruction>An earthquake has occurred; a tsunami is not expected.</instruction>", "<instruction>No action needed in Hawaii.</instruction>"))
        found = data.place("Hawaii")
        self.assertEqual(len(found["matches"]), 1)
        self.assertEqual(found["matches"][0]["matched_in"], "bulletin text")

    def test_a_place_no_bulletin_names(self):
        data, _feed = make_data()
        found = data.place("Japan")
        self.assertEqual(found["matches"], [])
        self.assertEqual(found["place"], "Japan")
        with self.assertRaises(ValueError):
            data.place("  ")


class RouteTests(unittest.TestCase):
    def test_a_status_question(self):
        self.assertEqual(route("is there a tsunami warning right now?"),
                         ("tsunami-status", {}))
        self.assertEqual(route("what was the last tsunami bulletin?"),
                         ("tsunami-status", {}))

    def test_a_place_question(self):
        self.assertEqual(route("is there a tsunami warning for Hawaii?"),
                         ("tsunami-place", {"place": "hawaii"}))
        self.assertEqual(route("any tsunami advisory for the west coast?"),
                         ("tsunami-place", {"place": "west coast"}))

    def test_a_question_with_no_tsunami_in_it(self):
        self.assertEqual(route("what is the weather in Tokyo?")[0], "help")
        self.assertEqual(route("how deep is the water table in Kansas?")[0], "help")

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
        from agent import TsunamiAgent

        agent_conn, client_conn = connected_pair()
        data, feed = make_data(**kw)
        agent = TsunamiAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client, feed
        finally:
            client.stop()

    def test_the_calm_answer_is_a_clear_no_with_the_dates_behind_it(self):
        result, client, _ = self.turn("is there a tsunami warning right now?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertEqual(len(client.permission_requests), 1)
        self.assertIn("No tsunami bulletin is in force right now.", result["text"])
        self.assertIn("sent 2026-09-18T13:25:30+00:00 (112.6 h ago)", result["text"])
        self.assertIn("expired 2026-09-18T14:25:30+00:00 - 111.6 h ago", result["text"])
        self.assertIn("VICINITY OF PUERTO RICO", result["text"])

    def test_a_live_warning_is_shown_in_full(self):
        result, _, _ = self.turn("is there a tsunami warning right now?", paaq=WARNING)
        self.assertIn("1 tsunami bulletin(s) are in force right now:", result["text"])
        self.assertIn("Tsunami Warning - NWS National Tsunami Warning Center", result["text"])
        self.assertIn("severity Extreme, urgency Unknown, certainty Likely", result["text"])
        self.assertIn("earthquake: magnitude 6.3 Mwp, depth 83 kilometers", result["text"])
        self.assertIn("expires 2026-09-24T15:23:45+00:00", result["text"])
        self.assertIn("status: IN FORCE", result["text"])

    def test_a_place_that_is_named(self):
        result, _, _ = self.turn("is there a tsunami warning for Puerto Rico?")
        self.assertIn("Puerto Rico - 1 bulletin(s) name it:", result["text"])
        self.assertIn("(expired)", result["text"])

    def test_a_place_that_is_not_named(self):
        result, _, _ = self.turn("is there a tsunami warning for Japan?")
        self.assertIn("No current or last bulletin names Japan.", result["text"])
        self.assertIn("not under any bulletin", result["text"])

    def test_one_centre_down_is_admitted(self):
        result, _, _ = self.turn("is there a tsunami warning right now?", missing=("PHEB",))
        self.assertIn("no answer from: PHEB", result["text"])

    def test_help_asks_nothing(self):
        result, client, feed = self.turn("what can you do?")
        self.assertIn("tsunami warning centres", result["text"])
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(feed.calls, [])

    def test_permission_denied(self):
        result, _, _ = self.turn("is there a tsunami warning right now?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)

    def test_both_centres_down(self):
        result, _, _ = self.turn("is there a tsunami warning right now?",
                                 missing=("PAAQ", "PHEB"))
        self.assertIn("I could not read the bulletins", result["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
