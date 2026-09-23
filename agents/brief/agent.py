"""Brief - gather live feeds, ask permission, then write a real file.

Two ACP features that only coding agents use today, used here for something that is not code:
`session/request_permission` (the agent asks before it acts) and `fs/write_text_file` (the
client writes the file, so the editor knows about it and keeps it in its own buffer).

That order is the point. The briefing is gathered first from five keyless feeds, then the file
is offered, then - only if you allow it - written to the workspace the editor launched the agent
in. Refuse, and nothing is written and the briefing is still printed in the chat. If the client
does not advertise the filesystem capability at all, this agent says so and prints the briefing
instead of pretending to have saved it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import (  # noqa: E402
    STOP_END_TURN,
    STOP_REFUSAL,
    AcpAgent,
    JsonRpcError,
    SessionContext,
    prompt_text,
)

from data import SECTIONS, BriefData, BriefError  # noqa: E402

FILENAME = "BRIEFING.md"

HELP = (
    "I gather live public feeds, then ask permission to write them into your workspace as "
    f"{FILENAME}. Ask me:\n"
    "  - brief me on Denver\n"
    "  - write a briefing for Tokyo\n"
    "  - give me a morning briefing for Miami\n"
    "Sections: weather, air quality, official weather alerts, earthquakes in the past day, and "
    "where the space station is. A dead source is reported as unavailable rather than dropped - "
    "and nothing is written until you approve the write."
)

PERMISSION_KEY = "brief-write-briefing-file"

SKILLS = ("brief-write", "brief-print", "help")

_PLACE_PATTERNS = (
    r"\b(?:brief(?:ing)?|brief me|update)\s+(?:me\s+)?(?:on|for|about)\s+(?:the\s+)?(.+?)(?=[?.!]|$)",
    r"\b(?:write|make|create|generate)\s+(?:me\s+)?(?:a\s+)?(?:morning|daily|quick|short)?\s*"
    r"brief(?:ing)?\s+(?:for|on|about)\s+(?:the\s+)?(.+?)(?=[?.!]|$)",
    r"\b(?:morning|daily|stand ?up)\s+brief(?:ing)?\s+(?:for|on)\s+(?:the\s+)?(.+?)(?=[?.!]|$)",
    r"\b(?:for|on|about)\s+(?:the\s+)?(.+?)(?=\s+(?:today|tonight|tomorrow)\b|[?.!]|$)",
)
_WRITE_WORDS = re.compile(r"\b(write|save|file|markdown|\.md|to disk|into my|workspace)\b",
                          re.IGNORECASE)
_PRINT_WORDS = re.compile(r"\b(just tell me|don'?t write|print|show me|in the chat|no file)\b",
                          re.IGNORECASE)


def place_from_text(text: str) -> str | None:
    """The place a briefing is for, or None."""
    work = str(text or "").strip()
    for pattern in _PLACE_PATTERNS:
        match = re.search(pattern, work, re.IGNORECASE)
        if not match:
            continue
        phrase = match.group(1).strip(" .,?!")
        phrase = re.split(r"[,;]", phrase)[0]  # "Denver, don't write a file" is about Denver
        phrase = re.sub(r"^(the|a|an)\s+", "", phrase, flags=re.IGNORECASE)
        phrase = re.sub(r"\b(in the chat|just tell me|don'?t write|no file|print|show me|please|"
                        r"for me|right now|today)\b.*$", "", phrase, flags=re.IGNORECASE).strip()
        if phrase and len(phrase.split()) <= 4:
            return phrase[:60]
    return None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    stripped = str(text or "").strip()
    if not stripped or re.search(r"\b(help|what can you do|commands?)\b", stripped, re.IGNORECASE):
        return "help", {}
    place = place_from_text(stripped)
    if not place:
        return "help", {}
    if _PRINT_WORDS.search(stripped):
        return "brief-print", {"place": place}
    if _WRITE_WORDS.search(stripped):
        return "brief-write", {"place": place}
    # 'brief me on Denver' with no instruction reads as the useful default: write the file,
    # which asks permission first anyway.
    return "brief-write", {"place": place}


class BriefAgent(AcpAgent):
    name = "brief"
    title = "Brief - live feeds written into your workspace, with your permission"
    version = "1.0.0"

    def __init__(self, connection=None, data: BriefData | None = None) -> None:
        super().__init__(connection)
        self.data = data or BriefData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Brief session opened. " + HELP.splitlines()[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Gather each feed independently", "medium"),
            ("Ask permission, then write the file into the workspace", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("I ask before writing, and I write through the editor's own filesystem "
                        "method, so the file shows up in your workspace rather than on the side.")
            return STOP_END_TURN

        place = params["place"]
        ctx.tool_call("call_briefing", f"Gather live feeds for {place}", kind="fetch",
                      name="brief-gather", raw_input={"skill": skill, "place": place})
        try:
            briefing = self.data.briefing(place)
        except (BriefError, ValueError) as exc:
            ctx.tool_call_update("call_briefing", status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not put a briefing together: {exc}")
            return STOP_END_TURN
        ctx.check_cancelled()
        markdown = self.data.markdown(briefing)
        sections = ", ".join(f"{section['name']}{' (unavailable)' if section['error'] else ''}"
                             for section in briefing["sections"])
        ctx.tool_call_update("call_briefing", status="completed",
                             content=ctx.text_content(f"{briefing['ok']} of "
                                                      f"{len(briefing['sections'])} sections: {sections}"))

        target = Path(ctx.session.cwd or ".") / FILENAME
        capabilities = (ctx.client_capabilities.get("fs") or {}) if ctx.client_capabilities else {}
        can_write = bool(capabilities.get("writeTextFile"))
        if skill == "brief-print" or not can_write:
            reason = ("you asked for it in the chat" if skill == "brief-print"
                      else "this client does not advertise fs.writeTextFile")
            ctx.stream_text(markdown)
            ctx.message(f"\nI have not written a file: {reason}. The briefing above is the same "
                        "text I would have saved.")
            return STOP_END_TURN

        write = "call_write_briefing"
        ctx.tool_call(write, f"Write {FILENAME}", kind="edit", name="brief-write",
                      raw_input={"path": str(target), "bytes": len(markdown.encode())},
                      locations=[{"path": str(target)}])
        if not ctx.ask_permission(write, f"Write {len(markdown.encode())} bytes to {target}?",
                                  remember_key=PERMISSION_KEY):
            ctx.tool_call_update(write, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("Nothing was written. Here is the briefing instead:\n")
            ctx.stream_text(markdown)
            return STOP_REFUSAL

        try:
            ctx.conn.request("fs/write_text_file",
                             {"sessionId": ctx.session.id, "path": str(target), "content": markdown},
                             timeout=30)
        except JsonRpcError as exc:
            ctx.tool_call_update(write, status="failed", content=ctx.text_content(exc.message))
            ctx.message(f"The editor refused the write ({exc.message}); nothing was saved. Here "
                        "is the briefing:\n")
            ctx.stream_text(markdown)
            return STOP_END_TURN

        ctx.tool_call_update(write, status="completed",
                             content=ctx.text_content(f"wrote {FILENAME}"))
        summary = (f"Wrote {target} ({len(markdown.encode())} bytes, "
                   f"{briefing['ok']} of {len(briefing['sections'])} sections live).")
        ctx.message(f"{summary}\n\nGenerated {briefing['generated_utc']}. A source that was down "
                    "is marked unavailable in the file rather than silently missing.")
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        return None  # one gather, one write


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        BriefAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
