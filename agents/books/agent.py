"""Books — Open Library inside your editor.

No ACP agent looks up a book. This one searches Open Library's 40-million-record catalog,
pulls a work's own description, and reads an author's record and works - with the record
ids it used, so any claim can be checked.

Deterministic on purpose: routing is rules, everything printed is Open Library's, and
nothing is guessed. It reports a plan, opens one tool call per lookup, asks permission
before its first read, streams the answer, and closes the tool call with a one-line summary.
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
    DATASET_AUTHOR,
    DATASET_SEARCH,
    DATASET_TRENDING,
    DATASET_WORK,
    BooksData,
    BooksError,
)

HELP = (
    "I read Open Library (keyless): the Internet Archive's catalog of books, authors and\n"
    "editions. Ask me:\n"
    "  • find books about urban foxes\n"
    "  • what is Dune about? (I will search, then read the work record)\n"
    "  • what has Ursula K. Le Guin written?\n"
    "  • what is OL893414W - or what is popular on Open Library today\n"
    "Every answer carries the record id it came from. I only know what the catalog says."
)

PERMISSION_KEY = "books-read-public-openlibrary"

SKILLS = ("books-search", "books-work", "books-author", "books-trending", "help")

_WORK_RE = re.compile(r"\b(OL\d+W)\b", re.IGNORECASE)
_TRENDING_WORDS = re.compile(
    r"\b(trending|popular|most read|everyone(?:'s| is) reading|what people are reading|bestsellers?)\b",
    re.IGNORECASE)
_BY_RE = re.compile(r"\b(?:books? by|works by|written by|by)\s+([A-Za-z][\w.'-]*(?:\s+[A-Za-z][\w.'-]*){0,3})",
                     re.IGNORECASE)
_HAS_WRITTEN_RE = re.compile(r"\bhas\s+([A-Za-z][\w.'-]*(?:\s+[A-Za-z][\w.'-]*){0,3})\s+written\b",
                             re.IGNORECASE)
_AUTHOR_WORDS = re.compile(r"\b(bibliography|author(?:'s)?|who wrote|what has)\b", re.IGNORECASE)
_ABOUT_WORDS = re.compile(r"\b(about|on the subject of)\b", re.IGNORECASE)

#: Words that are the question, not the search. Stripped wherever they appear.
_QUERY_STOP_WORDS = frozenset({
    "please", "can", "could", "you", "find", "search", "for", "look", "up", "show", "me",
    "tell", "give", "get", "what", "whats", "which", "is", "are", "was", "any", "a", "an",
    "the", "some", "book", "books", "novel", "novels", "read", "reads", "reading", "about",
    "of", "on", "called", "titled", "named", "subject", "written", "wrote", "writes", "has",
    "who", "by", "and", "to",
})


def query_from_text(text: str) -> str:
    """The words to search for: the question with its asking-words peeled off."""
    cleaned = re.sub(r"[^\w\s'-]", " ", text)
    words = [word for word in cleaned.split() if word.lower().strip("'") not in _QUERY_STOP_WORDS]
    return " ".join(words).strip(" -")


def author_from_text(text: str) -> str | None:
    """An author's name: after 'by', between 'has ... written', or the remainder of a
    bibliography question. 'Who wrote X' is a book question, so it returns None."""
    match = _BY_RE.search(text)
    if match:
        return match.group(1).strip(" ?.!,") or None
    match = _HAS_WRITTEN_RE.search(text)
    if match:
        return match.group(1).strip(" ?.!,") or None
    if re.search(r"\b(bibliography|author(?:'s)?)\b", text, re.IGNORECASE):
        return query_from_text(text) or None
    return None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    params: dict = {}
    work = _WORK_RE.search(text)
    if work:
        params["work"] = work.group(1).upper()
        return "books-work", params
    if _TRENDING_WORDS.search(text):
        params["trending"] = True
        return "books-trending", params
    author = author_from_text(text)
    if author:
        params["author"] = author
        return "books-author", params
    query = query_from_text(text)
    if _ABOUT_WORDS.search(text) and query:
        params["query"] = query
        return "books-search", params
    if query:
        params["query"] = query
        return "books-search", params
    return "books-search", params


class BooksAgent(AcpAgent):
    name = "books"
    title = "Books — Open Library"
    version = "1.0.0"

    def __init__(self, connection=None, data: BooksData | None = None) -> None:
        super().__init__(connection)
        self.data = data or BooksData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Books session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read the matching Open Library record", "medium"),
            ("Answer with the record id and the catalog's own words", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("One source: Open Library, the Internet Archive's catalog. It is a "
                        "catalog, not a bookseller - I cannot tell you a price or a stock level.")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read Open Library for {skill}", kind="fetch", name=skill,
                      raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Books to read Open Library's public catalog?",
                                 remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the public catalog before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (BooksError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read Open Library: {exc}")
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
        if skill == "books-work":
            return self._work(params)
        if skill == "books-author":
            return self._author(params)
        if skill == "books-trending":
            return self._trending(params)
        if skill == "books-search":
            return self._search(params)
        return HELP, {"summary": "help", "dataset": None}

    def _work(self, params: dict) -> tuple[str, dict]:
        reference = str(params.get("work") or "")
        try:
            work = self.data.work(reference)
        except ValueError as exc:
            return (f"{exc}.", {"summary": "bad work id", "dataset": DATASET_WORK, "known": False})
        artifact = {
            "summary": f"{DATASET_WORK}{work['key']}: {work['title']}"
                       + (f" ({work['first_publish_date']})" if work["first_publish_date"] else ""),
            "dataset": DATASET_WORK,
            "key": work["key"],
            "title": work["title"],
            "first_publish_date": work["first_publish_date"],
            "subjects": work["subjects"],
            "description": work["description"],
            "links": work["links"],
        }
        lines = [f"{work['title']}"
                 + (f" ({work['first_publish_date']})" if work["first_publish_date"] else "")
                 + f" - {work['key']}:"]
        if work["description"]:
            description = work["description"]
            lines.append("  " + (description if len(description) <= 700 else description[:697] + "..."))
        else:
            lines.append("  Open Library has no description for this work record.")
        if work["subjects"]:
            lines.append("  • Subjects: " + ", ".join(work["subjects"][:6]))
        if work["links"]:
            lines.append("  • Links on the record: " + ", ".join(link for link in work["links"][:2]))
        lines.append(f"\nSource: {DATASET_WORK}{work['key']} (read live). Descriptions in Open "
                     "Library are contributed by the public and vary in quality; the subjects come "
                     "from the catalog's own tagging.")
        return "\n".join(lines), artifact

    def _search(self, params: dict) -> tuple[str, dict]:
        query = str(params.get("query") or "")
        if not query:
            return (
                "I need something to search for - a title, a subject or an author. For example "
                "'books about urban foxes', 'Dune', or 'books by Ursula K. Le Guin'.",
                {"summary": "no query given", "dataset": DATASET_SEARCH, "known": False},
            )
        result = self.data.search(query, limit=int(params.get("limit") or 5))
        artifact = {
            "summary": f"{DATASET_SEARCH}: {len(result['books'])} of {result['found']} record(s) "
                       f"for {query!r}",
            "dataset": DATASET_SEARCH,
            "query": query,
            "found": result["found"],
            "books": result["books"],
        }
        if not result["books"]:
            return (
                f"Open Library has no record matching {query!r}.\n\nSource: {DATASET_SEARCH} "
                "(read live). Try fewer or plainer words - the catalog matches titles, authors and "
                "subject tags, not full sentences.",
                artifact,
            )
        lines = [f"{result['found']} record(s) match {query!r} in Open Library; the first "
                 f"{len(result['books'])}:"]
        for book in result["books"]:
            authors = ", ".join(book["authors"][:2]) or "author not listed"
            year = book["first_publish_year"] or "year unknown"
            editions = book["edition_count"] if book["edition_count"] is not None else "?"
            lines.append(f"  • {book['title']} - {authors} ({year}), {editions} edition(s)"
                         + (f", {book['key']}" if book["key"] else ""))
        lines.append(f"\nSource: {DATASET_SEARCH} (read live). Numbers are catalog counts, not "
                     "sales. Ask me about an id above and I will read that work record.")
        return "\n".join(lines), artifact

    def _author(self, params: dict) -> tuple[str, dict]:
        name = str(params.get("author") or "").strip()
        if not name:
            return (
                "I need an author name - for example 'books by Ursula K. Le Guin'.",
                {"summary": "no author given", "dataset": DATASET_AUTHOR, "known": False},
            )
        author = self.data.author(name, works=int(params.get("works") or 5))
        artifact = {
            "summary": f"{DATASET_AUTHOR}{author['key']}: {author['name']}, "
                       f"{author['work_count']} work(s) in the catalog",
            "dataset": DATASET_AUTHOR,
            "key": author["key"],
            "name": author["name"],
            "birth_date": author["birth_date"],
            "death_date": author["death_date"],
            "work_count": author["work_count"],
            "top_work": author["top_work"],
            "bio": author["bio"],
            "works": author["works"],
        }
        dates = " - ".join(part for part in (author["birth_date"], author["death_date"]) if part)
        lines = [f"{author['name']} ({author['key']}"
                 + (f", {dates}" if dates else "") + "):"]
        if author["bio"]:
            bio = author["bio"]
            lines.append("  " + (bio if len(bio) <= 500 else bio[:497] + "..."))
        if author["work_count"] is not None:
            lines.append(f"  • {author['work_count']} work(s) in the catalog"
                         + (f", best known for {author['top_work']}" if author["top_work"] else ""))
        if author["works"]:
            lines.append("  • Works as Open Library lists them (its own order, not ranked):")
            for item in author["works"]:
                lines.append(f"      - {item['title']} ({item['key']})")
        lines.append(f"\nSource: {DATASET_AUTHOR}{author['key']} and its works list (read live). "
                     "The works list is incomplete - catalogs list what they hold, not everything "
                     "someone ever published.")
        return "\n".join(lines), artifact

    def _trending(self, params: dict) -> tuple[str, dict]:
        trending = self.data.trending(limit=int(params.get("limit") or 5))
        artifact = {
            "summary": f"{DATASET_TRENDING}: {len(trending['books'])} work(s) popular today",
            "dataset": DATASET_TRENDING,
            "books": trending["books"],
        }
        if not trending["books"]:
            return (f"Open Library's trending feed is empty right now.\n\nSource: {DATASET_TRENDING}.",
                    artifact)
        lines = ["Popular on Open Library today (its own trending feed):"]
        for book in trending["books"]:
            authors = ", ".join(book["authors"]) or "author not listed"
            lines.append(f"  • {book['title']} - {authors}"
                         + (f" ({book['first_publish_year']})" if book["first_publish_year"] else "")
                         + (f", {book['key']}" if book["key"] else ""))
        lines.append(f"\nSource: {DATASET_TRENDING} (read live). \"Trending\" means page views on "
                     "Open Library, which is not a sales chart - it is what people are looking up here.")
        return "\n".join(lines), artifact


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        BooksAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
