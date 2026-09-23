"""Food - what is in a packaged food, from Open Food Facts.

"how much sugar is in Nutella?" and "what is in barcode 3017620422003" are real questions with
real answers, keyless, for over three million products. Two honest details ride along: every
number is per 100 g (the database has no serving size for most products), and a nutrient that
was never recorded is said to be unrecorded rather than shown as a zero.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from data import (DATASET, NUTRIENTS, FoodData, FoodError, barcode_from_text,  # noqa: E402
                  clean_name, nutrient_from_text)

HELP = (
    "I read Open Food Facts (keyless) - over three million packaged foods. Ask me:\n"
    "  - how much sugar is in Nutella?\n"
    "  - what is in barcode 3017620422003?\n"
    "  - salt per 100g in Cheerios\n"
    "  - how many calories in a Snickers bar?\n"
    "  - is there milk in Oreos? (allergens)\n"
    "Everything I report is per 100 g, because most products in the database carry no serving\n"
    "size; and when a nutrient was never recorded I say so instead of calling it zero."
)

PERMISSION_KEY = "food-read-open-food-facts"

SKILLS = ("food-barcode", "food-find", "help")

_LIMIT_RE = re.compile(r"\b(?:top|first|show|list)\s+(\d{1,2})\b|\b(\d{1,2})\s+"
                       r"(?:products?|results?|matches?)\b", re.IGNORECASE)
#: Words that mean "open the product up" rather than "just give me the number". The search API's
#: hits carry no allergen tags, so these questions cost a second request by barcode.
DETAIL_WORDS = ("allergen", "allergens", "allergic", "ingredient", "ingredients", "label",
                "milk", "dairy", "nut", "nuts", "gluten", "soy", "soya", "egg", "eggs",
                "contains", "free from", "made of", "nutrition facts")

#: The ones that name an allergen a person can ask about directly.
ALLERGEN_NAMES = ("milk", "dairy", "nut", "nuts", "gluten", "soy", "soya", "egg", "eggs")

_DETAIL_WORDS = re.compile(r"\b(" + "|".join(sorted(DETAIL_WORDS, key=len, reverse=True))
                           + r")\b", re.IGNORECASE)
_FIND_WORDS = re.compile(r"\b(what is in|what's in|whats in|nutrition|nutrients|ingredients|"
                         r"label|how much|how many|calories|salt|sugar|fat|protein|carb|fibre|"
                         r"fiber|kcal|per 100g|per 100 g)\b", re.IGNORECASE)


def allergen_word(text: str) -> str:
    """The allergen the question is asked about ('milk' in 'is there milk in Oreos?'), or ''."""
    lowered = str(text or "").lower()
    for word in ALLERGEN_NAMES:
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return word
    return ""


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    stripped = str(text or "").strip()
    if not stripped or re.search(r"\b(help|what can you do|commands?)\b", stripped, re.IGNORECASE):
        return "help", {}
    barcode = barcode_from_text(stripped)
    if barcode:
        return "food-barcode", {"barcode": barcode}
    _, nutrient_word = nutrient_from_text(stripped)
    asking_about_allergen = allergen_word(stripped)
    target = clean_name(stripped, extra=(asking_about_allergen,) if asking_about_allergen else ())
    #: A name is worth searching when there is something left after the question words, or when
    #: the question names a nutrient ("how much sugar is in nutella?" leaves "nutella").
    if target and (_FIND_WORDS.search(stripped) or nutrient_word
                   or _DETAIL_WORDS.search(stripped)):
        params: dict = {"name": target[:60]}
        if nutrient_word:
            params["nutrient"] = nutrient_word
        limit = _LIMIT_RE.search(stripped)
        if limit:
            params["limit"] = int(limit.group(1) or limit.group(2))
        if asking_about_allergen or re.search(r"\b(allergens?|ingredients?|label)\b", stripped,
                                              re.IGNORECASE):
            params["detail"] = True
            if asking_about_allergen:
                params["allergen"] = asking_about_allergen
        return "food-find", params
    return "help", {}


def nutrient_of(product: dict, key: str) -> dict:
    """One nutrient row out of a product, or an empty row when nothing was asked about."""
    for row in product["nutrients"]:
        if row["key"] == key:
            return row
    return {"key": key, "label": "", "unit": "g", "value": None}


def shown(value, unit: str) -> str:
    """A recorded value as a person reads it: 56.756756756757 g -> 56.8 g, 540.5 kcal -> 541 kcal.

    The database carries full float precision for values that were only ever entered as a label
    figure, so printing the raw float would claim more precision than exists.
    """
    if unit == "kcal":
        return f"{round(float(value)):,}"
    return f"{round(float(value), 1):g}"


def render_product(product: dict, nutrient: dict | None = None,
                   allergen: str = "") -> str:
    """A product's label, per 100 g."""
    head = product["name"]
    bits = [product["brand"], product["quantity"]]
    extra = " - ".join(bit for bit in bits if bit)
    lines = [f"{head}{f' ({extra})' if extra else ''}"]
    if product["barcode"]:
        lines.append(f"  barcode: {product['barcode']}")
    if product["grade"]:
        lines.append(f"  Nutri-Score: {product['grade']}")
    if product["nova"]:
        lines.append(f"  processing: NOVA {product['nova']}")
    if nutrient.get("label"):
        if nutrient["value"] is None:
            lines.append(f"  {nutrient['label']}: not recorded in Open Food Facts")
        else:
            lines.append(f"  {nutrient['label']}: {shown(nutrient['value'], nutrient['unit'])} "
                         f"{nutrient['unit']} per 100 g")
    lines.append("  per 100 g:")
    for row in product["nutrients"]:
        if row["key"] == "sodium":  # salt is what labels carry; sodium repeats it
            continue
        if row["value"] is None:
            lines.append(f"    {row['label']}: not recorded")
        else:
            lines.append(f"    {row['label']}: {shown(row['value'], row['unit'])} {row['unit']}")
    if allergen:
        name = allergen.rstrip("s")
        listed = {tag.lower() for tag in product["allergens"]} | {
            part.strip() for tag in product["allergens"] for part in tag.split("-")}
        traces = {tag.lower() for tag in product["traces"]}
        if any(name in tag for tag in listed):
            lines.append(f"  {allergen}: yes, it is a declared allergen")
        elif any(name in tag for tag in traces):
            lines.append(f"  {allergen}: not an ingredient, but traces may be present")
        else:
            lines.append(f"  {allergen}: not among the allergens this product declares "
                         f"({', '.join(product['allergens']) or 'none declared'})")
    if product["allergens"]:
        lines.append(f"  allergens: {', '.join(product['allergens'])}")
    if product["traces"]:
        lines.append(f"  may contain traces of: {', '.join(product['traces'])}")
    if product["ingredients"]:
        lines.append(f"  ingredients: {product['ingredients'][:400]}")
    lines.append(f"  {product['url']}")
    return "\n".join(lines)


