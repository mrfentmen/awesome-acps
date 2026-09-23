"""Chart - the first ACP agent that answers with a picture.

Forty agents on the vendors' list, every one of them writes code, and not one of them has ever
sent an image. ACP has carried image content blocks from the start (they are MCP's ContentBlock
shape, re-used on purpose), so this agent uses one: it reads real numbers - Open-Meteo's hourly
forecast, the Treasury's daily debt, USGS's earthquake feed - and returns a charted PNG beside
the summary.

The image is built with the standard library only (zlib, struct) because this repo has no
plotting dependency and adding one is not allowed. Nothing about the numbers is invented: the
window starts at the place's own local hour, long series are sampled and said to be sampled,
and an empty feed is an error rather than a flat line at zero.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

import render  # noqa: E402  (the agent's own directory is on sys.path when it runs)
from data import MAX_POINTS, ChartData, ChartError, sample  # noqa: E402

HELP = (
    "I answer with a chart: a real PNG of real numbers, sent as an ACP image content block. "
    "Ask me:\n"
    "  - chart the temperature in Seattle for the next 24 hours\n"
    "  - chart the rain in Tokyo tomorrow\n"
    "  - chart the air quality in Delhi\n"
    "  - chart the national debt for the last 90 days\n"
    "  - chart the earthquakes in the past 12 hours\n"
    "I read Open-Meteo, the US Treasury and USGS, all keyless, and every chart names the window "
    "it covers - including when a long series was sampled to fit the pixels."
)

PERMISSION_KEY = "chart-read-public-data"

SKILLS = ("chart-temperature", "chart-rain", "chart-air", "chart-debt", "chart-quakes", "help")

_METRIC_WORDS = (
    ("rain", re.compile(r"\b(rain|rainfall|precip\w*|showers?|wet)\b", re.IGNORECASE)),
    ("air", re.compile(r"\b(air quality|aqi|smog|pollution|pm2\.?5|particulate)\b", re.IGNORECASE)),
    ("temperature", re.compile(r"\b(temperature|temps?|how (?:hot|cold)|warm|cold|heat)\b",
                               re.IGNORECASE)),
    ("debt", re.compile(r"\b(debt|national debt|treasury|borrowing)\b", re.IGNORECASE)),
    ("quakes", re.compile(r"\b(earthquakes?|quakes?|seismic|tremors?)\b", re.IGNORECASE)),
)

_HOURS_RE = re.compile(r"\b(?:next|past|last|coming)\s+(\d{1,3})\s*(hours?|hrs?|h\b|days?|d\b)",
                       re.IGNORECASE)
_DAYS_RE = re.compile(r"\b(?:next|past|last|coming)\s+(\d{1,4})\s*days?\b", re.IGNORECASE)
_WORDS_FOR_SPAN = re.compile(r"\b(today|tonight|tomorrow|this week|next week|the week|this month|"
                            r"yesterday|the past day)\b", re.IGNORECASE)
_CHART_WORDS = re.compile(r"\b(chart|plot|graph|show me|draw|visuali[sz]e|picture|trend)\b",
                          re.IGNORECASE)


def place_from_text(text: str) -> str | None:
    """The place a chart question is about, or None."""
    work = str(text or "").strip()
    patterns = (
        r"\b(?:temperature|temps?|rain|rainfall|precipitation|air quality|aqi|smog|pollution)\s+"
        r"(?:in|at|for|near|around)\s+(?:the\s+)?(.+?)(?=\s*(?:for|over|during|today|tonight|"
        r"tomorrow|this|next|past|last)\b|[?.!]|$)",
        r"\b(?:in|at|for|near|around)\s+(?:the\s+)?(.+?)(?=\s*(?:for|over|during|today|tonight|"
        r"tomorrow|this|next|past|last)\b|[?.!]|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, work, re.IGNORECASE)
        if not match:
            continue
        phrase = match.group(1).strip(" .,?!")
        phrase = re.sub(r"^(the|a|an)\s+", "", phrase, flags=re.IGNORECASE)
        words = [word for word in phrase.split() if word]
        if not words or len(words) > 4:
            continue
        if all(word.lower() in {"next", "past", "last", "hours", "days", "hour", "day"} for word in words):
            continue
        return " ".join(words)[:60]
    return None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    stripped = str(text or "").strip()
    if not stripped or re.search(r"\b(help|what can you do|commands?)\b", stripped, re.IGNORECASE):
        return "help", {}

    kind = None
    for name, pattern in _METRIC_WORDS:
        if pattern.search(stripped):
            kind = name
            break
    params: dict = {}
    hours = _HOURS_RE.search(stripped)
    if hours:
        amount = int(hours.group(1))
        unit = hours.group(2).lower()
        params["hours"] = amount * 24 if unit.startswith("d") else amount
        if params["hours"] > 24 * 7:
            params["hours"] = 24 * 7
    days = _DAYS_RE.search(stripped)
    if days:
        params["days"] = int(days.group(1))
        if "hours" not in params:
            params["hours"] = min(24 * 7, int(days.group(1)) * 24)
    span = _WORDS_FOR_SPAN.search(stripped)
    if span:
        word = span.group(1).lower()
        if "week" in word:
            params.setdefault("hours", 24 * 7)
        elif "month" in word:
            params.setdefault("days", 30)
            params.setdefault("hours", 24 * 7)
        elif word in ("tomorrow", "tonight"):
            params.setdefault("hours", 24)
        elif word in ("today", "the past day", "yesterday"):
            params.setdefault("hours", 24)
    place = place_from_text(stripped)
    if place:
        params["place"] = place

    if kind:
        if kind in ("debt", "quakes"):
            params.pop("place", None)
        elif not place:
            # A weather or air chart with no place has nothing to draw: show the examples
            # instead of asking the data source to guess a city.
            return "help", {}
        params["kind"] = kind
        return f"chart-{kind}", params
    if _CHART_WORDS.search(stripped):
        return "help", {}
    return "help", {}


def _summary_lines(series: dict) -> list[str]:
    """The words that go with the picture: window, extremes, and where the numbers came from."""
    values = series["values"]
    labels = series["labels"]
    unit = series["unit"]
    lowest = min(values)
    highest = max(values)
    lines = [series["title"],
             f"  - window: {series['subtitle']}",
             f"  - range: {render.format_value(lowest)}{unit} to {render.format_value(highest)}{unit}",
             f"  - last point: {render.format_value(values[-1])}{unit} at {labels[-1]}"]
    if series["kind"] == "temperature":
        warm = labels[values.index(highest)]
        cool = labels[values.index(lowest)]
        lines.append(f"  - warmest {render.format_value(highest)}{unit} at {warm}, "
                     f"coolest {render.format_value(lowest)}{unit} at {cool}")
    if series["kind"] == "air":
        peak = max(values)
        band = "good" if peak <= 50 else "moderate" if peak <= 100 else \
            "unhealthy for sensitive groups" if peak <= 150 else "unhealthy"
        lines.append(f"  - the worst hour reaches {render.format_value(peak)} AQI ({band})")
    if series["kind"] == "debt":
        change = values[-1] - values[0]
        lines.append(f"  - change over the window: {'+' if change >= 0 else ''}"
                     f"{render.format_value(change)} dollars")
    if series["kind"] == "quakes":
        total = int(sum(values))
        lines.append(f"  - total counted: {total} event(s) at M2.5 or above")
    return lines


class ChartAgent(AcpAgent):
    name = "chart"
    title = "Chart - answers with a chart, not just text"
    version = "1.0.0"

    def __init__(self, connection=None, data: ChartData | None = None) -> None:
        super().__init__(connection)
        self.data = data or ChartData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Chart session opened. " + HELP.splitlines()[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read the real series for that window", "medium"),
            ("Draw it and send the PNG as an image content block", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("Every chart I send is built from the numbers in the same answer, with "
                        "the standard library - there is no plotting dependency here.")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read data for {params['kind']}", kind="fetch", name=skill,
                      raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Chart to read the public data sources?",
                                  remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the public data before I can draw it.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            series = self.data.series(params["kind"], place=params.get("place"),
                                      hours=params.get("hours") or 24,
                                      days=params.get("days") or 30)
        except (ChartError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the data for that chart: {exc}")
            return STOP_END_TURN
        ctx.check_cancelled()

        labels, values, step = sample(series["labels"], series["values"], MAX_POINTS,
                                      align_last=series["kind"] in ("debt", "quakes"))
        if step > 1:
            series["subtitle"] += f", sampled every {step} points"
        points = list(zip(labels, values))
        try:
            if series["chart"] == "bar":
                blob = render.bar_chart(series["title"], series["subtitle"], points, series["unit"])
            else:
                blob = render.line_chart(series["title"], series["subtitle"], points, series["unit"])
        except ValueError as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not draw that chart: {exc}")
            return STOP_END_TURN

        width, height = render.png_size(blob)
        lines = _summary_lines(series)
        lines.append("")
        lines.append(f"Chart: {width}x{height} PNG, {len(points)} of {len(series['values'])} "
                     f"points drawn, sent as an image content block.")
        lines.append(f"Source: {series['source']}, read live. {series['note']}")
        ctx.tool_call_update(tool, status="completed",
                             content=ctx.text_content(f"{width}x{height} PNG, {len(blob)} bytes"))
        ctx.stream_text("\n".join(lines))
        ctx.image(blob, "image/png")
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        return None  # one read, one draw


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        ChartAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
