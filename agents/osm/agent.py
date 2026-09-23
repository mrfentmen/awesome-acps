"""OSM - what is mapped near a place, straight out of OpenStreetMap.

Every ACP agent on the vendors' list writes code. This one answers 'nearest drinking water',
'how many EV chargers are within 2 km of here', 'is there a pharmacy near this park' - any
point on Earth, keyless, through Nominatim and Overpass. No other ACP agent has points of
interest at all.

Honest about the data: OpenStreetMap is volunteer-mapped. An empty answer means no mapper has
added that feature yet, not that it is absent, and every answer says so instead of implying
the place does not exist. Every answer also names the radius it searched and whether it hit
its own cap.
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
    MAX_EXAMPLES,
    OsmData,
    OsmError,
    kinds_from_text,
    radius_from_text,
)

HELP = (
    "I read OpenStreetMap, keyless (Nominatim for places, Overpass for what is mapped). Ask me:\n"
    "  - nearest drinking water to Bryant Park, New York\n"
    "  - EV chargers within 2 km of 40.7536,-73.9832\n"
    "  - pharmacies near Miami Beach\n"
    "  - playgrounds within 500 m of Central Park\n"
    "I map places, not opinions: OSM is volunteer-mapped, so an empty answer means nobody has "
    "mapped it yet, and I say that rather than pretending it is not there."
)

PERMISSION_KEY = "osm-read-public-openstreetmap"

SKILLS = ("osm-near", "osm-place", "help")

_COORDS_RE = re.compile(r"(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)")
_TRAILING_NOISE = (" right now", " now", " today", " near here", " nearby", " around here",
                   " at the moment", " currently", " please")


def coords_from_text(text: str) -> tuple[float, float] | None:
    """A latitude, longitude pair written in the question, or None."""
    match = _COORDS_RE.search(str(text or ""))
    if not match:
        return None
    latitude, longitude = float(match.group(1)), float(match.group(2))
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return None
    return latitude, longitude


#: A sentence end is punctuation followed by space or end - not the dot inside a coordinate.
_END = r"(?=[?.!](?:\s|$)|$)"

_PLACE_PATTERNS = (
    re.compile(r"\b(?:near|nearby|around|at|in|off|to|from|by)\s+(?:the\s+)?(.+?)"
               r"(?=\s+(?:right now|now|today|currently)\b|\s*[?.!](?:\s|$)|$)", re.IGNORECASE),
    re.compile(r"\b(?:within|inside)\s+[\d.]+\s*(?:km|kilomet(?:er|re)s?|m|meters?|metres?|mi|miles?)\s+"
               r"(?:of|from)\s+(?:the\s+)?(.+?)" + _END, re.IGNORECASE),
)

#: Words that mean the sentence is a question, not a place name.
_QUESTION_WORDS = re.compile(r"\b(what|how|why|when|where|who|which|help|tell|show|list|find|"
                             r"is|are|do|does|can|could|nearest|closest)\b", re.IGNORECASE)


def bare_place(text: str) -> str | None:
    """A place name given on its own, like 'Bryant Park, New York' or 'Tokyo'.

    Deliberately narrow: a comma or every word capitalised, no question words, few words.
    'pizza' and 'tell me a joke' are not place names and must not be sent to Nominatim.
    """
    work = str(text or "").strip().rstrip("?.!").strip()
    if not work or len(work) > 80 or _QUESTION_WORDS.search(work):
        return None
    words = work.split()
    if not 1 <= len(words) <= 5:
        return None
    if "," in work:
        return work
    if all(word[:1].isupper() for word in words if word[:1].isalpha()) and any(
            word[:1].isalpha() for word in words):
        return work
    return None


def _place_match(text: str):
    for pattern in _PLACE_PATTERNS:
        match = pattern.search(str(text or ""))
        if match:
            return match
    return None


def place_from_text(text: str) -> str | None:
    """The place a look-up question is about, or None."""
    match = _place_match(text)
    if not match:
        return None
    phrase = match.group(1).strip(" .,?!")
    phrase = re.sub(r"^(the|a|an)\s+", "", phrase, flags=re.IGNORECASE)
    for noise in _TRAILING_NOISE:
        if phrase.lower().endswith(noise):
            phrase = phrase[: -len(noise)]
    if not phrase or _COORDS_RE.fullmatch(phrase.strip()):
        return None
    return phrase.strip(" .,?!")[:80]


def without_place(text: str) -> str:
    """The question with the place name and any coordinates taken out.

    Feature words must be read from what is left: 'Bryant Park' would otherwise also register
    as a request for parks, and 'playgrounds within 500 m of Central Park' for both.
    """
    work = str(text or "")
    match = _place_match(work)
    if match:
        work = work[: match.start(1)] + " " + work[match.end(1):]
    return _COORDS_RE.sub(" ", work)


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    stripped = str(text or "").strip()
    if not stripped or re.search(r"\b(help|what can you do|commands?)\b", stripped, re.IGNORECASE):
        return "help", {}

    params: dict = {}
    kinds = kinds_from_text(without_place(stripped))
    if kinds:
        params["kinds"] = kinds
    radius = radius_from_text(stripped)
    if radius:
        params["radius_m"] = radius
    coords = coords_from_text(stripped)
    if coords:
        # Coordinates are the whole position; there is no place name to resolve as well.
        params["latitude"], params["longitude"] = coords
    else:
        place = place_from_text(stripped)
        if place:
            params["query"] = place

    if kinds and any(key in params for key in ("latitude", "query")):
        return "osm-near", params
    if not coords and not params.get("query"):
        # A place name on its own ('Bryant Park, New York') is a place look-up. Checked before
        # the feature words, or the 'Park' inside that name registers as a request for parks.
        alone = bare_place(stripped)
        if alone:
            return "osm-place", {"query": alone}
    if kinds:
        return "help", {}
    if coords or params.get("query"):
        # No feature named: resolve the place and say what can be asked about it.
        return "osm-place", params
    return "help", {}


def _tag_line(tags: dict) -> str:
    """The three or so tags worth printing after a name, in plain words."""
    if not tags:
        return ""
    bits = []
    for tag in ("opening_hours", "wheelchair", "fee", "operator", "brand", "cuisine"):
        if tag in tags:
            bits.append(f"{tag.replace('_', ' ')}: {tags[tag]}")
    address = " ".join(part for part in (tags.get("addr:housenumber"), tags.get("addr:street")) if part)
    if address:
        bits.append(f"at {address}")
    return f" ({'; '.join(bits[:3])})" if bits else ""


class OsmAgent(AcpAgent):
    name = "osm"
    title = "OSM - OpenStreetMap places and what is near them"
    version = "1.0.0"

    def __init__(self, connection=None, data: OsmData | None = None) -> None:
        super().__init__(connection)
        self.data = data or OsmData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "OSM session opened. " + HELP.splitlines()[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Turn the place into coordinates (Nominatim)", "medium"),
            ("Ask Overpass what OpenStreetMap has mapped there", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("I answer about what OpenStreetMap has mapped. An empty answer is a "
                        "mapping gap, not proof that the thing is absent.")
            return STOP_END_TURN

        tool = "call_overpass" if skill == "osm-near" else "call_nominatim"
        ctx.tool_call(tool, f"Ask OpenStreetMap ({skill.removeprefix('osm-')})", kind="fetch",
                      name=skill, raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow OSM to query OpenStreetMap's public services?",
                                  remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to query the public OpenStreetMap services first.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (OsmError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read OpenStreetMap: {exc}")
            return STOP_END_TURN
        ctx.check_cancelled()
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(artifact["summary"]))
        ctx.stream_text(answer)
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        return None  # every skill is a single lookup

    # -- skills ------------------------------------------------------------

    def _run_skill(self, skill: str, params: dict) -> tuple[str, dict]:
        if skill == "osm-near":
            return self._near(params)
        if skill == "osm-place":
            return self._place(params)
        return HELP, {"summary": "help", "dataset": None}

    def _origin(self, params: dict) -> dict:
        """Where the question is about: coordinates given, or a geocoded place name."""
        if params.get("latitude") is not None:
            return {"query": f"{params['latitude']}, {params['longitude']}",
                    "name": f"{params['latitude']}, {params['longitude']}",
                    "display_name": f"{params['latitude']}, {params['longitude']}",
                    "latitude": params["latitude"], "longitude": params["longitude"],
                    "kind": "coordinates"}
        if params.get("query"):
            return self.data.geocode(params["query"])
        raise ValueError("tell me where: a place like Bryant Park, New York, or coordinates "
                         "such as 40.7536,-73.9832")

    def _near(self, params: dict) -> tuple[str, dict]:
        origin = self._origin(params)
        radius = params.get("radius_m") or 800
        kinds = params["kinds"]
        blocks = []
        artifacts = []
        for key, value, label in kinds[:3]:
            result = self.data.pois(origin["latitude"], origin["longitude"], key, value, radius)
            artifacts.append({"kind": label, "key": key, "value": value, **{k: result[k] for k in
                              ("radius_m", "total", "capped", "pois")}})
            header = (f"{label.capitalize()} near {origin['name']} "
                      f"({origin['latitude']:.4f}, {origin['longitude']:.4f}) within "
                      f"{result['radius_m']} m:")
            if result["pois"]:
                lines = [header]
                for poi in result["pois"][:8]:
                    name = poi["name"] or "(unnamed on OpenStreetMap)"
                    lines.append(f"  - {poi['distance_km'] * 1000:.0f} m  {name}{_tag_line(poi['tags'])}")
                if result["capped"]:
                    lines.append(f"  ... OpenStreetMap returned at least {MAX_EXAMPLES} matches "
                                 f"(my cap), so there are more than shown.")
                lines.append(f"  OpenStreetMap has {result['total']} mapped within {result['radius_m']} m.")
            else:
                lines = [header,
                         "  - nothing mapped. An empty result means no OpenStreetMap volunteer "
                         f"has added a {label} there yet - it does not prove there is none."]
            blocks.append("\n".join(lines))

        first = artifacts[0]
        summary = (f"{DATASET}: {first['total']} {kinds[0][2]} mapped within {first['radius_m']} m "
                   f"of {origin['name']}")
        radii = sorted({block["radius_m"] for block in artifacts})
        lines = ["\n".join(blocks)]
        lines.append("")
        lines.append(f"Source: {DATASET}, read live. Distances are straight-line from the point "
                     f"asked about, inside a "
                     f"{', '.join(str(radius) + ' m' for radius in radii)} search radius. "
                     "OpenStreetMap is volunteer-mapped: missing means unmapped, not absent.")
        return "\n".join(lines), {"summary": summary, "dataset": DATASET, "origin": origin,
                                  "groups": artifacts}

    def _place(self, params: dict) -> tuple[str, dict]:
        origin = self._origin(params)
        artifact = {"summary": f"{DATASET}: {origin['display_name']} at "
                               f"{origin['latitude']:.4f}, {origin['longitude']:.4f}",
                    "dataset": DATASET, "origin": origin}
        lines = [f"{origin['display_name']}",
                 f"  - coordinates: {origin['latitude']:.5f}, {origin['longitude']:.5f}",
                 f"  - matched through Nominatim from {origin['query']!r}"]
        if origin.get("kind"):
            lines.append(f"  - OpenStreetMap types it as: {origin['kind']}")
        lines.append("")
        lines.append("I answered with the place itself because no feature was named. Ask for one "
                     "and I will look around it: nearest drinking water, EV chargers within 2 km, "
                     "pharmacies near here, playgrounds, ATMs, benches and more.")
        lines.append(f"Source: {DATASET}, geocoding, read live.")
        return "\n".join(lines), artifact


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        OsmAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
