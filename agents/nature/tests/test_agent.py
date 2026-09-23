"""Tests for the nature agent: GBIF reader, name resolution chain, routing, turns.

    python3 agents/nature/tests/test_agent.py

Every feed answer is injected (no network). The resolver is exercised through the real
GbifData logic by dispatching a fake fetch on the request path: GBIF for scientific
names and occurrence counts, iNaturalist for common names.
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

from agent import NatureAgent, route, taxon_from_text  # noqa: E402
from data import (  # noqa: E402
    DATASET,
    GbifData,
    NameNotFound,
    NatureError,
    country_label,
    format_number,
    month_name,
    name_words,
    singular,
)

MATCH_MONARCH = {
    "usageKey": 5133088, "scientificName": "Danaus plexippus (Linnaeus, 1758)",
    "canonicalName": "Danaus plexippus", "rank": "SPECIES", "status": "ACCEPTED",
    "confidence": 99, "matchType": "EXACT", "kingdom": "Animalia",
    "family": "Nymphalidae", "genus": "Danaus",
}

#: What iNaturalist returns for a real common name: the species first, a lookalike below it.
INAT_MONARCH = {
    "results": [
        {"id": 48662, "name": "Danaus plexippus", "rank": "species",
         "preferred_common_name": "Monarch", "matched_term": "Monarch Butterfly"},
        {"id": 60551, "name": "Danaus", "rank": "genus",
         "preferred_common_name": "Tiger Milkweed Butterflies", "matched_term": "Tigers & Monarchs"},
    ],
}

#: A broad rank: GBIF's matcher has no entry for an infraclass, so the backbone is searched.
INAT_SHARK = {
    "results": [
        {"id": 50503, "name": "Selachii", "rank": "infraclass",
         "preferred_common_name": "Sharks", "matched_term": "Sharks"},
    ],
}

GBIF_SELACHII = {
    "results": [
        {"key": 121468044, "canonicalName": "Selachii", "rank": "ORDER"},
        {"key": 180181771, "canonicalName": "Selachii", "scientificName": "Selachii",
         "rank": "INFRACLASS", "kingdom": "Animalia"},
    ],
}

#: An ambiguous word: nothing lines up exactly, so the agent must refuse instead of guessing.
INAT_AMBIGUOUS = {
    "results": [
        {"id": 52293, "name": "Euplagia quadripunctaria", "rank": "species",
         "preferred_common_name": "Jersey Tiger", "matched_term": "Jersey Tiger"},
        {"id": 52800, "name": "Erebidae", "rank": "family",
         "preferred_common_name": "Underwing, Tiger, Tussock, and Allied Moths",
         "matched_term": "Underwing, Tiger, Tussock, and Allied Moths"},
    ],
}

MATCH_SYNONYM = {
    "usageKey": 2421169, "scientificName": "Squalus carcharias Linnaeus, 1758",
    "rank": "SPECIES", "status": "SYNONYM", "acceptedUsageKey": 2420694,
    "matchType": "EXACT", "confidence": 99,
}

SPECIES_ACCEPTED = {
    "key": 2420694, "canonicalName": "Carcharodon carcharias", "scientificName": "Carcharodon carcharias (Linnaeus, 1758)",
    "rank": "SPECIES", "kingdom": "Animalia", "family": "Lamnidae", "genus": "Carcharodon",
}

OCCURRENCE_WORLD = {
    "count": 817344,
    "facets": [{"field": "COUNTRY", "counts": [
        {"name": "United States of America", "count": 669108},
        {"name": "Canada", "count": 87502},
        {"name": "Mexico", "count": 18634},
        {"name": "Australia", "count": 11737},
        {"name": "Spain", "count": 7727},
    ]}],
}

OCCURRENCE_CANADA = {"count": 87502}

OCCURRENCE_MONTHS = {
    "count": 817344,
    "facets": [{"field": "MONTH", "counts": [
        {"name": "8", "count": 106158}, {"name": "7", "count": 81175},
        {"name": "9", "count": 72355}, {"name": "10", "count": 45292},
        {"name": "6", "count": 31464},
    ]}],
}

OCCURRENCE_RECENT = {
    "count": 8972,
    "results": [
        {"eventDate": "2026-09-21", "country": "United States of America", "stateProvince": "Kansas",
         "locality": "Wichita", "recordedBy": "A. Observer", "basisOfRecord": "HUMAN_OBSERVATION",
         "decimalLatitude": 37.69, "decimalLongitude": -97.34,
         "scientificName": "Danaus plexippus plexippus (Linnaeus, 1758)"},
        {"eventDate": "2026-09-20", "country": "Mexico", "locality": None,
         "basisOfRecord": "PRESERVED_SPECIMEN"},
    ],
}

SPECIES_PAYLOAD = {"key": 2420694}


class FakeFeed:
    """Dispatch on the GBIF path; records every call so the resolver chain can be checked."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    def __call__(self, url: str, params: dict):
        if self.fail:
            raise NatureError("GBIF is offline")
        self.calls.append((url, dict(params)))
        path = url.split("/v1", 1)[1]
        if "inaturalist" in url:
            if path != "/taxa":
                raise AssertionError(f"unexpected iNaturalist path {path}")
            query = str(params.get("q", "")).lower()
            if "tiger" in query:
                return INAT_AMBIGUOUS
            if "shark" in query:
                return INAT_SHARK
            if "atlantis" in query:
                return {"results": []}
            return INAT_MONARCH
        if path == "/species/match":
            name = params.get("name")
            if name == "Danaus plexippus":
                return MATCH_MONARCH
            if name == "Squalus carcharias":
                return MATCH_SYNONYM
            if name == "Carcharodon carcharias":
                return MATCH_MONARCH_OR_ACCEPTED[name]
            return {"matchType": "NONE", "confidence": 100}
        if path == "/species/search":
            if "selachii" in str(params.get("q", "")).lower():
                return GBIF_SELACHII
            return {"results": []}
        if path.startswith("/species/"):
            return SPECIES_ACCEPTED
        if path == "/occurrence/search":
            if params.get("facet") == "country":
                return OCCURRENCE_WORLD
            if params.get("facet") == "month":
                return OCCURRENCE_MONTHS
            if params.get("country") == "CA":
                return OCCURRENCE_CANADA
            if params.get("limit"):
                return OCCURRENCE_RECENT
            return OCCURRENCE_WORLD
        raise AssertionError(f"unexpected path {path}")


