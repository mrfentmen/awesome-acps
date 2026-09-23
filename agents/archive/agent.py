"""Archive - search the Internet Archive, and look inside one of its items.

The archive is the largest thing nobody queries from an editor: millions of films, recordings,
books and software, all with keyless JSON. Ask it "find Apollo 11 recordings" and it says how
many exist across the whole archive plus the most downloaded ones; ask "what is in the item
apollo11" and it lists the files a person can actually open, and says how many of the item's
files were derivatives that were left out.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from data import (DATASET, MEDIATYPES, ArchiveData, ArchiveError,  # noqa: E402
                  mediatype_from_text)

HELP = (
    "I search the Internet Archive (keyless) - films, recordings, books, software - and can\n"
    "open one item up. Ask me:\n"
    "  - find Apollo 11 recordings\n"
    "  - search the archive for old radio dramas\n"
    "  - is there anything about the 1918 flu in the archive?\n"
    "  - how many films about the moon landing exist?\n"
    "  - what is in the item Apollo11Audio?\n"
    "  - list the files in the item MisharyRasyidPerJuz\n"
    "Searches come back most-downloaded first and tell you the archive-wide total, not just the\n"
    "few I show; an item's file list keeps the files you can open and says how many it skipped."
)

PERMISSION_KEY = "archive-read-internet-archive"

SKILLS = ("archive-search", "archive-item", "help")

#: Only an explicit "top 5" / "5 results" is a count. A bare number is a name, and "Apollo 11
#: recordings" must keep its 11.
_LIMIT_RE = re.compile(r"\b(?:top|first|last|show|list|only)\s+(\d{1,2})\b|"
                       r"\b(\d{1,2})\s+(?:results?|hits?|items?)\b", re.IGNORECASE)
_COUNT_WORDS = re.compile(r"\b(how many|how much|count of|number of)\b", re.IGNORECASE)
_ITEM_RE = re.compile(r"\b(?:item|identifier|collection)\s+(?:called\s+|named\s+|id\s+)?"
                      r"[\"']?([A-Za-z0-9][A-Za-z0-9_.\-]{1,80})[\"']?", re.IGNORECASE)
_FILES_RE = re.compile(r"\b(?:files?|contents?|tracks?)\s+(?:in|inside|of|for)\s+"
                       r"[\"']?([A-Za-z0-9][A-Za-z0-9_.\-]{1,80})[\"']?", re.IGNORECASE)
_SEARCH_RE = re.compile(r"\b(?:search|find|look for|looking for|anything about|archive for|"
                        r"in the archive for)\b\s*(?:the\s+archive\s+)?(?:for\s+)?(.+)",
                        re.IGNORECASE)
_ITEM_WORDS = re.compile(r"\b(item|identifier|details page|metadata|what is in|files in|"
                         r"contents of|inside)\b", re.IGNORECASE)

#: Words to drop from a query once they have named the mediatype.
#: Glue words that carry no search meaning. Trimmed from the ends of a query, never the middle,
#: so "how many films about the moon landing exist" becomes "moon landing" and not "landing".
GLUE = {"the", "a", "an", "please", "me", "us", "i", "you", "is", "are", "was", "were",
        "do", "does", "did", "have", "has", "had", "there", "anything", "any", "some",
        "in", "on", "at", "for", "of", "to", "from", "about", "with", "that", "it",
        "exist", "exists", "search", "find", "look", "looking", "archive", "how", "many",
        "much", "number", "count", "tell", "show", "list", "give", "get", "and", "or"}


def clean_query(text: str, mediatype_word: str = "") -> str:
    """A query a title search can actually match.

    Drops the count ("top 4 ..."), and the mediatype word when it sits at either end
    ("books about beekeeping" -> beekeeping; "Apollo 11 recordings" -> Apollo 11), then trims glue
    from both ends. A mediatype word in the middle stays - in "old radio dramas" the radio is
    part of what is being asked for, not a filter word to throw away.
    """
    work = str(text or "")
    work = re.sub(r"^\s*(?:top|first|last|show|list|only)\s+\d{1,2}\b", " ", work,
                  flags=re.IGNORECASE)
    work = re.sub(r"^\s*\d{1,2}\s+(?:results?|hits?|items?)\b", " ", work, flags=re.IGNORECASE)
    work = re.sub(r"[^\w\s'\-]", " ", work)
    trim = set(GLUE)
    if mediatype_word:
        trim.add(mediatype_word.lower())
    words = work.split()
    while words and words[0].lower() in trim:
        words.pop(0)
    while words and words[-1].lower() in trim:
        words.pop()
    return " ".join(words)[:80]


def query_from_text(text: str) -> str | None:
    """The thing to search the archive for, or None."""
    match = _SEARCH_RE.search(str(text or ""))
    if not match:
        return None
    _, mediatype_word = mediatype_from_text(text)
    phrase = clean_query(match.group(1), mediatype_word)
    return phrase or None


def item_from_text(text: str) -> str | None:
    """The item identifier, or None."""
    work = str(text or "")
    for pattern in (_ITEM_RE, _FILES_RE):
        match = pattern.search(work)
        if match:
            return match.group(1)
    return None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    stripped = str(text or "").strip()
    if not stripped or re.search(r"\b(help|what can you do|commands?)\b", stripped, re.IGNORECASE):
        return "help", {}
    identifier = item_from_text(stripped)
    if identifier and _ITEM_WORDS.search(stripped) and not _COUNT_WORDS.search(stripped):
        params: dict = {"identifier": identifier}
        limit = _LIMIT_RE.search(stripped)
        if limit:
            params["limit"] = int(limit.group(1) or limit.group(2))
        return "archive-item", params
    query = query_from_text(stripped)
    if query:
        mediatype, _ = mediatype_from_text(stripped)
        return "archive-search", _search_params(stripped, query, mediatype)
    mediatype, mediatype_word = mediatype_from_text(stripped)
    #: No verb, no count, but a mediatype or a "find me things" shape - a bare noun is still a
    #: real search here ("film footage of the Hindenburg"), so search rather than shrug.
    if mediatype or _LIMIT_RE.search(stripped):
        fallback = clean_query(stripped, mediatype_word)
        if fallback:
            return "archive-search", _search_params(stripped, fallback, mediatype)
    return "help", {}


def _search_params(text: str, query: str, mediatype: str | None) -> dict:
    """Search params, with a count only when one was asked for."""
    params: dict = {"query": query}
    if mediatype:
        params["mediatype"] = mediatype
    limit = _LIMIT_RE.search(text)
    if limit:
        params["limit"] = int(limit.group(1) or limit.group(2))
    elif _COUNT_WORDS.search(text):
        params["limit"] = 3
    return params


def render_search(found: dict) -> str:
    """The hits, and the archive-wide total behind them."""
    where = f" ({found['mediatype']})" if found.get("mediatype") else ""
    total = found.get("total")
    head = (f"{total:,} item(s) match {found['query']!r}{where} in the Internet Archive:"
            if total else f"Items matching {found['query']!r}{where}:")
    lines = [head]
    for row in found["rows"]:
        bits = [row["mediatype"] or "?", str(row["year"]) if row["year"] else None,
                row["creator"] if row["creator"] else None]
        meta = ", ".join(bit for bit in bits if bit)
        downloads = (f"{row['downloads']:,} downloads" if row.get("downloads")
                     else "downloads not reported")
        lines.append(f"  {row['identifier']}")
        lines.append(f"    {row['title']}")
        lines.append(f"    {meta} - {downloads}" if meta else f"    {downloads}")
        lines.append(f"    {row['url']}")
    if total and total > len(found["rows"]):
        lines.append(f"  (I showed the {len(found['rows'])} most downloaded of {total:,})")
    return "\n".join(lines)


def render_item(item: dict) -> str:
    """One item, its files, and what was left out of them."""
    lines = [item["title"]]
    for label, value in (("identifier", item["identifier"]), ("creator", item["creator"]),
                         ("date", item["date"]), ("mediatype", item["mediatype"]),
                         ("collection", item["collection"]), ("subject", item["subject"])):
        if not value:
            continue
        if isinstance(value, list):
            value = ", ".join(str(part) for part in value[:4])
        lines.append(f"  {label}: {str(value)[:200]}")
    if item["description"]:
        lines.append(f"  description: {item['description'][:280]}")
    lines.append(f"  files: {item['files_kept']} you can open, "
                 f"{item['total_size_text']} in total")
    for row in item["rows"]:
        lines.append(f"    {row['name']}  [{row['format']}] {row['size_text']}")
    if item["files_skipped"]:
        lines.append(f"  (left out {item['files_skipped']} derivative file(s) - torrents, "
                     "thumbnails and metadata the archive makes for itself)")
    lines.append(f"  {item['url']}")
    return "\n".join(lines)


class ArchiveAgent(AcpAgent):
    name = "archive"
    title = "Archive - search the Internet Archive and open one item"
    version = "1.0.0"

    def __init__(self, connection=None, data: ArchiveData | None = None) -> None:
        super().__init__(connection)
        self.data = data or ArchiveData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Archive session opened. " + HELP.splitlines()[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read the Internet Archive's JSON APIs", "medium"),
            ("Report the hits, the archive-wide total and the files that were left out", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("Everything here is from the archive's own catalogue, read live.")
            return STOP_END_TURN

        tool = f"call_{skill.replace('-', '_')}"
        ctx.tool_call(tool, "Read the Internet Archive", kind="fetch", name=skill,
                      raw_input=dict(params))
        if not ctx.ask_permission(tool, "Allow Archive to read the Internet Archive's public "
                                        "catalogue?", remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the archive first.")
            return STOP_REFUSAL
        ctx.tool_call_update(tool, status="in_progress")

        try:
            if skill == "archive-item":
                item = self.data.item(params["identifier"], limit=params.get("limit") or 8)
                body = render_item(item)
                summary = (f"{item['identifier']}: {item['files_kept']} openable file(s), "
                           f"{item['total_size_text']}")
            else:
                found = self.data.search(params["query"], mediatype=params.get("mediatype"),
                                         limit=params.get("limit") or 5)
                body = render_search(found)
                summary = f"{len(found['rows'])} shown of {found['total']:,} match(es)"
        except (ArchiveError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the archive: {exc}")
            return STOP_END_TURN

        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(summary))
        ctx.stream_text(body + "\n")
        ctx.message(f"\nSource: {DATASET}, read live. Downloads are the archive's own all-time "
                    "counter, so they say what people came for over years, not what is hot "
                    "this hour.")
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        return None  # one read, one answer


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        ArchiveAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
