"""Nature — the GBIF occurrence index inside your editor.

Every ACP agent on the vendors' list writes code. This one answers biodiversity questions
against GBIF: how many published records exist for a taxon, which countries hold them, in
which months they were recorded, and what the newest record is. Keyless, worldwide, and a
different shelf from the `species` agent here (iNaturalist = observations near a point,
GBIF = the global published record).

Deterministic on purpose: routing is rules, the numbers are GBIF's, and a name it cannot
find is refused by name with its own suggestions, never guessed. It reports a plan, opens
one tool call per lookup, asks permission before its first read, streams the answer, and
closes the tool call with a one-line summary.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from data import (  # noqa: E402
    DATASET,
    GbifData,
    NameNotFound,
    NatureError,
    country_label,
    format_number,
)

HELP = (
    "I read GBIF, the global biodiversity occurrence index (keyless), and use "
    "iNaturalist's taxon search to make sense of common names. Ask me:\n"
    "  • how many records of Danaus plexippus are there?\n"
    "  • how many monarch butterflies are in Canada?\n"
    "  • when are monarch butterflies recorded most?\n"
    "  • what is the newest bald eagle record?\n"
    "  • what species is Carcharodon carcharias?\n"
    "Answers carry GBIF's own numbers. These are catalog records in museums, surveys and "
    "citizen science, not a count of living animals. If a name is too vague to pin down, "
    "I say so instead of guessing."
)

PERMISSION_KEY = "nature-read-public-gbif"

SKILLS = ("nature-taxon", "nature-count", "nature-season", "nature-recent", "help")

_RECENT_WORDS = re.compile(r"\b(latest|recent|newest|last seen|most recent|just recorded)\b", re.IGNORECASE)
_SEASON_WORDS = re.compile(r"\b(season|seasonal|months?|monthly|time of year|what time|when)\b", re.IGNORECASE)
_COUNT_WORDS = re.compile(
    r"\b(how many|count|number of|records|observations|occurrences|how widespread)\b", re.IGNORECASE)
_TAXON_WORDS = re.compile(
    r"\b(what is|what are|what species|what kind|identify|scientific name|taxonomy|taxon|recognise|recognize)\b",
    re.IGNORECASE)

#: Words that never belong to a taxon name.
_NAME_STOPWORDS = frozenset({
    "what", "which", "who", "is", "are", "was", "were", "the", "a", "an", "how", "many",
    "much", "records", "record", "observations", "observation", "occurrences", "occurrence",
    "of", "in", "for", "about", "there", "do", "does", "did", "have", "has", "had", "on",
    "earth", "worldwide", "globally", "world", "total", "number", "species", "taxon",
    "please", "tell", "me", "show", "find", "get", "give", "and", "or", "to", "at", "near",
    "seen", "saw", "recorded", "collected", "when", "where", "latest", "recent", "newest",
    "most", "peak", "best", "time", "months", "month", "season", "year", "common",
    "commonest", "abundant", "help", "up", "with", "by",
})

#: Phrases to drop before the fallback name guess.
_TRAILING_NOISE = (" there", " worldwide", " globally", " on earth", " in total", " at all")


def _clean_captured(name: str) -> str | None:
    """Drop question noise from the edges of a captured phrase: 'newest bald eagle record' -> 'bald eagle'."""
    words = " ".join(name.split()).strip(" .,?!").split()
    while words and words[0].lower() in _NAME_STOPWORDS:
        words.pop(0)
    while words and words[-1].lower() in _NAME_STOPWORDS:
        words.pop()
    if not words or all(word.lower() in _NAME_STOPWORDS for word in words):
        return None
    return " ".join(words)


def taxon_from_text(text: str) -> str | None:
    """A taxon name in a sentence, or None. Patterns first, stopword strip as fallback."""
    work = str(text).strip()
    for pattern in (
        r"\b(?:of|for|about)\s+(.+?)(?=\s+(?:in|are|is|do|does|were|was|has|have|live|lives|grow|grows|recorded|seen|appear|exist)\b|[?.!,]|$)",
        r"\bhow many\s+(.+?)(?=\s+(?:in|are|is|were|was|do|does|have|has|live|grow|appear|exist)\b|[?.!,]|$)",
        r"\b(?:see|saw|find|identify|identifying|recognise|recognize)\s+(.+?)(?=\s+(?:in|are|is|at|near)\b|[?.!,]|$)",
        r"\bwhat species\s+(?:is|are)\s+(?:a|an|the)?\s*(.+?)(?=\s+(?:in|found|from|native|live|lives)\b|[?.!,]|$)",
        r"\b(?:what|which)\s+(?:is|are|was|were)\s+(?:a|an|the)?\s*(.+?)(?=\s+(?:in|found|from|native|live|lives)\b|[?.!,]|$)",
    ):
        match = re.search(pattern, work, re.IGNORECASE)
        if match:
            name = _clean_captured(match.group(1))
            if name:
                return name

    for noise in _TRAILING_NOISE:
        if work.lower().endswith(noise):
            work = work[: -len(noise)]
    words = [word for word in re.findall(r"[A-Za-z][\w.'-]*", work)
             if word.lower() not in _NAME_STOPWORDS]
    if not words:
        return None
    return " ".join(words[:4])


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    params: dict = {}
    country, phrase = GbifData.country_phrase_from_text(text)
    work = text
    if country and phrase:
        params["country"] = country
        # Subtract only the words that named the country, so a taxon name survives: in
        # "how many records of Oak in the United States", only "in the United States" goes.
        work = re.sub(r"\b(?:in|from|of)(?:\s+the)?\s+" + re.escape(phrase) + r"\b", "", work,
                      flags=re.IGNORECASE)
    name = taxon_from_text(work)
    if name:
        params["name"] = name

    if _RECENT_WORDS.search(text):
        return "nature-recent", params
    if _SEASON_WORDS.search(text):
        return "nature-season", params
    if _COUNT_WORDS.search(text):
        return "nature-count", params
    if _TAXON_WORDS.search(text) or "name" in params:
        return "nature-taxon", params
    return "help", {}


class NatureAgent(AcpAgent):
    name = "nature"
    title = "Nature — GBIF biodiversity records"
    version = "1.0.0"

    def __init__(self, connection=None, data: GbifData | None = None) -> None:
        super().__init__(connection)
        self.data = data or GbifData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Nature session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Resolve the name against the GBIF backbone", "medium"),
            ("Read the occurrence index and report its numbers", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("I read one product: GBIF's occurrence index and its name backbone. "
                        "It is catalog records published by museums, surveys and volunteers.")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read GBIF occurrences for {skill.removeprefix('nature-')}", kind="fetch",
                      name=skill, raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Nature to read GBIF's public biodiversity index?",
                                  remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the public GBIF and iNaturalist feeds before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except NameNotFound as exc:
            ctx.tool_call_update(tool, status="completed", content=ctx.text_content("name not recognised"))
            ctx.stream_text(self._unknown_name_answer(str(exc), exc.candidates))
            return STOP_END_TURN
        except (NatureError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the nature feeds: {exc}")
            return STOP_END_TURN
        ctx.check_cancelled()
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(artifact["summary"]))
        ctx.stream_text(answer)
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        # Every skill is a single read; nothing long-running to interrupt.
        return None

    # -- skills ------------------------------------------------------------

    def _run_skill(self, skill: str, params: dict) -> tuple[str, dict]:
        if skill == "nature-count":
            return self._count(params)
        if skill == "nature-season":
            return self._season(params)
        if skill == "nature-recent":
            return self._recent(params)
        if skill == "nature-taxon":
            return self._taxon(params)
        return HELP, {"summary": "help", "dataset": None}

    @staticmethod
    def _unknown_name_answer(message: str, candidates: list[dict]) -> str:
        lines = [f"{message}."]
        if candidates:
            lines.append("Closest names it does have:")
            for candidate in candidates[:5]:
                rank = candidate.get("rank") or "?"
                common = candidate.get("common_name")
                label = f"{candidate.get('scientific_name')} ({rank})"
                if common:
                    label = f"{candidate.get('scientific_name')} - {common} ({rank})"
                lines.append(f"  • {label}")
            lines.append("Be more specific, or use one of those.")
        else:
            lines.append("Try a scientific name, for example 'Danaus plexippus'.")
        lines.append("\nSource: GBIF's backbone and iNaturalist's taxon search, read live. "
                     "No guess was made.")
        return "\n".join(lines)

    def _name_or_ask(self, params: dict) -> tuple[str | None, tuple[str, dict] | None]:
        if params.get("name"):
            try:
                return GbifData.check_name(params["name"]), None
            except ValueError as exc:
                return None, (f"{exc}.", {"summary": "bad name", "dataset": DATASET})
        return None, ("I need a taxon to look up - a scientific name like 'Danaus plexippus' or a "
                      "common name like 'monarch butterfly'.", {"summary": "no name given", "dataset": DATASET})

    @staticmethod
    def _describe_taxon(taxon: dict) -> str:
        parts = [f"{taxon.get('scientific_name')}"]
        if taxon.get("rank"):
            parts.append(f"rank {taxon['rank']}")
        if taxon.get("status"):
            parts.append(f"status {taxon['status']}")
        if taxon.get("key"):
            parts.append(f"GBIF key {taxon['key']}")
        if taxon.get("match_type"):
            parts.append(f"match {taxon['match_type']}"
                         + (f" ({taxon['confidence']}% confidence)" if taxon.get("confidence") is not None else ""))
        return " - ".join(parts)

    def _taxon(self, params: dict) -> tuple[str, dict]:
        name, ask = self._name_or_ask(params)
        if ask:
            return ask
        taxon = self.data.resolve(name)
        artifact = {
            "summary": f"{DATASET}: {taxon['scientific_name']} ({taxon['rank']}, {taxon['status']}) "
                       f"key {taxon['key']}",
            "dataset": DATASET,
            **taxon,
        }
        lines = [f"GBIF recognises {taxon['query']!r} as:", f"  • {self._describe_taxon(taxon)}"]
        classification = [taxon.get("kingdom"), taxon.get("family"), taxon.get("genus")]
        classification = [part for part in classification if part]
        if classification:
            lines.append(f"  • Classification: {' > '.join(classification)}")
        if taxon.get("synonym_of"):
            lines.append(f"  • {taxon['synonym_of']} is a synonym; the accepted name is "
                         f"{taxon['scientific_name']}.")
        lines.append(f"\nSource: {DATASET}, name backbone, read live. Ask me how many records exist, "
                     "where they are, in which months, or what the newest one is.")
        return "\n".join(lines), artifact

    def _count(self, params: dict) -> tuple[str, dict]:
        name, ask = self._name_or_ask(params)
        if ask:
            return ask
        result = self.data.count(name, params.get("country"))
        taxon = result["taxon"]
        total = result["total"]
        artifact = {
            "summary": f"{DATASET}: {format_number(total)} records for {taxon['scientific_name']}"
                       + (f", country {result['country']}" if result["country"] else ""),
            "dataset": DATASET,
            "scientific_name": taxon["scientific_name"],
            "key": taxon["key"],
            "total": total,
            "country": result["country"],
            "country_count": result["country_count"],
            "top_countries": result["top_countries"],
        }
        lines = [f"GBIF holds {format_number(total)} occurrence records for "
                 f"{taxon['scientific_name']}."]
        if result["country"]:
            scoped = result["country_count"]
            share = (scoped / total * 100) if total else 0
            lines.append(f"  • In {country_label(result['country'])}: {format_number(scoped)} "
                         f"records ({share:.1f}% of the world total)")
        if result["top_countries"]:
            lines.append("  • Top countries:")
            for entry in result["top_countries"][:5]:
                lines.append(f"      - {entry['country']}: {format_number(entry['count'])}")
        lines.append(f"\nSource: {DATASET}, occurrence search with a country facet, read live. "
                     "These are published catalog records, not a population estimate: effort "
                     "differs by country, so absence of records is not absence of the animal.")
        return "\n".join(lines), artifact

    def _season(self, params: dict) -> tuple[str, dict]:
        name, ask = self._name_or_ask(params)
        if ask:
            return ask
        result = self.data.season(name)
        taxon = result["taxon"]
        months = result["months"]
        artifact = {
            "summary": f"{DATASET}: {taxon['scientific_name']} recorded most in "
                       + (f"{months[0]['month']} ({format_number(months[0]['count'])})" if months else "no months"),
            "dataset": DATASET,
            "scientific_name": taxon["scientific_name"],
            "key": taxon["key"],
            "total": result["total"],
            "months": months,
        }
        lines = [f"When {format_number(result['total'])} GBIF records of {taxon['scientific_name']} "
                 "were made, by month:"]
        for entry in months[:6]:
            lines.append(f"  • {entry['month']}: {format_number(entry['count'])}")
        if months:
            lines.append(f"\nBusiest month: {months[0]['month']} "
                         f"({format_number(months[0]['count'])} records). "
                         "Remember this counts recording effort too - spring surveys and summer "
                         "holidays show up in the data.")
        lines.append(f"Source: {DATASET}, month facet, read live.")
        return "\n".join(lines), artifact

    def _recent(self, params: dict) -> tuple[str, dict]:
        name, ask = self._name_or_ask(params)
        if ask:
            return ask
        result = self.data.recent(name, params.get("limit") or 3, params.get("country"))
        taxon = result["taxon"]
        records = result["records"]
        first = records[0] if records else {}
        artifact = {
            "summary": f"{DATASET}: newest {taxon['scientific_name']} record "
                       f"{first.get('date')} in {first.get('country') or 'unknown country'}",
            "dataset": DATASET,
            "scientific_name": taxon["scientific_name"],
            "key": taxon["key"],
            "total": result["total"],
            "records": records,
            "country": result["country"],
        }
        lines = [f"Newest GBIF records for {taxon['scientific_name']}:"]
        for record in records:
            where = ", ".join(part for part in (record.get("locality"), record.get("country")) if part) or "no place given"
            who = f" by {record['recorded_by']}" if record.get("recorded_by") else ""
            basis = f" [{record['basis']}]" if record.get("basis") else ""
            lines.append(f"  • {record.get('date') or 'date unknown'} - {where}{who}{basis}")
        if not records:
            lines.append("  • no individual records came back")
        lines.append(f"\nSource: {DATASET}, occurrence search ordered by event date, read live. "
                     "Some publishers lag months behind, so the newest record is not necessarily "
                     "the most recent sighting.")
        return "\n".join(lines), artifact


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        NatureAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
