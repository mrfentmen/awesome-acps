"""Groundwater - how deep the water table is, at a well or across a state.

USGS publishes every monitoring well's daily depth to water, keyless, and nobody asks it from an
editor. "how deep is the water table in Kansas?" gets the newest reading from each well reporting
in the last week, with the spread, because a water table is not one number.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from data import (DATASET, GroundwaterData, GroundwaterError,  # noqa: E402
                  GroundwaterNotFound, identifier_from_text, state_from_text)

HELP = (
    "I read USGS's water API (keyless) - every monitoring well's daily depth to water. Ask me:\n"
    "  - how deep is the water table in Kansas?\n"
    "  - water level at site 395847084085500\n"
    "  - well OH015-395847084085500 over the last 30 days\n"
    "  - is the water table dropping in Arizona?\n"
    "The figure is depth to water in feet *below land surface*, so a bigger number is a deeper\n"
    "water table. For a state I report the newest reading from each well that reported this week,\n"
    "with the median and the spread, because one number cannot describe a whole state.\n"
    "A whole-country figure is not offered: the API's daily data is not state-filterable, so a\n"
    "country would mean paging through every well, which I do not fake."
)

PERMISSION_KEY = "groundwater-read-usgs-water-api"

SKILLS = ("groundwater-well", "groundwater-state", "help")

_WINDOW_RE = re.compile(r"\b(?:last|past|over the last|for)\s+(\d{1,3})\s+days?\b", re.IGNORECASE)
_WEEKS_RE = re.compile(r"\b(?:last|past|over the last)\s+(\d{1,2})\s+weeks?\b", re.IGNORECASE)
_LIMIT_RE = re.compile(r"\b(?:top|first|sample|show|list)\s+(\d{1,3})\b", re.IGNORECASE)
_GROUNDWATER_WORDS = re.compile(r"\b(water table|groundwater|ground water|water level|aquifer|"
                                r"well|wells|depth to water|drought|dropping|dropped|"
                                r"recharging|recharge)\b", re.IGNORECASE)
_WELL_WORDS = re.compile(r"\b(site|well|gauge|monitoring location|location)\b", re.IGNORECASE)
_OTHER_WORDS = re.compile(r"\b(weather|temperature|forecast|snow|surf|tide|river stage|"
                          r"flood|earthquake|air quality)\b", re.IGNORECASE)


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    stripped = str(text or "").strip()
    if not stripped or re.search(r"\b(help|what can you do|commands?)\b", stripped, re.IGNORECASE):
        return "help", {}
    identifier = identifier_from_text(stripped)
    state = state_from_text(stripped)
    if identifier and (state is None or _WELL_WORDS.search(stripped)):
        params: dict = {"identifier": identifier}
        days = _WINDOW_RE.search(stripped)
        if days:
            params["days"] = int(days.group(1))
        else:
            weeks = _WEEKS_RE.search(stripped)
            if weeks:
                params["days"] = int(weeks.group(1)) * 7
        return "groundwater-well", params
    if state and not _OTHER_WORDS.search(stripped):
        params = {"state": state}
        days = _WINDOW_RE.search(stripped)
        weeks = _WEEKS_RE.search(stripped)
        if days:
            params["days"] = int(days.group(1))
        elif weeks:
            params["days"] = int(weeks.group(1)) * 7
        limit = _LIMIT_RE.search(stripped)
        if limit:
            params["limit"] = int(limit.group(1))
        return "groundwater-state", params
    if _GROUNDWATER_WORDS.search(stripped) and identifier:
        return "groundwater-well", {"identifier": identifier}
    return "help", {}


def render_well(well: dict) -> str:
    """One well: its record, then its depths."""
    lines = [f"{well['name']} ({well['number']})"]
    where = ", ".join(bit for bit in (well["county"], well["state"]) if bit)
    if where:
        lines.append(f"  location: {where}")
    if well["site_type"]:
        lines.append(f"  site type: {well['site_type']}")
    if well["well_depth"]:
        lines.append(f"  well depth: {well['well_depth']} ft")
    if well["aquifer"]:
        lines.append(f"  aquifer: {well['aquifer']}")
    if well["latitude"] is not None:
        lines.append(f"  coordinates: {float(well['latitude']):.5f}, "
                     f"{float(well['longitude']):.5f}")
    depth = well["latest"]["depth_ft"]
    lines.append(f"  depth to water: {depth:g} ft below land surface on {well['latest']['date']}"
                 + (f" ({well['latest']['status']})" if well["latest"].get("status") else ""))
    if well["days"] > 1:
        first = well["oldest"]["depth_ft"]
        change = depth - first
        word = ("dropping" if change > 0.05 else ("rising" if change < -0.05 else "steady"))
        lines.append(f"  {well['days']} reading(s) from {well['oldest']['date']} to "
                     f"{well['latest']['date']}: {change:+.2f} ft ({word})")
        for row in well["readings"][-8:]:
            lines.append(f"    {row['date']}: {row['depth_ft']:g} ft")
    lines.append(f"  {well['url']}")
    return "\n".join(lines)


def render_state(overview: dict) -> str:
    """A state's wells: how many, how deep, and how wide the spread."""
    lines = [f"{overview['state']} - depth to water in the last {overview['days']} day(s):"]
    lines.append(f"  {overview['wells']} well(s) reported"
                 + (f" (the newest {overview['asked_for']} returned by USGS, so there may be more)"
                    if overview["truncated"] else ""))
    lines.append(f"  median: {overview['median_ft']:g} ft below land surface")
    lines.append(f"  shallowest: {overview['shallowest']['depth_ft']:g} ft "
                 f"({overview['shallowest']['location_id']}, {overview['shallowest']['date']})")
    if overview["shallowest"]["depth_ft"] < 0:
        lines.append("  (a negative depth means the water stood above the land surface at that "
                     "well)")
    lines.append(f"  deepest: {overview['deepest']['depth_ft']:g} ft "
                 f"({overview['deepest']['location_id']}, {overview['deepest']['date']})")
    others = overview["rows"][1:-1]
    if others:
        lines.append("  other wells: "
                     + ", ".join(f"{row['depth_ft']:g} ft" for row in others[:10]))
    return "\n".join(lines)


