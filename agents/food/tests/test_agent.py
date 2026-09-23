"""Tests for the food agent: search, opening a label, allergens, routing and turns.

    python3 agents/food/tests/test_agent.py

No network: the payloads are injected, shaped like the live ones read on 2026-09-23
(search hit 'Nutella' code 0098008952506 with sugars_100g 56.756756756757; product
3017620422003 with sugars 56.3, Nutri-Score e, allergens milk/nuts/soybeans).
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

from agent import allergen_word, route, shown  # noqa: E402
from data import (FoodData, FoodError, barcode_from_text, clean_name,  # noqa: E402
                  nutrient_from_text)

SEARCH = {
    "hits": [
        {"code": "0009800800049", "product_name": "Nutella & go! hazelnut spread",
         "brands": ["Nutella"], "nutriscore_grade": "e", "unique_scans_n": 9,
         "nutriments": {"sugars_100g": 44.23076923077, "energy-kcal_100g": 500.0}},
        {"code": "0098008952506", "product_name": "Nutella", "brands": ["Nutella"],
         "nutriscore_grade": "unknown",
         "nutriments": {"sugars_100g": 56.756756756757, "salt_100g": 0.10135135135135}},
    ],
    "count": 631,
}

PRODUCT = {
    "status": 1,
    "product": {"code": "3017620422003", "product_name": "Nutella", "brands": "Nutella, Ferrero",
                "quantity": "400 g e", "nutriscore_grade": "e", "nova_group": 4,
                "allergens_tags": ["en:milk", "en:nuts", "en:soybeans"],
                "traces_tags": ["\x1fen:\x1fen:milk\x1f"],
                "ingredients_text": "Sugar, palm oil, HAZELNUTS 13%, cocoa.",
                "nutriments": {"energy-kcal_100g": 539, "fat_100g": 30.9,
                               "saturated-fat_100g": 10.6, "carbohydrates_100g": 57.5,
                               "sugars_100g": 56.3, "fiber_100g": 0, "proteins_100g": 6.3,
                               "salt_100g": 0.107, "sodium_100g": 0.0428}},
}

MISSING_PRODUCT = {"status": 0, "status_verbose": "product not found"}


class FakeFeeds:
    def __init__(self, search=None, product=None, fail=None):
        self.calls: list[str] = []
        self.queries: list[str] = []
        self.search = SEARCH if search is None else search
        self.product = PRODUCT if product is None else product
        self.fail = fail

    def __call__(self, url: str, params: dict):
        self.calls.append(url)
        if isinstance(params, dict) and params.get("q"):
            self.queries.append(params["q"])
        if self.fail and self.fail in url:
            raise FoodError("Open Food Facts is unreachable")
        if "/api/v2/product/" in url:
            return json.dumps(self.product)
        if "search.openfoodfacts.org" in url:
            return json.dumps(self.search)
        raise AssertionError(f"unexpected url {url}")


def make_data(**kw):
    feed = FakeFeeds(**kw)
    return FoodData(fetch=feed), feed


class HelperTests(unittest.TestCase):
    def test_a_nutrient_word_is_recognised_and_the_longer_one_wins(self):
        self.assertEqual(nutrient_from_text("how much sugar is in Nutella?"),
                         ("sugars_100g", "sugar"))
        self.assertEqual(nutrient_from_text("saturated fat per 100g in butter"),
                         ("saturated-fat_100g", "saturated fat"))
        self.assertEqual(nutrient_from_text("calories in a Snickers"),
                         ("energy-kcal_100g", "calorie"))
        self.assertEqual(nutrient_from_text("what is in barcode 3017620422003"),
                         (None, ""))

    def test_a_barcode_is_8_to_14_digits(self):
        self.assertEqual(barcode_from_text("what is in barcode 3017620422003?"),
                         "3017620422003")
        self.assertIsNone(barcode_from_text("how much sugar in Nutella?"))
        self.assertIsNone(barcode_from_text("the year 1969 was busy"))

    def test_names_lose_the_question_words_but_keep_their_own(self):
        self.assertEqual(clean_name("how much sugar is in Nutella?"), "Nutella")
        self.assertEqual(clean_name("is there milk in Oreos?", extra=("milk",)), "Oreos")
        # "in" inside a word is not a filler word: Vitamin Water must survive intact.
        self.assertEqual(clean_name("what is in Vitamin Water?"), "Vitamin Water")
        self.assertEqual(clean_name("salt per 100g in Cheerios"), "Cheerios")

    def test_the_allergen_being_asked_about(self):
        self.assertEqual(allergen_word("is there milk in Oreos?"), "milk")
        self.assertEqual(allergen_word("does it have nuts"), "nuts")
        self.assertEqual(allergen_word("is it gluten free"), "gluten")
        self.assertEqual(allergen_word("how much sugar in Coke"), "")

    def test_values_read_like_a_label(self):
        self.assertEqual(shown(56.756756756757, "g"), "56.8")
        self.assertEqual(shown(540.54054054054, "kcal"), "541")
        self.assertEqual(shown(0, "g"), "0")


class ReadTests(unittest.TestCase):
    def test_a_search_keeps_the_total_and_the_full_precision_value(self):
        data, feed = make_data()
        found = data.search("Nutella", limit=2)
        self.assertEqual(found["total"], 631)
        self.assertEqual(found["rows"][0]["barcode"], "0009800800049")
        self.assertEqual(feed.queries, ["Nutella"])
        sugars = [row for row in found["rows"][1]["nutrients"] if row["key"] == "sugars_100g"][0]
        self.assertEqual(sugars["value"], 56.756756756757)  # full precision kept in the data
        self.assertEqual(found["rows"][0]["grade"], "e (worst)")
        self.assertIsNone(found["rows"][1]["grade"])  # "unknown" is not a grade

    def test_a_product_by_barcode_keeps_the_whole_label(self):
        data, feed = make_data()
        product = data.product("3017620422003")
        self.assertEqual(product["name"], "Nutella")
        self.assertEqual(product["brand"], "Nutella")  # the list collapses to the first
        self.assertEqual(product["quantity"], "400 g e")
        self.assertEqual(product["grade"], "e (worst)")
        self.assertEqual(product["nova"], "4 - ultra-processed")
        self.assertEqual(product["allergens"], ["milk", "nuts", "soybeans"])
        self.assertEqual(product["traces"], ["milk"])  # unit separators and the en: prefix gone
        self.assertTrue(product["full"])
        self.assertIn("/api/v2/product/3017620422003.json", feed.calls[-1])

    def test_a_barcode_that_is_not_in_the_database(self):
        data, _feed = make_data(product=MISSING_PRODUCT)
        with self.assertRaises(FoodError):
            data.product("0000000000000")
        data, _feed = make_data()
        with self.assertRaises(ValueError):
            data.product("123")  # too short to be a barcode

    def test_a_label_question_takes_the_second_step_by_barcode(self):
        data, feed = make_data()
        found = data.label_for("Nutella", limit=2)
        self.assertTrue(found["opened"])
        self.assertEqual(found["rows"][0]["barcode"], "3017620422003")
        self.assertEqual(found["rows"][0]["allergens"], ["milk", "nuts", "soybeans"])
        #: The opened product takes the top slot, and the hits below it stay on as other matches.
        self.assertEqual(found["rows"][1]["barcode"], "0098008952506")
        #: The fetch follows the *best* match's barcode, not the last hit's.
        self.assertIn("/api/v2/product/0009800800049.json", feed.calls[-1])

    def test_a_label_question_still_answers_when_the_product_has_gone(self):
        data, _feed = make_data(product=MISSING_PRODUCT)
        found = data.label_for("Nutella", limit=2)
        self.assertFalse(found.get("opened"))
        self.assertEqual(found["rows"][0]["barcode"], "0009800800049")

    def test_a_search_with_nothing_matching_is_an_error(self):
        data, _feed = make_data(search={"hits": [], "count": 0})
        with self.assertRaises(FoodError):
            data.search("zzzzz")
        data, _feed = make_data()
        with self.assertRaises(ValueError):
            data.search("   ")

    def test_a_dead_service(self):
        data, _feed = make_data(fail="search.openfoodfacts.org")
        with self.assertRaises(FoodError):
            data.search("Nutella")
        data, _feed = make_data(fail="/api/v2/product/")
        with self.assertRaises(FoodError):
            data.product("3017620422003")


class RouteTests(unittest.TestCase):
    def test_a_nutrient_question_keeps_only_the_food_name(self):
        skill, params = route("how much sugar is in Nutella?")
        self.assertEqual(skill, "food-find")
        self.assertEqual(params, {"name": "Nutella", "nutrient": "sugar"})

    def test_a_barcode_question(self):
        skill, params = route("what is in barcode 3017620422003?")
        self.assertEqual(skill, "food-barcode")
        self.assertEqual(params, {"barcode": "3017620422003"})

    def test_an_allergen_question_asks_for_the_whole_label(self):
        skill, params = route("is there milk in Oreos?")
        self.assertEqual(skill, "food-find")
        self.assertEqual(params["name"], "Oreos")
        self.assertTrue(params["detail"])
        self.assertEqual(params["allergen"], "milk")

    def test_a_count_and_a_nutrient(self):
        skill, params = route("top 5 cereals by sugar")
        self.assertEqual(skill, "food-find")
        self.assertEqual(params["name"], "cereals")
        self.assertEqual(params["limit"], 5)
        self.assertEqual(params["nutrient"], "sugar")

    def test_help_and_nonsense(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")
        self.assertEqual(route("hello world")[0], "help")


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
        from agent import FoodAgent

        agent_conn, client_conn = connected_pair()
        data, feed = make_data(**kw)
        agent = FoodAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client, feed
        finally:
            client.stop()

    def test_a_nutrient_answer_is_rounded_and_per_100g(self):
        result, client, _ = self.turn("how much sugar is in Nutella?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertEqual(len(client.permission_requests), 1)
        self.assertIn("631 product(s) named 'Nutella'", result["text"])
        self.assertIn("sugars: 44.2 g per 100 g", result["text"])
        self.assertIn("sugars: 56.8 g per 100 g", result["text"])
        self.assertIn("per 100 g", result["text"])

    def test_a_barcode_answer_shows_the_whole_label(self):
        result, _, _ = self.turn("what is in barcode 3017620422003?")
        self.assertIn("Nutella (Nutella - 400 g e)", result["text"])
        self.assertIn("Nutri-Score: e (worst)", result["text"])
        self.assertIn("processing: NOVA 4 - ultra-processed", result["text"])
        self.assertIn("salt: 0.1 g", result["text"])
        self.assertIn("energy: 539 kcal", result["text"])
        self.assertIn("allergens: milk, nuts, soybeans", result["text"])
        self.assertIn("may contain traces of: milk", result["text"])
        self.assertIn("crowd-sourced", result["text"])

    def test_an_allergen_answer_opens_the_label_and_answers_precisely(self):
        #: The search API's hits carry no allergen tags, so the allergen answer has to come from
        #: the product fetched by barcode - here an Oreo whose milk is a trace, not an ingredient.
        oreo = {"status": 1, "product": {
            "code": "0009800800049", "product_name": "Oreo", "brands": "Oreo",
            "allergens_tags": ["en:gluten", "en:soybeans"],
            "traces_tags": ["\x1fen:\x1fen:milk\x1f"],
            "nutriments": {"sugars_100g": 38.0, "energy-kcal_100g": 480}}}
        result, _, _ = self.turn("is there milk in Oreos?", product=oreo)
        self.assertIn("milk: not an ingredient, but traces may be present", result["text"])
        self.assertIn("allergens: gluten, soybeans", result["text"])
        self.assertIn("other matches: Nutella (0098008952506)", result["text"])

    def test_an_allergen_answer_says_yes_when_it_is_declared(self):
        result, _, _ = self.turn("is there milk in Nutella?")
        self.assertIn("milk: yes, it is a declared allergen", result["text"])

    def test_help_asks_nothing(self):
        result, client, feed = self.turn("what can you do?")
        self.assertIn("Open Food Facts", result["text"])
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(feed.calls, [])

    def test_permission_denied(self):
        result, _, _ = self.turn("how much sugar is in Nutella?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)

    def test_a_dead_service(self):
        result, _, _ = self.turn("how much sugar is in Nutella?",
                                 fail="search.openfoodfacts.org")
        self.assertIn("I could not read Open Food Facts", result["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
