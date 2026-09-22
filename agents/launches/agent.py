"""Launches — the global launch schedule inside your editor.

No ACP agent knows when the next rocket goes up. This one reads Launch Library 2 - the
schedule flight trackers use - and answers with the window, the vehicle, the pad and the
status, straight from the record.

Deterministic on purpose: routing is rules, every field is the catalog's, and nothing is
guessed. It reports a plan, opens one tool call per lookup, asks permission before its
first read, streams the answer, and closes the tool call with a one-line summary of the
record it read.
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
    DATASET_DETAIL,
    DATASET_PREVIOUS,
    DATASET_UPCOMING,
    LaunchesData,
    LaunchesError,
)

HELP = (
    "I read Launch Library 2, the launch schedule the trackers use (keyless). Ask me:\n"
    "  • what is the next launch?\n"
    "  • when is the next Starlink launch?\n"
    "  • what launched in the last few days?\n"
    "  • what is 63063b9d-ade9-4448-8717-6f910aa81188?\n"
    "A launch window is a plan: the no-earlier-than time moves, so I always print the status "
    "and the window I read. The free feed allows 15 requests an hour, so I cache for 15 minutes."
)

PERMISSION_KEY = "launches-read-public-launchlibrary"

SKILLS = ("launches-upcoming", "launches-recent", "launches-detail", "help")

_UUID_RE = re.compile(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b", re.IGNORECASE)
_RECENT_WORDS = re.compile(r"\b(launched|went up|flew|already (?:launched|flew)|previous|last launch(?:es)?|"
                           r"recent launches?|past launches?)\b", re.IGNORECASE)
_UPCOMING_WORDS = re.compile(r"\b(next|upcoming|scheduled|when is the next|schedule|manifest|later this)\b",
                             re.IGNORECASE)
_DAYS_RE = re.compile(r"\b(?:next|in|within|over)\s+(\d{1,3})\s*(days?|weeks?)\b", re.IGNORECASE)
_DAYS_WORDS = {"day": 1, "week": 7}
#: Words that are part of the question, not the launch name to search for.
_SEARCH_STOP_WORDS = frozenset({
    "what", "whats", "when", "is", "the", "a", "an", "next", "upcoming", "launch", "launches",
    "scheduled", "for", "of", "in", "on", "this", "week", "weeks", "day", "days", "just",
    "went", "up", "has", "have", "did", "launched", "recent", "recently", "last", "latest",
    "past", "me", "please", "tell", "about", "few", "couple", "several", "some", "still",
    "today", "tomorrow", "yesterday", "tonight", "month", "months", "year", "years",
})


def search_from_text(text: str) -> str | None:
    """The launch name to search for: the question with its asking-words peeled off."""
    cleaned = re.sub(r"[^\w\s'-]", " ", text)
    words = [word for word in cleaned.split() if word.lower().strip("'") not in _SEARCH_STOP_WORDS]
    candidate = " ".join(words).strip(" -")
    return candidate if len(candidate) >= 2 else None


def days_from_text(text: str) -> int | None:
    match = _DAYS_RE.search(text)
    if not match:
        return None
    unit = "week" if match.group(2).lower().startswith("week") else "day"
    try:
        return LaunchesData.check_days(int(match.group(1)) * _DAYS_WORDS[unit])
    except ValueError:
        return None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    params: dict = {}
    identity = _UUID_RE.search(text)
    if identity:
        params["id"] = identity.group(1).lower()
        return "launches-detail", params

    search = search_from_text(text)
    days = days_from_text(text)
    if _RECENT_WORDS.search(text) and not _UPCOMING_WORDS.search(text):
        params["limit"] = 5
        if search:
            params["search"] = search
        return "launches-recent", params
    params["limit"] = 5
    if search:
        params["search"] = search
    if days:
        params["days"] = days
    return "launches-upcoming", params


class LaunchesAgent(AcpAgent):
    name = "launches"
    title = "Launches — Launch Library 2"
    version = "1.0.0"

    def __init__(self, connection=None, data: LaunchesData | None = None) -> None:
        super().__init__(connection)
        self.data = data or LaunchesData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Launches session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read the Launch Library record", "medium"),
            ("Report the window, the vehicle, the pad and the status", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("One source: Launch Library 2, the community launch catalog. It lists "
                        "planned windows, and those move - check the status in every answer.")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read Launch Library for {skill}", kind="fetch", name=skill,
                      raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Launches to read Launch Library 2's public schedule?",
                                 remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the public launch schedule before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (LaunchesError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the launch schedule: {exc}")
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
        if skill == "launches-recent":
            return self._recent(params)
        if skill == "launches-detail":
            return self._detail(params)
        if skill == "launches-upcoming":
            return self._upcoming(params)
        return HELP, {"summary": "help", "dataset": None}

    @staticmethod
    def _one_line(launch: dict) -> str:
        line = f"{launch['name']} - {launch['net'] or 'window not set'}"
        if launch["status"]:
            line += f" [{launch['status']}]"
        if launch["provider"]:
            line += f" - {launch['provider']}"
        if launch["pad"]:
            line += f" at {launch['pad']}" + (f", {launch['location']}" if launch["location"] else "")
        return line

    def _upcoming(self, params: dict) -> tuple[str, dict]:
        result = self.data.upcoming(limit=int(params.get("limit") or 5), search=params.get("search"),
                                   days=params.get("days"))
        artifact = {
            "summary": f"{DATASET_UPCOMING}: {len(result['launches'])} of {result['count']} "
                       f"scheduled launch(es)"
                       + (f" matching {result['search']!r}" if result["search"] else ""),
            "dataset": DATASET_UPCOMING,
            "count": result["count"],
            "search": result["search"],
            "days": result["days"],
            "launches": result["launches"],
        }
        if not result["launches"]:
            what = f" matching {result['search']!r}" if result["search"] else ""
            window = f" in the next {result['days']} days" if result["days"] else ""
            return (
                f"Launch Library has no scheduled launch{what}{window}.\n\nSource: {DATASET_UPCOMING} "
                "(read live). Widen the window, or try a shorter search word.",
                artifact,
            )
        matching = f" matching {result['search']!r}" if result["search"] else ""
        within = f" within {result['days']} days" if result["days"] else ""
        lines = [f"Next launches in Launch Library{matching}{within}:"]
        for launch in result["launches"]:
            lines.append(f"  • {self._one_line(launch)}")
            if launch["window_start"] and launch["window_start"] != launch["net"]:
                lines.append(f"      window {launch['window_start']} to {launch['window_end']}"
                             + (f", precision {launch['net_precision']}" if launch["net_precision"] else ""))
        lines.append(f"\n{result['count']} launch(es) on the upcoming list in total. Source: "
                     f"{DATASET_UPCOMING} (read live). A window is a plan - the status column is what "
                     "tells you whether it is still on.")
        return "\n".join(lines), artifact

    def _recent(self, params: dict) -> tuple[str, dict]:
        result = self.data.recent(limit=int(params.get("limit") or 5), search=params.get("search"))
        artifact = {
            "summary": f"{DATASET_PREVIOUS}: {len(result['launches'])} recent launch(es)"
                       + (f" matching {result['search']!r}" if result["search"] else ""),
            "dataset": DATASET_PREVIOUS,
            "search": result["search"],
            "launches": result["launches"],
        }
        if not result["launches"]:
            return (f"Launch Library has no past launch on record for that search.\n\n"
                    f"Source: {DATASET_PREVIOUS}.", artifact)
        lines = ["Launches that already flew, newest first:"]
        for launch in result["launches"]:
            outcome = launch["status"] or "status not listed"
            lines.append(f"  • {launch['name']} - {launch['net']} - {outcome} "
                         f"({launch['provider'] or 'provider not listed'})")
        lines.append(f"\nSource: {DATASET_PREVIOUS} (read live). Status comes from the catalog: "
                     "'Success', 'Failure' or the last known state.")
        return "\n".join(lines), artifact

    def _detail(self, params: dict) -> tuple[str, dict]:
        launch = self.data.detail(str(params.get("id") or ""))
        artifact = {
            "summary": f"{DATASET_DETAIL}/{launch['id']}: {launch['name']} - {launch['net']} "
                       f"[{launch['status']}]",
            "dataset": DATASET_DETAIL,
            "id": launch["id"],
            "name": launch["name"],
            "net": launch["net"],
            "net_precision": launch["net_precision"],
            "status": launch["status"],
            "provider": launch["provider"],
            "rocket": launch["rocket"],
            "mission": launch["mission"],
            "mission_type": launch["mission_type"],
            "orbit": launch["orbit"],
            "pad": launch["pad"],
            "location": launch["location"],
            "window_start": launch["window_start"],
            "window_end": launch["window_end"],
            "probability": launch["probability"],
            "webcast_live": launch["webcast_live"],
            "failreason": launch["failreason"],
            "mission_description": launch["mission_description"],
            "url": launch["url"],
        }
        lines = [
            f"{launch['name']} - {launch['net']} (precision: {launch['net_precision'] or 'not stated'})",
            f"  • Status {launch['status'] or 'not listed'}"
            + (f" - {launch['failreason']}" if launch["failreason"] else ""),
            f"  • Vehicle {launch['rocket'] or 'not listed'}, provider {launch['provider'] or 'not listed'}",
            f"  • Pad {launch['pad'] or 'not listed'}"
            + (f", {launch['location']}" if launch["location"] else ""),
            f"  • Mission {launch['mission'] or 'not listed'}"
            + (f" ({launch['mission_type']}, orbit {launch['orbit']})"
               if launch["mission_type"] or launch["orbit"] else ""),
        ]
        if launch["window_start"] and launch["window_start"] != launch["net"]:
            lines.append(f"  • Window {launch['window_start']} to {launch['window_end']}")
        if launch["probability"] is not None:
            lines.append(f"  • Weather probability {launch['probability']}%")
        if launch["mission_description"]:
            description = launch["mission_description"]
            lines.append("  " + (description if len(description) <= 600 else description[:597] + "..."))
        lines.append(f"\nSource: {DATASET_DETAIL}/{launch['id']} (read live). "
                     + (f"Record page: {launch['url']}. " if launch["url"] else "")
                     + "Launch times move; check the status before you plan anything around it.")
        return "\n".join(lines), artifact


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        LaunchesAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
