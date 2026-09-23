"""Profile a text file, using nothing but the standard library and no network at all.

This is the only agent here whose data source is the file the *client* hands it, so there is
no feed, no cache and no transport in this module: the ACP prompt carries either the file's
text (`resource` with `text`) or a `file://` uri the agent reads with `fs/read_text_file`.

The rule everywhere else in this repo holds here too: nothing is invented. A profile says
what was counted and how, and anything that would need a real parser says so - the Python
and JavaScript counts come from regexes over the text, and are labelled as such rather than
presented as an abstract syntax tree.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from urllib import parse

#: How many CSV rows are read to decide the delimiter and the column types.
SNIFF_ROWS = 200

#: Sample sizes kept in a profile, so one answer cannot turn into a wall of text.
MAX_COLUMNS = 40
MAX_KEYS = 60
MAX_SAMPLES = 3

TEXT_EXTENSIONS = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".csv": "csv",
    ".tsv": "tsv",
    ".json": "json",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
    ".txt": "text",
    ".log": "text",
    ".py": "code",
    ".js": "code",
    ".mjs": "code",
    ".ts": "code",
    ".tsx": "code",
    ".jsx": "code",
    ".go": "code",
    ".rs": "code",
    ".rb": "code",
    ".java": "code",
    ".c": "code",
    ".h": "code",
    ".cpp": "code",
    ".sh": "code",
    ".bash": "code",
    ".sql": "code",
    ".yml": "code",
    ".yaml": "code",
    ".toml": "code",
    ".ini": "code",
}

INTEGER = re.compile(r"^[+-]?\d{1,18}$")
DECIMAL = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+[eE][+-]?\d+|\d+\.\d*[eE][+-]?\d+)$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?")
BOOLS = {"true", "false", "yes", "no"}


class AttachError(RuntimeError):
    """The attached file could not be read or profiled."""


def parse_file_uri(uri: str) -> str | None:
    """The path in a file:// uri, or None for anything this agent cannot read (http, etc.)."""
    try:
        parsed = parse.urlparse(str(uri or ""))
    except ValueError:
        return None
    if parsed.scheme != "file":
        return None
    return parse.unquote(parsed.path) or None


def name_of(uri: str) -> str:
    """The last path segment of a uri, which is what a reader calls the file."""
    path = parse_file_uri(uri) or str(uri or "")
    return os.path.basename(path.rstrip("/")) or str(uri or "")


def kind_of(name: str, text: str) -> str:
    """The flavour of a file, from its name, its opening characters, then its shape."""
    if not text.strip():
        return "empty"
    if "\x00" in text:
        return "binary"
    extension = os.path.splitext(str(name or ""))[1].lower()
    hinted = TEXT_EXTENSIONS.get(extension)
    stripped = text.lstrip()
    if hinted == "json":
        return "json"  # the extension is the claim; profile() reports it if the text disagrees
    if stripped.startswith(("{", "[")):
        try:
            json.loads(text)
            return "json"
        except ValueError:
            pass
    if hinted == "jsonl" or (hinted is None and _looks_like_jsonl(text)):
        return "jsonl"
    if hinted in {"csv", "tsv"} or (hinted is None and _csv_delimiter(text) is not None):
        return "tsv" if hinted == "tsv" else "csv"
    return hinted or "text"


def _looks_like_jsonl(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    for line in lines[:5]:
        if not line.lstrip().startswith(("{", "[")):
            return False
        try:
            json.loads(line)
        except ValueError:
            return False
    return True


def _csv_delimiter(text: str) -> str | None:
    """The delimiter that splits this text into columns, or None when nothing does.

    A reader is certain of a delimiter when every sampled row splits into the same number of
    fields. Rows that disagree - a trailing total, a quoted field holding a newline - would
    hide the delimiter completely, so a strong majority is accepted here and the disagreement
    is left to the ragged-row count, which reports it rather than hiding it.
    """
    sample = "\n".join(text.splitlines()[:SNIFF_ROWS])
    if not sample.strip():
        return None
    best: tuple[float, int, str] | None = None
    for delimiter in (",", "\t", ";", "|"):
        rows = list(csv.reader(io.StringIO(sample), delimiter=delimiter))
        rows = [row for row in rows if any(field.strip() for field in row)]
        if len(rows) < 2 or not rows[0][0].strip():
            continue
        width = len(rows[0])
        if width < 2:
            continue
        agree = sum(1 for row in rows if len(row) == width) / len(rows)
        if agree < 0.9:
            continue
        if best is None or (agree, width) > best[:2]:
            best = (agree, width, delimiter)
    return best[2] if best else None


def delimiter_of(text: str, kind: str) -> str:
    """The delimiter a file actually uses: a tab when it says so, else the sniffed one.

    The sniff has to be carried through to the parsing, or a semicolon-separated table would be
    read with commas, split into no columns at all, and reported as plain text.
    """
    if kind == "tsv":
        return "\t"
    return _csv_delimiter(text) or ","


def table_rows(text: str, kind: str) -> list[list[str]]:
    """The non-empty rows of a delimited file, split by that file's own delimiter."""
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter_of(text, kind)))
    return [row for row in rows if any(field.strip() for field in row)]


