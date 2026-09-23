"""Floodwatch — river levels and flood stages, inside your editor.

Every ACP agent on the vendors' list writes code. This one answers the question a river town
asks: how high is the water, how high is it forecast to go, and how far is that from the
published flood stages. It reads NWPS, the National Water Prediction Service - the river
forecast centers' own gauges - and uses USGS's site search to turn a river name into a gauge.

Deterministic on purpose: routing is rules, the stage, flow and threshold numbers are NWPS's
own, a river that cannot be resolved is refused by name, and the answer states which gauge it
used. It reports a plan, opens one tool call per lookup, asks permission before its first
read, streams the answer, and closes the tool call with a one-line summary.
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
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    DATASET,
    FloodData,
    FloodError,
    gauge_id_from_text,
    river_query,
)

HELP = (
    "I read NWPS, the National Water Prediction Service (keyless), and use USGS to find\n"
    "gauges by river name. Ask me:\n"
    "  • what is the Mississippi River at St. Louis doing?\n"
    "  • how high is the water at gauge EADM7?\n"
    "  • is the Colorado River at Austin flooding?\n"
    "  • which gauges do you have for the Willamette River?\n"
    "I give you the observed stage, the forecast stage and the published action, minor,\n"
    "moderate and major flood stages for that gauge - not a guess about your street."
)

PERMISSION_KEY = "floodwatch-read-public-nwps"

SKILLS = ("flood-status", "flood-find", "help")

_FIND_WORDS = re.compile(r"\b(which|what|list|show|find|any)\b.{0,24}\bgauges?\b|"
                         r"\bgauges? (?:do you|are there|for)\b", re.IGNORECASE)
_STATUS_WORDS = re.compile(r"\b(how high|how full|flooding|flood stage|stage|level|crest|"
                           r"forecast|cresting|rising|falling|doing|right now)\b", re.IGNORECASE)


def _number(value, digits: int = 2) -> str:
    """Trim padding without eating a real digit: 13.08 stays, 30.0 -> '30'."""
    if value is None:
        return "not published"
    text = f"{float(value):.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    params: dict = {}
    gid = gauge_id_from_text(text)
    river = river_query(text)
    if gid:
        params["gauge"] = gid
    elif river:
        params["river"] = river

    if _FIND_WORDS.search(text):
        return "flood-find", params
    if gid or river:
        return "flood-status", params
    if _STATUS_WORDS.search(text):
        return "flood-status", params
    return "help", {}


class FloodwatchAgent(AcpAgent):
    name = "floodwatch"
    title = "Floodwatch — river levels and flood stages"
    version = "1.0.0"

    def __init__(self, connection=None, data: FloodData | None = None) -> None:
        super().__init__(connection)
        self.data = data or FloodData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Floodwatch session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Resolve the gauge (id, or river name through USGS)", "medium"),
            ("Read the NWPS stage, forecast and flood categories", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("I read one product: NWPS gauge status, which is the river forecast "
                        "centers' own observed and forecast stage. USGS is only used to find "
                        "a gauge by name.")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read NWPS {skill.removeprefix('flood-')}", kind="fetch",
                      name=skill, raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Floodwatch to read the public NWPS river gauges?",
                                  remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the public NWPS feeds before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (FloodError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the river feeds: {exc}")
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
        if skill == "flood-status":
            return self._status(params)
        if skill == "flood-find":
            return self._find(params)
        return HELP, {"summary": "help", "dataset": None}

    def _target_or_ask(self, params: dict) -> tuple[str | None, tuple[str, dict] | None]:
        if params.get("gauge"):
            return params["gauge"], None
        if params.get("river"):
            return params["river"], None
        return None, ("I need a gauge id like EADM7, or a river name like "
                      "'Mississippi River at St. Louis'.",
                      {"summary": "no gauge given", "dataset": DATASET})

    @staticmethod
    def _category(value) -> str:
        key = str(value or "").strip().lower()
        return CATEGORY_LABELS.get(key, key.replace("_", " ") or "not published")

    def _status(self, params: dict) -> tuple[str, dict]:
        target, ask = self._target_or_ask(params)
        if ask:
            return ask
        report = self.data.flood_status(target)
        observed, forecast = report["observed"], report["forecast"]
        artifact = {
            "summary": f"{DATASET}: {report['name']} observed {_number(observed['stage'])} "
                       f"{observed['stage_unit'] or 'ft'} ({self._category(observed['category'])})"
                       + (f", forecast {_number(forecast['stage'])} {forecast['stage_unit'] or 'ft'}"
                          if forecast["stage"] is not None else ""),
            "dataset": DATASET,
            **report,
        }
        place = ", ".join(part for part in (report.get("state"), report.get("county")) if part)
        header = f"{report['lid']} - {report['name']}" + (f" ({place})" if place else "")
        office = f", forecast by {report['forecast_office']}" if report.get("forecast_office") else ""
        lines = [f"{header}{office}:"]
        lines.append(f"  • Observed: {_number(observed['stage'])} {observed['stage_unit'] or 'ft'}"
                     + (f", flow {_number(observed['flow'])} {observed['flow_unit']}"
                        if observed["flow"] is not None else ", flow not published")
                     + f" as of {observed['at'] or 'an unpublished time'}"
                     + f" - {self._category(observed['category'])}")
        if forecast["stage"] is not None:
            lines.append(f"  • Forecast: {_number(forecast['stage'])} {forecast['stage_unit'] or 'ft'}"
                         + (f", flow {_number(forecast['flow'])} {forecast['flow_unit']}"
                            if forecast["flow"] is not None else "")
                         + f" by {forecast['at'] or 'an unpublished time'}"
                         + f" - {self._category(forecast['category'])}")
        else:
            lines.append("  • Forecast: none published for this gauge")
        stages = report.get("categories") or {}
        if stages:
            listed = ", ".join(f"{key} {_number(stages[key])} ft" for key in CATEGORY_ORDER if key in stages)
            lines.append(f"  • Flood stages here: {listed}")
        next_up = report.get("next_threshold")
        if next_up:
            lines.append(f"  • Next: {next_up['category']} at {_number(next_up['stage'])} ft, "
                         f"{_number(next_up['feet_to_go'])} ft above the observed stage")
        elif observed["stage"] is not None:
            lines.append("  • Every published flood stage is at or below the observed level - "
                         "check the category above.")
        if report.get("via") and report["via"] != "gauge id":
            lines.append(f"  • Gauge chosen by {report['via']}")
        lines.append(f"\nSource: {DATASET}, read live. Stages are on that gauge's own local "
                     "datum, so they are not elevations above sea level and are not comparable "
                     "between gauges. A forecast is a forecast: the crest can move.")
        return "\n".join(lines), artifact

    def _find(self, params: dict) -> tuple[str, dict]:
        query = params.get("river") or params.get("gauge") or ""
        if not query:
            return ("Tell me which river, for example 'which gauges do you have for the "
                    "Willamette River?'.", {"summary": "no river given", "dataset": DATASET})
        matches = self.data.find_gauges(query, limit=10)
        artifact = {
            "summary": f"{DATASET}: {len(matches)} USGS site(s) matching {query!r}",
            "dataset": DATASET,
            "query": query,
            "sites": matches,
        }
        if not matches:
            return (f"USGS has no monitoring location matching {query!r}. Try a bigger river "
                    f"name, or a gauge id like EADM7.\n\nSource: {DATASET}, read live.",
                    {"summary": "no site matched", "dataset": DATASET})
        lines = [f"USGS monitoring locations matching {query!r}:"]
        for row in matches:
            mark = "" if row["usable"] else " (no NWPS forecast point)"
            where = ", ".join(part for part in (row.get("county"), row.get("state")) if part)
            lines.append(f"  • {row['site']} - {row['name']}"
                         + (f" [{where}]" if where else "") + mark)
        lines.append("\nAsk me for the level at any of those ids. "
                     f"Source: {DATASET}, read live.")
        return "\n".join(lines), artifact


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        FloodwatchAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
