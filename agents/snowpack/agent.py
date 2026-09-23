"""Snowpack - how much water is in the mountain snow, from NRCS's own network.

Every automated snow station reports how much water its snow holds, with the median for that day
attached, so "percent of normal" is a real number and not a guess. In late summer the honest
answer is usually zero, and the agent says that instead of dressing it up.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from data import (DATASET, STATE_CODES, STATE_NAMES, SnowData, SnowpackError,  # noqa: E402
                  SnowpackNotFound)

HELP = (
    "I read NRCS's snow network (keyless) - the automated stations that measure the mountain\n"
    "snowpack. Ask me:\n"
    "  - how much water is in the California snowpack?\n"
    "  - snow water equivalent at station 301:CA:SNTL\n"
    "  - snow depth in Colorado right now\n"
    "  - snow at 301:CA:SNTL over the last 60 days\n"
    "The headline figure is *snow water equivalent*: the water in the snow, in inches, with each\n"
    "value's own median beside it, so percent of normal is real. Snow depth is a different number\n"
    "and is never mixed in with it. Off season most stations read zero, and I report that plainly.\n"
    "For a place I answer by state: NRCS has no Sierra or Cascade grouping, and I do not invent one."
)

PERMISSION_KEY = "snowpack-read-nrcs-snow-network"

SKILLS = ("snowpack-station", "snowpack-state", "help")

_TRIPLET_RE = re.compile(r"\b(\d{1,7}:[A-Za-z]{2}:([A-Za-z0-9]{2,6}))\b")
_DAYS_RE = re.compile(r"\b(?:last|past|over the last|for)\s+(\d{1,3})\s+days?\b", re.IGNORECASE)
_WEEKS_RE = re.compile(r"\b(?:last|past|over the last)\s+(\d{1,2})\s+weeks?\b", re.IGNORECASE)
_LIMIT_RE = re.compile(r"\b(?:top|first|show|list)\s+(\d{1,3})\b", re.IGNORECASE)
_SNOW_WORDS = re.compile(r"\b(snowpack|snow|snowpack's|snowy|snowmelt|melt|powder|snow depth|"
                         r"snow water|wteq|snwd|snotel)\b", re.IGNORECASE)
_DEPTH_WORDS = re.compile(r"\b(depth|deep|how deep)\b", re.IGNORECASE)
_PRICE_WORDS = re.compile(r"\b(ski|skiing|resort|lift)\b", re.IGNORECASE)
_US_STATE_RE = re.compile(r"\b(" + "|".join(STATE_CODES) + r")\b")


def state_code_from_text(text: str) -> str | None:
    """A state by full name, or by a two-letter code written as one ('or' is the word, OR is
    Oregon)."""
    original = str(text or "")
    lowered = original.lower()
    for name in sorted(STATE_NAMES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            return STATE_NAMES[name]
    match = _US_STATE_RE.search(original)
    return match.group(1) if match else None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    stripped = str(text or "").strip()
    if not stripped or re.search(r"\b(help|what can you do|commands?)\b", stripped, re.IGNORECASE):
        return "help", {}
    triplet = _TRIPLET_RE.search(stripped)
    if triplet and _SNOW_WORDS.search(stripped):
        params: dict = {"triplet": triplet.group(1)}
        window = _window(stripped)
        if window:
            params["days"] = window
        if _DEPTH_WORDS.search(stripped):
            params["elements"] = ("SNWD",)
        return "snowpack-station", params
    state = state_code_from_text(stripped)
    if state and _SNOW_WORDS.search(stripped):
        params = {"state": state}
        window = _window(stripped)
        if window:
            params["days"] = window
        limit = _LIMIT_RE.search(stripped)
        if limit:
            params["limit"] = int(limit.group(1))
        if _DEPTH_WORDS.search(stripped):
            params["element"] = "SNWD"
        return "snowpack-state", params
    if _PRICE_WORDS.search(stripped):
        return "help", {}
    return "help", {}


def _window(text: str) -> int | None:
    days = _DAYS_RE.search(text)
    if days:
        return int(days.group(1))
    weeks = _WEEKS_RE.search(text)
    if weeks:
        return int(weeks.group(1)) * 7
    return None


def render_station(station: dict, days: int = 30) -> str:
    """One station: its record, then each element's newest value beside its median."""
    record = station["station"]
    lines = [f"{record.get('name') or station['station']['triplet']} "
             f"({station['station']['triplet']})"]
    where = ", ".join(str(bit) for bit in (record.get("county"), record.get("state")) if bit)
    if where:
        lines.append(f"  location: {where}")
    if record.get("elevation_ft"):
        lines.append(f"  elevation: {record['elevation_ft']:g} ft")
    if record.get("latitude") is not None:
        lines.append(f"  coordinates: {float(record['latitude']):.5f}, "
                     f"{float(record['longitude']):.5f}")
    for series in station["series"]:
        latest = series["latest"]
        line = (f"  {series['label']}: {float(latest['value']):g} {series['unit']} "
                f"on {latest.get('date')}")
        if latest.get("median") is not None:
            line += f" (median for the day: {float(latest['median']):g} {series['unit']}"
            if series["percent_of_median"] is not None:
                line += f", {series['percent_of_median']:.0f}% of normal"
            line += ")"
        lines.append(line)
        if len(series["rows"]) > 1:
            shown = ", ".join(f"{row['date'][5:]}: {float(row['value']):g}"
                              for row in series["rows"][-10:])
            lines.append(f"    last {min(len(series['rows']), 10)} day(s) - {shown}")
    lines.append(f"  {station['url']}")
    return "\n".join(lines)


