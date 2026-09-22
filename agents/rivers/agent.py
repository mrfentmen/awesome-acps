"""Rivers — USGS stream gauges inside your editor.

No ACP agent reads a river. This one answers "how high is the water, is it rising, and
which gauge is nearest" from USGS's new OGC water API - the same feed flood forecasters
and paddlers read - with the gauge's own units and the observation timestamp.

Two honest limits are built into every answer: a reading is what one gauge saw at one
time, not a flood forecast; and USGS's legacy NWIS endpoint is answering 503 as of
2026-09-22, so this agent talks to the new API only.

Deterministic on purpose: routing is rules, the numbers are USGS's, and nothing is
guessed. It reports a plan, opens one tool call per lookup, asks permission before its
first read, streams the answer, and closes the tool call with a one-line summary.
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
    DATASET_LATEST,
    DATASET_SERIES,
    DATASET_SITES,
    PARAMETERS,
    RiversData,
    RiversError,
)

HELP = (
    "I read USGS stream gauges (the new OGC water API, keyless). Ask me:\n"
    "  • what is the Colorado River doing at gauge 09380000 right now?\n"
    "  • has gauge 06730500 been rising in the last 24 hours?\n"
    "  • what gauges are near 40.71,-74.01?\n"
    "I report what one gauge measured at one time. I am not a flood forecast - for those, "
    "read the National Weather Service."
)

PERMISSION_KEY = "rivers-read-public-usgs"

SKILLS = ("river-stage", "river-recent", "river-near", "help")

_POINT_RE = re.compile(r"\b(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\b")
_SITE_RE = re.compile(r"\b(?:USGS-)?(\d{8,15})\b")
_WINDOW_RE = re.compile(r"\b(?:last|past|previous|over the last)\s+(\d{1,3})\s*(hours?|days?)\b", re.IGNORECASE)
_RISE_WORDS = re.compile(r"\b(rising|falling|trend|change|changed|been|last|past|history|series|graph|higher|lower)",
                         re.IGNORECASE)
_NEAR_WORDS = re.compile(r"\b(near|nearby|closest|around|within|gauges|stations|sites|map)\b", re.IGNORECASE)
_PARAMETER_WORDS = {
    "temperature": "00010", "temp": "00010",
    "height": "00065", "level": "00065", "stage": "00065", "depth": "00065",
    "discharge": "00060", "flow": "00060", "streamflow": "00060", "cfs": "00060",
}


def site_from_text(text: str) -> str | None:
    match = _SITE_RE.search(text)
    return f"USGS-{match.group(1)}" if match else None


def hours_from_text(text: str) -> int:
    match = _WINDOW_RE.search(text)
    if not match:
        return 24
    amount = int(match.group(1))
    hours = amount * 24 if match.group(2).lower().startswith("day") else amount
    return hours if 1 <= hours <= 720 else 24


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    params: dict = {}
    site = site_from_text(text)
    if site:
        params["site"] = site
    point_match = _POINT_RE.search(text)
    if point_match:
        try:
            params["point"] = RiversData.check_point(f"{point_match.group(1)},{point_match.group(2)}")
        except ValueError:
            pass
    for word, code in _PARAMETER_WORDS.items():
        if re.search(rf"\b{word}\b", text, re.IGNORECASE):
            params["parameter"] = code
            break
    radius = re.search(r"\b(\d{1,3})\s*(?:km|kilometers?|kilometres?)\b", text, re.IGNORECASE)
    if radius:
        try:
            params["radius_km"] = RiversData.check_radius_km(radius.group(1))
        except ValueError:
            pass

    if "site" not in params:
        # No gauge named: a point (or a question about nearby gauges) means "find me one".
        if "point" in params or _NEAR_WORDS.search(text):
            return "river-near", params
        return "river-stage", params
    # A gauge was named. A question about time gets the series; otherwise the latest readings.
    wants_series = bool(_RISE_WORDS.search(text)) or bool(re.search(r"\b(hours?|days?)\b", text, re.IGNORECASE))
    if wants_series:
        params["hours"] = hours_from_text(text)
        params.setdefault("parameter", "00060")
        return "river-recent", params
    return "river-stage", params


class RiversAgent(AcpAgent):
    name = "rivers"
    title = "Rivers — USGS stream gauges"
    version = "1.0.0"

    def __init__(self, connection=None, data: RiversData | None = None) -> None:
        super().__init__(connection)
        self.data = data or RiversData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Rivers session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Find the gauge or the nearest gauges", "medium"),
            ("Read USGS and report the value with its unit and time", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("One product family: USGS water data through the new OGC API. Their old "
                        "NWIS endpoint answers 503 as of 2026-09-22, which is why this agent uses "
                        "the OGC collections.")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read USGS water data for {skill}", kind="fetch", name=skill,
                      raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Rivers to read USGS's public water data?",
                                 remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read USGS's public water data before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (RiversError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the USGS water feed: {exc}")
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
        if skill == "river-recent":
            return self._recent(params)
        if skill == "river-near":
            return self._near(params)
        if skill == "river-stage":
            return self._stage(params)
        return HELP, {"summary": "help", "dataset": None}

    @staticmethod
    def _need_site(params: dict) -> str | None:
        return params.get("site") or None

    def _stage(self, params: dict) -> tuple[str, dict]:
        site = self._need_site(params)
        if not site:
            return (
                "I need a USGS gauge number - an 8 to 15 digit site id like 06730500 (the Boulder "
                "Creek gauge at Boulder, Colorado). Ask me for gauges near a latitude/longitude "
                "point if you do not have one, or read one from a USGS station page URL.",
                {"summary": "no gauge given", "dataset": DATASET_LATEST, "known": False},
            )
        latest = self.data.latest(site)
        artifact = {
            "summary": f"{DATASET_LATEST}: {len(latest['observations'])} current reading(s) at "
                       f"{latest['site']}",
            "dataset": DATASET_LATEST,
            "site": latest["site"],
            "site_name": latest["site_name"],
            "observed_at": latest["observed_at"],
            "observations": latest["observations"],
            "units": "USGS publishes each parameter in its own unit; they are passed through as-is",
        }
        lines = [f"Latest USGS readings at {latest['site']}"
                 + (f" - {latest['site_name']}" if latest["site_name"] else "") + ":"]
        for item in latest["observations"]:
            lines.append(f"  • {item['parameter_name']} ({item['parameter_code']}): "
                         f"{item['value']} {item['unit'] or ''} at {item['time']}")
        lines.append(f"\nNewest timestamp in this payload: {latest['observed_at']}. USGS gauges "
                     "report at their own intervals (often every 15 minutes), so two parameters "
                     "can carry different times.")
        lines.append(f"\nSource: {DATASET_LATEST} (read live). One gauge, one moment - not a "
                     "flood forecast. Blank or missing values mean the gauge did not report.")
        return "\n".join(lines), artifact

    def _recent(self, params: dict) -> tuple[str, dict]:
        site = self._need_site(params)
        if not site:
            return (
                "I need a USGS gauge number to look up a series - for example 06730500. Tell me the "
                "gauge and how many hours back you want.",
                {"summary": "no gauge given", "dataset": DATASET_SERIES, "known": False},
            )
        hours = int(params.get("hours") or 24)
        parameter = params.get("parameter") or "00060"
        series = self.data.series(site, hours=hours, parameter=parameter)
        artifact = {
            "summary": f"{DATASET_SERIES}: {series['count']} reading(s) of {series['parameter_name']} "
                       f"at {series['site']} over {hours}h, last {series['last']['value']} "
                       f"{series['unit']} ({'rising' if series['rising'] else 'falling'})",
            "dataset": DATASET_SERIES,
            "site": series["site"],
            "site_name": series["site_name"],
            "parameter_code": series["parameter_code"],
            "parameter_name": series["parameter_name"],
            "unit": series["unit"],
            "window_hours": series["window_hours"],
            "count": series["count"],
            "first": series["first"],
            "last": series["last"],
            "min": series["min"],
            "max": series["max"],
            "mean": series["mean"],
            "change": series["change"],
            "rising": series["rising"],
            "rows": series["rows"],
        }
        direction = "rising" if series["rising"] else "falling" if series["change"] < 0 else "flat"
        lines = [
            f"{series['parameter_name'].capitalize()} at {series['site']}"
            + (f" - {series['site_name']}" if series["site_name"] else "")
            + f", last {series['window_hours']} hours:",
            f"  • {series['count']} readings, from {series['first']['value']} {series['unit']} at "
            f"{series['first']['time']}",
            f"  • to {series['last']['value']} {series['unit']} at {series['last']['time']}",
            f"  • Range {series['min']} to {series['max']} {series['unit']}, mean {series['mean']}",
            f"  • Change over the window: {series['change']:+g} {series['unit']} ({direction})",
        ]
        lines.append(f"\nSource: {DATASET_SERIES} (read live, newest first, then sorted oldest to "
                     "newest for the maths). Trend over a day says nothing about tomorrow - rain "
                     "upstream can change it faster than the gauge reports.")
        return "\n".join(lines), artifact

    def _near(self, params: dict) -> tuple[str, dict]:
        point = params.get("point")
        if not point:
            return (
                "I need a latitude/longitude point to search around - for example 40.71,-74.01. "
                "Or give me a gauge number directly if you already have one.",
                {"summary": "no point given", "dataset": DATASET_SITES, "known": False},
            )
        radius = float(params.get("radius_km") or 25)
        result = self.data.sites_near(point, radius_km=radius)
        artifact = {
            "summary": f"{DATASET_SITES}: {len(result['sites'])} station(s) within {radius:g} km of "
                       f"{result['point']}",
            "dataset": DATASET_SITES,
            "point": result["point"],
            "radius_km": radius,
            "matched": result["matched"],
            "sites": result["sites"],
        }
        if not result["sites"]:
            return (
                f"USGS has no monitoring locations in the box around {result['point']} "
                f"({radius:g} km). Try a wider radius, or a point nearer a river.\n\nSource: {DATASET_SITES}.",
                artifact,
            )
        lines = [f"USGS monitoring locations within about {radius:g} km of {result['point']}:"]
        for site in result["sites"]:
            distance = f"{site['distance_km']} km" if site["distance_km"] is not None else "distance unknown"
            lines.append(f"  • {site['site']} - {site['name']} ({distance})"
                         + (f", {site['county']}" if site.get("county") else "")
                         + (f" [{site['site_type']}]" if site.get("site_type") else ""))
        lines.append(f"\nSource: {DATASET_SITES} (USGS station list, read live). {result['matched']} "
                     "site(s) matched in the search box; ask me for the latest reading at any id "
                     "above and I will read it.")
        return "\n".join(lines), artifact


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        RiversAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
