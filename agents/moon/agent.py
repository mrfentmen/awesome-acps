"""Moon - the next full moon, and when the light goes.

Two questions people ask a computer that no editor agent answers: "when is the next full
moon?" and "when does it get dark in Denver?". The first comes from the US Naval Observatory,
the second from sunrise-sunset.org with the place's own timezone, so the clock in the answer is
the clock on the wall where the place is.

Phase times are UT because that is what the observatory publishes, and every line says so. This
agent prints the phases around a date rather than a made-up percentage of full.
"""

from __future__ import annotations

import datetime
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from data import BIG_PHASES, PHASES, MoonData, MoonError  # noqa: E402

HELP = (
    "I read the US Naval Observatory and sunrise-sunset.org (both keyless). Ask me:\n"
    "  - when is the next full moon?\n"
    "  - when is the next new moon?\n"
    "  - what are the moon phases this month?\n"
    "  - when does it get dark in Denver tonight?\n"
    "  - sunrise in Tokyo tomorrow\n"
    "  - how much daylight is there in Oslo today?\n"
    "Phase times are the observatory's own, in UT; sun times are the place's own clock with its "
    "UTC offset shown."
)

PERMISSION_KEY = "moon-read-usno-and-sun"

SKILLS = ("moon-phases", "sun-times", "help")

_PLACE_IN = re.compile(
    r"\b(?:in|at|for|near)\s+(.+?)(?=\s+(?:today|tonight|tomorrow|this week|this month|"
    r"next week)\b|[?.!]|$)", re.IGNORECASE)
_PLACE_OF = re.compile(
    r"\b(?:dark|light|daylight|sunrise|sunset|sun|dawn|dusk)\b[^.?!]*?\b(?:in|at)\s+"
    r"([A-Za-z][A-Za-z .'\-]{2,40})(?=[?.!]|$)", re.IGNORECASE)
_SUN_WORDS = re.compile(r"\b(sunrise|sunset|dark|light|daylight|twilight|dawn|dusk|noon|"
                        r"sun\b|golden hour)\b", re.IGNORECASE)
_MOON_WORDS = re.compile(r"\b(moon|moonlight|lunar|full|new moon|quarter|phase|eclipse)\b",
                         re.IGNORECASE)
_MONTH = re.compile(r"\b(this month|next month|phases|calendar)\b", re.IGNORECASE)
_COUNT_RE = re.compile(r"\bnext\s+(\d)\b", re.IGNORECASE)
_PHASE_NAMES = (("full", "Full Moon"), ("new", "New Moon"), ("first quarter", "First Quarter"),
                ("last quarter", "Last Quarter"), ("third quarter", "Last Quarter"))
_TRAILING = (" tonight", " today", " tomorrow", " this week", " please", " right now", " now")
_TZ_WORDS = {"in", "at", "on", "for", "near", "utc", "gmt", "the", "and", "is", "are"}


def phase_from_text(text: str) -> str | None:
    """The phase asked about, or None for the list."""
    lowered = str(text or "").lower()
    for word, phase in _PHASE_NAMES:
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return phase
    return None


def place_from_text(text: str) -> str | None:
    """The place a sun question is about, or None."""
    work = str(text or "").strip()
    for pattern in (_PLACE_IN, _PLACE_OF):
        match = pattern.search(work)
        if not match:
            continue
        phrase = match.group(1).strip(" .,?!")
        for noise in _TRAILING:
            if phrase.lower().endswith(noise):
                phrase = phrase[: -len(noise)]
        words = [word for word in phrase.split() if word]
        if words and len(words) <= 4 and words[0].lower() not in _TZ_WORDS:
            return " ".join(words)[:60]
    return None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    stripped = str(text or "").strip()
    if not stripped or re.search(r"\b(help|what can you do|commands?)\b", stripped, re.IGNORECASE):
        return "help", {}
    if _SUN_WORDS.search(stripped) and not re.search(r"\b(moon phase|moonlight)\b", stripped,
                                                      re.IGNORECASE):
        place = place_from_text(stripped)
        if not place:
            return "help", {}
        return "sun-times", {"place": place}
    if _MOON_WORDS.search(stripped):
        params: dict = {}
        phase = phase_from_text(stripped)
        if phase:
            params["phase"] = phase
        if _MONTH.search(stripped):
            params["count"] = 8
        count = _COUNT_RE.search(stripped)
        if count:
            params["count"] = max(1, int(count.group(1)))
        return "moon-phases", params
    return "help", {}


def render_phases(reading: dict, params: dict) -> str:
    """The phases from the date onwards, with the one the caller asked for called out."""
    wanted = params.get("phase")
    if wanted:
        rows = [row for row in reading["phases"] if row["phase"] == wanted]
        if not rows:
            rows = reading["phases"]
        head = f"Next {wanted.lower()}:"
    else:
        rows = reading["phases"]
        head = f"Moon phases from {reading['from']}:"
    lines = [head]
    for row in rows:
        mark = "  <-" if wanted and row["phase"] == wanted else ""
        lines.append(f"  - {row['date']}  {row['phase']}{mark}")
    return "\n".join(lines)


def render_sun(reading: dict) -> str:
    """The day's light, in the place's own clock."""
    where = ", ".join(bit for bit in (reading["place"]["name"], reading["place"].get("admin"),
                                      reading["place"].get("country")) if bit)
    lines = [f"Sun times for {where} on {reading['date']} ({reading['tzid']}):"]
    for row in reading["times"]:
        lines.append(f"  - {row['label']:<26} {row['clock']} (UTC{row['offset']})")
    lines.append(f"  - {'day length':<26} {reading['day_length']}")
    return "\n".join(lines)


class MoonAgent(AcpAgent):
    name = "moon"
    title = "Moon - phases, sunrise and sunset for a place"
    version = "1.0.0"

    def __init__(self, connection=None, data: MoonData | None = None) -> None:
        super().__init__(connection)
        self.data = data or MoonData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Moon session opened. " + HELP.splitlines()[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read the observatory / sun service", "medium"),
            ("Report the times with their clock named", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("Phase times are UT because that is what the observatory publishes; sun "
                        "times are the place's own clock.")
            return STOP_END_TURN

        tool = f"call_{skill.replace('-', '_')}"
        title = (f"Read the next moon phases" if skill == "moon-phases"
                 else f"Read sun times for {params['place']}")
        ctx.tool_call(tool, title, kind="fetch", name=skill, raw_input=dict(params))
        if not ctx.ask_permission(tool, f"Allow Moon to read {title.lower()}?", remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read those almanac feeds first.")
            return STOP_REFUSAL
        ctx.tool_call_update(tool, status="in_progress")

        try:
            if skill == "moon-phases":
                reading = self.data.phases(count=params.get("count") or 4)
                body = render_phases(reading, params)
                summary = f"{len(reading['phases'])} phase(s) read"
            else:
                reading = self.data.sun(params["place"])
                body = render_sun(reading)
                summary = f"{len(reading['times'])} time(s) read"
        except (MoonError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read that: {exc}")
            return STOP_END_TURN

        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(summary))
        ctx.stream_text(body + "\n")
        source = ("US Naval Observatory" if skill == "moon-phases" else "sunrise-sunset.org")
        ctx.message(f"\nSource: {source}, read live. "
                    + ("Phase times are UT (the observatory's own clock)."
                       if skill == "moon-phases"
                       else "Times are in the place's own clock and the UTC offset is printed "
                            "next to each one."))
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        return None  # one read, one answer


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        MoonAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