MATCH_MONARCH_OR_ACCEPTED = {
    "Danaus plexippus": MATCH_MONARCH,
    "Carcharodon carcharias": {
        "usageKey": 2420694, "scientificName": "Carcharodon carcharias (Linnaeus, 1758)",
        "rank": "SPECIES", "status": "ACCEPTED", "matchType": "EXACT", "confidence": 99,
        "kingdom": "Animalia", "family": "Lamnidae", "genus": "Carcharodon",
    },
}


def live_data(fail: bool = False) -> tuple[GbifData, FakeFeed]:
    feed = FakeFeed(fail=fail)
    return GbifData(fetch=feed), feed


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
    def test_month_names_and_number_format(self):
        self.assertEqual(month_name("8"), "August")
        self.assertEqual(month_name(1), "January")
        self.assertIsNone(month_name("13"))
        self.assertIsNone(month_name(None))
        self.assertEqual(format_number(817344), "817,344")
        self.assertEqual(format_number(None), "unknown")

    def test_name_validation(self):
        self.assertEqual(GbifData.check_name("  Danaus plexippus? "), "Danaus plexippus")
        with self.assertRaises(ValueError):
            GbifData.check_name("123")

    def test_country_codes_and_phrases(self):
        self.assertEqual(GbifData.country_code("canada"), "CA")
        self.assertEqual(GbifData.country_code("UK"), "GB")
        self.assertEqual(GbifData.country_code("us"), "US")
        self.assertIsNone(GbifData.country_code(""))
        with self.assertRaises(ValueError):
            GbifData.country_code("Atlantis")
        code, phrase = GbifData.country_phrase_from_text("how many in the United States?")
        self.assertEqual((code, phrase), ("US", "united states"))
        self.assertEqual(GbifData.country_phrase_from_text("how many records of oak?")[0], None)

    def test_country_labels_expand_iso_codes(self):
        self.assertEqual(country_label("ES"), "Spain")
        self.assertEqual(country_label("US"), "United States of America")
        self.assertEqual(country_label("gb"), "United Kingdom")
        self.assertEqual(country_label("Canada"), "Canada")
        self.assertEqual(country_label(None), "")

    def test_resolve_a_scientific_name(self):
        data, _ = live_data()
        taxon = data.resolve("Danaus plexippus")
        self.assertTrue(taxon["resolved"])
        self.assertEqual(taxon["key"], 5133088)
        self.assertEqual(taxon["status"], "ACCEPTED")
        self.assertEqual(taxon["via"], "match")
        self.assertEqual(taxon["family"], "Nymphalidae")

    def test_resolve_a_common_name_through_inaturalist(self):
        data, feed = live_data()
        taxon = data.resolve("monarch butterfly")
        self.assertEqual(taxon["key"], 5133088)
        self.assertEqual(taxon["scientific_name"], "Danaus plexippus (Linnaeus, 1758)")
        self.assertEqual(taxon["via"], "inat")
        self.assertEqual(taxon["common_name"], "Monarch")
        names = [params.get("name") for url, params in feed.calls if url.endswith("/species/match")]
        self.assertEqual(names[0], "monarch butterfly")
        self.assertIn("Danaus plexippus", names)
        self.assertEqual([params.get("q") for url, params in feed.calls if "inaturalist" in url],
                         ["monarch butterfly"])

    def test_resolve_folds_plurals_before_asking(self):
        data, feed = live_data()
        taxon = data.resolve("monarch butterflies")
        self.assertEqual(taxon["key"], 5133088)
        asked = [params.get("q") for url, params in feed.calls if "inaturalist" in url]
        self.assertEqual(asked, ["monarch butterflies"])

    def test_a_broad_rank_comes_from_the_backbone_search(self):
        data, feed = live_data()
        taxon = data.resolve("shark")
        self.assertEqual(taxon["key"], 180181771)
        self.assertEqual(taxon["rank"], "INFRACLASS")
        self.assertEqual(taxon["common_name"], "Sharks")
        self.assertEqual(taxon["match_type"], "BACKBONE_SEARCH")
        searched = [params.get("q") for url, params in feed.calls if url.endswith("/species/search")]
        self.assertEqual(searched, ["Selachii"])

    def test_a_name_only_backbone_node_is_refused(self):
        """Selachii exists in the backbone with zero records; answering from it would lie."""
        def fetch(url: str, params: dict):
            if "inaturalist" in url:
                return INAT_SHARK
            if url.endswith("/species/match"):
                return {"matchType": "NONE"}
            if url.endswith("/species/search"):
                return GBIF_SELACHII
            if url.endswith("/occurrence/search"):
                return {"count": 0}
            raise AssertionError(f"unexpected url {url}")

        with self.assertRaises(NameNotFound):
            GbifData(fetch=fetch).resolve("shark")

    def test_an_ambiguous_word_is_refused_with_candidates(self):
        data, _ = live_data()
        with self.assertRaises(NameNotFound) as caught:
            data.resolve("tiger")
        self.assertIn("could not pin down", str(caught.exception))
        self.assertEqual([c["scientific_name"] for c in caught.exception.candidates],
                         ["Euplagia quadripunctaria", "Erebidae"])
        self.assertEqual(caught.exception.candidates[0]["common_name"], "Jersey Tiger")

    def test_resolve_a_synonym_follows_the_accepted_key(self):
        data, _ = live_data()
        taxon = data.resolve("Squalus carcharias")
        self.assertEqual(taxon["key"], 2420694)
        self.assertEqual(taxon["scientific_name"], "Carcharodon carcharias")
        self.assertEqual(taxon["status"], "ACCEPTED")
        self.assertEqual(taxon["synonym_of"], "Squalus carcharias Linnaeus, 1758")

    def test_singular_folds_only_for_matching(self):
        self.assertEqual(singular("butterflies"), "butterfly")
        self.assertEqual(singular("oaks"), "oak")
        self.assertEqual(singular("grass"), "grass")
        self.assertEqual(name_words("Monarch Butterflies"), ["monarch", "butterfly"])
        self.assertEqual(name_words(None), [])

    def test_unknown_name_raises_with_candidates(self):
        data = GbifData(fetch=lambda url, params: {"matchType": "NONE"} if url.endswith("/match")
                        else {"results": []})
        with self.assertRaises(NameNotFound) as caught:
            data.resolve("Atlantis weed")
        self.assertEqual(caught.exception.candidates, [])

    def test_count_reports_country_share(self):
        data, _ = live_data()
        result = data.count("Danaus plexippus", "CA")
        self.assertEqual(result["total"], 817344)
        self.assertEqual(result["country"], "CA")
        self.assertEqual(result["country_count"], 87502)
        self.assertEqual(result["top_countries"][0]["country"], "United States of America")

    def test_season_sorts_by_month_count(self):
        data, _ = live_data()
        result = data.season("Danaus plexippus")
        self.assertEqual(result["months"][0]["month"], "August")
        self.assertEqual(result["months"][0]["count"], 106158)
        self.assertEqual(result["months"][1]["month"], "July")

    def test_recent_records(self):
        data, _ = live_data()
        result = data.recent("Danaus plexippus", 2)
        self.assertEqual(len(result["records"]), 2)
        self.assertEqual(result["records"][0]["date"], "2026-09-21")
        self.assertEqual(result["records"][0]["locality"], "Wichita")

    def test_limit_is_validated(self):
        with self.assertRaises(ValueError):
            GbifData.check_limit(0)
        with self.assertRaises(ValueError):
            GbifData.check_limit(50)


