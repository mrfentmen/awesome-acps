"""Surf — waves and wind at any spot, inside your editor.

Every ACP agent on the vendors' list writes code. This one answers the question a surfer
actually asks: how big is it, how clean is it, and when is the best window. It reads
Open-Meteo's marine model for wave height, period and direction, and its wind model for the
wind that shapes those waves. Keyless, worldwide, and a different shelf from the `buoys`
agent here (a buoy measures one stretch of water; this model answers for any point on Earth,
including places with no buoy for a thousand miles).

Deterministic on purpose: routing is rules, the numbers are Open-Meteo's own, and the one
inference made - whether the wind is offshore, cross-shore or onshore - is derived from the
wind and wave directions and explained in the answer rather than asserted as local knowledge.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from data import DATASET, SURF_SPOTS, GROUNDSWELL_PERIOD_S, SurfData, SurfError, compass  # noqa: E402

HELP = (
    "I read Open-Meteo's marine and wind models (keyless). Ask me:\n"
    "  • how big are the waves at Santa Cruz right now?\n"
    "  • when is it worth surfing at Nazare in the next two days?\n"
    "  • what is the swell doing at Pipeline?\n"
    "  • which spots do you know?\n"
    "I give you the model's wave height, period and direction, the wind, and how the wind "
    "sits against the swell. I do not know how any particular beach faces, and I do not tell "
    "you whether a wave is safe or worth paddling into."
)

PERMISSION_KEY = "surf-read-public-open-meteo"

SKILLS = ("surf-now", "surf-forecast", "surf-spots", "help")

_FORECAST_WORDS = re.compile(
    r"\b(forecast|tomorrow|this week|weekend|next few days|next \d+|when|best (?:time|window)|"
    r"worth (?:surfing|a paddle)|swell (?:window|arrive)|coming days)\b", re.IGNORECASE)
_SPOT_WORDS = re.compile(r"\b(which|what|list|show|known)\b.{0,24}\bspots?\b|"
                         r"\bwhere can (?:i|you) surf\b", re.IGNORECASE)
_NOW_WORDS = re.compile(r"\b(right now|now|currently|today|at the moment|how big|how clean|"
                        r"how is it|conditions)\b", re.IGNORECASE)

_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _number(value, digits: int = 1) -> str:
    """Trim padding without eating a real digit: 11.0 -> '11', 20 stays '20'."""
    if value is None:
        return "not reported"
    text = f"{value:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    params: dict = {}
    spot = SurfData.spot_from_text(text)
    if spot:
        params["spot"] = spot

    if _SPOT_WORDS.search(text):
        return "surf-spots", params
    hours = re.search(r"\b(?:next|in the next|within)\s+(\d{1,3})\s*(?:hours?|hrs?)\b", text, re.IGNORECASE)
    if hours:
        params["hours"] = int(hours.group(1))
    if _FORECAST_WORDS.search(text):
        return "surf-forecast", params
    if spot:
        return "surf-now", params
    if _NOW_WORDS.search(text):
        return "surf-now", params
    return "help", {}


class SurfAgent(AcpAgent):
    name = "surf"
    title = "Surf — waves and wind at any spot"
    version = "1.0.0"

    def __init__(self, connection=None, data: SurfData | None = None) -> None:
        super().__init__(connection)
        self.data = data or SurfData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Surf session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Locate the spot", "medium"),
            ("Read the marine model and the wind, then compare them", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("I read two Open-Meteo products: the marine wave model and the wind "
                        "forecast. Both are model output, not a buoy measurement.")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read Open-Meteo marine {skill.removeprefix('surf-')}", kind="fetch",
                      name=skill, raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Surf to read Open-Meteo's public marine and wind models?",
                                  remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the public Open-Meteo models before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (SurfError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the marine model: {exc}")
            return STOP_END_TURN
        ctx.check_cancelled()
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(artifact["summary"]))
        ctx.stream_text(answer)
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        # Every skill is a single read; nothing long-running to interrupt.
        return None

    # -- skills ------------------------------------------------------------

    def _run_skill(self, skill: str, params: dict) -> tuple[str, dict]:
        if skill == "surf-now":
            return self._now(params)
        if skill == "surf-forecast":
            return self._forecast(params)
        if skill == "surf-spots":
            return self._spots(params)
        return HELP, {"summary": "help", "dataset": None}

    def _spot_or_ask(self, params: dict) -> tuple[tuple[str, str] | None, tuple[str, dict] | None]:
        """((point, name), None) or (None, an answer to send instead)."""
        asked = str(params.get("spot") or "")
        if asked:
            try:
                return (self.data.check_point(asked), self._label(asked)), None
            except ValueError as exc:
                return None, (f"{exc}.", {"summary": "unknown spot", "dataset": DATASET})
        return None, ("I need a spot: a name I know, like Santa Cruz, Nazare or Pipeline, or "
                      "coordinates like 36.95,-122.03.",
                      {"summary": "no spot given", "dataset": DATASET})

    @staticmethod
    def _label(spot: str) -> str:
        """The name for a point when the caller used one, else the coordinates themselves."""
        lowered = " ".join(re.sub(r"[^a-z ]+", " ", str(spot).lower()).split())
        return lowered if lowered in SURF_SPOTS else str(spot)

    def _now(self, params: dict) -> tuple[str, dict]:
        located, ask = self._spot_or_ask(params)
        if ask:
            return ask
        spot, label = located
        result = self.data.conditions(spot)
        waves, wind, band = result["waves"], result["wind"], result["band"]
        relation = result["wind_relation"]
        artifact = {
            "summary": f"{DATASET}: {spot} at {result['observed_at']}, waves "
                       f"{_number(waves['height_m'])} m at {_number(waves['period_s'])} s, wind "
                       f"{_number(wind['speed_kmh'])} km/h {relation['label']}",
            "dataset": DATASET,
            "point": spot,
            "observed_at": result["observed_at"],
            "waves": waves,
            "wind": wind,
            "wind_relation": relation,
            "band": band,
        }
        lines = [f"{label} ({result['point']}) at {result['observed_at']} "
                 f"({result['timezone']}):"]
        period = waves["period_s"]
        swell_type = f" - {result['swell_type']}" if result.get("swell_type") else ""
        lines.append(f"  • Waves: {_number(waves['height_m'])} m significant height "
                     f"({band['label']}, {band['description']}), {_number(period)} s period "
                     f"from {compass(waves['direction_deg']) or '?'} ({_number(waves['direction_deg'], 0)} degrees)"
                     f"{swell_type}")
        if waves.get("swell_height_m") is not None or waves.get("wind_wave_height_m") is not None:
            lines.append(f"  • Split: {_number(waves.get('swell_height_m'))} m swell, "
                         f"{_number(waves.get('wind_wave_height_m'))} m local wind wave")
        lines.append(f"  • Wind: {_number(wind['speed_kmh'])} km/h from "
                     f"{compass(wind['direction_deg']) or '?'} ({_number(wind['direction_deg'], 0)} degrees)"
                     + (f", gusting {_number(wind['gust_kmh'])} km/h" if wind.get("gust_kmh") is not None else "")
                     + (f", air {_number(wind['temperature_c'])} degC" if wind.get("temperature_c") is not None else ""))
        lines.append(f"  • Read: {relation['label']} - {relation['why']}"
                     + (f" ({_number(relation['degrees_apart'], 0)} degrees apart)"
                        if relation.get("degrees_apart") is not None else ""))
        lines.append(f"\nSource: {DATASET}, read live. Model output, not a buoy: wave height is "
                     f"significant height (the average of the highest third), and a {GROUNDSWELL_PERIOD_S:.0f} s "
                     "period or more usually means organised groundswell rather than local chop. "
                     "The offshore/onshore read is inferred by comparing wind direction with wave "
                     "direction, because I do not know which way this beach faces.")
        return "\n".join(lines), artifact

    def _forecast(self, params: dict) -> tuple[str, dict]:
        located, ask = self._spot_or_ask(params)
        if ask:
            return ask
        spot, label = located
        hours = params.get("hours") or 48
        result = self.data.forecast(spot, hours)
        biggest = result["biggest"]
        artifact = {
            "summary": f"{DATASET}: {spot} next {result['hours']} h, biggest "
                       + (f"{_number(biggest[0]['height_m'])} m at {biggest[0]['time']}" if biggest else "none"),
            "dataset": DATASET,
            "point": spot,
            "hours": result["hours"],
            "biggest": biggest,
            "rows": result["rows"],
        }
        lines = [f"Wave height at {label} ({result['point']}) for the next "
                 f"{result['hours']} hours ({result['timezone']}):"]
        daily: dict[str, float] = {}
        for row in result["rows"]:
            if row["height_m"] is None:
                continue
            day = row["time"][:10]
            daily[day] = max(daily.get(day, 0.0), row["height_m"])
        for day, peak in sorted(daily.items()):
            label = _DAYS[_weekday(day)] if _weekday(day) is not None else day
            lines.append(f"  • {label} {day}: peak {_number(peak)} m")
        if biggest:
            lines.append("\nBiggest windows in that stretch:")
            for row in biggest[:5]:
                lines.append(f"  • {row['time']}: {_number(row['height_m'])} m "
                             f"({row['band']}), {_number(row['period_s'])} s")
        lines.append(f"\nSource: {DATASET}, hourly model output, read live. A peak in the model "
                     "is not a promise about a beach: tide, bank shape and local wind decide what "
                     "actually breaks. Ask me for the spot right now to see the wind.")
        return "\n".join(lines), artifact

    def _spots(self, params: dict) -> tuple[str, dict]:
        names = sorted(SURF_SPOTS)
        artifact = {
            "summary": f"{DATASET}: {len(names)} known spots",
            "dataset": DATASET,
            "spots": names,
        }
        lines = [f"I know {len(names)} spots by name (and any coordinates you give me):"]
        for start in range(0, len(names), 4):
            lines.append("  • " + ", ".join(names[start:start + 4]))
        lines.append(f"\nSource: {DATASET}. Any point on Earth works if you give me "
                     "coordinates - these names are just shortcuts.")
        return "\n".join(lines), artifact


def _weekday(day: str) -> int | None:
    """'2026-09-22' -> 1 (Monday). None when the date is unparseable."""
    import datetime

    try:
        return datetime.date.fromisoformat(day).weekday()
    except ValueError:
        return None


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        SurfAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
