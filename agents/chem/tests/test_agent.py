"""Tests for the chem agent: PubChem compounds, RxNorm drugs, fallbacks and turns.

    python3 agents/chem/tests/test_agent.py

No network: the payloads are injected, shaped like the live ones read on 2026-09-23
(ibuprofen CID 3672 C13H18O2 206.28; RxNorm 'lipitor' -> atorvastatin 80 MG Oral Tablet [Lipitor]).
"""

from __future__ import annotations

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

from agent import route  # noqa: E402
from data import (ChemData, ChemError, ChemNotFound, clean_name,  # noqa: E402
                  ingredient_of, word_from)

COMPOUND = {"PropertyTable": {"Properties": [
    {"CID": 3672, "MolecularFormula": "C13H18O2", "MolecularWeight": "206.28",
     "ConnectivitySMILES": "CC(C)CC1=CC=C(C=C1)C(C)C(=O)O",
     "IUPACName": "2-[4-(2-methylpropyl)phenyl]propanoic acid",
     "Title": "Ibuprofen, (+-)-"}]}}

#: PubChem resolves brand names through its synonym list: 'table salt' -> sodium chloride.
SALT = {"PropertyTable": {"Properties": [
    {"CID": 5234, "MolecularFormula": "ClNa", "MolecularWeight": "58.44",
     "ConnectivitySMILES": "[Na+].[Cl-]", "IUPACName": "sodium chloride",
     "Title": "Sodium Chloride"}]}}

DRUG = {"drugGroup": {"name": "lipitor", "conceptGroup": [
    {"tty": "BPCK", "conceptProperties": []},
    {"tty": "SBD", "conceptProperties": [
        {"rxcui": "262095", "name": "atorvastatin 80 MG Oral Tablet [Lipitor]",
         "synonym": "Lipitor 80 MG Oral Tablet"},
        {"rxcui": "617314", "name": "atorvastatin 10 MG Oral Tablet [Lipitor]",
         "synonym": "Lipitor 10 MG Oral Tablet"}]}]}}

EMPTY_DRUG = {"drugGroup": {"name": "caffeine", "conceptGroup": []}}


class FakeNotFound(ChemNotFound):
    pass


class FakeFeeds:
    """Routes by URL. A compound miss is PubChem's real 404; a drug payload can be a dict
    keyed by the searched name, which is how RxNorm's whole-string matching behaves."""

    def __init__(self, compound=None, drug=None, missing=False, fail=None):
        self.calls: list[str] = []
        self.compound = COMPOUND if compound is None else compound
        self.drug = DRUG if drug is None else drug
        self.missing = missing
        self.fail = fail

    def __call__(self, url: str, params):
        self.calls.append(url)
        if self.fail and self.fail in url:
            raise ChemError("the service is unreachable")
        if "pubchem" in url:
            if self.missing:
                raise FakeNotFound("HTTP Error 404: Not Found")
            return json.dumps(self.compound)
        if "rxnav" in url:
            payload = self.drug
            if isinstance(payload, dict) and "drugGroup" not in payload:
                payload = payload.get(params.get("name"), EMPTY_DRUG)
            payload = json.loads(json.dumps(payload))
            #: RxNorm echoes back the term it matched, so a fake that does not looks broken.
            payload.setdefault("drugGroup", {})["name"] = params.get("name")
            return json.dumps(payload)
        raise AssertionError(f"unexpected url {url}")


def make_data(**kw):
    feed = FakeFeeds(**kw)
    return ChemData(fetch=feed), feed


class HelperTests(unittest.TestCase):
    def test_the_longest_matching_word_wins(self):
        self.assertEqual(word_from("what is the molecular weight of caffeine", ("mass", "weight",
                                                                              "molecular weight")),
                         "molecular weight")
        self.assertEqual(word_from("what is aspirin", ("generic", "brand")), "")

    def test_a_product_name_carries_its_ingredient(self):
        self.assertEqual(ingredient_of("atorvastatin 80 MG Oral Tablet [Lipitor]"), "atorvastatin")
        self.assertEqual(ingredient_of("ibuprofen 20 MG/ML Oral Suspension [Advil]"), "ibuprofen")
        self.assertIsNone(ingredient_of("[Lipitor]"))

    def test_names_lose_the_question_words(self):
        self.assertEqual(clean_name("what is the formula for ibuprofen?"), "ibuprofen")
        self.assertEqual(clean_name("molecular weight of caffeine"), "caffeine")
        self.assertEqual(clean_name("is Advil the same as ibuprofen?"), "Advil ibuprofen")


