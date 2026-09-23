"""Trending - what the world is reading today, from Wikipedia's own numbers.

This is the closest thing to a popularity feed that has no feed: Wikimedia publishes how many
times every article was read, so "what is trending?" is a real question with a real answer.

Two honest details ride along. The metrics lag by a day - the newest day available is
yesterday, UTC - and the raw top list is topped by `Main_Page` and `Special:Search`, which are
navigation rather than reading, so they are dropped and the number dropped is reported.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from data import DATASET, PROJECTS, TrendingData, TrendingError  # noqa: E402

HELP = (
    "I read Wikimedia's pageview metrics (keyless). Ask me:\n"
    "  - what is trending on Wikipedia today?\n"
    "  - top 10 articles yesterday\n"
    "  - what is Germany reading today?\n"
    "  - how many people read the Lizzie Borden article this week?\n"
    "  - pageviews for Ada Lovelace over the last 30 days\n"
    "The numbers are yesterday's (they lag a day), and I leave out the front page and search "
    "titles - and say how many I left out."
)

PERMISSION_KEY = "trending-read-wikimedia-metrics"

SKILLS = ("trending-top", "trending-article", "help")

_LIMIT_RE = re.compile(r"\b(?:top|first|biggest|most read)\s+(\d{1,2})\b|\b(\d{1,2})\s+"
                       r"(?:articles?|pages?|top)\b", re.IGNORECASE)
_DAYS_RE = re.compile(r"\b(?:last|past|over the last|for)\s+(\d{1,3})\s+days?\b", re.IGNORECASE)
_WINDOW = re.compile(r"\b(this (?:week|month)|last (?:week|month)|today|yesterday|"
                     r"the last (?:week|month))\b", re.IGNORECASE)
_PROJECT_RE = re.compile(r"\b(?:in|on|for)\s+(german|french|spanish|japanese|chinese|russian|"
                         r"italian|portuguese|arabic|dutch|polish|ukrainian)\b", re.IGNORECASE)
_TOP_WORDS = re.compile(r"\b(trending|top|most read|popular|pageviews today|reading|read)\b",
                        re.IGNORECASE)
_ARTICLE_WORDS = re.compile(r"\b(pageviews?|views|how many people read|readership|traffic)\b",
                            re.IGNORECASE)
_LANGUAGE = {"german": "de", "french": "fr", "spanish": "es", "japanese": "ja",
             "chinese": "zh", "russian": "ru", "italian": "it", "portuguese": "pt",
             "arabic": "ar", "dutch": "nl", "polish": "pl", "ukrainian": "uk"}


def project_from_text(text: str) -> str | None:
    """The wiki asked about by language name, or None for English."""
    match = _PROJECT_RE.search(str(text or ""))
    if not match:
        return None
    code = _LANGUAGE.get(match.group(1).lower())
    return PROJECTS.get(code or "", None)


def article_from_text(text: str) -> str | None:
    """The article name in a pageviews question, or None."""
    work = str(text or "").strip()
    match = re.search(r"\b(?:pageviews?|views?)\s+(?:for|of|on)\s+(.+?)(?=\s+(?:over|for|in|on|"
                      r"this|last)\b|[?.!]|$)", work, re.IGNORECASE)
    if not match:
        match = re.search(r"\b(?:read|reading)\s+(?:the\s+)?(.+?)\s+article(?=\s+(?:this|last|in)\b"
                          r"|[?.!]|$)", work, re.IGNORECASE)
    if not match:
        return None
    phrase = match.group(1).strip(" .,?!\"'")
    return phrase[:60] if phrase else None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    stripped = str(text or "").strip()
    if not stripped or re.search(r"\b(help|what can you do|commands?)\b", stripped, re.IGNORECASE):
        return "help", {}
    article = article_from_text(stripped)
    if article and _ARTICLE_WORDS.search(stripped):
        params: dict = {"article": article}
        project = project_from_text(stripped)
        if project:
            params["project"] = project
        days = _DAYS_RE.search(stripped)
        if days:
            params["days"] = int(days.group(1))
        elif re.search(r"\bthis week\b|\blast week\b", stripped, re.IGNORECASE):
            params["days"] = 7
        elif re.search(r"\bthis month\b|\blast month\b", stripped, re.IGNORECASE):
            params["days"] = 30
        return "trending-article", params
    if _TOP_WORDS.search(stripped):
        params = {}
        limit = _LIMIT_RE.search(stripped)
        if limit:
            params["limit"] = int(limit.group(1) or limit.group(2))
        project = project_from_text(stripped)
        if project:
            params["project"] = project
        return "trending-top", params
    return "help", {}


def render_top(reading: dict) -> str:
    """The day's list, and what was left out of it."""
    lines = [f"Most read on {reading['project']} for {reading['date']}:"]
    for row in reading["rows"]:
        lines.append(f"  {row['rank']:>2}. {row['title']} - {row['views']:,} views")
    if reading["navigation_skipped"]:
        lines.append(f"  (left out {reading['navigation_skipped']} navigation title(s) such as "
                     "the front page and search, out of the 100 Wikimedia listed)")
    return "\n".join(lines)


def render_article(reading: dict) -> str:
    """One article's daily views."""
    lines = [f"{reading['title']} on {reading['project']} "
             f"({reading['from']} to {reading['to']}):"]
    lines.append(f"  {reading['total']:,} views over {len(reading['rows'])} day(s)")
    lines.append(f"  best day {reading['best']['date']} with {reading['best']['views']:,}")
    for row in reading["rows"][-7:]:
        lines.append(f"  {row['date']}: {row['views']:,}")
    return "\n".join(lines)


class TrendingAgent(AcpAgent):
    name = "trending"
    title = "Trending - what the world is reading, from Wikipedia pageviews"
    version = "1.0.0"

    def __init__(self, connection=None, data: TrendingData | None = None) -> None:
        super().__init__(connection)
        self.data = data or TrendingData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Trending session opened. " + HELP.splitlines()[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read Wikimedia's pageview metrics", "medium"),
            ("Report the numbers, the day they cover and what was left out", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("These numbers are real reads, not an algorithm's guess, and they cover "
                        "yesterday rather than this minute.")
            return STOP_END_TURN

        tool = f"call_{skill.replace('-', '_')}"
        ctx.tool_call(tool, "Read Wikimedia pageviews", kind="fetch", name=skill,
                      raw_input=dict(params))
        if not ctx.ask_permission(tool, "Allow Trending to read Wikimedia's public pageview "
                                        "metrics?", remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the metrics first.")
            return STOP_REFUSAL
        ctx.tool_call_update(tool, status="in_progress")

        try:
            if skill == "trending-article":
                reading = self.data.article(params["article"], project=params.get("project"),
                                            days=params.get("days") or 7)
                body = render_article(reading)
                summary = f"{reading['total']:,} views for {reading['title']}"
            else:
                reading = self.data.top(project=params.get("project"),
                                        limit=params.get("limit") or 10)
                body = render_top(reading)
                summary = f"top {len(reading['rows'])} for {reading['date']}"
        except (TrendingError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the metrics: {exc}")
            return STOP_END_TURN

        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(summary))
        ctx.stream_text(body + "\n")
        ctx.message(f"\nSource: {DATASET}, read live. These metrics lag by a day (UTC), so the "
                    "newest day that exists is yesterday - I do not report a partial today.")
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        return None  # one read, one answer


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        TrendingAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