class NameGuessTests(unittest.TestCase):
    def test_scientific_name_after_of(self):
        self.assertEqual(taxon_from_text("how many records of Danaus plexippus are there?"),
                         "Danaus plexippus")

    def test_common_name_after_how_many(self):
        self.assertEqual(taxon_from_text("how many monarch butterflies are there?"),
                         "monarch butterflies")

    def test_when_question_strips_noise(self):
        self.assertEqual(taxon_from_text("when are monarch butterflies recorded most?"),
                         "monarch butterflies")

    def test_species_question(self):
        self.assertEqual(taxon_from_text("what species is Carcharodon carcharias?"),
                         "Carcharodon carcharias")

    def test_no_name(self):
        self.assertIsNone(taxon_from_text("how many records are there?"))


class RouteTests(unittest.TestCase):
    def test_count_is_the_common_question(self):
        skill, params = route("how many records of Danaus plexippus are there?")
        self.assertEqual(skill, "nature-count")
        self.assertEqual(params["name"], "Danaus plexippus")

    def test_country_is_extracted_without_eating_the_name(self):
        skill, params = route("how many monarch butterflies are in Canada?")
        self.assertEqual(skill, "nature-count")
        self.assertEqual(params["country"], "CA")
        self.assertEqual(params["name"], "monarch butterflies")

    def test_season_question(self):
        skill, params = route("when are monarch butterflies recorded most?")
        self.assertEqual(skill, "nature-season")
        self.assertEqual(params["name"], "monarch butterflies")

    def test_recent_question(self):
        skill, params = route("what is the newest bald eagle record?")
        self.assertEqual(skill, "nature-recent")
        self.assertEqual(params["name"], "bald eagle")

    def test_taxon_question(self):
        skill, params = route("what species is Carcharodon carcharias?")
        self.assertEqual(skill, "nature-taxon")
        self.assertEqual(params["name"], "Carcharodon carcharias")

    def test_help(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")


class TurnTests(unittest.TestCase):
    def turn(self, text: str, fail: bool = False, permission: str = "allow-once"):
        agent_conn, client_conn = connected_pair()
        data, _ = live_data(fail=fail)
        agent = NatureAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_count_names_the_source_and_the_total(self):
        result, client = self.turn("how many records of Danaus plexippus are there?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("GBIF holds 817,344 occurrence records for Danaus plexippus", result["text"])
        self.assertIn("United States of America: 669,108", result["text"])
        self.assertIn(DATASET, result["text"])
        self.assertEqual(len(client.permission_requests), 1)

    def test_country_question_reports_the_share(self):
        result, _ = self.turn("how many monarch butterflies are in Canada?")
        self.assertIn("In Canada: 87,502 records (10.7% of the world total)", result["text"])

    def test_season_lists_the_busiest_months(self):
        result, _ = self.turn("when are monarch butterflies recorded most?")
        self.assertIn("August: 106,158", result["text"])
        self.assertIn("Busiest month: August", result["text"])

    def test_recent_lists_the_newest_records(self):
        result, _ = self.turn("what is the newest Danaus plexippus record?")
        self.assertIn("2026-09-21 - Wichita, United States of America by A. Observer", result["text"])

    def test_taxon_question_describes_the_identity(self):
        result, _ = self.turn("what species is Carcharodon carcharias?")
        self.assertIn("GBIF recognises 'Carcharodon carcharias'", result["text"])
        self.assertIn("rank SPECIES", result["text"])

    def test_common_name_resolves_and_the_answer_says_how(self):
        result, _ = self.turn("what species is monarch butterfly?")
        self.assertIn("Danaus plexippus", result["text"])

    def test_unknown_name_is_refused_not_guessed(self):
        result, client = self.turn("how many records of Atlantis weed are there?")
        self.assertIn("I could not pin down the name 'Atlantis weed'", result["text"])
        self.assertIn("No guess was made", result["text"])
        self.assertEqual(len(client.permission_requests), 1)

    def test_streaming_is_chunked_under_one_message_id(self):
        result, _ = self.turn("how many records of Danaus plexippus are there?")
        chunks = [update for update in result["updates"] if update.get("sessionUpdate") == "agent_message_chunk"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len({chunk["messageId"] for chunk in chunks}), 1)

    def test_help_does_not_ask_permission(self):
        result, client = self.turn("what can you do?")
        self.assertIn("GBIF", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _ = self.turn("how many records of Danaus plexippus?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_a_feed_failure_is_reported(self):
        result, _ = self.turn("how many records of Danaus plexippus?", fail=True)
        self.assertIn("could not read the nature feeds", result["text"])
        statuses = [update.get("status") for update in result["updates"]
                    if update.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", statuses)


if __name__ == "__main__":
    unittest.main()