def render_state(summary: dict) -> str:
    """A state's snow stations, and what the ones with snow actually hold."""
    lines = [f"{summary['state']} - {summary['label']} ({summary['unit']}), from every "
             f"{'SNOTEL' if summary['element'] == 'WTEQ' else ''} snow station in the state:".strip()]
    lines.append(f"  {summary['stations']} station(s) reported a value; "
                 f"{summary['with_snow']} of them have any {summary['label']} at all")
    if summary["average_percent_of_median"] is not None:
        lines.append(f"  median-for-the-day comparison over "
                     f"{summary['stations_with_a_median']} station(s): "
                     f"{summary['average_percent_of_median']:.0f}% of normal on average")
    elif summary.get("medians_are_all_zero"):
        lines.append("  every station's median for today is 0 in, so percent of normal does not "
                     "exist off season - it is 0 divided by 0")
    else:
        lines.append("  no station in the state reported a median for today")
    if not summary["top"]:
        lines.append("  no station in the state has any of it today - off season that is the "
                     "honest answer")
    for row in summary["top"]:
        where = f", {row.get('elevation_ft'):g} ft" if row.get("elevation_ft") else ""
        percent = (f" - {row['percent_of_median']:.0f}% of normal"
                   if row.get("percent_of_median") is not None else "")
        lines.append(f"  {row['name']} ({row['triplet']}{where}): {float(row['value']):g} "
                     f"{summary['unit']} on {row['date']}{percent}")
    return "\n".join(lines)


class SnowpackAgent(AcpAgent):
    name = "snowpack"
    title = "Snowpack - water in the mountain snow, from NRCS's snow network"
    version = "1.0.0"

    def __init__(self, connection=None, data: SnowData | None = None) -> None:
        super().__init__(connection)
        self.data = data or SnowData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Snowpack session opened. " + HELP.splitlines()[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read NRCS's snow network", "medium"),
            ("Report the value beside its own median", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("Snow water equivalent is inches of water in the snow; snow depth is a "
                        "different measurement.")
            return STOP_END_TURN

        tool = f"call_{skill.replace('-', '_')}"
        ctx.tool_call(tool, "Read NRCS's snow network", kind="fetch", name=skill,
                      raw_input=dict(params))
        if not ctx.ask_permission(tool, "Allow Snowpack to read NRCS's public snow network?",
                                  remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the snow network first.")
            return STOP_REFUSAL
        ctx.tool_call_update(tool, status="in_progress")

        try:
            if skill == "snowpack-station":
                station = self.data.station(params["triplet"], days=params.get("days") or 30,
                                           elements=params.get("elements") or ("WTEQ", "SNWD"))
                body = render_station(station, days=params.get("days") or 30)
                latest = station["series"][0]["latest"]
                summary = (f"{station['station'].get('name')}: "
                           f"{float(latest['value']):g} {station['series'][0]['unit']}")
            else:
                summary_data = self.data.state(params["state"], days=params.get("days") or 7,
                                               limit=params.get("limit") or 40,
                                               element=params.get("element") or "WTEQ")
                body = render_state(summary_data)
                summary = (f"{summary_data['state']}: {summary_data['with_snow']} of "
                           f"{summary_data['stations']} station(s) with any snow")
        except (SnowpackError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the snow network: {exc}")
            return STOP_END_TURN

        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(summary))
        ctx.stream_text(body + "\n")
        ctx.message(f"\nSource: {DATASET}, read live. The value is snow water equivalent - "
                    "inches of water held in the snow - and the median beside it is NRCS's own "
                    "figure for that calendar day, so percent of normal needs no other source. "
                    "Most stations only accumulate snow from late autumn, so a zero in summer is "
                    "the season, not a reading failure.")
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        return None  # one read, one answer


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        SnowpackAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
