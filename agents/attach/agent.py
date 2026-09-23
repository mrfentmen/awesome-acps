"""attach - the agent that reads the file the client attached to the prompt.

No agent in the registry uses the protocol's `embeddedContext` surface, so nothing answers
"what is in this file I gave you?". A prompt may carry a `resource` block (text the client
already embedded, or a `file://` uri) or a `resource_link`. This agent reads whichever it is
given, and when the client advertises `fs.readTextFile` it asks the client for the file over
ACP instead of guessing a path or shelling out.

Everything it says about the file is a count it made - lines, columns, keys, headings, hashes -
and every counting rule is printed with it. Nothing is sent anywhere: there is no feed here.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, JsonRpcError, prompt_text  # noqa: E402

from data import MAX_COLUMNS, MAX_KEYS, AttachError, name_of, parse_file_uri, profile  # noqa: E402

HELP = (
    "I read the file you attach to me in your editor - nothing else, and nothing leaves this "
    "machine. Ask me:\n"
    "  - what is in this file?\n"
    "  - profile this CSV\n"
    "  - how many rows and columns are in the table I attached?\n"
    "  - which columns have missing values?\n"
    "  - what are the top-level keys in this JSON?\n"
    "  - outline this markdown file\n"
    "  - how big is this file, and what is its hash?\n"
    "I count lines, rows, columns, keys, headings and words, and I say how each count was made. "
    "Attach a file (or two) and ask."
)

PERMISSION_KEY = "attach-read-file"

SKILLS = ("attach-profile", "attach-help")

#: Long files are read in full only up to this many lines, and the answer says so.
MAX_LINES = 20000

#: More attachments than this in one prompt, and the rest are named but not read.
MAX_FILES = 8

_WANTS = {
    "rows": re.compile(r"\b(row|rows|record|records|lines of data)\b", re.IGNORECASE),
    "columns": re.compile(r"\b(col|cols|column|columns|header|headers|field|fields)\b", re.IGNORECASE),
    "missing": re.compile(r"\b(missing|empty|blank|null|nulls|gaps|incomplete)\b", re.IGNORECASE),
    "keys": re.compile(r"\b(key|keys|schema|structure|shape|nested|fields)\b", re.IGNORECASE),
    "headings": re.compile(r"\b(heading|headings|outline|sections|table of contents|toc)\b",
                           re.IGNORECASE),
    "lines": re.compile(r"\b(line|lines|how many lines|length)\b", re.IGNORECASE),
    "size": re.compile(r"\b(size|big|large|bytes|kilobytes|megabytes|hash|sha|checksum)\b",
                       re.IGNORECASE),
    "counts": re.compile(r"\b(function|functions|class|classes|imports|comments|words|"
                         r"duplicate)\b", re.IGNORECASE),
    "sample": re.compile(r"\b(sample|example|first row|show me a row|preview)\b", re.IGNORECASE),
}


def wants(text: str) -> set[str]:
    """Which details the question names, so the answer leads with those."""
    found = {name for name, pattern in _WANTS.items() if pattern.search(str(text or ""))}
    return found or {"everything"}


def attachments(prompt: list[dict]) -> list[dict]:
    """Every file the client attached: embedded text, a file uri, or a link it cannot read."""
    found = []
    for block in prompt or []:
        kind = block.get("type")
        if kind == "resource":
            resource = block.get("resource") or {}
            uri = str(resource.get("uri") or "")
            found.append({"name": name_of(uri) or "attachment", "uri": uri,
                          "path": parse_file_uri(uri), "text": resource.get("text"),
                          "mime": resource.get("mimeType")})
        elif kind == "resource_link":
            uri = str(block.get("uri") or "")
            found.append({"name": str(block.get("name") or name_of(uri) or "link"), "uri": uri,
                          "path": parse_file_uri(uri), "text": None,
                          "mime": block.get("mimeType")})
    return found


def route(text: str, attached: int) -> tuple[str, dict]:
    """Deterministic routing: with nothing attached this agent can only explain itself."""
    stripped = str(text or "").strip()
    if attached:
        return "attach-profile", {"wants": sorted(wants(stripped))}
    return "attach-help", {}


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size / (1024 * 1024):.1f} MiB"


def render(reading: dict, asked: set[str]) -> str:
    """The profile of one file. A question about rows or columns widens the lists."""
    details = reading["details"]
    columns_shown = MAX_COLUMNS if asked & {"rows", "columns", "missing"} else 12
    keys_shown = MAX_KEYS if "keys" in asked else 15
    lines = [f"{reading['name']}  ({reading['kind']}, {human_size(reading['bytes'])}, "
             f"{reading['lines']} lines, sha256 {reading['sha256']}...)"]
    lines.append(f"  - characters {reading['characters']}, blank lines {reading.get('blank_lines', 0)}, "
                 f"longest line {reading.get('longest_line', 0)} "
                 f"(line {reading.get('longest_line_number', 0)}), "
                 f"ends with a newline: {'yes' if reading['final_newline'] else 'no'}")

    if reading["kind"] == "csv" or reading["kind"] == "tsv":
        lines.append(f"  - table: {details['rows']} row(s) of data, {details['column_count']} "
                     f"column(s), delimiter {details['delimiter']!r}")
        lines.append(f"  - header: {', '.join(details['header'][:12])}"
                     + (" ..." if details["column_count"] > 12 else ""))
        for column in details["columns"][:columns_shown]:
            mark = f", {column['empty']} empty" if column["empty"] else ""
            lines.append(f"      {column['name']}: {column['type']}, {column['filled']} filled{mark}")
        if details["column_count"] > columns_shown:
            lines.append(f"      ... and {details['column_count'] - columns_shown} more column(s); "
                         "ask me about the columns to see them all")
        if asked & {"sample", "everything"} and details["sample"]:
            for row in details["sample"]:
                lines.append(f"  - sample: {', '.join(row[:12])}")
        empty = [column["name"] for column in details["columns"] if column["empty"]]
        if empty:
            lines.append(f"  - columns with empty values: {', '.join(empty[:12])}")
    elif reading["kind"] == "json":
        shape = details["shape"]
        lines.append(f"  - top level: {details['top_level']}"
                     + (f" with {details['top_level_size']} entries" if details["top_level_size"] is not None else ""))
        for key in shape.get("keys", [])[:20]:
            size = f", {key['size']}" if key["size"] is not None else ""
            lines.append(f"      {key['key']}: {key['type']}{size}")
        if details["key_paths"]:
            lines.append(f"  - key paths ({len(details['key_paths'])}"
                         + (f"+ of {MAX_KEYS}" if details["key_paths_truncated"] else "")
                         + f"): {', '.join(details['key_paths'][:keys_shown])}")
    elif reading["kind"] == "jsonl":
        lines.append(f"  - records: {details['records']} JSON object(s) on their own lines, "
                     f"{len(details['keys'])} distinct key(s)")
        lines.append(f"  - keys: {', '.join(details['keys'][:15])}")
    elif reading["kind"] == "markdown":
        lines.append(f"  - {details['heading_count']} heading(s), {details['links']} link(s), "
                     f"{details['code_fences']} code block(s), {details['table_rows']} table row(s), "
                     f"{details['words']} word(s)")
        for heading in details["headings"][:keys_shown]:
            lines.append(f"      {'  ' * (heading['level'] - 1)}{'#' * heading['level']} {heading['text']}")
    elif reading["kind"] == "code":
        counts = details["counts"]
        lines.append(f"  - {details['language']}: {counts['functions']} function(s), "
                     f"{counts['classes']} class/struct(s), {counts['imports']} import(s), "
                     f"{counts['comments']} comment line(s), {details['words']} word(s)")
        if details["names"].get("functions"):
            lines.append(f"  - functions seen: {', '.join(details['names']['functions'][:12])}")
    elif reading["kind"] in {"text", "empty", "binary"} and details:
        lines.append(f"  - {details.get('words', 0)} word(s), {details.get('unique_words', 0)} "
                     f"unique, {details.get('paragraphs', 0)} paragraph(s), "
                     f"{details.get('duplicate_lines', 0)} duplicate line(s), "
                     f"{details.get('sentences', 0)} sentence end(s)")

    for note in reading["notes"]:
        lines.append(f"  - note: {note}")
    return "\n".join(lines)


class AttachAgent(AcpAgent):
    name = "attach"
    title = "Attach - reads the file you attached, and profiles it"
    version = "1.0.0"

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Attach session opened. " + HELP.splitlines()[0])
        return {}

    def _capabilities(self, ctx: SessionContext) -> dict:
        return (ctx.client_capabilities.get("fs") or {}) if ctx.client_capabilities else {}

    def _read_over_fs(self, ctx: SessionContext, item: dict) -> str:
        """Ask the client for the file. Raises AttachError with the client's own words."""
        if not self._capabilities(ctx).get("readTextFile"):
            raise AttachError(f"this client does not advertise fs.readTextFile, so I cannot read "
                              f"{item['path']}: attach the text itself instead")
        reply = ctx.conn.request("fs/read_text_file",
                                 {"sessionId": ctx.session.id, "path": item["path"],
                                  "limit": MAX_LINES},
                                 timeout=30)
        text = (reply or {}).get("content")
        if not isinstance(text, str):
            raise AttachError(f"the client answered without file content for {item['path']}")
        return text

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        files = attachments(prompt)
        skill, params = route(text, len(files))
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read the attached file (embedded text, or fs/read_text_file)", "medium"),
            ("Count what is in it and say how each count was made", "medium"),
        ])

        if skill == "attach-help":
            ctx.stream_text(HELP)
            if files:
                ctx.message("I can see an attachment but nothing to do with it yet - ask me a "
                            "question about it.")
            return STOP_END_TURN

        asked = set(params.get("wants") or [])
        reading = "call_attach_profile"
        ctx.tool_call(reading, f"Profile {len(files)} attached file(s)", kind="read",
                      name="attach-profile", raw_input={"files": [item["name"] for item in files]})

        blocks, unreadable, read_over_fs = [], [], []
        refused = False
        for item in files[:MAX_FILES]:
            if item["text"] is not None:
                blocks.append(self._one(item, item["text"], "embedded in the prompt", asked,
                                       truncated=False))
                continue
            if not item["path"]:
                unreadable.append(f"{item['name']} ({item['uri'] or 'no uri'}): I read files, not "
                                  "web links, so I cannot open this one")
                continue
            call_id = f"call_read_{len(read_over_fs)}"
            if not self._capabilities(ctx).get("readTextFile"):
                # There is no point asking to read a file the client has already said it cannot
                # hand over; say that instead of asking and then failing.
                ctx.tool_call(call_id, f"Read {item['name']}", kind="read", name="attach-read",
                              status="failed", raw_input={"path": item["path"]})
                unreadable.append(f"{item['name']}: this client does not advertise "
                                  "fs.readTextFile, so I cannot read it - attach the text itself "
                                  "instead")
                continue
            if not ctx.ask_permission(call_id, f"Allow attach to read {item['path']}?",
                                      remember_key=PERMISSION_KEY):
                refused = True
                ctx.tool_call(call_id, f"Read {item['name']}", kind="read", name="attach-read",
                              status="failed", raw_input={"path": item["path"]})
                unreadable.append(f"{item['name']}: you did not allow the read")
                continue
            ctx.tool_call(call_id, f"Read {item['name']}", kind="read", name="attach-read",
                          status="in_progress", raw_input={"path": item["path"]},
                          locations=[{"path": item["path"]}])
            try:
                content = self._read_over_fs(ctx, item)
            except (JsonRpcError, AttachError) as exc:
                message = getattr(exc, "message", None) or str(exc)
                ctx.tool_call_update(call_id, status="failed", content=ctx.text_content(message))
                unreadable.append(f"{item['name']}: {message}")
                continue
            read_over_fs.append(item["path"])
            ctx.tool_call_update(call_id, status="completed",
                                 content=ctx.text_content(f"{len(content)} characters"))
            blocks.append(self._one(item, content, "read over ACP", asked,
                                    truncated=len(content.splitlines()) >= MAX_LINES))

        if len(files) > MAX_FILES:
            unreadable.append(f"{len(files) - MAX_FILES} more attachment(s) were not read: this "
                              f"agent profiles at most {MAX_FILES} at a time")

        if not blocks:
            ctx.tool_call_update(reading, status="failed",
                                 content=ctx.text_content("; ".join(unreadable) or "nothing to read"))
            ctx.message("I could not read anything that was attached:\n  - " + "\n  - ".join(unreadable))
            return STOP_REFUSAL if refused else STOP_END_TURN

        ctx.tool_call_update(reading, status="completed",
                            content=ctx.text_content(f"{len(blocks)} file(s), "
                                                     f"{', '.join(item['kind'] for item in blocks)}"))
        ctx.stream_text("\n\n".join(block["text"] for block in blocks) + "\n")
        if unreadable:
            ctx.message("\nNot read:\n  - " + "\n  - ".join(unreadable))
        source = ("the text your editor embedded in the prompt" if not read_over_fs
                  else "read with fs/read_text_file over ACP from " + ", ".join(read_over_fs))
        ctx.message(f"\nSource: {source}. No request left this machine: every number above is a "
                    "count of what was attached, and the counting rule is printed with it.")
        return STOP_END_TURN

    def _one(self, item: dict, content: str, how: str, asked: set[str], truncated: bool) -> dict:
        """One file's block for the answer, or a block saying why it could not be profiled."""
        try:
            reading = profile(item["name"], content)
        except AttachError as exc:
            return {"kind": "error", "text": f"{item['name']}  ({how})\n  - {exc}"}
        if truncated:
            reading["notes"].append(f"only the first {MAX_LINES} lines were read, so every count "
                                    "above is of those lines, not of a longer file.")
        return {"kind": reading["kind"],
                "text": render(reading, asked) + f"\n  - how I got it: {how}"}

    def on_cancel(self, session) -> None:
        return None  # one read, one answer


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        AttachAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
