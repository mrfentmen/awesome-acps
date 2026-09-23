"""ClinicalTrials - which studies exist for a condition, and where they are running.

"trials for melanoma in Boston" is a question with a public, structured, keyless answer that no
agent in any editor can read. This one searches ClinicalTrials.gov, and it is careful about the
one trap in that API: a place filter matches a *study* whose site list contains the place, not a
site, so a study returned for Boston can have its first sites in Arizona. The sites that really
match the place are found and named, and the total number of sites is printed beside them.

Every answer carries the same three sentences, because this is a health topic and they are true:
the registry holds what a sponsor filed, a status is the sponsor's own word, and none of this is
medical advice.
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
    MAX_INTERVENTIONS,
    MAX_LOCATIONS,
    MAX_STUDIES,
    STATUSES,
    TrialsData,
    TrialsError,
    date_text,
    enrollment_text,
    matching_sites,
    phase_text,
    site_line,
    status_text,
)

HELP = (
    "I read ClinicalTrials.gov (keyless). Ask me:\n"
    "  - trials for melanoma in Boston\n"
    "  - recruiting trials for ALS in California\n"
    "  - completed studies for long COVID in the UK\n"
    "  - what is trial NCT01065467?\n"
    "I answer with the study's number, status, phase, sponsor, how many people it plans to enrol, "
    "the sites that match the place you named, when the record was last updated and a link to it. "
    "This is the registry's record, not medical advice, and a status is the sponsor's own word."
)

PERMISSION_KEY = "trials-read-registry"

SKILLS = ("trials-search", "trials-study", "trials-help")

#: The condition, between 'for' and the place or the end.
_CONDITION = re.compile(
    r"\b(?:trials?|studies|study|research)\s+(?:for|about|on|into)\s+(.+?)"
    r"(?=\s+(?:in|near|around|at|across|within)\s+|[?.!]|$)", re.IGNORECASE)
_CONDITION_PLAIN = re.compile(r"\b(?:for|about|on|into)\s+(.+?)(?=\s+(?:in|near|around|at)\s+|[?.!]|$)",
                              re.IGNORECASE)
_PLACE = re.compile(r"\b(?:in|near|around|at|across|within)\s+(.+?)(?=\s+(?:right now|now|today|"
                    r"currently)\b|[?.!]|$)", re.IGNORECASE)
_NCT = re.compile(r"\b(NCT\s*\d{6,8})\b", re.IGNORECASE)
_STATUS = re.compile(r"\b(" + "|".join(sorted(STATUSES, key=len, reverse=True)) + r")\b",
                     re.IGNORECASE)
_TRAILING = (" right now", " now", " today", " currently", " please", " that are recruiting",
             " which are recruiting")
#: Words that are a status or a filler, not a condition.
_NOT_CONDITION = {"trials", "studies", "study", "research", "me", "us", "this", "it",
                  "clinical trials", "clinical trials.gov"}
_NOT_PLACE = {"me", "us", "here", "there", "my area", "the area", "my city", "this city"}


def nct_from_text(text: str) -> str | None:
    """The study number a question names, tidied to the form the registry uses."""
    found = _NCT.search(str(text or ""))
    return re.sub(r"\s+", "", found.group(1)).upper() if found else None


def status_from_text(text: str) -> str | None:
    """The status asked about as the API's own token, or None."""
    found = _STATUS.search(str(text or ""))
    return STATUSES.get(found.group(1).lower()) if found else None


def condition_from_text(text: str) -> str | None:
    """The condition a question is about, or None when it names none it can use."""
    work = str(text or "").strip()
    for pattern in (_CONDITION, _CONDITION_PLAIN):
        found = pattern.search(work)
        if not found:
            continue
        phrase = _tidy(found.group(1))
        if phrase and phrase.lower() not in _NOT_CONDITION and not _NCT.fullmatch(phrase):
            return phrase
    return None


