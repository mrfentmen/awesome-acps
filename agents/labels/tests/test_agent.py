"""Tests for the labels agent: openFDA reader, routing, skills, permissions.

    python3 agents/labels/tests/test_agent.py
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

from agent import LabelsAgent, drug_from_text, route  # noqa: E402
from data import DATASET, SECTIONS, LabelsData, LabelsError  # noqa: E402

LABEL_PAYLOAD = {"meta": {"results": {"total": 1, "skip": 0, "limit": 1}}, "results": [{
    "effective_time": "20260715",
    "openfda": {
        "brand_name": ["LIPITOR"], "generic_name": ["ATORVASTATIN CALCIUM"],
        "manufacturer_name": ["VIATRIS SPECIALTY LLC"], "substance_name": ["ATORVASTATIN CALCIUM"],
    },
    "indications_and_usage": ["LIPITOR is indicated to reduce the risk of cardiovascular events."],
    "dosage_and_administration": ["The recommended dose is 10 to 80 mg once daily."],
    "warnings": ["WARNING: DO NOT USE IN PREGNANCY. " + "x" * 1400],
    "contraindications": ["Active liver disease."],
    "adverse_reactions": ["The most common adverse reactions are myalgia and diarrhea."],
    "drug_interactions": ["Avoid with strong CYP3A4 inhibitors."],
}]}

EMPTY_PAYLOAD = {"meta": {"results": {"total": 0, "skip": 0, "limit": 1}}, "results": []}


class FakeData(LabelsData):
    """Same interface as the real reader, no network."""

    def __init__(self, raise_error: bool = False, missing: bool = False):
        self.raise_error = raise_error
        self.missing = missing
        self.calls: list[dict] = []

    def search(self, name: str, limit: int = 3) -> dict:
        if self.raise_error:
            raise LabelsError("openFDA is offline")
        self.calls.append({"kind": "search", "name": name})
        return {"dataset": DATASET, "query": name, "rows": [{
            "openfda": {"brand_name": "LIPITOR", "generic_name": "ATORVASTATIN CALCIUM",
                        "manufacturer": "VIATRIS SPECIALTY LLC", "substance_name": "ATORVASTATIN CALCIUM"},
            "has_sections": ["adverse_reactions", "contraindications", "dosage_and_administration",
                             "drug_interactions", "indications_and_usage", "warnings"],
        }]}

    def label(self, name: str) -> dict:
        if self.raise_error:
            raise LabelsError("openFDA is offline")
        self.calls.append({"kind": "label", "name": name})
        if self.missing:
            return {"dataset": DATASET, "query": name, "found": False, "label": None, "sections": {}}
        return {
            "dataset": DATASET, "query": name, "found": True,
            "openfda": {"brand_name": "LIPITOR", "generic_name": "ATORVASTATIN CALCIUM",
                        "manufacturer": "VIATRIS SPECIALTY LLC", "substance_name": "ATORVASTATIN CALCIUM"},
            "effective_time": "20260715",
            "sections": {
                "indications_and_usage": "LIPITOR is indicated to reduce the risk of cardiovascular events.",
                "dosage_and_administration": "The recommended dose is 10 to 80 mg once daily.",
                "warnings": "WARNING: DO NOT USE IN PREGNANCY.",
                "contraindications": "Active liver disease.",
                "adverse_reactions": "The most common adverse reactions are myalgia and diarrhea.",
                "drug_interactions": "Avoid with strong CYP3A4 inhibitors.",
            },
        }


class QueueReader:
    """One side's inbox: an iterator of lines, plus push to add one."""

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
    def test_drug_validation(self):
        self.assertEqual(LabelsData.check_drug("  lipitor "), "lipitor")
        self.assertEqual(LabelsData.check_drug('lipitor"'), "lipitor")
        with self.assertRaises(ValueError):
            LabelsData.check_drug("x")
        with self.assertRaises(ValueError):
            LabelsData.check_drug("a" * 70)

    def test_label_reads_the_sections(self):
        data = LabelsData(fetch=lambda url, params: LABEL_PAYLOAD)
        result = data.label("lipitor")
        self.assertTrue(result["found"])
        self.assertEqual(result["openfda"]["brand_name"], "LIPITOR")
        self.assertEqual(result["sections"]["warnings"], LABEL_PAYLOAD["results"][0]["warnings"][0])
        self.assertEqual(len(result["sections"]), len(SECTIONS))

    def test_a_missing_label_is_an_empty_result_not_an_error(self):
        data = LabelsData(fetch=lambda url, params: EMPTY_PAYLOAD)
        result = data.label("notadrug")
        self.assertFalse(result["found"])
        self.assertEqual(result["sections"], {})

    def test_the_search_query_asks_for_brand_and_generic(self):
        seen = {}

        def fetch(url, params):
            seen.update(params)
            return LABEL_PAYLOAD

        data = LabelsData(fetch=fetch)
        data.search("lipitor", limit=2)
        self.assertIn('openfda.brand_name:"lipitor"', seen["search"])
        self.assertIn('openfda.generic_name:"lipitor"', seen["search"])
        self.assertEqual(seen["limit"], "2")

    def test_trim_cuts_long_sections_with_a_note(self):
        trimmed = LabelsData.trim("y" * 2000)
        self.assertLess(len(trimmed), 700)
        self.assertIn("[cut at 600 characters]", trimmed)
        self.assertEqual(LabelsData.trim("short"), "short")

    def test_a_payload_without_results_is_an_error(self):
        data = LabelsData(fetch=lambda url, params: {"meta": {}})
        with self.assertRaises(LabelsError):
            data.label("lipitor")