class GroundwaterAgent(AcpAgent):
    name = "groundwater"
    title = "Groundwater - depth to the water table, at a well or across a state"
    version = "1.0.0"

    def __init__(self, connection=None, data: GroundwaterData | None = None) -> None:
        super().__init__(connection)
        self.data = data or GroundwaterData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Groundwater session opened. " + HELP.splitlines()[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read USGS's water API", "medium"),
            ("Report the depth, the dates and the spread", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("Depths are feet below land surface, so bigger means deeper.")
            return STOP_END_TURN

        tool = f"call_{skill.replace('-', '_')}"
        ctx.tool_call(tool, "Read USGS's water API", kind="fetch", name=skill,
                      raw_input=dict(params))
        if not ctx.ask_permission(tool, "Allow Groundwater to read USGS's public water data?",
                                  remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read USGS's water data first.")
            return STOP_REFUSAL
        ctx.tool_call_update(tool, status="in_progress")

        try:
            if skill == "groundwater-well":
                well = self.data.well(params["identifier"], days=params.get("days") or 14)
                body = render_well(well)
                summary = (f"{well['name']}: {well['latest']['depth_ft']:g} ft on "
                           f"{well['latest']['date']}")
            else:
                overview = self.data.state(params["state"], days=params.get("days") or 7,
                                           limit=params.get("limit") or 200)
                body = render_state(overview)
                summary = (f"{overview['state']}: {overview['wells']} well(s), median "
                           f"{overview['median_ft']:g} ft")
        except (GroundwaterError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read USGS's water data: {exc}")
            return STOP_END_TURN

        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(summary))
        ctx.stream_text(body + "\n")
        ctx.message(f"\nSource: {DATASET}, read live. A larger number means a *deeper* water "
                    "table, because the measurement is feet down from the land surface. Readings "
                    "are provisional until USGS approves them, and many wells only report during "
                    "the growing season, so a quiet well is normal.")
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        return None  # one read, one answer


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        GroundwaterAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
