"""Chem - what a chemical is made of, and what a drug actually is.

Two questions, two keyless national databases. "what is the formula for ibuprofen" goes to
PubChem and comes back C13H18O2, 206.28 g/mol and the structure. "what is Lipitor" goes to
RxNorm, which knows every product name carries its ingredient, and so answers "atorvastatin"
without guessing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from data import (CHEM_WORDS, DATASET, DRUG_WORDS, ChemData, ChemError,  # noqa: E402
                  clean_name, word_from)

HELP = (
    "I read PubChem and RxNorm (both keyless, both from the US National Library of Medicine).\n"
    "Ask me:\n"
    "  - what is the formula for ibuprofen?\n"
    "  - molecular weight of caffeine\n"
    "  - what is aspirin made of?\n"
    "  - what is Lipitor (the generic name)?\n"
    "  - is Advil the same as ibuprofen?\n"
    "  - what is the formula for sodium bicarbonate?\n"
    "For a compound I give the formula, the molar mass and the structure, all from PubChem's\n"
    "own reference record. For a drug I give the ingredient names and the brand names RxNorm\n"
    "links to it - brand names are not chemistry, so they come from the drug database instead."
)

PERMISSION_KEY = "chem-read-pubchem-rxnorm"

SKILLS = ("chem-compound", "chem-drug", "help")

_LIMIT_RE = re.compile(r"\b(?:top|first|show|list)\s+(\d{1,2})\b|\b(\d{1,2})\s+"
                       r"(?:products?|brands?|names?)\b", re.IGNORECASE)
_FORMULA_RE = re.compile(r"\b(formula|molecular weight|molar mass|structure|smiles|mass|weighs?"
                         r"|atomic)\b", re.IGNORECASE)
_IDENTITY_RE = re.compile(r"\b(generic|brand|medicine|medication|drug|sold as|also called|"
                          r"other names|same as)\b", re.IGNORECASE)


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    stripped = str(text or "").strip()
    if not stripped or re.search(r"\b(help|what can you do|commands?)\b", stripped, re.IGNORECASE):
        return "help", {}
    chem_word = word_from(stripped, CHEM_WORDS)
    drug_word = word_from(stripped, DRUG_WORDS)
    target = clean_name(stripped)
    if not target:
        return "help", {}
    params: dict = {"name": target[:60]}
    limit = _LIMIT_RE.search(stripped)
    #: A formula question is chemistry even when a drug is named ("molecular weight of aspirin"),
    #: so the stronger signal wins before plain "what is X".
    if chem_word and (len(chem_word) >= len(drug_word) or _FORMULA_RE.search(stripped)):
        if limit:
            params["limit"] = int(limit.group(1) or limit.group(2))
        return "chem-compound", params
    if drug_word or _IDENTITY_RE.search(stripped):
        if limit:
            params["limit"] = int(limit.group(1) or limit.group(2))
        return "chem-drug", params
    #: With no signal at all, chemistry is the better first guess: "table salt", "citric acid"
    #: and even "Lipitor" (PubChem resolves it through its synonym list) all land in PubChem.
    #: If there is no such compound, the prompt goes to RxNorm next, so the guess costs one call.
    if limit:
        params["limit"] = int(limit.group(1) or limit.group(2))
    return "chem-compound", params


def render_compound(compound: dict) -> str:
    """PubChem's reference record for one compound."""
    lines = [f"{compound['title']}"]
    if compound["query"].lower() not in compound["title"].lower():
        #: PubChem searches its synonym list too, so "Lipitor" answers with "Atorvastatin
        #: Calcium" - true, and worth saying out loud rather than looking like a mix-up.
        lines.append(f"  (PubChem matched {compound['query']!r} to this record through its "
                     "synonym list)")
    lines.append(f"  PubChem CID: {compound['cid']}")
    lines.append(f"  formula: {compound['formula'] or 'not recorded'}")
    lines.append(f"  molar mass: {compound['mass']} g/mol" if compound["mass"]
                 else "  molar mass: not recorded")
    if compound["iupac"]:
        lines.append(f"  IUPAC name: {compound['iupac']}")
    if compound["smiles"]:
        lines.append(f"  structure (SMILES): {compound['smiles']}")
    lines.append(f"  {compound['url']}")
    return "\n".join(lines)