def has_no_columns(text: str, kind: str) -> bool:
    """True when a file named as a table splits into no columns at all."""
    rows = table_rows(text, kind)
    return not rows or all(len(row) == 1 for row in rows)


def column_type(values: list[str]) -> str:
    """The narrowest honest type for a column: what every non-empty value looks like.

    Integers and decimals are checked together, so a column of 9.5 and 7 is a decimal column
    rather than being called text - which is what happens if each pattern is tested alone.
    """
    seen = [value.strip() for value in values if value.strip()]
    if not seen:
        return "empty"
    integers = [bool(INTEGER.match(value)) for value in seen]
    decimals = [bool(DECIMAL.match(value)) for value in seen]
    if all(integers):
        return "integer"
    if all(one or two for one, two in zip(integers, decimals)):
        return "decimal"
    if all(ISO_DATE.match(value) for value in seen):
        return "date"
    if all(value.lower() in BOOLS for value in seen):
        return "boolean"
    return "text"


def text_details(text: str, lines: list[str]) -> dict:
    """What can honestly be counted about prose, or about a file that only claims to be data."""
    words = re.findall(r"[A-Za-z0-9']+", text)
    non_blank = [line for line in lines if line.strip()]
    return {
        "words": len(words),
        "unique_words": len({word.lower() for word in words}),
        "paragraphs": len([block for block in re.split(r"\n\s*\n", text) if block.strip()]),
        # Blank lines are not duplicates of each other, so they are left out of this count.
        "duplicate_lines": len(non_blank) - len(set(non_blank)),
        "sentences": len(re.findall(r"[.!?](\s|$)", text)),
    }


def json_shape(value, depth: int = 0) -> dict:
    """Container shapes and a bounded key list, enough to talk about a JSON file."""
    shape = {"type": type(value).__name__, "depth": depth}
    if isinstance(value, dict):
        shape["keys"] = []
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_KEYS:
                shape["keys_truncated"] = len(value) - MAX_KEYS
                break
            shape["keys"].append({"key": key, "type": type(item).__name__,
                                  "size": len(item) if isinstance(item, (list, dict, str)) else None})
        shape["size"] = len(value)
    elif isinstance(value, list):
        shape["size"] = len(value)
        children = [json_shape(item, depth + 1) for item in value[:3]]
        shape["sample"] = children
        shape["deepest"] = max((child.get("deepest", child["depth"]) for child in children),
                               default=depth)
    return shape


def _flatten_keys(value, prefix: str = "", out: list[str] | None = None,
                  limit: int = MAX_KEYS) -> list[str]:
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.append(path)
            if isinstance(item, (dict, list)):
                _flatten_keys(item, path, out, limit)
            if len(out) >= limit:
                break
    elif isinstance(value, list):
        for item in value[:2]:
            _flatten_keys(item, f"{prefix}[]", out, limit)
    # Two list items have the same shape, so the same path arrives twice; a key list that says
    # "users[].id" twice is not a key list. This is done in place, because the caller holds the
    # same list this function was given.
    unique = list(dict.fromkeys(out))
    out[:] = unique
    return unique


