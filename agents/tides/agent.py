"""Tides - NOAA's tide predictions and live gauge readings inside your editor.

Every ACP agent on the vendors' list writes code. This one reads the water: when the next high
tide is, what the gauge says right now, and which station covers a stretch of coast - straight
from NOAA's Tides and Currents network, keyless. A different shelf from the `surf` agent here
(Open-Meteo models wave height in open water) and from `buoys` (NDBC's offshore buoys): this is
the shore gauge a harbour master and a fisherman actually read.

Deterministic on purpose: routing is rules, the numbers and the times are NOAA's own, a
prediction is never presented as an observation, and a station with no live sensor is told
apart from one that is quiet. It streams the answer, opens one tool call per lookup, and asks
permission before its first read.
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
    DATUM,
    DATUM_NOTE,
    STATE_CODES,
    STATE_NAMES,
    TidesData,
    TidesError,
    place_words,
    station_id,
    state_from_words,
)

HELP = (
    "I read NOAA Tides and Currents (keyless). Ask me:\n"
    "  • when is the next high tide in Boston?\n"
    "  • what is the tide at The Battery right now?\n"
    "  • the tide table for Portland, Maine\n"
    "  • what tide stations are near Miami?\n"
    "  • tides in Florida\n"
    "I report NOAA's own predictions and the gauge's own reading, both in station local time, "
    f"heights above {DATUM}, and I say 'prediction' when a number is a prediction."
)

PERMISSION_KEY = "tides-read-public-noaa"

SKILLS = ("tide-next", "tide-now", "tide-stations", "help")

_NOW_WORDS = re.compile(r"\b(right now|currently|current|observed|observation|how high|how low|"
                        r"what is the level|level now|actual)\b", re.IGNORECASE)
_NEXT_WORDS = re.compile(r"\b(next|when|upcoming|high tide|low tide|hi/lo|hilo|tide table|table|"
                         r"today|tomorrow|tonight|this week)\b", re.IGNORECASE)
_STATION_WORDS = re.compile(r"\b(stations?|gauges?|gages?)\b", re.IGNORECASE)

_COORDS_RE = re.compile(r"(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)")
_TRAILING_NOISE = (" right now", " now", " today", " tonight", " tomorrow", " at the moment",
                   " currently")


def coords_from_text(text: str) -> tuple[float, float] | None:
    """A latitude, longitude pair written in the question, or None."""
    match = _COORDS_RE.search(str(text or ""))
    if not match:
        return None
    latitude, longitude = float(match.group(1)), float(match.group(2))
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return None
    return latitude, longitude


def place_from_text(text: str) -> str | None:
    """The place a tide question is about, or None."""
    work = str(text or "").strip()
    patterns = (
        r"\b(?:at|in|for|near|off|around|from)\s+(?:the\s+)?(.+?)(?=\s+(?:right now|now|today|tonight|tomorrow|currently)\b|[?.!,]|$)",
        r"\b(?:tide|tides|tide table|water level|level)\s+(?:for|at|in|near|off)\s+(?:the\s+)?(.+?)(?=[?.!,]|$)",
        r"\bnext\s+(?:high|low)\s+tide\s+(?:in|at|near|off)\s+(?:the\s+)?(.+?)(?=[?.!,]|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, work, re.IGNORECASE)
        if not match:
            continue
        phrase = match.group(1).strip(" .,?!")
        phrase = re.sub(r"^(the|a|an)\s+", "", phrase, flags=re.IGNORECASE)
        for noise in _TRAILING_NOISE:
            if phrase.lower().endswith(noise):
                phrase = phrase[: -len(noise)]
        words = place_words(phrase)
        if words:
            return " ".join(words[:4])
    return None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    params: dict = {}
    try:
        params["station"] = station_id(text)
    except ValueError:
        pass
    coords = coords_from_text(text)
    if coords:
        params["latitude"], params["longitude"] = coords
    place = place_from_text(text)
    if place:
        params["query"] = place

    # A state on its own ('tides in Florida') covers hundreds of stations: list them instead
    # of picking one at random and calling it Florida's tide.
    state, leftover = state_from_words(place_words(text))
    if state and not leftover and not params.get("station"):
        params["state"] = state
        params.pop("query", None)
        return "tide-stations", params

    # 'which tide stations ... near Miami' is a lookup; 'the next high tide in Boston' is not.
    if _STATION_WORDS.search(text) and not params.get("station"):
        return "tide-stations", params
    if re.search(r"\bhigh(?:s|\s+tides?)?\b", text, re.IGNORECASE):
        params["kind"] = "high"
    elif re.search(r"\blow(?:s|\s+tides?)?\b", text, re.IGNORECASE):
        params["kind"] = "low"
    if _NOW_WORDS.search(text):
        params.pop("kind", None)  # 'how high is it now' is a reading, not a hi/lo request
        return "tide-now", params
    if _NEXT_WORDS.search(text) or params.get("station") or place or coords:
        return "tide-next", params
    return "help", {}


def _number(value, digits: int = 2) -> str:
    """Two decimals without trailing padding: 10.30 -> '10.3', 0.42 -> '0.42'."""
    if value is None:
        return "not available"
    text = f"{value:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _where(row: dict) -> str:
    bits = [row.get("name") or "unnamed"]
    if row.get("state"):
        bits.append(row["state"])
    return ", ".join(bits)


class TidesAgent(AcpAgent):
    name = "tides"
    title = "Tides - NOAA tide predictions and live gauges"
    version = "1.0.0"

    def __init__(self, connection=None, data: TidesData | None = None) -> None:
        super().__init__(connection)
        self.data = data or TidesData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Tides session opened. " + HELP.splitlines()[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Find the NOAA station", "medium"),
            ("Read the gauge reading and NOAA's own hi/lo predictions", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("I read two products: the gauge's live water level and NOAA's harmonic "
                        "hi/lo predictions. I never call a prediction an observation.")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read NOAA {skill.removeprefix('tide-')}", kind="fetch",
                      name=skill, raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Tides to read NOAA's public tide feeds?",
                                  remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the public NOAA feeds before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (TidesError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the NOAA feed: {exc}")
            return STOP_END_TURN
        ctx.check_cancelled()
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(artifact["summary"]))
        ctx.stream_text(answer)
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        return None  # every skill is a single read

    # -- skills ------------------------------------------------------------

    def _run_skill(self, skill: str, params: dict) -> tuple[str, dict]:
        if skill == "tide-next":
            return self._next(params)
        if skill == "tide-now":
            return self._now(params)
        if skill == "tide-stations":
            return self._stations(params)
        return HELP, {"summary": "help", "dataset": None}

    def _resolve(self, params: dict) -> tuple[dict | None, tuple[str, dict] | None]:
        """(station row, None) or (None, an answer to send instead)."""
        if params.get("station"):
            try:
                return self.data.station(params["station"]), None
            except ValueError as exc:
                return None, (f"{exc}.", {"summary": "bad station", "dataset": DATASET})
        if params.get("latitude") is not None:
            matches = self.data.nearest(params["latitude"], params["longitude"], limit=1)
            if matches:
                return matches[0], None
            return None, ("NOAA has no tide station near that point.",
                          {"summary": "no station nearby", "dataset": DATASET})
        if params.get("query"):
            matches = self.data.find(params["query"])
            if matches:
                return matches[0], None
            state, left = state_from_words(place_words(params["query"]))
            where = f" in {state}" if state and not left else ""
            return None, (
                f"I found no NOAA tide station matching {params['query']!r}{where}. Try the "
                "station's own name as NOAA writes it (The Battery, Portland, Boston), a state "
                "like Florida, or a seven digit station id such as 8518750.",
                {"summary": "no station matched", "dataset": DATASET})
        return None, (
            "I need a station: a place like Boston or The Battery, a state like Florida, a "
            "coordinate pair such as 25.77,-80.13, or a station id like 8518750.",
            {"summary": "no station given", "dataset": DATASET})

    def _table(self, result: dict, events: list[dict], count: int) -> list[str]:
        """The reading line and the hi/lo lines, shared by both tide answers."""
        lines = []
        reading = result["observed"]
        if reading:
            lines.append(f"  - right now: {_number(reading['height_ft'])} ft above {DATUM} "
                         f"(the gauge read {reading['time_local']} station local time)")
        else:
            lines.append("  - right now: this station publishes no live water level, so there is "
                         "nothing observed to show")
        if not events:
            lines.append("  - no hi/lo events came back for the next window")
            return lines
        label = "next" if result["anchored"] else "predicted"
        for event in events[:count]:
            lines.append(f"  - {label} {'HIGH' if event['kind'] == 'high' else 'LOW ':<4} "
                         f"{_number(event['height_ft'])} ft at {event['time_local']}")
        return lines

    def _next(self, params: dict) -> tuple[str, dict]:
        station, ask = self._resolve(params)
        if ask:
            return ask
        result = self.data.tide(station["id"], count=8)
        # When the question names high or low, answer that, not the interleaved table.
        wanted = params.get("kind")
        events = [event for event in result["upcoming"] if event["kind"] == wanted] if wanted else []
        events = events or result["upcoming"]
        artifact = {
            "summary": f"{DATASET}: {station['name']} ({station['id']}), "
                       f"{len(events)} upcoming {'hi' if not wanted else wanted} events",
            "dataset": DATASET,
            "station": station,
            "observed": result["observed"],
            "kind": wanted,
            "upcoming": events,
            "anchored": result["anchored"],
        }
        subject = f"the next {wanted} tide" if wanted else "the tides"
        lines = [f"{subject.capitalize()} for {_where(station)} ({station['id']}):"]
        lines.extend(self._table(result, events, 4))
        lines.append("")
        if result["anchored"]:
            lines.append(f"Times are the station's own local clock and the gauge's latest reading "
                         f"({result['observed']['time_local']}) is what 'next' is measured from.")
        else:
            lines.append("This station has no live gauge, so 'next' cannot be anchored to now - "
                         "the events listed are the first NOAA predicts in the window, in station "
                         "local time.")
        lines.append(f"Heights are in feet above {DATUM_NOTE}. These are harmonic predictions: "
                     f"where the water will be, not where it is. Source: {DATASET}, read live.")
        return "\n".join(lines), artifact

    def _now(self, params: dict) -> tuple[str, dict]:
        station, ask = self._resolve(params)
        if ask:
            return ask
        result = self.data.tide(station["id"], count=3)
        reading = result["observed"]
        artifact = {
            "summary": f"{DATASET}: {station['name']} ({station['id']}) read "
                       f"{_number(reading['height_ft']) if reading else 'nothing'} ft",
            "dataset": DATASET,
            "station": station,
            "observed": reading,
            "upcoming": result["upcoming"],
        }
        lines = [f"{_where(station)} ({station['id']}):"]
        if reading:
            lines.append(f"  - the gauge reads {_number(reading['height_ft'])} ft above {DATUM} at "
                         f"{reading['time_local']} station local time")
            if reading["quality"]:
                flag = "preliminary" if reading["quality"] == "p" else "verified"
                lines.append(f"  - quality flag: {flag} (NOAA's own {reading['quality']!r}), "
                             f"sigma {_number(reading['sigma_ft'])} ft")
        else:
            lines.append("  - this station publishes no live water level; NOAA offers predictions "
                         "for it but no observed reading")
        lines.extend(self._table(result, result["upcoming"], 3)[1:])
        lines.append("")
        lines.append(f"Source: {DATASET}, read live. Heights are feet above {DATUM_NOTE}, times "
                     "are the station's local clock.")
        return "\n".join(lines), artifact

    def _stations(self, params: dict) -> tuple[str, dict]:
        query = params.get("query") or ""
        rows: list[dict]
        title: str
        total = None
        if params.get("state"):
            code = params["state"]
            rows = self.data.by_state(code)
            total = len(rows)
            if rows:
                title = (f"NOAA lists {total} tide stations in {STATE_NAMES.get(code, code)}. "
                         f"The first few by name:")
            else:
                return (f"NOAA lists no tide station in {STATE_NAMES.get(code, code)}.",
                        {"summary": "no station in state", "dataset": DATASET})
        elif params.get("latitude") is not None:
            rows = self.data.nearest(params["latitude"], params["longitude"], limit=6)
            title = f"Tide stations nearest {params['latitude']}, {params['longitude']}:"
        elif query:
            rows = self.data.find(query, limit=6)
            title = f"NOAA tide stations matching {query!r}:"
        else:
            return ("Tell me a state (tides in Florida), a place (stations near Miami), or a "
                    "point (25.77,-80.13).",
                    {"summary": "no query", "dataset": DATASET})
        artifact = {"summary": f"{DATASET}: {len(rows)} station(s)",
                    "dataset": DATASET, "query": query, "state": params.get("state"),
                    "total_in_state": total, "stations": rows}
        if not rows:
            return (f"No NOAA tide station matches {query!r}. Try a state name like Florida or a "
                    "place like Miami.",
                    {"summary": "no station matched", "dataset": DATASET})
        lines = [title]
        for row in rows[:6]:
            where = f" ({row['state']})" if row.get("state") else ""
            distance = f", {row['distance_km']} km away" if row.get("distance_km") is not None else ""
            coords = ""
            if row.get("latitude") is not None and row.get("longitude") is not None:
                coords = f" at {row['latitude']:.3f}, {row['longitude']:.3f}"
            lines.append(f"  - {row['id']} {row['name']}{where}{coords}{distance}")
        if total and total > len(rows[:6]):
            lines.append(f"  ... and {total - len(rows[:6])} more in that state.")
        lines.append("")
        lines.append("Ask for the tide at any of those ids, by name, or from that coordinate. "
                     f"Source: {DATASET}, station list, read live.")
        return "\n".join(lines), artifact


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        TidesAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