class RouteTests(unittest.TestCase):
    def test_help(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")

    def test_info(self):
        skill, params = route("what is lipitor?")
        self.assertEqual(skill, "label-info")
        self.assertEqual(params["drug"], "lipitor")

    def test_focus_on_warnings(self):
        skill, params = route("warnings for metformin")
        self.assertEqual(skill, "label-info")
        self.assertEqual(params["drug"], "metformin")
        self.assertEqual(params["focus"], "warnings")

    def test_search(self):
        skill, params = route("which drugs are called metformin?")
        self.assertEqual(skill, "label-search")
        self.assertEqual(params["drug"], "metformin")

    def test_drug_extraction_is_conservative(self):
        self.assertIsNone(drug_from_text("what can you do?"))
        self.assertEqual(drug_from_text("dosage for amoxicillin"), "amoxicillin")


class TurnTests(unittest.TestCase):
    def turn(self, text: str, data: FakeData | None = None, permission: str = "allow-once"):
        agent_conn, client_conn = connected_pair()
        agent = LabelsAgent(agent_conn, data or FakeData())
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_info_quotes_the_label(self):
        result, client = self.turn("what is lipitor?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("LIPITOR (ATORVASTATIN CALCIUM) - VIATRIS SPECIALTY LLC", result["text"])
        self.assertIn("Label effective date: 2026-07-15", result["text"])
        self.assertIn("LIPITOR is indicated to reduce the risk of cardiovascular events.", result["text"])
        self.assertIn("not medical advice", result["text"])
        tools = [update for update in result["updates"] if update.get("sessionUpdate") == "tool_call"]
        self.assertEqual(tools[0]["name"], "label-info")
        self.assertEqual(len(client.permission_requests), 1)

    def test_focus_puts_warnings_first(self):
        result, _ = self.turn("warnings for lipitor")
        self.assertLess(result["text"].index("Warnings:"), result["text"].index("Indications and usage:"))

    def test_search_lists_the_labels(self):
        result, _ = self.turn("which drugs are called lipitor?")
        self.assertIn("LIPITOR (ATORVASTATIN CALCIUM) - VIATRIS SPECIALTY LLC", result["text"])
        self.assertIn("6 sections on file", result["text"])

    def test_a_missing_drug_says_so(self):
        result, _ = self.turn("what is notadrug?", data=FakeData(missing=True))
        self.assertIn("no label matching 'notadrug'", result["text"])

    def test_help_does_not_ask_permission(self):
        result, client = self.turn("what can you do?")
        self.assertIn("openFDA", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _ = self.turn("what is lipitor?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_a_feed_failure_is_reported(self):
        result, _ = self.turn("what is lipitor?", data=FakeData(raise_error=True))
        self.assertIn("could not read the openFDA feed", result["text"])
        statuses = [update.get("status") for update in result["updates"]
                    if update.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", statuses)


if __name__ == "__main__":
    unittest.main()
