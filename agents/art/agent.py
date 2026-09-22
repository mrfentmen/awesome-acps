"""Art — museum collections inside your editor.

No ACP agent could look at art before this one. It searches the keyless open-access APIs
of the Art Institute of Chicago, the Cleveland Museum of Art and The Met, and can pull a
random public-domain piece from the Met when you just want to see something.

Deterministic on purpose: routing is rules, the titles are the museums', and nothing is
invented. It reports a plan, opens one tool call per lookup, asks permission before its
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

from data import DATASET_AIC, DATASET_MET, ArtData, ArtError  # noqa: E402

HELP = (
    "I search three keyless museum collections. Ask me:\n"
    "  • paintings by Monet\n"
    "  • find art about the sea\n"
    "  • surprise me with a piece\n"
    "I answer with the work, the artist, the date and a link; the random pick is "
    "public-domain and comes from The Met."
)

PERMISSION_KEY = "art-read-public-museums"

SKILLS = ("art-search", "art-random", "help")

_RANDOM_RE = re.compile(r"\b(surprise|random|anything|serendipity|lucky dip)\b", re.IGNORECASE)
_QUERY_AFTER_RE = re.compile(
    r"\b(?:about|by|of|with|depicting|showing|search for|search|find|show me|works? by|"
    r"paintings? by|sculptures? by|pieces? by)\s+([A-Za-z0-9][\w'\- ]{1,60})", re.IGNORECASE)

_STOP_TAIL_WORDS = frozenset({
    "please", "me", "for", "the", "a", "an", "art", "artwork", "artworks", "painting",
    "paintings", "work", "works", "piece", "pieces", "collection", "museum", "right", "now",
    "today",
})


def query_from_text(text: str) -> str | None:
    """The search terms in a question, or None. Keeps the original case."""
    match = _QUERY_AFTER_RE.search(text)
    if not match:
        return None
    words = match.group(1).strip(" .,?!'\"()").split()
    # "find art about the sea" - drop the art words between the verb and the subject.
    while words and words[0].lower() in _STOP_TAIL_WORDS:
        words.pop(0)
    while words and words[-1].lower() in _STOP_TAIL_WORDS:
        words.pop()
    query = " ".join(words)
    query = re.sub(r"^(?:about|by|of|with|depicting|showing)\s+", "", query, flags=re.IGNORECASE)
    words = query.split()
    while words and words[0].lower() in _STOP_TAIL_WORDS:
        words.pop(0)
    query = " ".join(words)
    return query if len(query) >= 2 else None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}
    if _RANDOM_RE.search(text):
        return "art-random", {}
    query = query_from_text(text)
    if query:
        return "art-search", {"query": query}
    return "art-search", {}


class ArtAgent(AcpAgent):
    name = "art"
    title = "Art — three museum collections"
    version = "1.0.0"

    def __init__(self, connection=None, data: ArtData | None = None) -> None:
        super().__init__(connection)
        self.data = data or ArtData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Art session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Ask the museum APIs for matching works", "medium"),
            ("Report the work, artist, date and link", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("I read the open-access APIs of the Art Institute of Chicago, the "
                        "Cleveland Museum of Art and The Met. Images stay on the museums' "
                        "servers; I only link to them.")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read museum collections for {skill}", kind="fetch", name=skill,
                      raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Art to read the public museum collection APIs?",
                                 remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the public museum collections before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (ArtError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the museum collections: {exc}")
            return STOP_END_TURN
        ctx.check_cancelled()
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(artifact["summary"]))
        ctx.stream_text(answer)
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        # Every skill is a few HTTP reads; nothing long-running to interrupt.
        return None

    # -- skills ------------------------------------------------------------

    def _run_skill(self, skill: str, params: dict) -> tuple[str, dict]:
        if skill == "art-search":
            return self._search(params)
        if skill == "art-random":
            return self._random()
        return HELP, {"summary": "help", "dataset": None}

    @staticmethod
    def _line(piece: dict) -> str:
        parts = [piece.get("title") or "untitled"]
        if piece.get("artist"):
            parts.append(piece["artist"])
        if piece.get("date"):
            parts.append(piece["date"])
        return " - ".join(parts)

    def _search(self, params: dict) -> tuple[str, dict]:
        query = params.get("query")
        if not query:
            return (
                "What should I look for? Give me an artist, a subject or a title, like 'Monet', "
                "'the sea' or 'Hokusai'.",
                {"summary": "no query given", "dataset": DATASET_AIC, "known": False},
            )
        result = self.data.search(query, limit=3)
        artifact = {
            "summary": f"{result['dataset']}: {len(result['rows'])} works for {result['query']}",
            "dataset": result["dataset"],
            "query": result["query"],
            "rows": result["rows"],
        }
        museum = result["rows"][0]["museum"] if result["rows"] else "a museum"
        lines = [f"Works matching {result['query']!r} ({museum}):"]
        for piece in result["rows"]:
            lines.append(f"  • {self._line(piece)}")
            if piece.get("medium"):
                lines.append(f"    {piece['medium']}")
            if piece.get("url"):
                lines.append(f"    {piece['url']}")
        lines.append(
            f"\nSource: {result['dataset']} (read live). I try the Art Institute of Chicago, "
            "then Cleveland, then The Met, and say which one answered."
        )
        return "\n".join(lines), artifact

    def _random(self) -> tuple[str, dict]:
        result = self.data.random_piece()
        piece = result["piece"]
        artifact = {
            "summary": f"{DATASET_MET}: {self._line(piece)} (public domain)",
            "dataset": DATASET_MET,
            "piece": piece,
            "pool": result["pool"],
        }
        lines = [
            "A random public-domain piece from The Met:",
            f"  • {self._line(piece)}",
        ]
        if piece.get("medium"):
            lines.append(f"    {piece['medium']}")
        if piece.get("department"):
            lines.append(f"    {piece['department']}")
        if piece.get("url"):
            lines.append(f"    {piece['url']}")
        lines.append(
            f"\nSource: {DATASET_MET} (read live, drawn from {result['pool']:,} object ids). "
            "Public domain means the museum says you may reuse it."
        )
        return "\n".join(lines), artifact


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        ArtAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