def place_from_text(text: str) -> str | None:
    """The place a question is about, or None when it names none it can use."""
    work = str(text or "").strip()
    found = _PLACE.search(work)
    if not found:
        return None
    phrase = _tidy(found.group(1))
    if not phrase or phrase.lower() in _NOT_PLACE or _NCT.fullmatch(phrase):
        return None
    return phrase


def _tidy(phrase: str) -> str:
    """The filler a person adds at the end of a phrase, removed."""
    cleaned = str(phrase or "").strip(" .,?!")
    changed = True
    while changed:
        changed = False
        for noise in _TRAILING:
            if cleaned.lower().endswith(noise):
                cleaned = cleaned[: -len(noise)].strip(" .,?!")
                changed = True
    return cleaned


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    stripped = str(text or "").strip()
    if not stripped or re.search(r"\b(help|what can you do|commands?)\b", stripped, re.IGNORECASE):
        return "trials-help", {}
    nct = nct_from_text(stripped)
    if nct and not condition_from_text(stripped):
        return "trials-study", {"nct": nct}
    params: dict = {}
    condition = condition_from_text(stripped)
    place = place_from_text(stripped)
    status = status_from_text(stripped)
    if condition:
        params["condition"] = condition
    if place:
        params["place"] = place
    if status:
        params["status"] = status
    if params:
        return "trials-search", params
    if nct:
        return "trials-study", {"nct": nct}
    return "trials-help", {}


def render_study(study: dict, place: str | None, heading: bool = True) -> list[str]:
    """One study as the lines a person reads, with the matching sites named."""
    label = f"{study['nct']}  {study['title']}" if heading else study["title"]
    lines = [f"  - {label}"]
    lines.append(f"      status {status_text(study['status'])}"
                 + (f", {study['study_type'].lower()}" if study["study_type"] else "")
                 + f", {phase_text(study['phases'])}")
    lines.append(f"      sponsor {study['sponsor'] or 'not stated'}"
                 + (f"; with {', '.join(study['collaborators'])}" if study["collaborators"] else ""))
    lines.append(f"      enrolling {enrollment_text(study['enrollment'])}"
                 + f"; starts {date_text(study['start'])}"
                 + f", {date_text(study['completion'])}")
    if study["conditions"]:
        lines.append(f"      conditions: {', '.join(study['conditions'][:4])}")
    if study["interventions"]:
        shown = "; ".join(f"{item['type']} {item['name']}" for item in study["interventions"])
        more = study["intervention_count"] - len(study["interventions"])
        lines.append(f"      studied: {shown}" + (f" (+{more} more)" if more > 0 else ""))
    eligibility = []
    if study["sex"] and study["sex"].upper() != "ALL":
        eligibility.append(f"sex {study['sex'].lower()}")
    if study["minimum_age"] or study["maximum_age"]:
        eligibility.append(f"ages {study['minimum_age'] or 'any'} to "
                           f"{study['maximum_age'] or 'any'}")
    if eligibility:
        lines.append(f"      eligibility: {', '.join(eligibility)}")
    sites = matching_sites(study, place) if place else []
    if place and sites:
        lines.append(f"      sites matching {place}: {len(sites)} of {study['location_count']}")
        for site in sites[:MAX_LOCATIONS]:
            lines.append(f"        {site_line(site)}")
        if len(sites) > MAX_LOCATIONS:
            lines.append(f"        ... and {len(sites) - MAX_LOCATIONS} more matching site(s)")
    elif place:
        lines.append(f"      sites: none of its {study['location_count']} listed site(s) mention "
                     f"{place} - the registry matched this study on another field")
    else:
        lines.append(f"      sites: {study['location_count']} listed"
                     + (f", first is {site_line(study['locations'][0])}"
                        if study["locations"] else ""))
    lines.append(f"      record last updated {date_text(study['last_update'])}"
                 + (", has results posted" if study["has_results"] else ""))
    lines.append(f"      {study['url']}")
    return lines


