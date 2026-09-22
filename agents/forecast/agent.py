"""Forecast — weather and rain windows inside your editor.

The ACP agent list is all coding agents; this one answers "when is it going to rain, and
how long do I have" without leaving the editor. It reads Open-Meteo (keyless) and turns
the hourly probability series into rain windows - the hours the model actually expects
rain - instead of repeating a single daily percentage.

Deterministic on purpose: routing is rules, the numbers are the model's, and nothing is
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

from data import DATASET, ForecastData, ForecastError  # noqa: E402

HELP = (
    "I read Open-Meteo's weather forecast (keyless). Ask me:\n"
    "  • what is the weather in London right now?\n"
    "  • when will it rain in Seattle today?\n"
    "  • what does the week look like in Nairobi?\n"
    "The rain answer is the useful one: I turn the hourly probabilities into the actual "
    "hours rain is expected, with the peak percentage and the millimetres. I read a model, "
    "not a station - and I am not a severe weather warning service."
)

PERMISSION_KEY = "forecast-read-public-weather"

SKILLS = ("weather-now", "weather-rain", "weather-daily", "help")

_POINT_RE = re.compile(r"\b(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\b")
_RAIN_WORDS = re.compile(r"\b(rain|rains|raining|showers?|drizzle|downpour|precipitation|wet|umbrella|storm)",
                         re.IGNORECASE)
_DAILY_WORDS = re.compile(r"\b(week|forecast|outlook|days?|tomorrow|weekend|next \d+ days?)", re.IGNORECASE)
_HOUR_WINDOW_RE = re.compile(r"\b(?:next|in|over|within)\s+(\d{1,2})\s*hours?\b", re.IGNORECASE)
_DAY_WINDOW_RE = re.compile(r"\b(?:next|in|over|for)\s+(\d{1,2})\s*days?\b", re.IGNORECASE)

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


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    params: dict = {}
    match = _POINT_RE.search(text)
    if match:
        try:
            params["point"] = ForecastData.check_point(f"{match.group(1)},{match.group(2)}")
        except ValueError:
            pass
    place = ForecastData.place_from_text(text)
    if place:
        params["place"] = place
        params.setdefault("point", ForecastData.point_from_place(place))
    elif not params.get("point"):
        unknown = unknown_place_from_text(text)
        if unknown:
            params["place"] = unknown

    hours = _HOUR_WINDOW_RE.search(text)
    if hours:
        try:
            params["hours"] = ForecastData.check_hours(hours.group(1))
        except ValueError:
            pass
    days = _DAY_WINDOW_RE.search(text)
    if days:
        try:
            params["days"] = ForecastData.check_days(days.group(1))
        except ValueError:
            pass

    if _RAIN_WORDS.search(text):
        params.setdefault("hours", 24)
        return "weather-rain", params
    if _DAILY_WORDS.search(text) or "days" in params:
        params.setdefault("days", 3)
        return "weather-daily", params
    return "weather-now", params


class ForecastAgent(AcpAgent):
    name = "forecast"
    title = "Forecast — Open-Meteo weather and rain windows"
    version = "1.0.0"

    def __init__(self, connection=None, data: ForecastData | None = None) -> None:
        super().__init__(connection)
        self.data = data or ForecastData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Forecast session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Resolve the place to a latitude/longitude point", "medium"),
            ("Read the forecast and turn it into plain hours", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("One product: Open-Meteo's forecast API. It is model output for any "
                        "point on Earth, which is why decisions about real weather still belong "
                        "with your local weather service.")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read Open-Meteo forecast for {skill}", kind="fetch", name=skill,
                      raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Forecast to read Open-Meteo's public weather data?",
                                 remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the public weather feed before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (ForecastError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the weather feed: {exc}")
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
        if skill == "weather-rain":
            return self._rain(params)
        if skill == "weather-daily":
            return self._daily(params)
        if skill == "weather-now":
            return self._now(params)
        return HELP, {"summary": "help", "dataset": None}

    def _point_or_ask(self, params: dict) -> tuple[str | None, tuple[str, dict] | None]:
        if params.get("point"):
            try:
                return ForecastData.check_point(params["point"]), None
            except ValueError as exc:
                return None, (f"{exc}.", {"summary": "bad point", "dataset": DATASET})
        if params.get("place"):
            return None, (
                f"I do not know the place {params['place']!r}, so I will not guess coordinates for it. "
                "Give me a latitude/longitude point, or a city from my list (London, New York, Seattle, "
                "Nairobi, Tokyo, Sydney and so on).",
                {"summary": "unknown place", "dataset": DATASET, "known": False},
            )
        return None, (
            "I need a place for the forecast - a latitude/longitude point like 51.51,-0.13, or a "
            "city name from my list (London, New York, Seattle, Nairobi, Tokyo, Sydney and so on).",
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
            "summary": f"{DATASET}: {current['weather']}, {reading.get('temperature_2m')} C at "
                       f"{current['point']}",
            "dataset": DATASET,
            "point": current["point"],
            "time": current["time"],
            "weather": current["weather"],
            "temperature_c": reading.get("temperature_2m"),
            "apparent_temperature_c": reading.get("apparent_temperature"),
            "relative_humidity_percent": reading.get("relative_humidity_2m"),
            "precipitation_mm": reading.get("precipitation"),
            "wind_speed_kmh": reading.get("wind_speed_10m"),
            "wind_direction_degrees": reading.get("wind_direction_10m"),
            "is_day": reading.get("is_day"),
        }
        lines = [
            f"Right now at {current['point']} ({current['time']} UTC): {current['weather']}.",
            f"  • Temperature {value('temperature_2m')} C, feels like {value('apparent_temperature')} C",
            f"  • Humidity {value('relative_humidity_2m')}%, precipitation {value('precipitation')} mm",
            f"  • Wind {value('wind_speed_10m')} km/h from {value('wind_direction_10m')} degrees",
        ]
        lines.append(
            f"\nSource: {DATASET} (Open-Meteo model output, read live). Ask me when it rains next "
            "and I will give you the hours, not just a percentage."
        )
        return "\n".join(lines), artifact

    def _rain(self, params: dict) -> tuple[str, dict]:
        point, ask = self._point_or_ask(params)
        if ask:
            return ask
        window = int(params.get("hours") or 24)
        hourly = self.data.hourly(point, window)
        windows = ForecastData.rain_windows(hourly["rows"])
        wettest = max(windows, key=lambda item: item["total_mm"]) if windows else None
        artifact = {
            "summary": f"{DATASET}: {len(windows)} rain window(s) in the next {hourly['window_hours']}h "
                       f"at {hourly['point']}"
                       + (f", first {windows[0]['start']} to {windows[0]['end']}" if windows else ""),
            "dataset": DATASET,
            "point": hourly["point"],
            "window_hours": hourly["window_hours"],
            "windows": windows,
            "wettest": wettest,
            "rows": hourly["rows"],
        }
        if not windows:
            return (
                f"No rain is expected at {hourly['point']} in the next {hourly['window_hours']} hours "
                f"- no hour reaches a 40% chance or any measurable amount.\n\nSource: {DATASET} "
                "(hourly Open-Meteo output, read live). The model can still put a shower on you that "
                "it did not expect an hour ago.",
                artifact,
            )
        lines = [f"Rain at {hourly['point']} over the next {hourly['window_hours']} hours (UTC):"]
        for item in windows[:5]:
            lines.append(f"  • {item['start']} to {item['end']} - {item['hours']} hour(s), peak "
                         f"{item['peak_probability']}% chance, about {item['total_mm']} mm")
        if len(windows) > 5:
            lines.append(f"  • ... {len(windows) - 5} more window(s) in the artifact")
        lines.append(f"\nWettest: {wettest['start']} to {wettest['end']}, about {wettest['total_mm']} mm "
                     f"at peak {wettest['peak_probability']}%.")
        lines.append(
            f"\nSource: {DATASET} (hourly Open-Meteo model output, read live). Hours, not a daily "
            "percentage: a 60% day can be one 60% hour or ten 60% hours, and those are different days."
        )
        return "\n".join(lines), artifact

    def _daily(self, params: dict) -> tuple[str, dict]:
        point, ask = self._point_or_ask(params)
        if ask:
            return ask
        days = int(params.get("days") or 3)
        daily = self.data.daily(point, days)
        artifact = {
            "summary": f"{DATASET}: {len(daily['days'])} day(s) at {daily['point']}",
            "dataset": DATASET,
            "point": daily["point"],
            "days": daily["days"],
        }
        lines = [f"Daily forecast for {daily['point']} (UTC dates):"]
        for row in daily["days"]:
            lines.append(f"  • {row['date']}: {row['weather']}, {row.get('temperature_2m_min')} to "
                         f"{row.get('temperature_2m_max')} C, rain {row.get('precipitation_sum')} mm "
                         f"(peak chance {row.get('precipitation_probability_max')}%), wind up to "
                         f"{row.get('wind_speed_10m_max')} km/h")
        lines.append(
            f"\nSource: {DATASET} (Open-Meteo model output, read live). Daily numbers hide timing - "
            "ask when it rains and I will give you the hours."
        )
        return "\n".join(lines), artifact


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        ForecastAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
