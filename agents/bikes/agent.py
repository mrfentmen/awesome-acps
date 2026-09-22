"""Bikes — live Citi Bike availability inside your editor.

No ACP agent read a bike-share feed before this one. It joins the public GBFS station
information and station status files (keyless) and answers three things: what is at a
named station, what is nearest to a point, and what the system looks like right now.

Deterministic on purpose: routing is rules, the counts are the feed's, and nothing is
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

from data import DATASET, BikesData, BikesError  # noqa: E402

HELP = (
    "I read Citi Bike's public GBFS feeds (keyless). Ask me:\n"
    "  • how many citibikes are available right now?\n"
    "  • what is at the Grand Central station?\n"
    "  • nearest citibike stations to 40.71,-74.01?\n"
    "Citi Bike covers New York City, Jersey City and Hoboken; I do not speak for other systems."
)

PERMISSION_KEY = "bikes-read-public-gbfs"

SKILLS = ("bikes-station", "bikes-near", "bikes-status", "help")

_POINT_RE = re.compile(r"\b(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\b")
_STATUS_RE = re.compile(
    r"\b(how many|system|total|right now|available|availability|overall|status|everywhere|whole)\b",
    re.IGNORECASE)
_NEAR_RE = re.compile(r"\b(near|nearest|closest|around|close to)\b", re.IGNORECASE)
_AT_RE = re.compile(r"\b(?:at|from|station)\s+(.+)$", re.IGNORECASE)


def station_name_from_text(text: str) -> str | None:
    match = _AT_RE.search(text)
    if not match:
        return None
    name = match.group(1).strip(" .,?!'\"")
    name = re.sub(r"\b(?:station|please|right now|now|today)\b", "", name, flags=re.IGNORECASE).strip()
    name = re.sub(r"^(?:the|a|an)\s+", "", name, flags=re.IGNORECASE).strip()
    return name if len(name) >= 3 else None


def unknown_place_from_text(text: str) -> str | None:
    """A place the service area does not cover, so the answer can say so by name."""
    match = re.search(r"\b(?:in|at|near|around|to)\s+([A-Za-z][\w'-]*(?:\s+[A-Za-z][\w'-]*){0,2})", text)
    if not match:
        return None
    words = match.group(1).strip(" .,?!").split()
    while words and words[-1].lower() in {"now", "today", "right", "please", "the", "a", "an"}:
        words.pop()
    return " ".join(words).lower() if words else None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    params: dict = {}
    match = _POINT_RE.search(text)
    if match:
        try:
            params["point"] = BikesData.check_point(f"{match.group(1)},{match.group(2)}")
        except ValueError:
            pass
    area = BikesData.area_from_text(text)
    if area:
        params["place"] = area
        params.setdefault("point", BikesData.point_from_area(area))

    if _NEAR_RE.search(text) and ("point" in params or "place" in params):
        return "bikes-near", params
    name = station_name_from_text(text)
    if name:
        params["station"] = name
        return "bikes-station", params
    if "place" in params:
        return "bikes-near", params
    if _NEAR_RE.search(text):
        unknown = unknown_place_from_text(text)
        if unknown:
            params["place"] = unknown
            return "bikes-near", params
    if re.search(r"\b(in|around|within)\b", text, re.IGNORECASE) and not params.get("point"):
        unknown = unknown_place_from_text(text)
        if unknown:
            params["place"] = unknown
            return "bikes-near", params
    if _STATUS_RE.search(text):
        return "bikes-status", params
    return "bikes-status", params


class BikesAgent(AcpAgent):
    name = "bikes"
    title = "Bikes — Citi Bike live availability"
    version = "1.0.0"

    def __init__(self, connection=None, data: BikesData | None = None) -> None:
        super().__init__(connection)
        self.data = data or BikesData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Bikes session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Join the GBFS station information and status files", "medium"),
            ("Answer with the counts the feed reports", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("I read two public GBFS files: station information and station status. "
                        "The status file is cached for a minute so repeat questions do not "
                        "hammer a public API.")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read Citi Bike GBFS for {skill}", kind="fetch", name=skill,
                      raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Bikes to read Citi Bike's public GBFS feeds?",
                                 remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the public GBFS feed before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (BikesError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the GBFS feed: {exc}")
            return STOP_END_TURN
        ctx.check_cancelled()
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(artifact["summary"]))
        ctx.stream_text(answer)
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        # Every skill is one or two HTTP reads; nothing long-running to interrupt.
        return None

    # -- skills ------------------------------------------------------------

    def _run_skill(self, skill: str, params: dict) -> tuple[str, dict]:
        if skill == "bikes-station":
            return self._station(params)
        if skill == "bikes-near":
            return self._near(params)
        if skill == "bikes-status":
            return self._status()
        return HELP, {"summary": "help", "dataset": None}

    def _status(self) -> tuple[str, dict]:
        system = self.data.system()
        artifact = {
            "summary": f"{DATASET}: {system['bikes']} bikes, {system['ebikes']} e-bikes across "
                       f"{system['stations']} stations",
            "dataset": DATASET,
            "system": system["system"],
            "stations": system["stations"],
            "bikes": system["bikes"],
            "ebikes": system["ebikes"],
            "docks": system["docks"],
            "renting": system["renting"],
        }
        lines = [
            f"{system['system']} right now:",
            f"  • {system['bikes']:,} bikes available across {system['stations']:,} stations",
            f"  • {system['ebikes']:,} of those are e-bikes",
            f"  • {system['docks']:,} empty docks to return a bike",
            f"  • {system['renting']:,} stations are currently renting",
        ]
        lines.append(
            f"\nSource: {DATASET} (read live, cached up to a minute). Counts move by the "
            "second - ask about a station before you walk to it."
        )
        return "\n".join(lines), artifact

    def _station(self, params: dict) -> tuple[str, dict]:
        name = params.get("station")
        if not name:
            return (
                "Which station should I check? Give me a name like 'Grand Central' or 'Broadway "
                "and 42 St'.",
                {"summary": "no station given", "dataset": DATASET, "known": False},
            )
        result = self.data.station(name)
        station = result["station"]
        artifact = {
            "summary": f"{DATASET}: {station['name']} has {station['bikes']} bikes "
                       f"({station['ebikes']} e-bikes), {station['docks']} docks",
            "dataset": DATASET,
            "station": station["name"],
            "bikes": station["bikes"],
            "ebikes": station["ebikes"],
            "docks": station["docks"],
            "renting": station["renting"],
            "capacity": station["capacity"],
            "last_reported": station["last_reported"],
            "matches": result["match_count"],
        }
        lines = [
            f"{station['name']} (matched {result['match_count']} station(s)):",
            f"  • {station['bikes']} bikes available ({station['ebikes']} e-bikes), "
            f"capacity {station['capacity']}",
            f"  • {station['docks']} open docks",
            f"  • renting: {'yes' if station['renting'] else 'no'}",
            f"  • last reported by the operator: {station['last_reported']}",
        ]
        lines.append(
            f"\nSource: {DATASET} (read live). 'Last reported' is when the station last sent "
            "its status, so a stale station can already be empty."
        )
        return "\n".join(lines), artifact

    def _near(self, params: dict) -> tuple[str, dict]:
        point = params.get("point")
        if not point:
            if params.get("place"):
                return (
                    f"I do not know the place {params['place']!r}. Citi Bike covers New York "
                    "City, Jersey City and Hoboken - name those, or give me a point like "
                    "40.71,-74.01.",
                    {"summary": "unknown place", "dataset": DATASET, "known": False},
                )
            return (
                "I need a point to search around - a latitude/longitude pair like 40.71,-74.01, "
                "or one of Manhattan, Brooklyn, Queens, the Bronx, Jersey City, Hoboken.",
                {"summary": "no place given", "dataset": DATASET, "known": False},
            )
        result = self.data.nearby(point, limit=3)
        artifact = {
            "summary": f"{DATASET}: nearest stations to {result['point']}",
            "dataset": DATASET,
            "point": result["point"],
            "rows": [{"name": item["station"]["name"], "distance_km": item["distance_km"],
                      "bikes": item["station"]["bikes"], "ebikes": item["station"]["ebikes"],
                      "docks": item["station"]["docks"]} for item in result["rows"]],
        }
        lines = [f"Nearest Citi Bike stations to {result['point']}:"]
        for item in result["rows"]:
            station = item["station"]
            lines.append(f"  • {item['distance_km']:.2f} km - {station['name']}: "
                         f"{station['bikes']} bikes ({station['ebikes']} e-bikes), "
                         f"{station['docks']} docks")
        lines.append(
            f"\nSource: {DATASET} (read live). Distances are straight-line, not walking routes."
        )
        return "\n".join(lines), artifact


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        BikesAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
