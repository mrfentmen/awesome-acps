"""What is in a packaged food, from Open Food Facts (keyless).

Verified live on 2026-09-23. Open Food Facts has two APIs that do different jobs, and this
reader uses each for what it can actually do:

  search  https://search.openfoodfacts.org/search?q=nutella&page_size=3
          -> hits carry the per-100 g nutriments and the Nutri-Score
  product https://world.openfoodfacts.org/api/v2/product/3017620422003.json
          -> one product's full label: allergens, ingredients, quantity

The legacy search (cgi/search.pl) answers **HTTP 503** and `api/v2/search?q=` ignores the query
and returns all 4.7 million products, so neither is used. Search alone cannot answer an allergen
question - its hits carry no allergen tags - so a question about allergens, ingredients or the
label goes search-then-fetch by the barcode the search returned.

Every nutrient here is **per 100 g**, not per serving: most products in the database carry no
serving size at all, so a per-serving number would be invented. And because the database is
crowd-sourced, a nutrient can be missing; a missing value is reported as not recorded rather
than as a zero, which would read as "this has no sugar".
"""

from __future__ import annotations

import json
import os
import re
import time
from urllib import parse, request

SEARCH_URL = "https://search.openfoodfacts.org/search"
PRODUCT_URL = "https://world.openfoodfacts.org/api/v2/product/"

DATASET = "Open Food Facts (search.openfoodfacts.org + world.openfoodfacts.org)"

DEFAULT_USER_AGENT = ("awesome-acps-food/1.0 (ACP agent; "
                      "+https://github.com/mrfentmen/awesome-acps)")

#: nutrient word -> (payload key, unit, plain label)
NUTRIENTS = {
    "sugar": ("sugars_100g", "g", "sugars"),
    "salt": ("salt_100g", "g", "salt"),
    "sodium": ("sodium_100g", "g", "sodium"),
    "fat": ("fat_100g", "g", "fat"),
    "saturated fat": ("saturated-fat_100g", "g", "saturated fat"),
    "carb": ("carbohydrates_100g", "g", "carbohydrates"),
    "protein": ("proteins_100g", "g", "proteins"),
    "fibre": ("fiber_100g", "g", "fiber"),
    "calorie": ("energy-kcal_100g", "kcal", "energy"),
}

#: Label rows in the order a label shows them, minus sodium (salt already says it).
LABELS = (("energy-kcal_100g", "energy", "kcal"), ("fat_100g", "fat", "g"),
          ("saturated-fat_100g", "saturated fat", "g"), ("carbohydrates_100g", "carbohydrates", "g"),
          ("sugars_100g", "sugars", "g"), ("fiber_100g", "fiber", "g"),
          ("proteins_100g", "proteins", "g"), ("salt_100g", "salt", "g"))

GRADES = {"a": "a (best)", "b": "b", "c": "c", "d": "d", "e": "e (worst)"}
NOVA = {1: "1 - unprocessed", 2: "2 - processed ingredient", 3: "3 - processed",
        4: "4 - ultra-processed"}

PRODUCT_FIELDS = ("code,product_name,brands,quantity,nutriments,ingredients_text,allergens_tags,"
                  "traces_tags,nutriscore_grade,nova_group")


class FoodError(RuntimeError):
    """Open Food Facts could not be read."""


def nutrient_from_text(text: str) -> tuple[str | None, str]:
    """('sugars_100g', 'sugar') when the question names a nutrient, else (None, '')."""
    lowered = str(text or "").lower()
    best_key, best_word = None, ""
    for word, (key, _unit, _label) in NUTRIENTS.items():
        if word in lowered and len(word) > len(best_word):
            # "saturated fat" contains "fat" - the longer, more specific word must win.
            best_key, best_word = key, word
    return best_key, best_word


def barcode_from_text(text: str) -> str | None:
    """An 8-14 digit barcode, or None."""
    tokens = "".join(ch if ch.isdigit() else " " for ch in str(text or "")).split()
    for token in tokens:
        if 8 <= len(token) <= 14:
            return token
    return None


def missing(value) -> bool:
    """Open Food Facts uses '', 'unknown' and -1 for 'not recorded'."""
    return value in (None, "", "unknown", "Unknown", "-", -1, "-1")


#: Question words that never belong in a food search.
QUESTION_WORDS = ("how much", "how many", "what is the", "what is", "what's", "whats",
                  "is there", "are there", "does it have", "do they have", "tell me about",
                  "tell me", "nutrition facts", "nutrition", "nutrients", "nutrient",
                  "ingredients list", "ingredients", "ingredient", "the label", "label",
                  "per 100g", "per 100 g", "per serving", "in a", "in an", "there",
                  "please", "for", "is", "are", "the", "of", "in", "a", "an", "it",
                  "its", "barcode", "product", "top", "first", "last", "show", "list",
                  "by", "with", "most", "least", "highest", "lowest", "free", "from")


def clean_name(text: str, extra: tuple[str, ...] = ()) -> str:
    """The food being asked about, with the question words taken off.

    Whole words only: a naive replace of "in" would turn "Vitamin Water" into "V tam Water".
    """
    words = list(QUESTION_WORDS) + list(NUTRIENTS) + list(extra)
    work = f" {str(text or '')} "
    for word in sorted(words, key=len, reverse=True):
        work = re.sub(rf"(?i)\b{re.escape(word)}\b", " ", work)
    tokens = work.split()
    #: A leading bare count ("top 5 cereals") is the count, not part of a name. Only leading and
    #: only digits, so "7up" and "Vitamin B12" keep their numbers.
    if tokens and tokens[0].isdigit() and len(tokens[0]) <= 2:
        tokens.pop(0)
    return " ".join(tokens).strip(" ?,.")


