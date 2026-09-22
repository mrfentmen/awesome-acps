"""Aurora — NOAA space weather inside your editor.

The first space-weather agent in any editor protocol: it answers how the geomagnetic
field is behaving right now, what NOAA predicts for the next days, whether the aurora
could be overhead at a place you name, and what the Space Weather Prediction Center has
actually issued — straight from SWPC's own keyless products.

Deterministic on purpose: routing is rules, every number is NOAA's, and nothing is
guessed. It reports a plan, opens one tool call per lookup, asks permission before the
first read, streams the answer, and closes the tool call with a one-line summary of the
product it read.

The aurora odds come from NOAA's OVATION model for the point you asked about — never from
a made-up latitude rule — and every visibility answer says that the model knows nothing
about clouds.
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
    CITY_COORDS,
    DATASET_ALERTS,
    DATASET_FORECAST,
    DATASET_KP_1M,
    DATASET_OVATION,
    SpaceWeatherData,
    SpaceWeatherError,
    kp_band,
)

HELP = (
    "I read NOAA's Space Weather Prediction Center products.\n"
    "Ask me:\n"
    "  • how are the geomagnetic conditions right now?\n"
    "  • what is the aurora forecast for the next 3 days?\n"
    "  • what are the odds of seeing the aurora in Fairbanks tonight?\n"
    "  • what has NOAA said about geomagnetic storms lately?\n"
    "Every answer names the SWPC product it came from. I read the agency's data; I do not "
    "forecast space weather myself."
)

PERMISSION_KEY = "aurora-read-public-swpc"

SKILLS = ("aurora-now", "aurora-forecast", "aurora-visibility", "aurora-messages", "help")

_POINT_RE = re.compile(r"(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)")
_FORECAST_WORDS = re.compile(
    r"\b(forecast|tonight|tomorrow|next \d+ days?|next few days|this week|coming days|expected|predict(?:ed|ion)?)\b",
    re.IGNORECASE)
_VISIBILITY_WORDS = re.compile(r"\b(see|seeing|visible|visibility|view|odds|chance|probabilit(?:y|ies)|overhead)\b",
                               re.IGNORECASE)
_MESSAGE_WORDS = re.compile(
    r"\b(messages?|warnings?|watches|advisory|advisories|bulletins?|alerts?|news|issued|said|noaa say)\b",
    re.IGNORECASE)
_NOW_WORDS = re.compile(r"\b(now|right now|current(?:ly)?|today|conditions?|at the moment)\b", re.IGNORECASE)

#: Words that mean a captured "in ..." phrase is not a place at all.
_STOP_PLACE_WORDS = frozenset({
    "the", "a", "an", "my", "our", "your", "this", "that", "these", "those", "next", "last",
    "here", "there", "today", "tomorrow", "tonight", "now", "right", "few", "some", "hours",
    "hour", "days", "day", "week", "weeks", "morning", "afternoon", "evening", "general",
})
_PLACE_PHRASE_RE = re.compile(r"\b(?:in|at|near|for)\s+([A-Za-z][\w'-]*(?:\s+[A-Za-z][\w'-]*){0,2})")


def unknown_place_from_text(text: str) -> str | None:
    """A place name the built-in list does not know, so the answer can say so by name."""
    match = _PLACE_PHRASE_RE.search(text)
    if not match:
        return None
    words = match.group(1).strip(" .,?!").split()
    while words and words[-1].lower() in _STOP_PLACE_WORDS:
        words.pop()
    if not words or words[0].lower() in _STOP_PLACE_WORDS:
        return None
    return " ".join(words).lower()
_KEYWORDS = {"storm": "storm", "geomagnetic": "storm", "flare": "flare", "x-ray": "flare",
             "radiation": "radiation", "blackout": "blackout", "proton": "proton",
             "electron": "electron", "cme": "CME"}


def place_from_text(text: str) -> str | None:
    """A city name from the built-in list. Longest names win ('new york' over 'york')."""
    lowered = " " + re.sub(r"[^a-z ]+", " ", text.lower()).strip() + " "
    for name in sorted(CITY_COORDS, key=len, reverse=True):
        if f" {name} " in re.sub(r"\s+", " ", lowered):
            return name
    return None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    params: dict = {}
    match = _POINT_RE.search(text)
    if match:
        params["point"] = f"{match.group(1)},{match.group(2)}"
    place = place_from_text(text)
    if place:
        params["place"] = place
    elif not params.get("point") and _VISIBILITY_WORDS.search(text):
        # A named place we do not have coordinates for - answer that plainly instead of guessing.
        unknown = unknown_place_from_text(text)
        if unknown:
            params["place"] = unknown
    contains = next((value for word, value in _KEYWORDS.items()
                     if re.search(rf"\b{re.escape(word)}s?\b", text.lower())), None)

    asked_for_forecast = bool(_FORECAST_WORDS.search(text))
    asked_for_visibility = bool(_VISIBILITY_WORDS.search(text))
    asked_for_messages = bool(_MESSAGE_WORDS.search(text))

    if params.get("point") or params.get("place"):
        return "aurora-visibility", params
    if asked_for_forecast:
        params["days"] = 3
        return "aurora-forecast", params
    # A keyword like "storm" only means "search the message feed" when the question is not
    # about the here and now - "how are conditions right now?" also mentions storm words.
    if asked_for_messages or (contains and not _NOW_WORDS.search(text)):
        if contains:
            params["contains"] = contains
        params["limit"] = 5
        return "aurora-messages", params
    if asked_for_visibility:
        return "aurora-visibility", params
    return "aurora-now", params


def _storm_phrase(kp: float | None) -> str:
    if kp is None:
        return "not reported"
    band = kp_band(kp)
    return f"Kp {kp:g} - {band} on NOAA's storm scale" if kp >= 5 else f"Kp {kp:g} ({band})"


class AuroraAgent(AcpAgent):
    name = "aurora"
    title = "Aurora — NOAA space weather"
    version = "1.0.0"

    def __init__(self, connection=None, data: SpaceWeatherData | None = None) -> None:
        super().__init__(connection)
        self.data = data or SpaceWeatherData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Aurora session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read NOAA's own SWPC product", "medium"),
            ("Answer with the product, its run time and the units", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("SWPC publishes the Kp index every minute, the forecast continuously, and "
                        "the aurora probability grid every few minutes.")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read NOAA SWPC for {skill}", kind="fetch", name=skill,
                      raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Aurora to read NOAA's public space weather data?",
                                 remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read NOAA's public data before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (SpaceWeatherError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the NOAA product: {exc}")
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
        if skill == "aurora-forecast":
            return self._forecast(params)
        if skill == "aurora-visibility":
            return self._visibility(params)
        if skill == "aurora-messages":
            return self._messages(params)
        if skill == "aurora-now":
            return self._now()
        return HELP, {"summary": "help", "dataset": None}

    def _now(self) -> tuple[str, dict]:
        recent = self.data.recent(hours=24)
        now = recent["now"]
        latest = self.data.messages(limit=1)["messages"]
        newest = latest[0] if latest else None
        artifact = {
            "summary": f"{DATASET_KP_1M}: estimated Kp {now.get('estimated_kp')} ({now['band']}) "
                       f"as of {now.get('time_tag')}",
            "dataset": DATASET_KP_1M,
            "also_read": [recent["dataset"], DATASET_ALERTS],
            "time_tag": now.get("time_tag"),
            "estimated_kp": now.get("estimated_kp"),
            "band": now.get("band"),
            "storm": now.get("storm"),
            "three_hourly": now.get("three_hourly"),
            "peak_kp_24h": recent["peak_kp"],
            "peak_band_24h": recent["peak_band"],
            "peak_time_tag_24h": recent["peak_time_tag"],
            "latest_message": (
                {"code": newest["code"], "kind": newest["kind"],
                 "issue_datetime": newest["issue_datetime"], "headline": newest["headline"]}
                if newest else None
            ),
        }
        kp = now.get("estimated_kp")
        lines = [
            f"Geomagnetic conditions right now: estimated Kp {kp:g} ({now['band']})"
            + (" - a geomagnetic storm is in progress." if now["storm"] else "."),
            f"  • Peak over the last {recent['window_hours']} hours: "
            f"{_storm_phrase(recent['peak_kp'])} at {recent['peak_time_tag']}",
            f"  • Latest 3-hourly value: Kp {now['three_hourly'].get('kp')} at "
            f"{now['three_hourly'].get('time_tag')} (a-index {now['three_hourly'].get('a_running')}, "
            f"{now['three_hourly'].get('station_count')} stations)",
        ]
        if newest:
            lines.append(f"  • Newest SWPC message: {newest['kind'] or 'MESSAGE'} {newest['code'] or ''} "
                         f"({newest['issue_datetime']}) - {newest['headline']}")
        lines.append(
            "\nSource: swpc.noaa.gov (the 1-minute Kp estimate and the 3-hourly planetary Kp, read "
            "live). Kp is a 0-9 planet-wide index: 5 and above is NOAA's storm scale, G1 (minor) "
            "through G5 (extreme). It describes the geomagnetic field, not what you would see above "
            "your own head - ask me about aurora odds at a place for that."
        )
        return "\n".join(lines), artifact

    def _forecast(self, params: dict) -> tuple[str, dict]:
        forecast = self.data.forecast(days=int(params.get("days") or 3))
        storms = [item for item in self.data.messages(limit=20, contains="storm")["messages"]
                  if (item["kind"] or "").upper() in ("WATCH", "WARNING", "EXTENDED WARNING", "ALERT")]
        artifact = {
            "summary": f"{DATASET_FORECAST}: {len(forecast['days'])} day(s), peak "
                       f"{_storm_phrase(forecast['peak_kp'])}",
            "dataset": DATASET_FORECAST,
            "also_read": [DATASET_ALERTS],
            "days": forecast["days"],
            "peak_kp": forecast["peak_kp"],
            "peak_band": forecast["peak_band"],
            "predicted_rows": forecast["predicted_rows"],
            "storm_messages": [
                {"code": item["code"], "kind": item["kind"], "issue_datetime": item["issue_datetime"],
                 "headline": item["headline"], "g_scale": item["g_scale"]}
                for item in storms[:3]
            ],
        }
        if not forecast["days"]:
            return ("NOAA's Kp forecast file contained no predicted rows, so there is nothing to show "
                    f"yet.\n\nSource: {DATASET_FORECAST}.", artifact)
        lines = ["NOAA's predicted planetary Kp, peak per UTC day (read live):"]
        for entry in forecast["days"]:
            lines.append(f"  • {entry['date']}: peak Kp {entry['max_kp']:g} ({entry['band']})"
                         + (" - storm level" if entry["storm"] else ""))
        lines.append(f"\nHighest expected in this window: {_storm_phrase(forecast['peak_kp'])}.")
        if storms:
            lines.append("SWPC storm messages in force:")
            for item in storms[:2]:
                lines.append(f"  • {item['kind']} {item['code'] or ''} - {item['headline']}")
        else:
            lines.append("No storm watch or warning is in force in the message feed right now.")
        lines.append("\nSource: swpc.noaa.gov (the 3-hourly Kp forecast file, which mixes observed and "
                     "predicted rows - only the predicted ones are shown). Forecast confidence drops "
                     "after about a day, and NOAA revises these numbers as the solar wind arrives.")
        return "\n".join(lines), artifact

    def _visibility(self, params: dict) -> tuple[str, dict]:
        found = None
        if params.get("point"):
            lat, lon = self.data.check_point(params["point"])
            found = (lat, lon, f"the point {lat},{lon}")
        elif params.get("place"):
            found = SpaceWeatherData.city(params["place"])
        if not found:
            return (
                f"I do not know the place {params.get('place')!r}, so I will not guess coordinates for "
                "it. Give me a latitude/longitude point, or a city from my list (Fairbanks, Tromso, "
                "Reykjavik, Edinburgh, Minneapolis, Toronto, Sydney, Ushuaia and so on).",
                {"summary": "unknown place", "dataset": DATASET_OVATION, "known": False},
            )
        lat, lon, label = found
        probability = self.data.aurora_probability(lat, lon)
        recent = self.data.recent(hours=24)
        now = recent["now"]
        artifact = {
            "summary": f"{DATASET_OVATION}: {probability['probability']}% at {label} "
                       f"(model run {probability.get('observation_time')})",
            "dataset": DATASET_OVATION,
            "also_read": [DATASET_KP_1M, recent["dataset"]],
            "place": label,
            "latitude": lat,
            "longitude": lon,
            "probability_percent": probability["probability"],
            "max_probability_nearby_percent": probability["max_probability_nearby"],
            "radius_degrees": probability["radius_degrees"],
            "nearest_grid_cell": probability["nearest_cell"],
            "model_observation_time": probability.get("observation_time"),
            "model_forecast_time": probability.get("forecast_time"),
            "estimated_kp": now.get("estimated_kp"),
            "band": now.get("band"),
            "peak_kp_24h": recent["peak_kp"],
        }
        chance = probability["probability"]
        nearby = probability["max_probability_nearby"]
        lines = [
            f"Aurora probability overhead at {label} ({lat},{lon}): {chance}%"
            + (f", and up to {nearby}% within {probability['radius_degrees']:g} degrees of it."
               if nearby != chance else "."),
            f"  • Model run: observed {probability.get('observation_time')}, "
            f"forecast {probability.get('forecast_time')}",
            f"  • Geomagnetic activity behind it: estimated Kp {now.get('estimated_kp'):g} "
            f"({now['band']}), 24-hour peak Kp {recent['peak_kp']:g} ({recent['peak_band']})",
        ]
        if chance >= 50:
            lines.append("  • That is a strong signal from the model - worth going outside if the sky is clear.")
        elif chance >= 20:
            lines.append("  • A real chance, but you would want dark sky and a clear horizon to the north.")
        else:
            lines.append("  • Low odds right now; the oval is not reaching this latitude.")
        lines.append(
            f"\nSource: {DATASET_OVATION} (NOAA's OVATION model, read live). It gives the probability "
            "of aurora overhead at a point - a model run, not a sighting - and it says nothing about "
            "clouds, so check a weather forecast too. The coordinates for named cities come from this "
            "agent's own list, not from NOAA; pass latitude,longitude for an exact point."
        )
        return "\n".join(lines), artifact

    def _messages(self, params: dict) -> tuple[str, dict]:
        result = self.data.messages(limit=int(params.get("limit") or 5), contains=params.get("contains"))
        messages = [
            {
                "product_id": item["product_id"],
                "issue_datetime": item["issue_datetime"],
                "code": item["code"],
                "serial": item["serial"],
                "kind": item["kind"],
                "g_scale": item["g_scale"],
                "headline": item["headline"],
                "summary": item["text"].strip().replace("\r\n", " ")[:1000],
            }
            for item in result["messages"]
        ]
        artifact = {
            "summary": f"{DATASET_ALERTS}: {len(messages)} message(s)"
                       + (f" matching {params['contains']!r}" if params.get("contains") else ""),
            "dataset": DATASET_ALERTS,
            "count": len(messages),
            "filter": params.get("contains"),
            "messages": messages,
        }
        if not messages:
            what = f" containing {params['contains']!r}" if params.get("contains") else ""
            return (f"NOAA's message feed has no recent SWPC messages{what}.\n\nSource: {DATASET_ALERTS}.",
                    artifact)
        lines = [f"{len(messages)} recent SWPC message(s)"
                 + (f" matching {params['contains']!r}" if params.get("contains") else "") + ", newest first:"]
        for item in messages[:5]:
            lines.append(f"  • [{item['kind'] or 'MESSAGE'}] {item['code'] or ''} #{item['serial'] or ''} "
                         f"({item['issue_datetime']}) - {item['headline']}")
        lines.append(f"\nSource: {DATASET_ALERTS} (read live from swpc.noaa.gov). Product codes identify "
                     "the series - WATA20 is a geomagnetic storm watch, ALTXMF an X-ray flux alert - and "
                     "NOAA's own wording is kept in the artifact summary.")
        return "\n".join(lines), artifact


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        AuroraAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