def profile(name: str, text: str) -> dict:
    """Everything this agent counts about one file, and nothing it did not."""
    kind = kind_of(name, text)
    data = {
        "name": name,
        "kind": kind,
        "bytes": len(text.encode("utf-8", errors="replace")),
        "characters": len(text),
        "lines": len(text.splitlines()),
        "sha256": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16],
        "final_newline": text.endswith("\n"),
        "details": {},
        "notes": [],
    }
    if kind == "binary":
        data["notes"].append("A NUL byte means this is not text: only its size and hash are "
                             "reported, not its contents.")
        return data
    if kind == "empty":
        data["notes"].append("The file has no printable characters in it.")
        return data

    lines = text.splitlines()
    lengths = [len(line) for line in lines] or [0]
    data["longest_line"] = max(lengths)
    data["longest_line_number"] = lengths.index(max(lengths)) + 1
    data["blank_lines"] = sum(1 for line in lines if not line.strip())
    data["non_ascii"] = sum(1 for character in text if ord(character) > 127)

    if kind in {"csv", "tsv"} and has_no_columns(text, kind):
        # A file named .csv with no delimiter anywhere in it is text, not a one-column table.
        data["kind"] = kind = "text"
        data["notes"].append("this file is named as a table but no delimiter splits it into "
                             "columns, so it is profiled as text")

    if kind == "json":
        try:
            value = json.loads(text)
        except ValueError as exc:
            data["kind"] = kind = "text"
            data["notes"].append(f"this file is named as JSON but does not parse ({exc}), so the "
                                 "counts below are of the raw text, not of a document")
            data["details"] = text_details(text, lines)
        else:
            flattened = _flatten_keys(value)
            data["details"] = {
                "shape": json_shape(value),
                "top_level": type(value).__name__,
                "top_level_size": len(value) if isinstance(value, (list, dict)) else None,
                "key_paths": flattened,
                "key_paths_truncated": len(flattened) >= MAX_KEYS,
            }
    elif kind == "jsonl":
        records, bad = [], 0
        for line in lines:
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                bad += 1
        keys = sorted({key for record in records if isinstance(record, dict) for key in record})
        data["details"] = {
            "records": len(records),
            "unparseable_lines": bad,
            "keys": keys[:MAX_KEYS],
            "first_record_keys": (sorted(records[0]) if records and isinstance(records[0], dict)
                                  else []),
        }
        if bad:
            data["notes"].append(f"{bad} line(s) are not JSON objects; the count above is of the "
                                 "lines that parsed.")
    elif kind in {"csv", "tsv"}:
        rows = table_rows(text, kind)
        header = [field.strip() for field in rows[0]]
        body = rows[1:]
        columns = []
        for index, title in enumerate(header[:MAX_COLUMNS]):
            values = [row[index] for row in body if index < len(row)]
            columns.append({
                "name": title or f"(column {index + 1})",
                "type": column_type(values),
                "filled": sum(1 for value in values if value.strip()),
                "empty": sum(1 for value in values if not value.strip()),
            })
        data["details"] = {
            "delimiter": delimiter_of(text, kind),
            "columns": columns,
            "column_count": len(header),
            "rows": len(body),
            "header": header[:MAX_COLUMNS],
            "ragged_rows": sum(1 for row in body if len(row) != len(header)),
            "sample": rows[1:1 + MAX_SAMPLES],
        }
        if len(header) > MAX_COLUMNS:
            data["notes"].append(f"only the first {MAX_COLUMNS} of {len(header)} columns are "
                                 "listed.")
        if data["details"]["ragged_rows"]:
            data["notes"].append(f"{data['details']['ragged_rows']} row(s) have a different "
                                 "number of fields than the header, so the row count is exact but "
                                 "the columns are not clean.")
    elif kind == "markdown":
        headings = [(len(match.group(1)), match.group(2).strip())
                    for match in re.finditer(r"^(#{1,6})\s+(.*)$", text, re.MULTILINE)]
        words = len(re.findall(r"\S+", text))
        data["details"] = {
            "headings": [{"level": level, "text": title} for level, title in headings[:MAX_KEYS]],
            "heading_count": len(headings),
            "links": len(re.findall(r"\[[^\]]*\]\([^)]*\)", text)),
            "code_fences": text.count("```") // 2,
            "table_rows": sum(1 for line in lines if line.strip().startswith("|")),
            "words": words,
        }
        if len(headings) > MAX_KEYS:
            data["notes"].append(f"only the first {MAX_KEYS} of {len(headings)} headings are listed.")
    elif kind == "code":
        extension = os.path.splitext(name)[1].lower()
        patterns = {
            "functions": r"^\s*(?:def|function|func|fn)\s+([A-Za-z_]\w*)",
            "classes": r"^\s*(?:class|struct|interface)\s+([A-Za-z_]\w*)",
            "imports": r"^\s*(?:import|from|require|use|#include)\b",
            "comments": r"^\s*(?:#|//|/\*|\*)",
        }
        counts, names = {}, {}
        for label, pattern in patterns.items():
            found = re.findall(pattern, text, re.MULTILINE)
            counts[label] = len(found)
            if label in {"functions", "classes"}:
                names[label] = found[:MAX_KEYS]
        data["details"] = {"language": extension.lstrip(".") or "unknown", "counts": counts,
                           "names": names, "words": len(re.findall(r"\S+", text))}
        data["notes"].append("Function, class, import and comment counts are regex matches over "
                             "the text, not a parse: a name inside a string is counted too.")
    else:
        data["details"] = text_details(text, lines)

    if data["non_ascii"]:
        data["notes"].append(f"{data['non_ascii']} non-ASCII character(s): the file is not "
                             "plain 7-bit text, which is normal for accents and emoji.")
    return data