class FoodData:
    """Search foods by name, and open one up by barcode."""

    def __init__(self, fetch=None, cache_ttl: float = 1800.0, timeout: float = 30.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("FOOD_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("FOOD_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("FOOD_USER_AGENT") or DEFAULT_USER_AGENT
        self._fetch = fetch or self._http_text
        self._cache: dict[str, tuple[float, object]] = {}

    # -- transport ---------------------------------------------------------

    def _http_text(self, url: str, params: dict) -> str:
        query = f"{url}?{parse.urlencode(params, doseq=True)}" if params else url
        req = request.Request(query, headers={"User-Agent": self.user_agent, "Accept": "*/*"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # urllib raises many types; callers see one error type
            raise FoodError(f"request failed: {exc}") from exc

    def _json(self, url: str, params: dict | None = None, ttl: float | None = None) -> dict:
        query = f"{url}?{parse.urlencode(params, doseq=True)}" if params else url
        now = time.time()
        hit = self._cache.get(query)
        if hit and hit[0] > now:
            return hit[1]
        payload = self._fetch(url, params or {})
        if isinstance(payload, (str, bytes)):
            try:
                payload = json.loads(payload)
            except ValueError as exc:
                raise FoodError(
                    f"Open Food Facts returned something that is not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise FoodError("Open Food Facts returned an unexpected payload")
        self._cache[query] = (now + (ttl if ttl is not None else self.cache_ttl), payload)
        return payload

    # -- shaping -----------------------------------------------------------

    def _nutrients(self, nutriments: dict) -> list[dict]:
        return [{"key": key, "label": label, "unit": unit,
                 "value": None if missing(nutriments.get(key)) else nutriments.get(key)}
                for key, label, unit in LABELS]

    def _tags(self, values) -> list[str]:
        """OFF tags arrive as '\x1fen:<name>\x1f' - unit separators and a language prefix."""
        out = []
        for tag in values or []:
            text = str(tag).replace("\x1f", "").strip().split(":")[-1].strip()
            if text:
                out.append(text)
        return out

    def _row(self, payload: dict, full: bool = False) -> dict:
        """One product, from either API, in one shape."""
        grade = str(payload.get("nutriscore_grade") or "").lower()
        nova = payload.get("nova_group")
        return {
            "barcode": payload.get("code") or payload.get("_id"),
            "name": str(payload.get("product_name") or "(no name recorded)").strip(),
            "brand": str(payload.get("brands") or "").split(",")[0].strip() or None,
            "quantity": payload.get("quantity"),
            "grade": GRADES.get(grade) if grade in GRADES else None,
            "nova": NOVA.get(nova) if nova else None,
            "allergens": self._tags(payload.get("allergens_tags")),
            "traces": self._tags(payload.get("traces_tags")),
            "ingredients": str(payload.get("ingredients_text") or "").strip() or None,
            "nutrients": self._nutrients(payload.get("nutriments") or {}),
            "full": full,
            "url": f"https://world.openfoodfacts.org/product/{payload.get('code')}",
            "source": DATASET,
        }

    # -- reads -------------------------------------------------------------

    def product(self, barcode: str) -> dict:
        """One product's full label, by its barcode."""
        code = "".join(ch for ch in str(barcode or "") if ch.isdigit())
        if not (8 <= len(code) <= 14):
            raise ValueError("a product barcode is 8 to 14 digits")
        payload = self._json(PRODUCT_URL + f"{code}.json", {"fields": PRODUCT_FIELDS}, ttl=3600.0)
        if payload.get("status") != 1 or not payload.get("product"):
            raise FoodError(f"no product in Open Food Facts carries the barcode {code} "
                            "(the database is crowd-sourced, so gaps are common)")
        return self._row(payload["product"], full=True)

    def search(self, name: str, limit: int = 3) -> dict:
        """Products whose name matches, best match first."""
        text = str(name or "").strip()
        if not text:
            raise ValueError("tell me which food, for example Nutella")
        wanted = max(1, min(int(limit), 20))
        payload = self._json(SEARCH_URL,
                            {"q": text, "page_size": str(wanted)}, ttl=1800.0)
        rows = [self._row(hit) for hit in payload.get("hits") or []]
        if not rows:
            raise FoodError(f"nothing in Open Food Facts matches {text!r}")
        return {"query": text, "rows": rows[:wanted], "total": payload.get("count"),
                "source": DATASET}

    def label_for(self, name: str, limit: int = 3) -> dict:
        """Search, then open the best match, so allergens and ingredients are real.

        The search API's hits carry no allergen tags at all, so an allergen question has to
        take the second step; the other matches are returned alongside so the answer can still
        say what else came up.
        """
        found = self.search(name, limit=limit)
        top = found["rows"][0]
        if not top.get("barcode"):
            return found
        try:
            opened = self.product(top["barcode"])
        except (FoodError, ValueError):
            return found  # the search hit is still a fair answer, just without allergen tags
        found["rows"] = [opened] + found["rows"][1:]
        found["opened"] = True
        return found
