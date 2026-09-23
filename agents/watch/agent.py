"""Watch — a feed that pushes at you instead of answering once.

Every agent in this repo, and on the vendors' ACP list, answers one question and stops. ACP
can do more than that: `session/update` is a stream, and `session/cancel` exists precisely
because some turns run for a while. This agent uses both. You say "watch the earthquakes",
and it keeps sending updates on an interval until the rounds you asked for are done or you
cancel the turn.

Honest about its limits: it polls, because these three feeds (USGS, NOAA SWPC, open-notify)
offer no push channel - there is no event to subscribe to. The pacing lives in exactly one
place, the loop is hard-capped (20 rounds, 180 seconds), and nothing keeps running after the
turn ends. Sleeping between rounds waits on the session's own cancel event, so a cancel stops
the watch immediately rather than after the current sleep.
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

from data import (  # noqa: E402
    DATASET,
    FEEDS,
    MAX_ROUNDS,
    MAX_SECONDS,
    WatchData,
    WatchError,
    bounds,
    haversine_km,
)

HELP = (
    "I watch a live feed and push updates while you keep the turn open. Ask me:\n"
    "  • watch the earthquakes\n"
    "  • watch the aurora for 4 rounds every 30 seconds\n"
    "  • watch the space station every 5 seconds\n"
    "  • watch earthquakes over magnitude 4 for 5 rounds\n"
    f"I can watch: {', '.join(f'{name} ({what})' for name, what in FEEDS.items())}.\n"
    f"I poll, because these feeds have no push channel. Hard limits: {MAX_ROUNDS} rounds, "
    f"{MAX_SECONDS:.0f} seconds. Cancel the turn to stop early, and nothing keeps running "
    "afterwards."
)

PERMISSION_KEY = "watch-read-public-feeds"

SKILLS = ("watch-quakes", "watch-aurora", "watch-iss", "help")

_FEED_WORDS: tuple[tuple[str, re.Pattern], ...] = (
    ("quakes", re.compile(r"\b(earth\s?quakes?|quakes?|tremors?|seismic)\b", re.IGNORECASE)),
    ("aurora", re.compile(r"\b(auroras?|northern lights|geomagnetic|kp\b|k-index|solar storm)\b",
                          re.IGNORECASE)),
    ("iss", re.compile(r"\b(iss|space station|international space station)\b", re.IGNORECASE)),
)
_ROUNDS_RE = re.compile(r"\b(?:for\s+)?(\d{1,3})\s*(?:rounds?|times?|updates?)\b", re.IGNORECASE)
_INTERVAL_RE = re.compile(r"\bevery\s+(\d{1,4})\s*(seconds?|secs?|s\b|minutes?|mins?|m\b)",
                          re.IGNORECASE)
_MAGNITUDE_RE = re.compile(r"\b(?:over|above|at least|min(?:imum)?|magnitude|mag)\s*"
                           r"(?:m\s*)?(\d{1,2}(?:\.\d)?)\b", re.IGNORECASE)


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    params: dict = {}
    feed = None
    for name, pattern in _FEED_WORDS:
        if pattern.search(text):
            feed = name
            break
    rounds = _ROUNDS_RE.search(text)
    if rounds:
        params["rounds"] = int(rounds.group(1))
    interval = _INTERVAL_RE.search(text)
    if interval:
        seconds = float(interval.group(1))
        if interval.group(2).lower().startswith("m"):  # minutes, mins or a bare 'm'
            seconds *= 60
        params["interval"] = seconds
    if feed == "quakes":
        magnitude = _MAGNITUDE_RE.search(text)
        if magnitude:
            params["min_magnitude"] = float(magnitude.group(1))
    if feed:
        params["feed"] = feed
        return f"watch-{feed}", params
    return "help", {}


def render_round(index: int, total: int, snapshot: dict, previous: dict | None) -> str:
    """One pushed line: which round, when, what the feed says, and what changed."""
    at = snapshot.get("at") or "now"
    line = f"Round {index}/{total} [{at}] {snapshot['line']}"
    change = _change(snapshot, previous)
    return f"{line} - {change}" if change else line


def _change(snapshot: dict, previous: dict | None) -> str | None:
    """What moved since the last round, in plain words."""
    if previous is None:
        return "first reading"
    feed = snapshot.get("feed")
    if snapshot.get("value") == previous.get("value"):
        return "no change since the last round"
    if feed == "quakes":
        return "a new event has appeared"
    if feed == "aurora":
        was, now = previous.get("value"), snapshot.get("value")
        direction = "rising" if (now or 0) > (was or 0) else "falling"
        return f"{direction} from Kp {was:.2f} to Kp {now:.2f}"
    if feed == "iss":
        before = previous.get("data") or {}
        after = snapshot.get("data") or {}
        moved = haversine_km((before.get("latitude"), before.get("longitude")),
                             (after.get("latitude"), after.get("longitude")))
        gap = (after.get("timestamp") or 0) - (before.get("timestamp") or 0)
        speed = f" ({moved / gap:.1f} km/s)" if gap > 0 else ""
        return f"moved {moved:.0f} km in {int(gap)}s{speed}"
    return "changed"


class WatchAgent(AcpAgent):
    name = "watch"
    title = "Watch — a live feed that pushes updates"
    version = "1.0.0"

    def __init__(self, connection=None, data: WatchData | None = None) -> None:
        super().__init__(connection)
        self.data = data or WatchData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Watch session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Take a first reading of the feed", "medium"),
            ("Push a reading each round until the rounds are done or you cancel", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("I only watch while a turn is open: cancel and I stop, and nothing "
                        "keeps running afterwards.")
            return STOP_END_TURN

        feed = params["feed"]
        rounds, gap = bounds(params.get("rounds"), params.get("interval"))
        tool = f"call_watch_{feed}"
        ctx.tool_call(tool, f"Watch {FEEDS[feed]}", kind="fetch", name=skill,
                      raw_input={"feed": feed, "rounds": rounds, "interval": gap,
                                 **({"min_magnitude": params["min_magnitude"]}
                                    if "min_magnitude" in params else {})})
        if not ctx.ask_permission(tool, "Allow Watch to poll USGS, NOAA SWPC and open-notify while this turn is open?",
                                  remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the public feeds before I can watch.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        # Each message ends its own line: a client appends chunks in order, so without the
        # newline the rounds would run together.
        ctx.message(f"Watching {FEEDS[feed]} for {rounds} round(s), {gap:.0f}s apart "
                    f"({rounds * gap:.0f}s at most). Cancel the turn to stop early.\n")
        started = time.monotonic()
        previous, latest, done = None, None, 0
        try:
            for index in range(1, rounds + 1):
                latest = self.data.snapshot(feed, **({"min_magnitude": params["min_magnitude"]}
                                                     if "min_magnitude" in params else {}))
                # One message per round, each with its own id: this is the part that streams.
                ctx.message(render_round(index, rounds, latest, previous) + "\n")
                previous, done = latest, index
                if index < rounds and ctx.session.cancel.wait(gap):
                    ctx.check_cancelled()
        except (WatchError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the feed: {exc}")
            return STOP_END_TURN

        elapsed = time.monotonic() - started
        summary = f"Watched {done} round(s) of {FEEDS[feed]} in {elapsed:.0f}s. Latest: {latest['line']}."
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(summary))
        ctx.message(
            f"\n{summary}\n\nSource: {DATASET}, polled live once per round. These feeds publish "
            "no push channel, so I ask again each round instead of pretending to be notified. "
            "Nothing is still running now that this turn is over."
        )
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        """Nothing to clean up: the loop sleeps on the session's cancel event itself."""
        return None


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        WatchAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
