"""Flights - who is flying over a place right now, from live ADS-B positions.

"what planes are over me right now?" has an answer that is public, keyless and live, and no agent
in any editor answers it. This one geocodes the place, asks OpenSky for that box of sky, and
reports each aircraft the way a person asks about it: how far away, how high, how fast, which way
it is going, and whether it is climbing or descending.

It also says what the answer cannot be: these are positions broadcast by the aircraft themselves,
so an aircraft that is not broadcasting is simply absent, and an empty box is not proof of empty
sky. The 25 km default box is stated in every answer, because "over me" is not a fixed thing.
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
    DEFAULT_RADIUS_KM,
    FlightsData,
    FlightsError,
    compass,
)

HELP = (
    "I read OpenSky's live aircraft positions (keyless, ADS-B). Ask me:\n"
    "  - what planes are over Bryant Park, New York right now?\n"
    "  - who is flying over Denver?\n"
    "  - how many aircraft are within 50 km of Heathrow?\n"
    "  - what is aircraft a487ef?\n"
    "I answer with the nearest aircraft first: distance, altitude, speed, heading and whether it "
    "is climbing or descending, plus what OpenSky says is left of the day's allowance. These are "
    "positions broadcast by the aircraft, so one that is not broadcasting is absent - an empty "
    "answer is not an empty sky."
)

PERMISSION_KEY = "flights-read-opensky"

SKILLS = ("flights-overhead", "flights-aircraft", "flights-help")

#: A place, as people write it after 'over' / 'above' / 'near'.
_PLACE_IN = re.compile(
    r"\b(?:over|above|near|around|at|in|for)\s+(.+?)(?=\s+(?:right now|now|today|tonight|"
    r"currently|this (?:morning|evening))\b|[?.!]|$)", re.IGNORECASE)
#: Six digits, so a radius far too large is read and then capped rather than ignored.
_RADIUS = re.compile(r"\b(\d{1,6})\s*(?:km|kilomet(?:er|re)s?)\b", re.IGNORECASE)
_MILES = re.compile(r"\b(\d{1,6})\s*(?:mi|miles?)\b", re.IGNORECASE)
#: "within 50 km of Heathrow" names a place too, and the distance comes first.
_PLACE_DISTANCE = re.compile(
    r"\b\d[\d.,]*\s*(?:km|kilomet(?:er|re)s?|mi|miles?)\s+(?:of|from|around|near)\s+"
    r"(.+?)(?=[?.!]|$)", re.IGNORECASE)
_OVERHEAD = re.compile(r"\b(plane|planes|aircraft|flight|flights|flying|overhead|airplane|"
                       r"airplanes|jet|jets|helicopter|helicopters|skies|sky|airspace)\b",
                       re.IGNORECASE)
_SINGLE = re.compile(r"\b(?:aircraft|plane|flight|heli)\s+(?:with\s+(?:address|hex)\s+)?"
                     r"([0-9a-fA-F]{6})\b", re.IGNORECASE)
_BARE_HEX = re.compile(r"\b([0-9a-f]{6})\b")
_ASKED_HEX = re.compile(r"\b(?:what|which|where)\s+is\s+(?:aircraft\s+)?([0-9a-fA-F]{6})\b",
                        re.IGNORECASE)
_TRAILING = (" right now", " now", " today", " tonight", " currently", " please", " overhead",
             " above me", " above us", " over me")

#: Words that are not a place, so a question that has none of its own gets the help text.
_NOT_PLACE = {"me", "us", "here", "there", "it", "them", "my house", "the sky", "this place"}


def place_from_text(text: str) -> str | None:
    """The place a question is about, or None when it names none it can use."""
    work = str(text or "").strip()
    match = _PLACE_DISTANCE.search(work) or _PLACE_IN.search(work)
    if not match:
        return None
    phrase = match.group(1).strip(" .,?!")
    for noise in _TRAILING:
        if phrase.lower().endswith(noise):
            phrase = phrase[: -len(noise)]
    for noise in (" within ", " with "):
        if noise in phrase.lower():
            phrase = phrase[: phrase.lower().index(noise)]
    return None if phrase.strip().lower() in _NOT_PLACE else phrase.strip() or None


def radius_from_text(text: str) -> float | None:
    """A radius in km, in kilometres or in miles, or None when none was named."""
    kilometers = _RADIUS.search(str(text or ""))
    if kilometers:
        return max(1.0, min(float(kilometers.group(1)), 500.0))
    miles = _MILES.search(str(text or ""))
    if miles:
        return max(1.0, min(float(miles.group(1)) * 1.60934, 500.0))
    return None


def address_from_text(text: str) -> str | None:
    """The 24-bit address a question names, or None. Only hex, never a flight number."""
    work = str(text or "")
    found = _SINGLE.search(work) or _ASKED_HEX.search(work)
    if found:
        return found.group(1).lower()
    if _OVERHEAD.search(work):
        return None
    bare = _BARE_HEX.search(work)
    return bare.group(1).lower() if bare else None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    stripped = str(text or "").strip()
    if not stripped or re.search(r"\b(help|what can you do|commands?)\b", stripped, re.IGNORECASE):
        return "flights-help", {}
    place = place_from_text(stripped)
    address = address_from_text(stripped)
    if address and not place:
        return "flights-aircraft", {"address": address}
    if place:
        params: dict = {"place": place}
        radius = radius_from_text(stripped)
        if radius:
            params["radius_km"] = radius
        return "flights-overhead", params
    if address:
        return "flights-aircraft", {"address": address}
    return "flights-help", {}


def meters(value, unit: str = "m") -> str:
    """An altitude as metres and feet, because both are used out loud."""
    if not isinstance(value, (int, float)):
        return "not reported"
    feet = float(value) * 3.28084
    return f"{round(float(value)):,} {unit} ({round(feet):,} ft)"


def speed(value) -> str:
    """A ground speed in m/s as km/h and knots."""
    if not isinstance(value, (int, float)):
        return "not reported"
    return (f"{round(float(value) * 3.6)} km/h ({round(float(value) * 1.94384)} kt)")


def rate(value) -> str:
    """A vertical rate as climbing, descending or level."""
    if not isinstance(value, (int, float)):
        return "not reported"
    if value > 0.5:
        return f"climbing {value:.1f} m/s"
    if value < -0.5:
        return f"descending {abs(value):.1f} m/s"
    return "level"


def age(seconds) -> str:
    if not isinstance(seconds, (int, float)):
        return "unknown"
    return f"{seconds:.0f}s old"


def box_text(box: dict) -> str:
    """A bounding box in words that stay true south of the equator.

    "-33.9 to -33.4 N" is nonsense, and 'N'/'E' labels are easy to bolt on and hard to keep
    honest, so the numbers are printed as they are and only the axes are named.
    """
    return (f"lat {box['lamin']:g} to {box['lamax']:g}, lon {box['lomin']:g} to "
            f"{box['lomax']:g}")


def render_aircraft(entry: dict, with_distance: bool = True) -> list[str]:
    """One aircraft as the lines a person reads, never as a raw row."""
    name = entry["callsign"] or "(no callsign broadcast)"
    where = entry["country"] or "unknown country"
    if with_distance and entry["distance_km"] is not None:
        where += f", {entry['distance_km']} km away"
    lines = [f"  - {name}  [{entry['icao24']}]  {where}"]
    if entry["on_ground"]:
        lines.append("      on the ground")
    else:
        climb = f", {rate(entry['vertical_rate_ms'])}" if entry["vertical_rate_ms"] \
            is not None else ""
        # OpenSky's alt_itude field is the barometric one, which is why a jet on a ramp can read
        # below sea level. Naming it is cheaper than explaining a negative height later.
        lines.append(f"      altitude {meters(entry['altitude_m'])} barometric{climb}")
    heading = (f", heading {round(float(entry['track']))} deg ({compass(entry['track'])})"
               if isinstance(entry["track"], (int, float)) else "")
    lines.append(f"      speed {speed(entry['speed_ms'])}{heading}")
    contact = (f", contact {age(entry['last_contact_age_s'])}"
               if entry["last_contact_age_s"] is not None else "")
    lines.append(f"      position {age(entry['time_position_age_s'])} at the snapshot{contact}")
    if entry["squawk"]:
        lines.append(f"      squawk {entry['squawk']}")
    return lines


class FlightsAgent(AcpAgent):
    name = "flights"
    title = "Flights - live aircraft over a place"
    version = "1.0.0"

    def __init__(self, connection=None, data: FlightsData | None = None) -> None:
        super().__init__(connection)
        self.data = data or FlightsData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Flights session opened. " + HELP.splitlines()[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Find the place, then read the live aircraft in that box", "medium"),
            ("Report each aircraft's distance, height, speed and heading", "medium"),
        ])

        if skill == "flights-help":
            ctx.stream_text(HELP)
            ctx.message("\nNothing is read until you name a place or an aircraft.")
            return STOP_END_TURN

        tool = f"call_{skill.replace('-', '_')}"
        asking = (f"aircraft over {params['place']}" if skill == "flights-overhead"
                  else f"aircraft {params['address']}")
        ctx.tool_call(tool, f"Read OpenSky for {asking}", kind="fetch", name=skill,
                      raw_input=dict(params))
        if not ctx.ask_permission(tool, "Allow Flights to read OpenSky's live positions?",
                                  remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read OpenSky first.")
            return STOP_REFUSAL
        ctx.tool_call_update(tool, status="in_progress")

        try:
            if skill == "flights-aircraft":
                body, summary = self._aircraft(params), f"one address: {params['address']}"
            else:
                body, summary = self._overhead(params)
        except FlightsError as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read that: {exc}")
            return STOP_END_TURN

        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(summary))
        ctx.stream_text(body + "\n")
        ctx.message(f"\nSource: {DATASET}, read live. These are positions broadcast by the "
                    f"aircraft themselves, so an aircraft that is not broadcasting is absent - an "
                    f"empty answer is not an empty sky.")
        return STOP_END_TURN

    # -- the two answers ---------------------------------------------------

    def _overhead(self, params: dict) -> tuple[str, str]:
        radius = float(params.get("radius_km") or DEFAULT_RADIUS_KM)
        reading = self.data.overhead(params["place"], radius_km=radius)
        place = reading["place"]
        box = place["box"]
        lines = [f"Aircraft within {radius:g} km of {place['name']} "
                 f"({place['display_name']}):"]
        lines.append(f"  - {reading['found']} in the box ({reading['airborne']} in the air, "
                     f"{reading['on_ground']} on the ground)")
        lines.append(f"  - box: {box_text(box)}, centred on {place['latitude']:.4f}, "
                     f"{place['longitude']:.4f}")
        if reading["nearest_km"] is not None:
            lines.append(f"  - nearest is {reading['nearest_km']} km away")
        if reading["highest_m"] is not None:
            lines.append(f"  - highest in the air: {meters(reading['highest_m'])}")
        if not reading["aircraft"]:
            lines.append("  - no aircraft are broadcasting positions in this box right now")
        else:
            lines.append(f"  - in the air first, then on the ground; nearest first within each "
                         f"(showing {len(reading['aircraft'])} of {reading['found']}):")
            for entry in reading["aircraft"]:
                lines.extend(render_aircraft(entry))
        if reading["remaining"] is not None:
            lines.append(f"  - OpenSky's anonymous allowance left today: {reading['remaining']} "
                         f"credits (a bigger box costs more)")
        return "\n".join(lines), f"{reading['found']} aircraft in {radius:g} km"

    def _aircraft(self, params: dict) -> str:
        reading = self.data.aircraft(params["address"])
        lines = [f"OpenSky's record for 24-bit address {reading['address']}:"]
        if reading["aircraft"] is None:
            lines.append("  - no aircraft with that address is broadcasting a position right now.")
            lines.append("  - that is as much as this API can say: it looks up by address, and a "
                         "missing address and a silent aircraft look the same here.")
        else:
            lines.extend(render_aircraft(reading["aircraft"], with_distance=False))
        if reading["remaining"] is not None:
            lines.append(f"  - OpenSky's anonymous allowance left today: {reading['remaining']}")
        return "\n".join(lines)

    def on_cancel(self, session) -> None:
        return None  # one snapshot, one answer


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        FlightsAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
