"""Labels — FDA drug labels inside your editor.

No ACP agent could read a drug label before this one. It reads openFDA's Structured
Product Labeling index (keyless) and answers with the manufacturer's own filed text:
indications, dosage, warnings, contraindications, adverse reactions and interactions.

It is not medical advice and says so. The label is what the maker filed with the FDA, and
the answer carries the effective date so you can see how current it is.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from data import DATASET, SECTIONS, LabelsData, LabelsError  # noqa: E402

HELP = (
    "I read openFDA's public drug-label index (keyless). Ask me:\n"
    "  • what is lipitor?\n"
    "  • warnings for metformin\n"
    "  • dosage for amoxicillin\n"
    "I quote the manufacturer's filed label text and say which section it came from. "
    "I am not medical advice."
)

PERMISSION_KEY = "labels-read-public-openfda"

SKILLS = ("label-info", "label-search", "help")

_SEARCH_RE = re.compile(r"\b(which drugs?|list|search|brands?|generic names?|what drugs?)\b",
                        re.IGNORECASE)
_FOCUS_RE = re.compile(
    r"\b(warnings?|dosage|dose|indications?|use|uses|contraindications?|adverse|side effects?|"
    r"interactions?)\b", re.IGNORECASE)

#: Question words -> the label section the answer should lead with.
FOCUS_SECTIONS = {
    "warning": "warnings", "warnings": "warnings",
    "dosage": "dosage_and_administration", "dose": "dosage_and_administration",
    "indication": "indications_and_usage", "indications": "indications_and_usage",
    "use": "indications_and_usage", "uses": "indications_and_usage",
    "contraindication": "contraindications", "contraindications": "contraindications",
    "adverse": "adverse_reactions", "reaction": "adverse_reactions",
    "interaction": "drug_interactions", "interactions": "drug_interactions",
}

_DRUG_AFTER_RE = re.compile(r"\b(?:for|about|of|is|are)\s+([A-Za-z][A-Za-z0-9\- ']{1,50})", re.IGNORECASE)
_STOP_TAIL_WORDS = frozenset({
    "right", "now", "today", "please", "me", "the", "a", "an", "drug", "medicine", "medication",
    "label", "labels", "warnings", "warning", "dosage", "dose", "uses", "use", "side", "effects",
    "interactions", "contraindications", "adverse", "reactions", "indications",
})


def drug_from_text(text: str) -> str | None:
    """The drug name in a question, or None. Keeps the original case."""
    match = _DRUG_AFTER_RE.search(text)
    if not match:
        return None
    words = match.group(1).strip(" .,?!'\"()").split()
    while words and words[-1].lower() in _STOP_TAIL_WORDS:
        words.pop()
    name = " ".join(words)
    name = re.sub(r"^(?:called|named|branded)\s+", "", name, flags=re.IGNORECASE)
    if len(name) < 2 or not re.search(r"[A-Za-z]", name):
        return None
    return name


def focus_from_text(text: str) -> str | None:
    """The label section a question is about, as its openFDA field name, or None."""
    match = _FOCUS_RE.search(text)
    if not match:
        return None
    word = match.group(1).lower()
    if word in FOCUS_SECTIONS:
        return FOCUS_SECTIONS[word]
    return FOCUS_SECTIONS.get(word.rstrip("s"))


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    params: dict = {}
    drug = drug_from_text(text)
    if drug:
        params["drug"] = drug
    focus = focus_from_text(text)
    if focus:
        params["focus"] = focus

    if _SEARCH_RE.search(text) and "drug" in params:
        return "label-search", params
    return "label-info", params


class LabelsAgent(AcpAgent):
    name = "labels"
    title = "Labels — openFDA drug labels"
    version = "1.0.0"

    def __init__(self, connection=None, data: LabelsData | None = None) -> None:
        super().__init__(connection)
        self.data = data or LabelsData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Labels session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Search the openFDA label index for the drug", "medium"),
            ("Quote the manufacturer's filed sections", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("I read openFDA's drug label index, which carries the Structured "
                        "Product Labeling text manufacturers file with the FDA. It is a "
                        "reference document, not advice for your situation - talk to a "
                        "pharmacist or doctor about that.")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read openFDA labels for {skill}", kind="fetch", name=skill,
                      raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Labels to read openFDA's public drug label data?",
                                 remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the public openFDA feed before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (LabelsError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the openFDA feed: {exc}")
            return STOP_END_TURN
        ctx.check_cancelled()
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(artifact["summary"]))
        ctx.stream_text(answer)
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        # Every skill is a single HTTP read; nothing long-running to interrupt.
        return None

    # -- skills ------------------------------------------------------------

    def _run_skill(self, skill: str, params: dict) -> tuple[str, dict]:
        if skill == "label-search":
            return self._search(params)
        if skill == "label-info":
            return self._info(params)
        return HELP, {"summary": "help", "dataset": None}

    @staticmethod
    def _title(openfda: dict, fallback: str) -> str:
        brand, generic = openfda.get("brand_name"), openfda.get("generic_name")
        if brand and generic:
            return f"{brand} ({generic})"
        return brand or generic or fallback

    def _search(self, params: dict) -> tuple[str, dict]:
        drug = params.get("drug")
        if not drug:
            return (
                "Which drug should I search for? Give me a brand or generic name, like 'lipitor' "
                "or 'metformin'.",
                {"summary": "no drug given", "dataset": DATASET, "known": False},
            )
        result = self.data.search(drug, limit=3)
        artifact = {
            "summary": f"{DATASET}: {len(result['rows'])} labels matching {result['query']}",
            "dataset": DATASET,
            "query": result["query"],
            "rows": result["rows"],
        }
        lines = [f"openFDA labels matching {result['query']!r}:"]
        if not result["rows"]:
            lines.append("  • no label matched that name.")
        for row in result["rows"]:
            openfda = row["openfda"]
            lines.append(f"  • {self._title(openfda, result['query'])} - "
                         f"{openfda.get('manufacturer') or 'manufacturer not listed'} "
                         f"({len(row['has_sections'])} sections on file)")
        lines.append(
            f"\nSource: {DATASET} (read live). The index covers labels filed with the FDA; "
            "ask about one by name for its sections."
        )
        return "\n".join(lines), artifact

    def _info(self, params: dict) -> tuple[str, dict]:
        drug = params.get("drug")
        if not drug:
            return (
                "Which drug should I look up? Give me a brand or generic name, like 'lipitor' "
                "or 'metformin'.",
                {"summary": "no drug given", "dataset": DATASET, "known": False},
            )
        result = self.data.label(drug)
        if not result["found"]:
            return (
                f"openFDA has no label matching {drug!r}. Check the spelling, or try the generic "
                "name (for example 'atorvastatin' instead of 'lipitor').",
                {"summary": f"no label for {drug}", "dataset": DATASET, "found": False},
            )
        openfda = result["openfda"]
        title = self._title(openfda, drug)
        sections = result["sections"]
        focus = params.get("focus")
        titles = {key: display for key, display in SECTIONS}
        order = [key for key, _ in SECTIONS if key in sections]
        if focus and focus in order:
            order.remove(focus)
            order.insert(0, focus)
        artifact = {
            "summary": f"{DATASET}: {title} - {len(sections)} sections"
                       + (f", leading with {focus}" if focus else ""),
            "dataset": DATASET,
            "drug": title,
            "query": drug,
            "effective_time": result["effective_time"],
            "sections_shown": order,
            "focus": focus,
        }
        lines = [f"{title}" + (f" - {openfda.get('manufacturer')}" if openfda.get("manufacturer") else "")]
        if result["effective_time"]:
            stamp = result["effective_time"]
            readable = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}" if len(str(stamp)) >= 8 else str(stamp)
            lines.append(f"Label effective date: {readable}")
        lines.append("")
        for key in order:
            lines.append(f"{titles[key]}:")
            lines.append(self.data.trim(sections[key]))
            lines.append("")
        missing = [display for key, display in SECTIONS if key not in sections]
        if missing:
            lines.append("Not on this label: " + ", ".join(missing) + ".")
        lines.append(
            f"\nSource: {DATASET} (read live), the manufacturer's filed label text, long "
            "sections cut with a note. This is not medical advice and not a substitute for "
            "the pharmacist's leaflet in the box."
        )
        return "\n".join(lines), artifact


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        LabelsAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
