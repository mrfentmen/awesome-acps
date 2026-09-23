"""Alert - quiet until something actually happens.

Every agent in this repo answers a question and stops, and the `watch` agent pushes every round
whether or not anything changed. This one is the other shape: it takes a *condition* with a
threshold, checks it on an interval, and says nothing at all until the condition is met.

That silence is the feature. "Tell me when the AQI in Delhi passes 200" should produce one
message, at the moment it passes 200, not twenty messages saying 154, 158, 161. The turn stays
open while it waits, so the editor can cancel it; a cancel stops the wait immediately because
the sleep is taken on the session's own cancel event rather than as a plain sleep.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

import render_values as values  # noqa: E402  (the agent's own directory is on sys.path)
from data import CONDITIONS, AlertData, AlertError  # noqa: E402

#: Hard limits: a condition watch can never hold a turn open forever.
MAX_ROUNDS = 60
MAX_INTERVAL = 300.0
DEFAULT_INTERVAL = 30.0
MAX_SECONDS = 900.0

HELP = (
    "I stay quiet until a condition is met, then tell you once. Ask me:\n"
    "  - alert me when the air quality in Delhi passes 200\n"
    "  - tell me when the temperature in Phoenix goes above 40\n"
    "  - alert me when quakes above 5 happen\n"
    "  - tell me when gauge 07010000 reaches flood stage\n"
    "  - alert me when the national debt passes 41 trillion\n"
    f"I can watch: {', '.join(f'{name} ({what})' for name, what in CONDITIONS.items())}.\n"
    f"Limits: {MAX_ROUNDS} checks, {MAX_INTERVAL:.0f}s apart at most. Cancel the turn and I stop "
    "at once - nothing keeps checking after it ends."
)

PERMISSION_KEY = "alert-watch-public-feeds"

SKILLS = ("alert-watch", "help")

_AQI_RE = re.compile(r"\b(?:aqi|air quality|air pollution|smog)\s*(?:in|at|for|near)?\s*"
                     r"(?P<place>.+?)?\s*(?:pass(?:es)?|go(?:es)?|reach(?:es)?|hits?|above|over|"
                     r"exceeds?|drops? below|below|under)\s*(?P<threshold>\d{1,3})\b", re.IGNORECASE)
_TEMP_RE = re.compile(r"\btemperature\s+(?:in|at|for|near)?\s*(?P<place>.+?)?\s*"
                      r"(?:pass(?:es)?|go(?:es)?|reach(?:es)?|hits?|above|over|exceeds?|"
                      r"below|under|drops? below)\s*(?P<threshold>-?\d{1,3})\b", re.IGNORECASE)
_QUAKE_RE = re.compile(r"\b(?:earth ?quakes?|quakes?|tremors?)\b.*?\b(?:above|over|at least|"
                       r"greater than|magnitude|mag|m)\s*(\d{1,2}(?:\.\d)?)\b", re.IGNORECASE)
_GAUGE_RE = re.compile(r"\b(\d{7,9})\b")
_FLOOD_RE = re.compile(r"\b(flood|flooding|flood stage|crest|rising water)\b", re.IGNORECASE)
_DEBT_RE = re.compile(r"\b(debt|national debt|borrowing)\b", re.IGNORECASE)
_MONEY_RE = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s*(trillion|t\b|billion|b\b)", re.IGNORECASE)
_BELOW_RE = re.compile(r"\b(below|under|drops? below|falls? below|less than)\b", re.IGNORECASE)
_INTERVAL_RE = re.compile(r"\bevery\s+(\d{1,4})\s*(seconds?|secs?|s\b|minutes?|mins?|m\b)",
                          re.IGNORECASE)
_ROUNDS_RE = re.compile(r"\b(?:for\s+)?(\d{1,3})\s*(?:checks?|rounds?|times?|samples?)\b",
                        re.IGNORECASE)
_ALERT_WORDS = re.compile(r"\b(watch|alert|notify|tell me|let me know|ping me|warn me|when)\b",
                          re.IGNORECASE)

_TRAILING = (" today", " now", " right now", " tonight", " tomorrow", " please", " for me")

#: Words that are the condition's verb, not part of a place name. The place group is lazy, so
#: "temperature in Phoenix goes above 40" captures "Phoenix goes" - the verb trails the name.
_DIRECTION_WORDS = frozenset((
    "goes", "go", "passes", "pass", "reaches", "reach", "hits", "hit", "rises", "rise",
    "drops", "drop", "exceeds", "exceed", "is", "are", "was", "were", "gets", "get",
    "climbs", "climb", "falls", "fall", "above", "over", "below", "under", "past",
))


def _clean_place(text: str | None) -> str | None:
    if not text:
        return None
    phrase = text.strip(" .,?!")
    phrase = re.sub(r"^(the|a|an)\s+", "", phrase, flags=re.IGNORECASE)
    for noise in _TRAILING:
        if phrase.lower().endswith(noise):
            phrase = phrase[: -len(noise)]
    phrase = re.sub(r"\b(and|but|then|check|every)\b.*$", "", phrase, flags=re.IGNORECASE).strip()
    words = [word for word in phrase.split() if word]
    while len(words) > 1 and words[-1].lower().strip(".,!?") in _DIRECTION_WORDS:
        words.pop()
    if not words or len(words) > 4:
        return None
    return " ".join(words)


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    stripped = str(text or "").strip()
    if not stripped or re.search(r"\b(help|what can you do|commands?)\b", stripped, re.IGNORECASE):
        return "help", {}
    if not _ALERT_WORDS.search(stripped):
        return "help", {}

    params: dict = {}
    interval = _INTERVAL_RE.search(stripped)
    if interval:
        amount = float(interval.group(1))
        if interval.group(2).lower().startswith("m"):  # minutes, mins or a bare 'm'
            amount *= 60
        params["interval"] = amount
    rounds = _ROUNDS_RE.search(stripped)
    if rounds:
        params["rounds"] = int(rounds.group(1))

    aqi = _AQI_RE.search(stripped)
    if aqi:
        params.update({"condition": "aqi", "threshold": float(aqi.group("threshold"))})
        place = _clean_place(aqi.group("place"))
        if not place:
            # "alert me when the air quality passes 200 in Delhi" puts the place after the number.
            tail = stripped[aqi.end():]
            place = _clean_place(re.sub(r"^\s*(?:in|at|for|near)\b", "", tail, flags=re.IGNORECASE))
        if not place:
            return "help", {}
        params["place"] = place
        return "alert-watch", _direction(params, stripped)

    temperature = _TEMP_RE.search(stripped)
    if temperature:
        params.update({"condition": "temperature", "threshold": float(temperature.group("threshold"))})
        place = _clean_place(temperature.group("place"))
        if not place:
            tail = stripped[temperature.end():]
            place = _clean_place(re.sub(r"^\s*(?:in|at|for|near)\b", "", tail, flags=re.IGNORECASE))
        if not place:
            return "help", {}
        params["place"] = place
        return "alert-watch", _direction(params, stripped)

    if _FLOOD_RE.search(stripped):
        gauge = _GAUGE_RE.search(stripped)
        if not gauge:
            return "help", {}
        params.update({"condition": "flood", "gauge": gauge.group(1)})
        return "alert-watch", params

    quake = _QUAKE_RE.search(stripped)
    if quake:
        params.update({"condition": "quakes", "threshold": float(quake.group(1))})
        return "alert-watch", params

    if _DEBT_RE.search(stripped):
        money = _MONEY_RE.search(stripped)
        if money:
            amount = float(money.group(1))
            if money.group(2).lower().startswith("t"):
                amount *= 1_000_000_000_000
            else:
                amount *= 1_000_000_000
            params.update({"condition": "debt", "threshold": amount})
            return "alert-watch", _direction(params, stripped)
        return "help", {}
    return "help", {}


def _direction(params: dict, text: str) -> dict:
    params["direction"] = "below" if _BELOW_RE.search(text) else "above"
    return params


def bounds(rounds, interval) -> tuple[int, float]:
    """(rounds, interval) inside the caps, and never more than MAX_SECONDS in total."""
    try:
        count = int(rounds)
    except (TypeError, ValueError):
        count = 20
    count = max(1, min(count, MAX_ROUNDS))
    try:
        gap = float(interval)
    except (TypeError, ValueError):
        gap = DEFAULT_INTERVAL
    gap = max(1.0, min(gap, MAX_INTERVAL))
    if count * gap > MAX_SECONDS:
        count = max(1, int(MAX_SECONDS // gap))
    return count, gap


def render_alert(reading: dict, params: dict, checks: int, seconds: float) -> str:
    """The one message this agent exists to send."""
    direction = params.get("direction", "above")
    threshold = params.get("threshold")
    value = reading.get("value")
    shown = values.number(value) if value is not None else "no reading"
    unit = reading.get("unit") or ""
    lines = [f"ALERT - {reading['label']} is {shown}{unit}"
             + (f", {direction} your threshold of {values.number(threshold)}"
                if threshold is not None else "")]
    if reading["condition"] == "flood":
        lines.append(f"NOAA's category for this gauge right now: {reading['category']}")
        thresholds = reading.get("thresholds") or {}
        if thresholds:
            levels = ", ".join(f"{name} {values.number(value)}"
                               for name, value in sorted(thresholds.items())
                               if isinstance(value, (int, float)))
            if levels:
                lines.append(f"Flood stages on this gauge: {levels}")
    if reading["condition"] == "quakes" and reading.get("event"):
        event = reading["event"]
        when = f" at {reading['when']}" if reading.get("when") else ""
        lines.append(f"Event M{event['magnitude']} - {event['place']}{when}")
        if event.get("url"):
            lines.append(f"{event['url']}")
        if reading.get("first"):
            lines.append("(this one was already under way when the watch started)")
    lines.append(f"Checked {checks} time(s) over {seconds:.0f}s. Threshold: {direction} "
                 + (values.number(threshold) if threshold is not None else "any"))
    lines.append(f"Source: {reading['source']}, read live.")
    return "\n".join(lines)


class AlertAgent(AcpAgent):
    name = "alert"
    title = "Alert - silent until a condition is met"
    version = "1.0.0"

    def __init__(self, connection=None, data: AlertData | None = None) -> None:
        super().__init__(connection)
        self.data = data or AlertData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Alert session opened. " + HELP.splitlines()[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Check the condition once and describe it", "medium"),
            ("Stay silent until it is met, then say so once", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("I am quiet by design: one message, at the moment the condition is met. "
                        "Cancel the turn and I stop at once.")
            return STOP_END_TURN

        condition = params["condition"]
        rounds, gap = bounds(params.get("rounds"), params.get("interval"))
        tool = f"watch_{condition}"
        ctx.tool_call(tool, f"Watch {CONDITIONS[condition]}", kind="fetch", name=skill,
                      raw_input={key: value for key, value in params.items() if key != "condition"})
        if not ctx.ask_permission(tool, "Allow Alert to check these public feeds while this turn "
                                        "is open?", remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to check the public feeds before I can watch.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        target = (f"{params.get('direction', 'above')} "
                  f"{values.number(params['threshold'])}" if params.get("threshold") is not None
                  else "reaching flood stage")
        ctx.message(f"Watching {CONDITIONS[condition]} - {target}. Up to {rounds} round(s), "
                    f"{gap:.0f}s apart ({rounds * gap:.0f}s at most). I will stay quiet until it "
                    "happens. Cancel the turn to stop.\n")

        started = time.monotonic()
        checks, latest, fired = 0, None, None
        try:
            for index in range(1, rounds + 1):
                latest = self.data.check(condition, direction=params.get("direction", "above"),
                                         threshold=params.get("threshold"),
                                         place=params.get("place"), gauge=params.get("gauge"))
                checks = index
                if latest.get("met"):
                    fired = latest
                    break
                if index < rounds and ctx.session.cancel.wait(gap):
                    ctx.check_cancelled()
        except (AlertError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I stopped: {exc}")
            return STOP_END_TURN

        elapsed = time.monotonic() - started
        if fired:
            ctx.message(render_alert(fired, params, checks, elapsed))
            summary = f"Condition met after {checks} check(s) in {elapsed:.0f}s."
        else:
            shown = values.number(latest["value"]) if latest and latest.get("value") is not None \
                else "no reading"
            summary = (f"Not met after {checks} check(s) in {elapsed:.0f}s "
                       f"(last reading {shown}{latest.get('unit') or '' if latest else ''}). "
                       "Nothing is still checking now.")
            ctx.message(summary)
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(summary))
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        """Nothing to clean up: the wait is on the session's own cancel event."""
        return None


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        AlertAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
