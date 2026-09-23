"""Drought - how dry a state or county is, week by week.

"how dry is California?" has a real answer: the US Drought Monitor publishes the share of every
state and county in each drought category, keyless, every week. The categories are cumulative -
D1 is "moderate drought or worse" - and that is said plainly, because "22% in D1" reads like
"22% exactly in moderate" if nobody explains it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from data import CATEGORIES, DATASET, STATE_CODES, STATE_FIPS, DroughtData, DroughtError  # noqa: E402

HELP = (
    "I read the US Drought Monitor (keyless) - the share of every US state and county that is in\n"
    "each drought category, weekly. Ask me:\n"
    "  - how dry is California?\n"
    "  - is Texas in drought right now?\n"
    "  - drought in CA over the last 8 weeks\n"
    "  - how dry is county 48201?\n"
    "I report the categories as they are published - D1 means moderate drought *or worse*, so the\n"
    "numbers are cumulative - and I name the week they cover, because the monitor updates weekly.\n"
    "A whole-country figure is not available: the monitor has no national endpoint, and I do not\n"
    "average states into one."
)

PERMISSION_KEY = "drought-read-us-drought-monitor"

SKILLS = ("drought-place", "help")

_WEEKS_RE = re.compile(r"\b(?:last|past|over the last|for|since)\s+(\d{1,2})\s+weeks?\b",
                       re.IGNORECASE)
_COUNTY_RE = re.compile(r"\b(?:county|fips)\s+(?:code\s+)?(\d{5})\b", re.IGNORECASE)
_DROUGHT_WORDS = re.compile(r"\b(drought|dry|driest|drying|droughts|dryness|moisture)\b",
                            re.IGNORECASE)
#: Weather questions that are not drought questions. Without this, "what is the weather in
#: Texas?" would be answered with a drought table, which is not what was asked.
_OTHER_WORDS = re.compile(r"\b(weather|temperature|forecast|snow|wind|humidity|storm|rain)\b",
                          re.IGNORECASE)
_STATE_NAMES = tuple(sorted(set(STATE_FIPS)))


def place_from_text(text: str) -> str | None:
    """A 5-digit county FIPS, a state name / code, or None."""
    original = str(text or "")
    match = _COUNTY_RE.search(original)
    if match:
        return match.group(1)
    lowered = original.lower()
    for name in sorted(_STATE_NAMES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            return name
    #: Two-letter codes only when they were written as codes. "in" is Indiana in the table and
    #: also the commonest word in an English question.
    for code in sorted(STATE_CODES, key=len, reverse=True):
        if re.search(rf"\b{code.upper()}\b", original):
            return code
    return None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    stripped = str(text or "").strip()
    if not stripped or re.search(r"\b(help|what can you do|commands?)\b", stripped, re.IGNORECASE):
        return "help", {}
    place = place_from_text(stripped)
    if place and _DROUGHT_WORDS.search(stripped):
        params: dict = {"place": place}
        weeks = _WEEKS_RE.search(stripped)
        if weeks:
            params["weeks"] = int(weeks.group(1))
        return "drought-place", params
    #: A place with no drought word at all ("California") is still a drought question here, since
    #: that is the only thing this agent knows how to answer - unless it is plainly a different
    #: kind of weather question, which belongs to another agent.
    if place and not _OTHER_WORDS.search(stripped):
        return "drought-place", {"place": place}
    return "help", {}


def percent(value) -> str:
    """A share as a person reads it, or a plain marker when the monitor had nothing."""
    if value is None:
        return "no data"
    return f"{value:.2f}%"


def render_drought(reading: dict, weeks: int = 6) -> str:
    """The newest week, the trend, and what each category means."""
    latest = reading["latest"]
    lines = [f"{reading['place']} - drought as of {latest['date']}"
             + (f" (week ending {latest['week_ends']})" if latest["week_ends"] else "") + ":"]
    lines.append(f"  {percent(latest['d0'])} of the area is at least abnormally dry (D0 or worse)")
    for key, label in CATEGORIES[1:]:
        lines.append(f"  {percent(latest[key])} at least {label}")
    if latest["none"] is not None:
        lines.append(f"  {percent(latest['none'])} has no drought at all")
    history = reading["rows"][:-1]
    if history:
        lines.append(f"  previous weeks (D0 or worse): "
                     + ", ".join(f"{row['date']} {percent(row['d0'])}" for row in history))
        first = history[0]["d0"]
        if first is not None and latest["d0"] is not None:
            change = latest["d0"] - first
            word = "worse" if change > 0.05 else ("better" if change < -0.05 else "unchanged")
            lines.append(f"  versus {history[0]['date']}: {change:+.2f} points ({word})")
    del weeks  # the history length is already what the caller asked for
    return "\n".join(lines)


class DroughtAgent(AcpAgent):
    name = "drought"
    title = "Drought - weekly drought area for a US state or county"
    version = "1.0.0"

    def __init__(self, connection=None, data: DroughtData | None = None) -> None:
        super().__init__(connection)
        self.data = data or DroughtData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Drought session opened. " + HELP.splitlines()[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read the US Drought Monitor", "medium"),
            ("Report the newest week, the trend, and what the categories mean", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("These figures come from the Drought Monitor's own weekly statistics.")
            return STOP_END_TURN

        tool = "call_drought_place"
        ctx.tool_call(tool, "Read the US Drought Monitor", kind="fetch",
                      name="drought-place", raw_input=dict(params))
        if not ctx.ask_permission(tool, "Allow Drought to read the US Drought Monitor's public "
                                        "statistics?", remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the Drought Monitor first.")
            return STOP_REFUSAL
        ctx.tool_call_update(tool, status="in_progress")

        try:
            reading = self.data.read(params["place"], weeks=params.get("weeks") or 6)
        except (DroughtError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the Drought Monitor: {exc}")
            return STOP_END_TURN

        body = render_drought(reading, weeks=params.get("weeks") or 6)
        latest = reading["latest"]
        ctx.tool_call_update(tool, status="completed",
                             content=ctx.text_content(f"{reading['place']} "
                                                      f"{percent(latest['d0'])} at D0 or worse "
                                                      f"as of {latest['date']}"))
        ctx.stream_text(body + "\n")
        ctx.message(f"\nSource: {DATASET}, read live. The categories are cumulative - "
                    "D1 means moderate drought or worse, not exactly moderate - and the monitor is "
                    "published weekly, on Thursdays, so the newest week ends a few days ago.")
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        return None  # one read, one answer


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        DroughtAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