def render_find(found: dict, nutrient_key: str | None = None, allergen: str = "") -> str:
    """Matches, with the asked-about nutrient pulled up to the front."""
    if found.get("by") == "barcode" or found.get("opened"):
        product = found["rows"][0]
        body = render_product(product, nutrient_of(product, nutrient_key or ""), allergen or "")
        others = found["rows"][1:]
        if others:
            body += ("\n  other matches: "
                     + "; ".join(f"{row['name']} ({row['barcode']})" for row in others))
        return body
    total = found.get("total")
    lines = [f"{total:,} product(s) named {found['query']!r} in Open Food Facts:"
             if total else f"Products named {found['query']!r}:"]
    for product in found["rows"]:
        bits = [product["brand"], product["quantity"]]
        extra = " - ".join(bit for bit in bits if bit)
        lines.append(f"  {product['name']}{f' ({extra})' if extra else ''}")
        lines.append(f"    barcode {product['barcode']}"
                     + (f" - Nutri-Score {product['grade']}" if product["grade"] else ""))
        if nutrient_key:
            row = nutrient_of(product, nutrient_key)
            lines.append(f"    {row['label']}: "
                         + (f"{shown(row['value'], row['unit'])} {row['unit']} per 100 g"
                            if row["value"] is not None
                            else "not recorded in Open Food Facts"))
        if product["allergens"]:
            lines.append(f"    allergens: {', '.join(product['allergens'])}")
    if total and total > len(found["rows"]):
        lines.append(f"  (I showed the {len(found['rows'])} most-scanned of {total:,})")
    return "\n".join(lines)


class FoodAgent(AcpAgent):
    name = "food"
    title = "Food - what is in a packaged food, by name or barcode"
    version = "1.0.0"

    def __init__(self, connection=None, data: FoodData | None = None) -> None:
        super().__init__(connection)
        self.data = data or FoodData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Food session opened. " + HELP.splitlines()[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read Open Food Facts", "medium"),
            ("Report the label per 100 g, and say what was not recorded", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("Every number is per 100 g - most products here carry no serving size.")
            return STOP_END_TURN

        tool = f"call_{skill.replace('-', '_')}"
        ctx.tool_call(tool, "Read Open Food Facts", kind="fetch", name=skill,
                      raw_input=dict(params))
        if not ctx.ask_permission(tool, "Allow Food to read Open Food Facts' public product "
                                        "database?", remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read Open Food Facts first.")
            return STOP_REFUSAL
        ctx.tool_call_update(tool, status="in_progress")

        nutrient_word = params.get("nutrient")
        nutrient_key = NUTRIENTS[nutrient_word][0] if nutrient_word in NUTRIENTS else None
        try:
            if skill == "food-barcode":
                product = self.data.product(params["barcode"])
                found = {"query": params["barcode"], "rows": [product], "total": 1,
                         "by": "barcode"}
                body = render_find(found, nutrient_key, params.get("allergen", ""))
                summary = f"{product['name']} ({params['barcode']})"
            else:
                if params.get("detail"):
                    found = self.data.label_for(params["name"], limit=params.get("limit") or 3)
                else:
                    found = self.data.search(params["name"], limit=params.get("limit") or 3)
                body = render_find(found, nutrient_key, params.get("allergen", ""))
                summary = f"{len(found['rows'])} match(es) for {params['name']}"
        except (FoodError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read Open Food Facts: {exc}")
            return STOP_END_TURN

        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(summary))
        ctx.stream_text(body + "\n")
        ctx.message(f"\nSource: {DATASET}, read live. These values are per 100 g and the "
                    "database is crowd-sourced, so an unrecorded nutrient means nobody entered "
                    "it - not that it is zero, and a recorded one can be wrong. The barcode "
                    "above is the entry the numbers came from, so they can be checked.")
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        return None  # one read, one answer


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        FoodAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
