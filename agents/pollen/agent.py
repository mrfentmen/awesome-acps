"""Pollen - is today a bad day for hay fever?

A pollen agent is the one allergy question a weather forecast cannot answer, and no editor
protocol has one. This reads the CAMS pollen model through Open-Meteo and reports the counts
per species, in the place's own clock, with the band each number falls in.

The model covers Europe only, and this agent says that out loud. Asked about New York it does
not print six zeros - it says the model has no pollen for that place, because on this feed a
real zero and "outside the model" are the same `null`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from data import DATASET, GROUPS, SPECIES, PollenData, PollenError  # noqa: E402

HELP = (
    "I read the CAMS pollen model (keyless). Ask me:\n"
    "  - is the pollen bad in Berlin today?\n"
    "  - grass pollen in London tomorrow\n"
    "  - when is birch pollen worst in Oslo this week?\n"
    "  - tree pollen in Munich\n"
    f"I report {', '.join(label for _key, label in SPECIES)} in grains/m3, in the place's own "
    "clock, with the band each number falls in. The model covers Europe: outside it I say so "
    "instead of printing zeros."
)

PERMISSION_KEY = "pollen-read-cams"

SKILLS = ("pollen-now", "pollen-forecast", "help")

_PLACE_IN = re.compile(
    r"\b(?:in|at|for|near)\s+(.+?)(?=\s+(?:today|tonight|tomorrow|this week|next week|"
    r"over the next|for the next)\b|[?.!]|$)", re.IGNORECASE)
_PLACE_AFTER = re.compile(
    r"\b(?:pollen|allergy|allergies|hay ?fever)\b(?:\s+counts?|\s+levels?|\s+forecast)?\s+"
    r"(?!in\b|at\b|for\b|near\b|today\b|tomorrow\b|bad\b|worst\b|high\b|low\b|is\b|are\b)"
    r"([A-Za-z][A-Za-z .'\-]{2,40})(?=[?.!]|$)", re.IGNORECASE)
_FORECAST = re.compile(r"\b(forecast|tomorrow|next|week|days?|worst|when|peak|rising)\b",
                       re.IGNORECASE)
_TODAY = re.compile(r"\b(today|right now|now|this morning|currently)\b", re.IGNORECASE)
_WORDS = re.compile(r"\b(pollen|allergy|allergies|hay ?fever|counts?|levels?|grass|birch|alder|"
                    r"mugwort|ragweed|olive|tree|weed)\b", re.IGNORECASE)
_DAYS_RE = re.compile(r"\b(?:next|coming|for)\s+(\d{1,2})\s+days?\b", re.IGNORECASE)
_TRAILING = (" please", " today", " tomorrow", " now", " right now", " this week", " for me")


def species_from_text(text: str) -> str | None:
    """The one species asked about, or None for all of them. Groups resolve to a prefix list."""
    lowered = str(text or "").lower()
    for label in ("alder", "birch", "grass", "mugwort", "ragweed", "olive"):
        if re.search(rf"\b{label}\b", lowered):
            return label
    if re.search(r"\btree\b", lowered):
        return None  # trees are several species: report them all, the caller notes the group
    return None


def group_from_text(text: str) -> str | None:
    """'tree pollen' / 'weed pollen' / 'grass pollen' as a named group, when one is named."""
    lowered = str(text or "").lower()
    for name in ("tree", "weed", "grass"):
        if re.search(rf"\b{name}\b", lowered):
            return name
    return None


def place_from_text(text: str) -> str | None:
    """The place asked about, or None."""
    work = str(text or "").strip()
    for pattern in (_PLACE_IN, _PLACE_AFTER):
        match = pattern.search(work)
        if not match:
            continue
        phrase = match.group(1).strip(" .,?!")
        for noise in _TRAILING:
            if phrase.lower().endswith(noise):
                phrase = phrase[: -len(noise)]
        words = [word for word in phrase.split() if word]
        if words and len(words) <= 4 and not _WORDS.fullmatch(" ".join(words)):
            return " ".join(words)[:60]
    return None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    stripped = str(text or "").strip()
    if not stripped or re.search(r"\b(help|what can you do|commands?)\b", stripped, re.IGNORECASE):
        return "help", {}
    if not _WORDS.search(stripped):
        return "help", {}
    place = place_from_text(stripped)
    if not place:
        return "help", {}
    params: dict = {"place": place}
    species = species_from_text(stripped)
    group = group_from_text(stripped)
    if species:
        params["species"] = species
    elif group and group in GROUPS:
        params["group"] = group
    days = _DAYS_RE.search(stripped)
    if days:
        params["days"] = int(days.group(1))
    if _FORECAST.search(stripped) and not _TODAY.search(stripped):
        return "pollen-forecast", params
    return "pollen-now", params


def render_current(reading: dict) -> str:
    """The species table, and the honest line when the model has nothing for the place."""
    where = ", ".join(bit for bit in (reading["place"]["name"], reading["place"].get("admin"),
                                      reading["place"].get("country")) if bit)
    if not reading["covered"]:
        return (f"No pollen has been modelled for {where} at all: the CAMS pollen model "
                "covers Europe, and this place is outside it. That is not a zero reading - a "
                "real zero would be reported as 0 grains/m3 and banded as none.")
    lines = [f"Pollen in {where} at {reading['at']} (the place's own clock):"]
    for row in reading["readings"]:
        shown = "not available" if row["value"] is None else f"{row['value']} grains/m3"
        lines.append(f"  - {row['label']:<8} {shown:<18} {row['band']}")
    worst = reading["worst"]
    lines.append(f"  Worst right now: {worst['label']} ({worst['value']} grains/m3, "
                 f"{worst['band']}).")
    return "\n".join(lines)


def render_forecast(reading: dict, params: dict) -> str:
    """Per-day peaks for the days ahead."""
    where = ", ".join(bit for bit in (reading["place"]["name"], reading["place"].get("admin"),
                                      reading["place"].get("country")) if bit)
    wanted = params.get("species") or params.get("group") or "all species"
    if params.get("group"):
        wanted = f"{params['group']} ({', '.join(reading['species'])})"
    if not reading["covered"]:
        return (f"No pollen forecast for {where}: the CAMS model covers Europe and this place "
                "is outside it. I will not print a week of zeros for a place the model does "
                "not cover.")
    lines = [f"{wanted.capitalize()} pollen peaks for {where}:"]
    for day in reading["days"]:
        if not day["peaks"]:
            lines.append(f"  {day['date']}: " + ("every species at 0 grains/m3"
                                                if day["covered"] else "no value modelled"))
            continue
        shown = ", ".join(f"{row['label']} {row['value']}" for row in day["peaks"][:4])
        lines.append(f"  {day['date']}: {shown} grains/m3 (worst {day['worst']['band']})")
    return "\n".join(lines)


class PollenAgent(AcpAgent):
    name = "pollen"
    title = "Pollen - CAMS counts per species for a place"
    version = "1.0.0"

    def __init__(self, connection=None, data: PollenData | None = None) -> None:
        super().__init__(connection)
        self.data = data or PollenData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Pollen session opened. " + HELP.splitlines()[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read the CAMS pollen model for the place", "medium"),
            ("Report each species with its band, or say the model does not cover it", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("I read a European model, so a place outside Europe gets an honest "
                        "\"not modelled\" instead of a table of zeros.")
            return STOP_END_TURN

        place = params["place"]
        tool = f"call_pollen_{'forecast' if skill == 'pollen-forecast' else 'now'}"
        ctx.tool_call(tool, f"Read CAMS pollen for {place}", kind="fetch", name=skill,
                      raw_input=dict(params))
        if not ctx.ask_permission(tool, f"Allow Pollen to read the CAMS model for {place}?",
                                  remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the pollen model first.")
            return STOP_REFUSAL
        ctx.tool_call_update(tool, status="in_progress")

        try:
            if skill == "pollen-forecast":
                reading = self.data.forecast(place, days=params.get("days") or 5,
                                             species=params.get("species"),
                                             group=params.get("group"))
                body = render_forecast(reading, params)
                summary = f"{len(reading['days'])} day(s) read"
            else:
                reading = self.data.current(place, species=params.get("species"),
                                            group=params.get("group"))
                body = render_current(reading)
                summary = f"{len(reading['readings'])} species read"
        except (PollenError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the pollen model: {exc}")
            return STOP_END_TURN

        covered = reading["covered"]
        if not covered:
            summary = "no pollen modelled for this place"
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(summary))
        ctx.stream_text(body + "\n")
        ctx.message(f"\nSource: {DATASET}, read live. Bands are the published grains/m3 scale "
                    "for pollen (none, low, moderate, high, very high) and every number keeps "
                    "its unit.")
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        return None  # one read, one answer


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        PollenAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