class TrialsAgent(AcpAgent):
    name = "clinicaltrials"
    title = "ClinicalTrials - studies for a condition, and where they run"
    version = "1.0.0"

    def __init__(self, connection=None, data: TrialsData | None = None) -> None:
        super().__init__(connection)
        self.data = data or TrialsData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "ClinicalTrials session opened. " + HELP.splitlines()[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Search the registry, or read one study by its number", "medium"),
            ("Report the study, the sites that match, and the registry's limits", "medium"),
        ])

        if skill == "trials-help":
            ctx.stream_text(HELP)
            ctx.message("\nThis is the registry's record, not medical advice.")
            return STOP_END_TURN

        tool = f"call_{skill.replace('-', '_')}"
        asking = (f"study {params['nct']}" if skill == "trials-study"
                  else ", ".join(f"{key} {value}" for key, value in params.items()))
        ctx.tool_call(tool, f"Search ClinicalTrials.gov for {asking}", kind="fetch", name=skill,
                      raw_input=dict(params))
        if not ctx.ask_permission(tool, "Allow ClinicalTrials to read the registry?",
                                  remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read ClinicalTrials.gov first.")
            return STOP_REFUSAL
        ctx.tool_call_update(tool, status="in_progress")

        try:
            if skill == "trials-study":
                study = self.data.study(params["nct"])
                body = "\n".join([f"{study['nct']}  {study['title']}"] +
                                 render_study(study, None, heading=False)[1:] +
                                 [f"      official title: {study['official_title']}"
                                  if study["official_title"] else "",
                                  f"      summary: {study['summary']}" if study["summary"] else ""])
                summary = f"one study: {study['nct']}"
            else:
                body, summary = self._search(params)
        except TrialsError as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read that: {exc}")
            return STOP_END_TURN

        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(summary))
        ctx.stream_text(body.rstrip("\n") + "\n")
        ctx.message(f"\nSource: {DATASET}, read live. This is the registry's own record: it holds "
                    f"what a sponsor filed, a status is the sponsor's own word, a place filter "
                    f"matches a study that lists a site there rather than the site itself, and "
                    f"none of this is medical advice.")
        return STOP_END_TURN

    # -- the search answer -------------------------------------------------

    def _search(self, params: dict) -> tuple[str, str]:
        reading = self.data.search(condition=params.get("condition"), place=params.get("place"),
                                   status=params.get("status"), limit=MAX_STUDIES)
        asked = []
        if reading["condition"]:
            asked.append(f"condition: {reading['condition']}")
        if reading["place"]:
            asked.append(f"place: {reading['place']}")
        if reading["status"]:
            asked.append(f"status: {status_text(reading['status'])}")
        total = reading["total"]
        lines = [f"ClinicalTrials.gov for {'; '.join(asked) or 'that search'}:"]
        lines.append(f"  - {total} study(ies) match"
                     if total is not None else "  - the registry did not say how many match")
        lines.append(f"  - newest record update first, showing {reading['returned']} of them")
        lines.append(f"  - the search itself: {reading['url']}")
        if not reading["studies"]:
            lines.append("  - no study in the registry matches that combination")
            return "\n".join(lines), "0 studies"
        for index, study in enumerate(reading["studies"]):
            lines.extend(render_study(study, reading["place"], heading=True))
            if index < reading["returned"] - 1:
                lines.append("")
        if reading.get("withheld"):
            lines.append("")
            lines.append(f"  - the registry sent {reading['withheld']} more row(s) than one "
                         f"answer shows; they were not printed")
        if reading["next_page"]:
            lines.append("")
            lines.append(f"  - more results exist; the registry gave a next-page token, so this "
                         f"list is the first {reading['returned']}")
        return "\n".join(lines), f"{reading['returned']} of {total} studies"

    def on_cancel(self, session) -> None:
        return None  # one search, one answer


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        TrialsAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
