"""Firehose - a real push stream: live Wikimedia edits, one message per event.

Every other agent in this repo can only poll, and says so. This one cannot: it is connected to
Wikimedia's EventStreams, a Server-Sent Events channel that pushes edits as they happen on
every Wikipedia, every minute. Each event is sent as its own `session/update`, so an editor
renders them as they arrive rather than as one block at the end.

It ends when the count is reached, when the deadline passes, or when the turn is cancelled -
and it reports how many events it saw versus how many it showed, so "10 edits" is never
mistaken for "the wiki is quiet". Nothing is left running when the turn ends.
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
    MAX_EVENTS,
    MAX_SECONDS,
    FirehoseData,
    FirehoseError,
)

HELP = (
    "I am a push stream, not a poll: I read Wikimedia's live edit stream and send each event "
    "as it arrives. Ask me:\n"
    "  - watch the wiki firehose for 10 edits\n"
    "  - live wikipedia edits\n"
    "  - new wikipedia articles\n"
    "  - live edits on de.wikipedia\n"
    "  - big wikipedia edits over 5000 bytes\n"
    f"Hard limits: {MAX_EVENTS} events and {MAX_SECONDS:.0f} seconds per turn. Cancel the turn "
    "and I stop immediately - nothing keeps reading the stream afterwards."
)

PERMISSION_KEY = "firehose-read-public-wikimedia-stream"

SKILLS = ("firehose-edits", "help")

_COUNT_RE = re.compile(r"\b(\d{1,3})\s*(?:edits?|events?|changes?|items?|updates?)\b", re.IGNORECASE)
_SECONDS_RE = re.compile(r"\b(?:for\s+)?(\d{1,3})\s*(seconds?|secs?|s\b|minutes?|mins?|m\b)",
                         re.IGNORECASE)
_BYTES_RE = re.compile(r"\b(?:over|above|more than|at least|bigger than)\s+"
                       r"(\d{1,7})\s*(bytes|kb|k|kilobytes?)\b", re.IGNORECASE)
_BIG_RE = re.compile(r"\b(big|large|huge|substantial)\s+(?:edits?|changes?)\b", re.IGNORECASE)
_NEW_RE = re.compile(r"\b(new|brand new|fresh|created)\s+(?:articles?|pages?|entries)\b|"
                     r"\barticles? being created\b", re.IGNORECASE)
_BOTS_IN_RE = re.compile(r"\b(include|including|with|count|show)\s+(?:the\s+)?bots?\b", re.IGNORECASE)
_LANGUAGE_RE = re.compile(r"\b(?:on|from|in)\s+([a-z]{2,3})(?:\.wikipedia|\.wiktionary|wiki)?\b",
                          re.IGNORECASE)
_WIKI_RE = re.compile(r"\b([a-z]{2,3})\.(?:wikipedia|wiktionary|wikinews|wikisource)\.org\b",
                      re.IGNORECASE)

#: Two letter codes that are English words in a question, never a wiki language here.
_NOT_A_LANGUAGE = {"in", "on", "at", "the", "a", "an", "to", "of", "me", "my", "it", "is", "as"}

_WORDS = re.compile(r"\b(watch|live|stream|firehose|wikipedia|wikipedia's|wikimedia|edits?|"
                    r"changes?|articles?|pages?|recent|random|real ?time)\b", re.IGNORECASE)


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    stripped = str(text or "").strip()
    if not stripped or re.search(r"\b(help|what can you do|commands?)\b", stripped, re.IGNORECASE):
        return "help", {}
    if not _WORDS.search(stripped):
        return "help", {}

    params: dict = {}
    count = _COUNT_RE.search(stripped)
    if count:
        params["count"] = int(count.group(1))
    seconds = _SECONDS_RE.search(stripped)
    if seconds:
        amount = float(seconds.group(1))
        if seconds.group(2).lower().startswith("m"):  # minutes, mins or a bare 'm'
            amount *= 60
        params["seconds"] = amount
    size = _BYTES_RE.search(stripped)
    if size:
        amount = float(size.group(1))
        params["min_delta"] = int(amount * 1000) if size.group(2).lower().startswith(("k", "kb", "kilobyte")) else int(amount)
    elif _BIG_RE.search(stripped):
        params["min_delta"] = 1000
    if _NEW_RE.search(stripped):
        params["only_new"] = True
    params["skip_bots"] = not _BOTS_IN_RE.search(stripped)
    wiki = _WIKI_RE.search(stripped)
    if wiki:
        params["wiki"] = f"{wiki.group(1).lower()}wiki"
        params["language"] = wiki.group(1).lower()
    else:
        language = _LANGUAGE_RE.search(stripped)
        if language and language.group(1).lower() not in _NOT_A_LANGUAGE:
            params["language"] = language.group(1).lower()
    return "firehose-edits", params


def render_event(index: int, event: dict) -> str:
    """One pushed line per event: where, what, how big, who, and why."""
    delta = event.get("delta")
    if delta is None:
        size = "size unknown"
    else:
        size = f"+{delta} bytes" if delta > 0 else f"{delta} bytes"
    kind = "NEW PAGE" if event.get("type") == "new" else ("minor edit" if event.get("minor") else "edit")
    flags = " [bot]" if event.get("bot") else ""
    line = (f"{index}. {event.get('server') or event.get('wiki')} - {event['title']} "
            f"({kind}{flags}, {size}) by {event['user']}")
    if event.get("comment"):
        comment = re.sub(r"\s+", " ", event["comment"])[:100]
        line += f'\n     "{comment}"'
    if event.get("url"):
        line += f"\n     {event['url']}"
    return line


class FirehoseAgent(AcpAgent):
    name = "firehose"
    title = "Firehose - live Wikimedia edit stream (push, not poll)"
    version = "1.0.0"

    def __init__(self, connection=None, data: FirehoseData | None = None) -> None:
        super().__init__(connection)
        self.data = data or FirehoseData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Firehose session opened. " + HELP.splitlines()[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Open Wikimedia's push stream", "medium"),
            ("Send each matching event as it arrives, until the count, the deadline or cancel",
             "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("Other agents in this repo poll and say so; this one is a push stream. "
                        "Nothing keeps reading it after the turn ends.")
            return STOP_END_TURN

        count = params.get("count") or 10
        gap = params.get("seconds") or 30.0
        tool = "call_firehose"
        raw = {key: value for key, value in params.items() if key in
               ("count", "seconds", "min_delta", "only_new", "skip_bots", "language", "wiki")}
        ctx.tool_call(tool, "Read the Wikimedia edit stream", kind="fetch", name=skill,
                      raw_input={"skill": skill, **raw})
        if not ctx.ask_permission(tool, "Allow Firehose to read the public Wikimedia edit stream?",
                                  remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to open the public Wikimedia stream first.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        watched = []
        if params.get("skip_bots"):
            watched.append("bots excluded")
        if params.get("only_new"):
            watched.append("new pages only")
        if params.get("language"):
            watched.append(f"{params['language']} wiki only")
        if params.get("min_delta"):
            watched.append(f"at least {params['min_delta']} bytes")
        ctx.message(f"Streaming {DATASET} for up to {count} events "
                    f"({gap:.0f}s at most; {'; '.join(watched) if watched else 'no filters'}). "
                    "Cancel the turn to stop.\n")

        started = time.monotonic()
        shown = 0
        last: dict | None = None
        try:
            for event in self.data.events(count=count, seconds=gap,
                                          skip_bots=params.get("skip_bots", True),
                                          only_new=bool(params.get("only_new")),
                                          language=params.get("language"),
                                          wiki=params.get("wiki"),
                                          min_delta=params.get("min_delta")):
                ctx.check_cancelled()  # the stream is busy: a cancel lands within one event
                shown += 1
                last = event
                ctx.message(render_event(shown, event) + "\n")
        except FirehoseError as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the stream: {exc}")
            return STOP_END_TURN

        stats = getattr(self.data, "stats", {"seen": shown, "matched": shown, "stopped": "count"})
        elapsed = time.monotonic() - started
        why = {"count": "the count you asked for", "deadline": "the time limit"}.get(
            stats.get("stopped"), "the end of the stream")
        summary = (f"Streamed {shown} event(s) from {stats.get('seen', 0)} seen in {elapsed:.0f}s "
                   f"(stopped at {why}).")
        if shown == 0:
            summary = (f"No events matched in {elapsed:.0f}s - the stream is live and busy, so "
                       "that is the filters, not a quiet wiki.")
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(summary))
        ctx.message(f"\n{summary}\nLast event: "
                    f"{last['title'] if last else 'none'}. Source: {DATASET}, pushed live; nothing "
                    "is still reading it now that this turn is over.")
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        """Nothing to clean up: the read loop checks the session's cancel flag per event."""
        return None


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        FirehoseAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