def render_drug(drug: dict, limit: int = 6) -> str:
    """RxNorm's view: what the thing is, under every name it has."""
    lines = [f"{drug['name']} - as RxNorm knows it:"]
    if drug.get("searched") and drug["searched"].lower() != drug["query"].lower():
        lines.append(f"  (RxNorm matches the whole name, so that question found nothing; this is "
                     f"the answer for {drug['searched']!r})")
    if drug["ingredients"]:
        lines.append(f"  active ingredient(s): {', '.join(drug['ingredients'])}")
    if drug["brands"]:
        shown_brands = drug["brands"][:8]
        lines.append(f"  brand name(s): {', '.join(shown_brands)}"
                     + (f" (and {len(drug['brands']) - len(shown_brands)} more)"
                        if len(drug["brands"]) > len(shown_brands) else ""))
    for product in drug["products"][:limit]:
        name = product["name"]
        lines.append(f"  product: {name if len(name) <= 120 else name[:117] + '...'}")
    if len(drug["products"]) > limit:
        lines.append(f"  ({len(drug['products']) - limit} more product(s) with the same name)")
    if not drug["products"]:
        lines.append("  (RxNorm lists names for this drug but no packaged products)")
    lines.append(f"  {drug['url']}")
    return "\n".join(lines)


class ChemAgent(AcpAgent):
    name = "chem"
    title = "Chem - compound formulas from PubChem, drug names from RxNorm"
    version = "1.0.0"

    def __init__(self, connection=None, data: ChemData | None = None) -> None:
        super().__init__(connection)
        self.data = data or ChemData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Chem session opened. " + HELP.splitlines()[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read the reference database", "medium"),
            ("Report the record, and say what is not in it", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("Compounds come from PubChem; drug and brand names come from RxNorm.")
            return STOP_END_TURN

        tool = f"call_{skill.replace('-', '_')}"
        ctx.tool_call(tool, "Read the reference database", kind="fetch", name=skill,
                      raw_input=dict(params))
        if not ctx.ask_permission(tool, "Allow Chem to read PubChem and RxNorm, the public "
                                        "chemistry databases?", remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read PubChem and RxNorm first.")
            return STOP_REFUSAL
        ctx.tool_call_update(tool, status="in_progress")

        try:
            if skill == "chem-compound":
                body, summary = self._compound_or_drug(params)
            else:
                body, summary = self._drug_or_compound(params)
        except (ChemError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the database: {exc}")
            return STOP_END_TURN

        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(summary))
        ctx.stream_text(body + "\n")
        ctx.message(f"\nSource: {DATASET}, read live. PubChem's formula and mass are for the "
                    "neutral compound, so a salt or a hydrate has its own, different record.")
        return STOP_END_TURN

    # -- the two lookups, each with the other as a fallback -----------------

    def _compound_or_drug(self, params: dict) -> tuple[str, str]:
        """PubChem first; if there is no such compound, the name may be a brand."""
        try:
            compound = self.data.compound(params["name"])
        except ChemError as exc:
            if "no compound called" not in str(exc):
                raise
            try:
                drug = self.data.drug(params["name"], limit=params.get("limit") or 6)
            except ChemError:
                raise exc
            body = (f"{exc}\n  But it is a drug name - RxNorm knows it:\n"
                    + render_drug(drug, limit=params.get("limit") or 6))
            return body, f"not a compound; RxNorm: {drug['name']}"
        return (render_compound(compound),
                f"{compound['title']} - {compound['formula']}")

    def _drug_or_compound(self, params: dict) -> tuple[str, str]:
        """RxNorm first; if it does not know the name, it may be a plain chemical."""
        try:
            drug = self.data.drug(params["name"], limit=params.get("limit") or 6)
        except ChemError as exc:
            if "does not know a drug" not in str(exc):
                raise
            try:
                compound = self.data.compound(params["name"])
            except ChemError:
                raise exc
            body = (f"{exc}\n  But it is a compound - PubChem knows it:\n"
                    + render_compound(compound))
            return body, f"not a drug; PubChem: {compound['title']}"
        summary = (f"{drug['name']}: "
                   + (", ".join(drug["ingredients"]) if drug["ingredients"]
                      else "no ingredient listed"))
        return render_drug(drug, limit=params.get("limit") or 6), summary

    def on_cancel(self, session) -> None:
        return None  # one read, one answer


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        ChemAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
