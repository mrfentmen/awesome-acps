"""Tests for the domains agent: RDAP parsing, routing and turns.

    python3 agents/domains/tests/test_agent.py

No network: RDAP is injected as (status, payload) pairs, shaped like the live answers verified
on 2026-09-23 (github.com and 8.8.8.8). The two behaviours worth guarding are here: a 404 is an
answer ('no registration found'), and a withheld registrar is reported as withheld rather than
printed as a blank.
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

from agent import domain_from_text, ip_from_text, route  # noqa: E402
from data import (  # noqa: E402
    DATASET,
    DomainsError,
    RdapData,
    days_until,
    display_date,
    normalise_domain,
    parse_date,
)

GITHUB = {
    "objectClassName": "domain",
    "handle": "1264983250_DOMAIN_COM-VRSN",
    "ldhName": "GITHUB.COM",
    "status": ["client delete prohibited", "client transfer prohibited"],
    "events": [
        {"eventAction": "registration", "eventDate": "2007-10-09T18:20:50Z"},
        {"eventAction": "expiration", "eventDate": "2028-10-09T18:20:50Z"},
        {"eventAction": "last changed", "eventDate": "2026-09-07T09:22:52Z"},
    ],
    "entities": [
        {"roles": ["registrar"],
         "vcardArray": ["vcard", [["version", {}, "text", "4.0"], ["fn", {}, "text", "MarkMonitor Inc."]]]},
    ],
    "nameservers": [{"ldhName": "DNS1.P08.NSONE.NET"}, {"ldhName": "NS-1283.AWSDNS-32.ORG"}],
    "secureDNS": {"delegationSigned": False},
}

#: RDAP records may omit the registrar entirely (redaction); the answer must say so.
NO_REGISTRAR = {key: value for key, value in GITHUB.items() if key != "entities"}

GOOGLE_IP = {
    "objectClassName": "ip network",
    "handle": "NET-8-8-8-0-2",
    "name": "GOGL",
    "type": "DIRECT ALLOCATION",
    "startAddress": "8.8.8.0",
    "endAddress": "8.8.8.255",
    "cidr0_cidrs": [{"v4prefix": "8.8.8.0", "length": 24}],
    "events": [{"eventAction": "registration", "eventDate": "2023-12-28T00:00:00Z"}],
    "entities": [{"roles": ["registrant"],
                  "vcardArray": ["vcard", [["fn", {}, "text", "Google LLC"]]]}],
}


class FakeRdap:
    """Returns canned (status, payload) pairs, and records the URLs it was asked for."""

    def __init__(self, records=None, status=200, raise_on=None):
        self.calls: list[str] = []
        self.records = records if records is not None else {}
        self.status = status
        self.raise_on = raise_on

    def __call__(self, url: str):
        self.calls.append(url)
        if self.raise_on and self.raise_on in url:
            raise DomainsError("connection refused")
        for name, payload in self.records.items():
            if name in url:
                return self.status, payload
        return 404, None


def make_data(**kwargs) -> tuple[RdapData, FakeRdap]:
    feed = FakeRdap(**kwargs)
    return RdapData(fetch=feed), feed


NOW = datetime.datetime(2026, 9, 23, tzinfo=datetime.timezone.utc)


class ParsingTests(unittest.TestCase):
    def test_domains_are_normalised(self):
        self.assertEqual(normalise_domain("https://www.GitHub.com/foo?x=1"), "github.com")
        self.assertEqual(normalise_domain("github.com."), "github.com")
        self.assertEqual(normalise_domain("user@example.org"), "example.org")
        with self.assertRaises(ValueError):
            normalise_domain("not a domain")
        with self.assertRaises(ValueError):
            normalise_domain("localhost")

    def test_timestamps_parse_with_and_without_milliseconds(self):
        self.assertEqual(parse_date("2028-10-09T18:20:50Z"),
                         datetime.datetime(2028, 10, 9, 18, 20, 50, tzinfo=datetime.timezone.utc))
        millis = parse_date("2026-09-23T02:02:23.558Z")
        self.assertEqual(millis.microsecond, 558000)
        self.assertIsNone(parse_date(None))
        self.assertIsNone(parse_date("not a date"))
        self.assertEqual(display_date("2027-08-30T04:00:00Z"), "2027-08-30")
        self.assertEqual(display_date(None), "unknown")

    def test_days_until_is_measured_from_now(self):
        self.assertEqual(days_until("2028-10-09T18:20:50Z", now=NOW), 747)
        self.assertLess(days_until("2026-09-01T00:00:00Z", now=NOW), 0)
        self.assertIsNone(days_until(None))


class ReaderTests(unittest.TestCase):
    def test_a_registered_domain_is_parsed(self):
        data, _ = make_data(records={"github.com": GITHUB})
        record = data.domain("github.com")
        self.assertTrue(record["registered"])
        self.assertEqual(record["registrar"], "MarkMonitor Inc.")
        self.assertEqual(record["tld"], ".com")
        self.assertEqual(record["expiration"], "2028-10-09T18:20:50Z")
        self.assertEqual(record["registration"], "2007-10-09T18:20:50Z")
        self.assertEqual(record["nameservers"][0], "dns1.p08.nsone.net")
        self.assertFalse(record["dnssec"])
        self.assertEqual(record["handle"], "1264983250_DOMAIN_COM-VRSN")

    def test_a_404_is_an_answer_not_an_error(self):
        data, _ = make_data(records={})
        record = data.domain("popsnax-zzz-2026.com")
        self.assertFalse(record["registered"])
        self.assertIsNone(record["registrar"])

    def test_com_and_net_fall_back_to_the_registry_when_the_bootstrap_fails(self):
        data, feed = make_data(records={"verisign": GITHUB}, raise_on="rdap.org")
        record = data.domain("github.com")
        self.assertTrue(record["registered"])
        self.assertIn("rdap.verisign.com", feed.calls[1])

    def test_a_withheld_registrar_stays_withheld(self):
        data, _ = make_data(records={"github.com": NO_REGISTRAR})
        self.assertIsNone(data.domain("github.com")["registrar"])

    def test_a_domain_with_no_events_has_no_dates(self):
        data, _ = make_data(records={"github.com": {"ldhName": "GITHUB.COM"}})
        record = data.domain("github.com")
        self.assertTrue(record["registered"])
        self.assertIsNone(record["expiration"])

    def test_an_ip_network_is_parsed(self):
        data, _ = make_data(records={"8.8.8.8": GOOGLE_IP})
        record = data.network("8.8.8.8")
        self.assertTrue(record["found"])
        self.assertEqual(record["name"], "GOGL")
        self.assertEqual(record["start"], "8.8.8.0")
        self.assertEqual(record["cidr"], ["24"])
        self.assertEqual(record["entities"], ["Google LLC"])

    def test_an_unknown_ip_is_not_a_guess(self):
        data, _ = make_data(records={})
        self.assertFalse(data.network("203.0.113.9")["found"])

    def test_a_dead_service_raises_one_error_type(self):
        data, _ = make_data(records={"github.com": GITHUB}, raise_on="rdap.org")
        with self.assertRaises(DomainsError):
            data.network("8.8.8.8")


class RouteTests(unittest.TestCase):
    def test_expiry_question(self):
        self.assertEqual(route("when does github.com expire?"),
                         ("domain-info", {"domain": "github.com"}))

    def test_availability_question(self):
        self.assertEqual(route("is popsnax.com taken?")[0], "domain-available")
        self.assertEqual(route("is this-name-is-free.net available?")[0], "domain-available")

    def test_registrar_question_is_information_not_availability(self):
        self.assertEqual(route("who is the registrar for example.org?")[0], "domain-info")

    def test_ip_question(self):
        self.assertEqual(route("who owns 8.8.8.8?"), ("ip-info", {"address": "8.8.8.8"}))

    def test_an_out_of_range_number_is_not_an_ip(self):
        self.assertIsNone(ip_from_text("999.1.1.1"))
        self.assertEqual(ip_from_text("203.0.113.9"), "203.0.113.9")

    def test_file_names_are_not_domains(self):
        self.assertIsNone(domain_from_text("open agent.py and README.md"))
        self.assertEqual(domain_from_text("check github.com please"), "github.com")

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
        from agent import DomainsAgent

        agent_conn, client_conn = connected_pair()
        data, feed = make_data(**kw)
        agent = DomainsAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client, feed
        finally:
            client.stop()

    def test_an_expiry_answer_reads_the_registry(self):
        result, client, _ = self.turn("when does github.com expire?", records={"github.com": GITHUB})
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("github.com is registered (a .com domain):", result["text"])
        self.assertIn("registrar: MarkMonitor Inc.", result["text"])
        self.assertIn("created: 2007-10-09", result["text"])
        self.assertIn("expires: 2028-10-09", result["text"])
        self.assertIn("DNSSEC: not signed", result["text"])
        self.assertIn(DATASET, result["text"])
        self.assertEqual(len(client.permission_requests), 1)

    def test_no_record_is_described_not_sold(self):
        result, _, _ = self.turn("is popsnax-zzz-2026.com taken?", records={})
        self.assertIn("has no registration record", result["text"])
        self.assertIn("does NOT mean the name is buyable", result["text"])
        self.assertIn("it can change at any moment", result["text"])
        self.assertNotIn("available!", result["text"])

    def test_a_taken_name_says_taken_with_the_date(self):
        result, _, _ = self.turn("is github.com taken?", records={"github.com": GITHUB})
        self.assertIn("github.com is taken", result["text"])
        self.assertIn("expiring 2028-10-09", result["text"])

    def test_a_withheld_registrar_is_said_out_loud(self):
        result, _, _ = self.turn("who is the registrar for github.com?",
                                 records={"github.com": NO_REGISTRAR})
        self.assertIn("registrar: not published in this registry's RDAP record", result["text"])

    def test_an_ip_answer_names_the_block_and_the_holder(self):
        result, _, _ = self.turn("who owns 8.8.8.8?", records={"8.8.8.8": GOOGLE_IP})
        self.assertIn("8.8.8.8 - GOGL:", result["text"])
        self.assertIn("block: 8.8.8.0 - 8.8.8.255", result["text"])
        self.assertIn("holder: Google LLC", result["text"])

    def test_help_does_not_ask_permission(self):
        result, client, _ = self.turn("what can you do?")
        self.assertIn("RDAP", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _, _ = self.turn("when does github.com expire?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_the_registry_is_used_when_the_bootstrap_is_down(self):
        result, _client, feed = self.turn("when does github.com expire?",
                                          records={"verisign": GITHUB}, raise_on="rdap.org")
        self.assertIn("github.com is registered", result["text"])
        self.assertIn("rdap.verisign.com", feed.calls[1])

    def test_a_dead_rdap_is_reported(self):
        result, _, _ = self.turn("when does github.com expire?",
                                 records={"github.com": GITHUB}, raise_on="rdap")
        self.assertIn("could not read RDAP", result["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