class ReadTests(unittest.TestCase):
    def test_a_compound_keeps_the_structure_field_pubchem_really_uses(self):
        data, feed = make_data()
        compound = data.compound("ibuprofen")
        self.assertEqual(compound["cid"], 3672)
        self.assertEqual(compound["formula"], "C13H18O2")
        self.assertEqual(compound["mass"], "206.28")
        self.assertEqual(compound["smiles"], "CC(C)CC1=CC=C(C=C1)C(C)C(=O)O")
        self.assertEqual(compound["title"], "Ibuprofen, (+-)-")
        self.assertIn("/compound/name/ibuprofen/property/", feed.calls[-1])
        self.assertIn("ConnectivitySMILES", feed.calls[-1])

    def test_a_compound_name_is_url_escaped(self):
        data, feed = make_data()
        data.compound("sodium bicarbonate")
        self.assertIn("sodium%20bicarbonate", feed.calls[-1])
        self.assertNotIn("sodium bicarbonate", feed.calls[-1])

    def test_a_compound_that_does_not_exist_says_so_plainly(self):
        data, _feed = make_data(missing=True)
        with self.assertRaises(ChemError) as caught:
            data.compound("zzzznotachemical")
        self.assertIn("PubChem has no compound called 'zzzznotachemical'", str(caught.exception))

    def test_a_drug_keeps_its_ingredients_brands_and_products(self):
        data, _feed = make_data()
        drug = data.drug("lipitor")
        self.assertEqual(drug["ingredients"], ["atorvastatin"])
        self.assertEqual(drug["brands"], ["Lipitor"])
        self.assertEqual(len(drug["products"]), 2)
        self.assertEqual(drug["products"][0]["rxcui"], "262095")
        self.assertEqual(drug["searched"], "lipitor")

    def test_rxnorm_matches_the_whole_string_so_a_longer_question_falls_back(self):
        data, feed = make_data(drug={"Advil ibuprofen": EMPTY_DRUG, "Advil": DRUG})
        drug = data.drug("Advil ibuprofen")
        #: The first call is the whole phrase; when it finds nothing the first name is tried.
        names = [call for call in feed.calls if "rxnav" in call]
        self.assertEqual(len(names), 2)
        self.assertEqual(drug["searched"], "Advil")
        self.assertEqual(drug["ingredients"], ["atorvastatin"])

    def test_a_drug_rxnorm_does_not_know(self):
        data, _feed = make_data(drug=EMPTY_DRUG)
        with self.assertRaises(ChemError):
            data.drug("zzzznotadrug")
        data, _feed = make_data()
        with self.assertRaises(ValueError):
            data.drug("   ")
        with self.assertRaises(ValueError):
            data.compound("")

    def test_a_dead_service(self):
        data, _feed = make_data(fail="pubchem")
        with self.assertRaises(ChemError):
            data.compound("ibuprofen")


class RouteTests(unittest.TestCase):
    def test_a_formula_question(self):
        self.assertEqual(route("what is the formula for ibuprofen?"),
                         ("chem-compound", {"name": "ibuprofen"}))
        self.assertEqual(route("molecular weight of caffeine"),
                         ("chem-compound", {"name": "caffeine"}))

    def test_made_of_is_chemistry_not_a_brand(self):
        self.assertEqual(route("what is aspirin made of?"),
                         ("chem-compound", {"name": "aspirin"}))

    def test_a_drug_identity_question(self):
        skill, params = route("is Advil the same as ibuprofen?")
        self.assertEqual(skill, "chem-drug")
        self.assertEqual(params["name"], "Advil ibuprofen")
        self.assertEqual(route("what is lipitor used for?"),
                         ("chem-drug", {"name": "lipitor"}))

    def test_a_bare_name_is_chemistry_first(self):
        #: PubChem resolves brand names through synonyms, so this guess usually lands.
        self.assertEqual(route("what is Lipitor?"), ("chem-compound", {"name": "Lipitor"}))

    def test_help_and_nonsense(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")
        self.assertEqual(route("??")[0], "help")


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
        from agent import ChemAgent

        agent_conn, client_conn = connected_pair()
        data, feed = make_data(**kw)
        agent = ChemAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client, feed
        finally:
            client.stop()

    def test_a_compound_answer(self):
        result, client, _ = self.turn("what is the formula for ibuprofen?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertEqual(len(client.permission_requests), 1)
        self.assertIn("Ibuprofen, (+-)-", result["text"])
        self.assertIn("formula: C13H18O2", result["text"])
        self.assertIn("molar mass: 206.28 g/mol", result["text"])
        self.assertIn("structure (SMILES): CC(C)CC", result["text"])

    def test_a_synonym_match_is_admitted(self):
        result, _, _ = self.turn("what is table salt?", compound=SALT)
        self.assertIn("Sodium Chloride", result["text"])
        self.assertIn("matched 'table salt' to this record through its synonym list",
                      result["text"])

    def test_a_drug_answer(self):
        result, _, _ = self.turn("is Advil the same as ibuprofen?")
        self.assertIn("active ingredient(s): atorvastatin", result["text"])
        self.assertIn("brand name(s): Lipitor", result["text"])
        self.assertIn("product: atorvastatin 80 MG Oral Tablet [Lipitor]", result["text"])

    def test_a_shorter_retry_is_admitted(self):
        result, _, _ = self.turn(
            "is Advil the same as ibuprofen?",
            drug={"Advil ibuprofen": EMPTY_DRUG, "Advil": DRUG})
        self.assertIn("this is the answer for 'Advil'", result["text"])

    def test_a_brand_name_that_is_really_a_compound_falls_through(self):
        #: PubChem has no such compound, so the same name is tried in RxNorm.
        result, _, _ = self.turn("what is zzzznotathing?", missing=True)
        self.assertIn("PubChem has no compound called 'zzzznotathing'", result["text"])
        self.assertIn("active ingredient(s): atorvastatin", result["text"])

    def test_help_asks_nothing(self):
        result, client, feed = self.turn("what can you do?")
        self.assertIn("PubChem", result["text"])
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(feed.calls, [])

    def test_permission_denied(self):
        result, _, _ = self.turn("what is the formula for ibuprofen?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)

    def test_a_dead_service(self):
        result, _, _ = self.turn("what is the formula for ibuprofen?", fail="pubchem")
        self.assertIn("I could not read the database", result["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
