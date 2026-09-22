"""Air — air quality inside your editor.

Every ACP agent on the vendors' list writes code. This one answers the question you ask
when you open a window in August: what am I breathing, and when does it get worse. It
reads Open-Meteo's CAMS air quality feed (keyless) and answers with the provider's own
numbers, the EPA category they fall in, and the hour the model says the peak arrives.

Deterministic on purpose: routing is rules, the numbers are the feed's, and nothing is
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

from data import DATASET, AirQualityData, AirQualityError, aqi_band, pm25_band  # noqa: E402

HELP = (
    "I read Open-Meteo's air quality feed (CAMS model output, keyless). Ask me:\n"
    "  • how is the air quality in Delhi right now?\n"
    "  • what is the pm2.5 at 40.71,-74.01?\n"
    "  • when does the air get worse today in Phoenix?\n"
    "Answers carry the provider's numbers, the EPA category, and the hour of the peak. "
    "I read a model, not a monitor, and I am not medical advice."
)

PERMISSION_KEY = "air-read-public-airquality"

SKILLS = ("air-now", "air-hours", "help")

_POINT_RE = re.compile(r"\b(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\b")
_HOURS_WORDS = re.compile(
    r"\b(later|today|tonight|tomorrow|next|hours?|forecast|outlook|get(?:s)? worse|improve|"
    r"peak|when|trend|rest of)\b", re.IGNORECASE)

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


def hours_from_text(text: str) -> int | None:
    match = re.search(r"\b(?:next|in|over)\s+(\d{1,2})\s*hours?\b", text, re.IGNORECASE)
    if match:
        try:
            return AirQualityData.check_hours(match.group(1))
        except ValueError:
            return None
    return None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    params: dict = {}
    match = _POINT_RE.search(text)
    if match:
        try:
            params["point"] = AirQualityData.check_point(f"{match.group(1)},{match.group(2)}")
        except ValueError:
            pass
    place = AirQualityData.place_from_text(text)
    if place:
        params["place"] = place
        params.setdefault("point", AirQualityData.point_from_place(place))
    elif not params.get("point"):
        # A named place we do not have coordinates for - answer that plainly instead of guessing.
        unknown = unknown_place_from_text(text)
        if unknown:
            params["place"] = unknown
    hours = hours_from_text(text)
    if hours:
        params["hours"] = hours

    if _HOURS_WORDS.search(text) and "point" in params:
        params.setdefault("hours", 24)
        return "air-hours", params
    if _HOURS_WORDS.search(text) and "place" in params:
        params.setdefault("hours", 24)
        return "air-hours", params
    if _HOURS_WORDS.search(text):
        return "air-hours", params
    return "air-now", params


class AirAgent(AcpAgent):
    name = "air"
    title = "Air — Open-Meteo air quality"
    version = "1.0.0"

    def __init__(self, connection=None, data: AirQualityData | None = None) -> None:
        super().__init__(connection)
        self.data = data or AirQualityData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Air session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Resolve the place to a latitude/longitude point", "medium"),
            ("Read the air quality feed and label the numbers", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("I read one product: Open-Meteo's air quality API, which carries CAMS "
                        "model output for any point on Earth. It is a model, so a monitor down "
                        "the road can disagree with it.")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read Open-Meteo air quality for {skill}", kind="fetch", name=skill,
                      raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Air to read Open-Meteo's public air quality data?",
                                 remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the public air quality feed before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (AirQualityError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the air quality feed: {exc}")
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
        if skill == "air-hours":
            return self._hours(params)
        if skill == "air-now":
            return self._now(params)
        return HELP, {"summary": "help", "dataset": None}

    def _point_or_ask(self, params: dict) -> tuple[str | None, tuple[str, dict] | None]:
        """The point to read, or the answer to send when we do not know the place."""
        if params.get("point"):
            try:
                return AirQualityData.check_point(params["point"]), None
            except ValueError as exc:
                return None, (f"{exc}.", {"summary": "bad point", "dataset": DATASET})
        if params.get("place"):
            return None, (
                f"I do not know the place {params['place']!r}, so I will not guess coordinates for it. "
                "Give me a latitude/longitude point, or a city from my list (New York, Delhi, Beijing, "
                "Lagos, Sao Paulo, Sydney and so on).",
                {"summary": "unknown place", "dataset": DATASET, "known": False},
            )
        return None, (
            "I need a place to read the air quality for - a latitude/longitude point like "
            "40.71,-74.01, or a city name from my list (New York, Delhi, Beijing, Nairobi, "
            "Sao Paulo, Sydney and so on).",
            {"summary": "no place given", "dataset": DATASET, "known": False},
        )

    def _now(self, params: dict) -> tuple[str, dict]:
        point, ask = self._point_or_ask(params)
        if ask:
            return ask
        current = self.data.current(point)
        reading = current["current"]

        def value(field: str) -> str:
            number = reading.get(field)
            return "not reported" if number is None else f"{number:g}"

        artifact = {
            "summary": f"{DATASET}: US AQI {reading.get('us_aqi')} ({current['us_aqi_band']}), "
                       f"PM2.5 {reading.get('pm2_5')} ug/m3 at {current['point']}",
            "dataset": DATASET,
            "point": current["point"],
            "time": current["time"],
            "us_aqi": reading.get("us_aqi"),
            "us_aqi_band": current["us_aqi_band"],
            "european_aqi": reading.get("european_aqi"),
            "pm2_5": reading.get("pm2_5"),
            "pm2_5_band": current["pm2_5_band"],
            "pm10": reading.get("pm10"),
            "ozone": reading.get("ozone"),
            "nitrogen_dioxide": reading.get("nitrogen_dioxide"),
            "sulphur_dioxide": reading.get("sulphur_dioxide"),
            "carbon_monoxide": reading.get("carbon_monoxide"),
            "uv_index": reading.get("uv_index"),
        }
        lines = [
            f"Air quality at {current['point']} as of {current['time']} UTC:",
            f"  • US AQI {value('us_aqi')} - {current['us_aqi_band']} "
            f"(European AQI {value('european_aqi')}, a different scale)",
            f"  • PM2.5 {value('pm2_5')} ug/m3 - {current['pm2_5_band']}; PM10 {value('pm10')} ug/m3",
            f"  • Ozone {value('ozone')} ug/m3, NO2 {value('nitrogen_dioxide')} ug/m3, "
            f"SO2 {value('sulphur_dioxide')} ug/m3, CO {value('carbon_monoxide')} ug/m3",
            f"  • UV index {value('uv_index')}",
        ]
        aqi = reading.get("us_aqi")
        if aqi is not None and aqi > 100:
            lines.append("  • Above 100: sensitive groups should cut down long outdoor effort.")
        elif aqi is not None and aqi > 50:
            lines.append("  • Moderate: fine for most people; unusual symptoms are possible.")
        lines.append(
            f"\nSource: {DATASET} (CAMS model output through Open-Meteo, read live). It is a "
            "model on a ~11 km grid, not the monitor on your roof, and the AQI bands are the "
            "US EPA categories. Ask me about the next hours for the peak."
        )
        return "\n".join(lines), artifact

    def _hours(self, params: dict) -> tuple[str, dict]:
        point, ask = self._point_or_ask(params)
        if ask:
            return ask
        window = int(params.get("hours") or 24)
        hourly = self.data.hourly(point, window)
        artifact = {
            "summary": f"{DATASET}: {window}h at {hourly['point']}, peak PM2.5 "
                       f"{hourly['peak_pm2_5']} at {hourly['peak_pm2_5_time']}, worst AQI "
                       f"{hourly['worst_aqi']}",
            "dataset": DATASET,
            "point": hourly["point"],
            "window_hours": hourly["window_hours"],
            "peak_pm2_5": hourly["peak_pm2_5"],
            "peak_pm2_5_time": hourly["peak_pm2_5_time"],
            "worst_aqi": hourly["worst_aqi"],
            "hours_above_aqi_100": hourly["hours_above_aqi_100"],
            "first_dirty_hour": hourly["first_dirty_hour"],
            "rows": hourly["rows"],
        }
        lines = [f"PM2.5 and US AQI for the next {hourly['window_hours']} hours at "
                 f"{hourly['point']} (UTC):"]
        for row in hourly["rows"][:12]:
            aqi = row.get("us_aqi")
            lines.append(f"  • {row['time']}: PM2.5 {row.get('pm2_5')} ug/m3, "
                         f"AQI {aqi if aqi is not None else 'n/a'}"
                         + (f" - {aqi_band(aqi)}" if aqi is not None and aqi > 50 else ""))
        if len(hourly["rows"]) > 12:
            lines.append(f"  • ... {len(hourly['rows']) - 12} more hours in the artifact")
        lines.append(f"\nPeak PM2.5 in the window: {hourly['peak_pm2_5']} ug/m3 at "
                     f"{hourly['peak_pm2_5_time']} ({pm25_band(hourly['peak_pm2_5'])}).")
        if hourly["hours_above_aqi_100"]:
            lines.append(f"{hourly['hours_above_aqi_100']} hour(s) above AQI 100, first at "
                         f"{hourly['first_dirty_hour']}.")
        else:
            lines.append("No hour in the window goes above AQI 100.")
        lines.append(
            f"\nSource: {DATASET} (hourly CAMS model output through Open-Meteo, read live). "
            "Model output, not a measurement, and smoke or dust can move faster than the model."
        )
        return "\n".join(lines), artifact


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        AirAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
