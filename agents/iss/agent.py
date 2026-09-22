"""Iss — where the space station is, and whether your sky would show it.

The official ACP list has 39 coding agents and no one watching the sky. This one reads the
station's live ground position (open-notify), the local sunset (sunrise-sunset.org) and the
cloud cover (Open-Meteo), all keyless, and answers two questions: where is it, and could I
see it from here right now.

It does not predict passes, and says so - predicting passes needs orbital elements, not a
position feed. What it gives you is the position, the great-circle distance from your
point, whether it is dark, and how cloudy the sky is.
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
    DATASET_CLOUD,
    DATASET_ISS,
    DATASET_SUN,
    IssData,
    IssError,
    distance_km,
)

HELP = (
    "I read the space station's live position and your sky conditions (all keyless). Ask me:\n"
    "  • where is the ISS right now?\n"
    "  • is the ISS near 40.71,-74.01?\n"
    "  • could I see the ISS from Denver tonight?\n"
    "I show where it is now and what the sky looks like. I do not predict passes."
)

PERMISSION_KEY = "iss-read-public-spacefeeds"

SKILLS = ("iss-now", "iss-sky", "help")

_POINT_RE = re.compile(r"\b(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\b")
_SKY_WORDS = re.compile(
    r"\b(see|visible|visibility|tonight|sky|spot|watch|view|look up|clouds?|dark)\b", re.IGNORECASE)

#: Rough bands for the ground distance to the station (the orbit is ~420 km up).
OVERHEAD_KM = 500.0
SKY_REGION_KM = 2000.0

#: Metro centres, so a place name works without coordinates.
CITY_POINTS = {
    "amsterdam": (52.37, 4.90), "atlanta": (33.75, -84.39), "bangkok": (13.76, 100.50),
    "berlin": (52.52, 13.40), "boston": (42.36, -71.06), "chicago": (41.88, -87.63),
    "delhi": (28.61, 77.21), "denver": (39.74, -104.99), "london": (51.51, -0.13),
    "los angeles": (34.05, -118.24), "mexico city": (19.43, -99.13), "miami": (25.76, -80.19),
    "new york": (40.71, -74.01), "paris": (48.86, 2.35), "san francisco": (37.77, -122.42),
    "seattle": (47.61, -122.33), "sydney": (-33.87, 151.21), "tokyo": (35.68, 139.69),
    "toronto": (43.65, -79.38), "vancouver": (49.28, -123.12),
}


def band(distance: float) -> str:
    """A plain reading of the ground distance to the station."""
    if distance <= OVERHEAD_KM:
        return "close to overhead (within a few hundred km on the ground)"
    if distance <= SKY_REGION_KM:
        return "in your part of the sky, but not overhead"
    return "on the far side of the planet from you"


def place_point_from_text(text: str) -> tuple[str | None, str | None]:
    """(point, place) from a known city name, else (None, None)."""
    lowered = " " + re.sub(r"[^a-z ]+", " ", text.lower()).strip() + " "
    lowered = re.sub(r"\s+", " ", lowered)
    for name in sorted(CITY_POINTS, key=len, reverse=True):
        if f" {name} " in lowered:
            latitude, longitude = CITY_POINTS[name]
            return f"{latitude},{longitude}", name
    return None, None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    params: dict = {}
    match = _POINT_RE.search(text)
    if match:
        try:
            params["point"] = IssData.check_point(f"{match.group(1)},{match.group(2)}")
        except ValueError:
            pass
    point, place = place_point_from_text(text)
    if point and "point" not in params:
        params["point"] = point
    if place:
        params["place"] = place

    if _SKY_WORDS.search(text) and ("point" in params or "place" in params):
        return "iss-sky", params
    return "iss-now", params


class IssAgent(AcpAgent):
    name = "iss"
    title = "ISS — live position and sky conditions"
    version = "1.0.0"

    def __init__(self, connection=None, data: IssData | None = None) -> None:
        super().__init__(connection)
        self.data = data or IssData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "ISS session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read the station's live ground position", "medium"),
            ("Check the sky conditions where you are", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("I read three public feeds: the station's position, sunrise/sunset "
                        "times, and cloud cover. Position is live but pass prediction needs "
                        "orbital elements, which I do not have.")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read space-station feeds for {skill}", kind="fetch", name=skill,
                      raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow ISS to read the public station position and weather feeds?",
                                 remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the public station and weather feeds before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (IssError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the space-station feeds: {exc}")
            return STOP_END_TURN
        ctx.check_cancelled()
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(artifact["summary"]))
        ctx.stream_text(answer)
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        # Every skill is a few HTTP reads; nothing long-running to interrupt.
        return None

    # -- skills ------------------------------------------------------------

    def _run_skill(self, skill: str, params: dict) -> tuple[str, dict]:
        if skill == "iss-sky":
            return self._sky(params)
        if skill == "iss-now":
            return self._now(params)
        return HELP, {"summary": "help", "dataset": None}

    def _position_lines(self, position: dict) -> list[str]:
        return [
            f"The ISS is at {position['latitude']} lat, {position['longitude']} lon "
            f"(ground point {position['point']}).",
        ]

    def _now(self, params: dict) -> tuple[str, dict]:
        position = self.data.position()
        point = params.get("point")
        artifact = {
            "summary": f"{DATASET_ISS}: ISS at {position['point']}",
            "dataset": DATASET_ISS,
            "point": position["point"],
            "latitude": position["latitude"],
            "longitude": position["longitude"],
            "timestamp": position["timestamp"],
        }
        lines = self._position_lines(position)
        if point:
            try:
                location = IssData.check_point(point)
            except ValueError as exc:
                lines.append(str(exc) + ".")
            else:
                gap = distance_km(location, position["point"])
                artifact["from_point"] = location
                artifact["distance_km"] = round(gap)
                lines.append(f"From {location} it is about {gap:,.0f} km away on the ground - "
                             f"{band(gap)}.")
        else:
            lines.append("Give me a point (for example 40.71,-74.01, or a city name) and I will "
                         "measure the distance from you.")
        lines.append(
            f"\nSource: {DATASET_ISS} (read live). The station is ~420 km up and moves about "
            "7.7 km/s, so this position is already a little stale. Ask me 'could I see it from "
            "Denver tonight?' for the sky check."
        )
        return "\n".join(lines), artifact

    def _sky(self, params: dict) -> tuple[str, dict]:
        point = params.get("point")
        if not point:
            if params.get("place"):
                return (
                    f"I do not know the place {params['place']!r}, so I will not guess coordinates "
                    "for it. Give me a latitude/longitude point, or a city from my list (New York, "
                    "Denver, London, Sydney, Tokyo and so on).",
                    {"summary": "unknown place", "dataset": DATASET_ISS, "known": False},
                )
            return (
                "I need a point for the sky check - a latitude/longitude pair like 39.74,-104.99, "
                "or a city name from my list (New York, Denver, London, Sydney, Tokyo and so on).",
                {"summary": "no place given", "dataset": DATASET_ISS, "known": False},
            )

        position = self.data.position()
        sky = self.data.sky(point)
        gap = distance_km(sky["point"], position["point"])
        artifact = {
            "summary": f"{DATASET_ISS}: ISS at {position['point']}, {round(gap)} km from "
                       f"{sky['point']}, clouds {sky['cloud_cover']}%",
            "dataset": DATASET_ISS,
            "station_point": position["point"],
            "point": sky["point"],
            "distance_km": round(gap),
            "sunset": sky["sunset"],
            "sunrise": sky["sunrise"],
            "cloud_cover": sky["cloud_cover"],
            "cloud_dataset": sky["cloud_dataset"],
            "dark": sky["dark"],
            "evaluated_at": sky["evaluated_at"],
        }

        def shorten(stamp) -> str:
            return str(stamp).replace("+00:00", "Z") if stamp else "unknown"

        lines = [
            f"Sky check for {sky['point']} at {sky['evaluated_at']}:",
            f"  • the station is at {position['point']} - {gap:,.0f} km away, {band(gap)}",
            f"  • sunset {shorten(sky['sunset'])}, sunrise {shorten(sky['sunrise'])}",
            f"  • cloud cover now {sky['cloud_cover']}% ({DATASET_CLOUD})",
        ]
        if sky["dark"] is True:
            lines.append("  • it is dark where you are, so reflected sunlight could show it")
        elif sky["dark"] is False:
            lines.append("  • it is still daylight where you are, so the station is not lit up "
                         "against a dark sky")
        else:
            lines.append("  • I could not parse the local day/night state from the feed")
        if gap > SKY_REGION_KM:
            lines.append("  • it is nowhere near you at this moment, so no sighting now - "
                         "it circles the planet roughly every 90 minutes")
        if (sky["cloud_cover"] or 0) >= 60:
            lines.append("  • clouds are heavy enough to hide most of the sky")
        lines.append(
            f"\nSource: {DATASET_ISS}, {DATASET_SUN} and {DATASET_CLOUD} (read live). This is a "
            "now-check, not a pass forecast: a sighting needs the station to pass over your "
            "horizon while your sky is dark, and pass times move by minutes every day."
        )
        return "\n".join(lines), artifact


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        IssAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
