"""Tests for the attach agent: uri parsing, file profiling, routing and turns.

    python3 agents/attach/tests/test_agent.py

No network anywhere - this agent has none. Two kinds of test live here:

  * the profiler, fed strings that stand for the files a client would attach, and
  * the turns, driven through the real kit (`AcpClient` in-process) so that `embeddedContext`,
    the permission ask and `fs/read_text_file` are exercised over ACP rather than mocked.

The fixtures are the shapes this reader exists to survive: a table with an empty cell and a
ragged row, a semicolon-separated table, a `.json` file that does not parse, and a JSON file
whose two list items would otherwise report the same key path twice.
"""

from __future__ import annotations

import json
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for path in (str(REPO_ROOT), str(AGENT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from acp_kit import AcpClient, Connection, STOP_END_TURN, STOP_REFUSAL  # noqa: E402

from agent import (  # noqa: E402
    HELP,
    MAX_FILES,
    MAX_LINES,
    attachments,
    human_size,
    render,
    route,
    wants,
)
from data import (  # noqa: E402
    MAX_COLUMNS,
    MAX_KEYS,
    AttachError,
    _csv_delimiter,
    delimiter_of,
    kind_of,
    name_of,
    parse_file_uri,
    profile,
)

CSV = ("name,city,pop\n"
       "springfield,IL,114394\n"
       "portland,ME,68408\n"
       ",OR,\n")

RAGGED_CSV = ("a,b,c\n"
              "1,2,3\n"
              "4,5\n"
              "6,7,8\n")

SEMICOLON = ("name;age\n"
             "ada;36\n"
             "grace;45\n")

JSON_TEXT = json.dumps({"users": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}],
                        "count": 2, "tags": ["x", "y"]})

MARKDOWN = ("# Notes\n\nintro\n\n## One\n- a\n- b\n\n## Two\n\n```py\nx=1\n```\n\n"
            "See [docs](https://x.test).\n")

CODE = ("import os\nfrom pathlib import Path\n\n# a comment\n\n"
        "class Thing:\n    def run(self):\n        return os.name\n\n"
        "def helper():\n    return Path('x')\n\nprint('look, def not_a_function():')\n")


class UriTests(unittest.TestCase):
    def test_a_file_uri_becomes_a_path(self):
        self.assertEqual(parse_file_uri("file:///tmp/a.csv"), "/tmp/a.csv")
        self.assertEqual(parse_file_uri("file:///tmp/my%20notes%20file.md"),
                         "/tmp/my notes file.md")

    def test_a_uri_this_agent_cannot_read_is_none(self):
        self.assertIsNone(parse_file_uri("https://example.test/a.csv"))
        self.assertIsNone(parse_file_uri(""))
        # An escape that means nothing is left as written rather than being thrown away.
        self.assertEqual(parse_file_uri("file:///tmp/%zz"), "/tmp/%zz")

    def test_the_name_is_the_last_segment(self):
        self.assertEqual(name_of("file:///tmp/reports/q3.csv"), "q3.csv")
        self.assertEqual(name_of("https://example.test/a/b.txt"), "b.txt")

    def test_an_unknown_extension_is_sniffed(self):
        self.assertEqual(kind_of("data", CSV), "csv")
        self.assertEqual(kind_of("data", SEMICOLON), "csv")
        self.assertEqual(kind_of("data", JSON_TEXT), "json")
        self.assertEqual(kind_of("data", '{"a": 1}\n{"a": 2}\n'), "jsonl")
        self.assertEqual(kind_of("data", "just prose, one comma\nand a second line\n"), "text")

    def test_the_extension_wins_over_a_sniff(self):
        self.assertEqual(kind_of("notes.md", "no headings here"), "markdown")
        self.assertEqual(kind_of("run.py", "x = 1"), "code")
        self.assertEqual(kind_of("t.tsv", "a\tb\n1\t2\n"), "tsv")

    def test_binary_and_empty(self):
        self.assertEqual(kind_of("x.bin", "a\x00b"), "binary")
        self.assertEqual(kind_of("x.txt", "   \n\n"), "empty")

    def test_the_sniffed_delimiter_is_the_one_used(self):
        self.assertEqual(_csv_delimiter(SEMICOLON), ";")
        self.assertEqual(_csv_delimiter("a|b\n1|2\n"), "|")
        self.assertEqual(_csv_delimiter("a\tb\n1\t2\n"), "\t")
        self.assertIsNone(_csv_delimiter("just words\nmore words\n"))
        self.assertEqual(delimiter_of(SEMICOLON, "csv"), ";")
        self.assertEqual(delimiter_of("a\tb\n1\t2\n", "tsv"), "\t")
        # A single comma in one line of prose is not a table.
        self.assertEqual(delimiter_of("hello, world\nmore prose\n", "csv"), ",")
        self.assertIsNone(_csv_delimiter("hello, world\nmore prose\n"))


class ProfileTests(unittest.TestCase):
    def test_a_csv_counts_rows_columns_and_empties(self):
        reading = profile("cities.csv", CSV)
        self.assertEqual(reading["kind"], "csv")
        self.assertEqual(reading["lines"], 4)
        self.assertEqual(reading["sha256"][:8], "26d8ba86")
        self.assertEqual(len(reading["sha256"]), 16)
        details = reading["details"]
        self.assertEqual(details["rows"], 3)
        self.assertEqual(details["column_count"], 3)
        self.assertEqual(details["header"], ["name", "city", "pop"])
        self.assertEqual(details["delimiter"], ",")
        types = {column["name"]: column["type"] for column in details["columns"]}
        self.assertEqual(types, {"name": "text", "city": "text", "pop": "integer"})
        empty = {column["name"]: column["empty"] for column in details["columns"]}
        self.assertEqual(empty, {"name": 1, "city": 0, "pop": 1})

    def test_a_semicolon_table_is_read_with_its_own_delimiter(self):
        reading = profile("people.csv", SEMICOLON)
        self.assertEqual(reading["kind"], "csv")
        self.assertEqual(reading["details"]["delimiter"], ";")
        self.assertEqual(reading["details"]["header"], ["name", "age"])
        self.assertEqual(reading["details"]["rows"], 2)
        self.assertEqual([column["type"] for column in reading["details"]["columns"]],
                         ["text", "integer"])

    def test_a_table_with_no_delimiter_is_profiled_as_text(self):
        reading = profile("one.csv", "just words\nand more words\n")
        self.assertEqual(reading["kind"], "text")
        self.assertIn("named as a table but no delimiter", reading["notes"][0])
        self.assertEqual(reading["details"]["words"], 5)

    def test_a_ragged_row_is_counted_and_reported(self):
        reading = profile("x.csv", RAGGED_CSV)
        self.assertEqual(reading["details"]["rows"], 3)
        self.assertEqual(reading["details"]["ragged_rows"], 1)
        self.assertIn("1 row(s) have a different number of fields", reading["notes"][0])

    def test_a_mixed_number_column_is_a_decimal_column(self):
        reading = profile("x.csv", "label,v\nx,9.5\ny,7\n")
        types = {column["name"]: column["type"] for column in reading["details"]["columns"]}
        self.assertEqual(types, {"label": "text", "v": "decimal"})

    def test_a_one_column_table_is_called_text_because_it_did_not_split(self):
        # Documented choice: with no delimiter anywhere there is nothing that says "one column"
        # rather than "one line at a time", so the counts of the text are reported instead.
        reading = profile("x.csv", "v\n9.5\n7\n")
        self.assertEqual(reading["kind"], "text")
        self.assertNotIn("columns", reading["details"])
        self.assertIn("no delimiter splits it into columns", reading["notes"][0])

    def test_types_are_narrowed_honestly(self):
        reading = profile("x.csv", "a,b,c,d\ntrue,2026-09-23,x,1\nfalse,2026-01-02,y,2\n")
        types = [column["type"] for column in reading["details"]["columns"]]
        self.assertEqual(types, ["boolean", "date", "text", "integer"])

    def test_many_columns_are_truncated_with_a_note(self):
        header = ",".join(f"c{index}" for index in range(MAX_COLUMNS + 5))
        row = ",".join(str(index) for index in range(MAX_COLUMNS + 5))
        reading = profile("wide.csv", header + "\n" + row + "\n")
        self.assertEqual(len(reading["details"]["columns"]), MAX_COLUMNS)
        self.assertEqual(reading["details"]["column_count"], MAX_COLUMNS + 5)
        self.assertIn(f"only the first {MAX_COLUMNS} of {MAX_COLUMNS + 5}", reading["notes"][0])

    def test_a_json_document_reports_its_shape(self):
        reading = profile("data.json", JSON_TEXT)
        self.assertEqual(reading["kind"], "json")
        details = reading["details"]
        self.assertEqual(details["top_level"], "dict")
        self.assertEqual(details["top_level_size"], 3)
        self.assertIn("users", [entry["key"] for entry in details["shape"]["keys"]])
        self.assertFalse(details["key_paths_truncated"])

    def test_a_key_path_is_listed_once_even_over_a_list(self):
        details = profile("data.json", JSON_TEXT)["details"]
        self.assertEqual(details["key_paths"].count("users[].id"), 1)
        self.assertEqual(details["key_paths"], ["users", "users[].id", "users[].name",
                                                "count", "tags"])

    def test_key_paths_stop_at_the_cap(self):
        deep = {f"k{index}": {f"inner{index}": index} for index in range(MAX_KEYS)}
        details = profile("deep.json", json.dumps(deep))["details"]
        self.assertLessEqual(len(details["key_paths"]), MAX_KEYS)
        self.assertTrue(details["key_paths_truncated"])

    def test_a_json_file_that_does_not_parse_is_text_with_a_note(self):
        reading = profile("broken.json", "{not json at all\n")
        self.assertEqual(reading["kind"], "text")
        self.assertIn("does not parse", reading["notes"][0])
        self.assertIn("words", reading["details"])

    def test_jsonl_records_and_bad_lines(self):
        reading = profile("log.jsonl", '{"a": 1}\n{"a": 2, "b": 3}\nnot json\n')
        self.assertEqual(reading["kind"], "jsonl")
        self.assertEqual(reading["details"]["records"], 2)
        self.assertEqual(reading["details"]["keys"], ["a", "b"])
        self.assertEqual(reading["details"]["unparseable_lines"], 1)
        self.assertIn("1 line(s) are not JSON objects", reading["notes"][0])

    def test_markdown_headings_links_and_fences(self):
        reading = profile("notes.md", MARKDOWN)
        details = reading["details"]
        self.assertEqual(details["heading_count"], 3)
        self.assertEqual([heading["level"] for heading in details["headings"]], [1, 2, 2])
        self.assertEqual(details["links"], 1)
        self.assertEqual(details["code_fences"], 1)
        self.assertEqual(details["words"], 16)

    def test_code_counts_are_labeled_as_regex_matches(self):
        reading = profile("thing.py", CODE)
        details = reading["details"]
        self.assertEqual(details["language"], "py")
        self.assertEqual(details["counts"]["classes"], 1)
        self.assertEqual(details["counts"]["imports"], 2)
        self.assertEqual(details["counts"]["comments"], 1)
        self.assertIn("helper", details["names"]["functions"])
        self.assertIn("run", details["names"]["functions"])
        self.assertTrue(any("regex matches over the text" in note for note in reading["notes"]))

    def test_prose_counts_words_paragraphs_and_duplicates(self):
        reading = profile("x.txt", "one two\n\none two\n\nthree.\n")
        details = reading["details"]
        self.assertEqual(details["words"], 5)
        self.assertEqual(details["unique_words"], 3)
        self.assertEqual(details["paragraphs"], 3)
        self.assertEqual(details["duplicate_lines"], 1)
        self.assertEqual(reading["blank_lines"], 2)
        self.assertEqual(reading["longest_line"], 7)
        self.assertEqual(reading["longest_line_number"], 1)
        self.assertTrue(reading["final_newline"])

    def test_non_ascii_is_noted_not_hidden(self):
        reading = profile("x.txt", "café — ok\n")
        self.assertEqual(reading["non_ascii"], 2)
        self.assertTrue(any("non-ASCII" in note for note in reading["notes"]))

    def test_binary_reports_only_size_and_hash(self):
        reading = profile("x.bin", "a\x00b")
        self.assertEqual(reading["kind"], "binary")
        self.assertEqual(reading["bytes"], 3)
        self.assertEqual(reading["details"], {})
        self.assertIn("NUL byte", reading["notes"][0])

    def test_an_empty_file_says_so(self):
        reading = profile("x.txt", "   \n")
        self.assertEqual(reading["kind"], "empty")
        self.assertIn("no printable characters", reading["notes"][0])

    def test_a_profile_never_touches_the_network(self):
        import data

        self.assertFalse([name for name in dir(data) if name in {"urlopen", "Request", "socket"}])


class RouteTests(unittest.TestCase):
    def test_wants_reads_the_question(self):
        self.assertEqual(wants("how many rows?"), {"rows"})
        self.assertIn("missing", wants("which columns have empty values?"))
        self.assertEqual(wants(""), {"everything"})

    def test_attachments_reads_both_block_types(self):
        blocks = [
            {"type": "text", "text": "hi"},
            {"type": "resource", "resource": {"uri": "file:///tmp/a.csv", "text": CSV}},
            {"type": "resource", "resource": {"uri": "file:///tmp/b.json"}},
            {"type": "resource_link", "uri": "https://example.test/c.md", "name": "c.md"},
            {"type": "image", "data": "..."},
        ]
        found = attachments(blocks)
        self.assertEqual([item["name"] for item in found], ["a.csv", "b.json", "c.md"])
        self.assertEqual(found[0]["text"], CSV)
        self.assertIsNone(found[1]["text"])
        self.assertEqual(found[1]["path"], "/tmp/b.json")
        self.assertIsNone(parse_file_uri(found[2]["uri"]))

    def test_no_attachment_means_help(self):
        self.assertEqual(route("what is in this file?", 0), ("attach-help", {}))
        skill, params = route("how many rows?", 2)
        self.assertEqual(skill, "attach-profile")
        self.assertEqual(params["wants"], ["rows"])

    def test_human_size(self):
        self.assertEqual(human_size(500), "500 bytes")
        self.assertEqual(human_size(2048), "2.0 KiB")
        self.assertEqual(human_size(3 * 1024 * 1024), "3.0 MiB")

    def test_render_leads_with_what_was_asked(self):
        reading = profile("cities.csv", CSV)
        asked = render(reading, {"rows", "columns"})
        self.assertIn("cities.csv  (csv, 59 bytes, 4 lines, sha256 26d8ba86da50ce69...)", asked)
        self.assertIn("delimiter ','", asked)
        self.assertIn("      pop: integer, 2 filled, 1 empty", asked)
        self.assertIn("  - columns with empty values: name, pop", asked)

    def test_render_shows_how_every_count_was_made(self):
        asked = render(profile("cities.csv", CSV), {"everything"})
        self.assertIn("row(s) of data", asked)
        self.assertIn("  - sample: springfield, IL, 114394", asked)

    def test_render_points_at_the_rest_of_a_wide_table(self):
        header = ",".join(f"c{index}" for index in range(MAX_COLUMNS + 2))
        row = ",".join("1" for index in range(MAX_COLUMNS + 2))
        asked = render(profile("wide.csv", header + "\n" + row + "\n"), {"columns"})
        self.assertIn(f"and 2 more column(s); ask me about the columns", asked)

    def test_render_indents_markdown_headings(self):
        asked = render(profile("notes.md", MARKDOWN), {"headings"})
        self.assertIn("      # Notes", asked)
        self.assertIn("        ## One", asked)

    def test_render_narrows_an_unasked_json_key_list(self):
        asked = render(profile("data.json", JSON_TEXT), {"size"})
        self.assertIn("  - top level: dict with 3 entries", asked)

    def test_the_help_text_names_the_rules(self):
        self.assertIn("I count lines, rows, columns", HELP)
        self.assertIn("nothing leaves this machine", HELP)


class QueueReader:
    def __init__(self):
        self._items: queue.Queue = queue.Queue()

    def push(self, text):
        self._items.put(text)

    def close(self):
        self._items.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        item = self._items.get()
        if item is None:
            raise StopIteration
        return item


class WiredWriter:
    def __init__(self, reader):
        self.reader = reader

    def write(self, text):
        self.reader.push(text)

    def flush(self):
        pass

    def close(self):
        self.reader.close()


def connected_pair():
    to_agent, to_client = QueueReader(), QueueReader()
    return (
        Connection(to_agent, WiredWriter(to_client), name="agent"),
        Connection(to_client, WiredWriter(to_agent), name="client"),
    )


class TurnTests(unittest.TestCase):
    """Turns over the real kit, with a client that can (or cannot) hand over a file."""

    def turn(self, blocks, permission="allow-once", root=None):
        from agent import AttachAgent

        agent_conn, client_conn = connected_pair()
        agent = AttachAgent(agent_conn)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission, fs_root=root)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd=str(root or "/tmp"))
        try:
            return client.prompt("", session_id, blocks=blocks), client
        finally:
            client.stop()

    def text_block(self, text):
        return {"type": "text", "text": text}

    def resource(self, uri, text=None, name=None):
        resource = {"uri": uri}
        if text is not None:
            resource["text"] = text
        if name is not None:
            resource["name"] = name
        return {"type": "resource", "resource": resource}

    def write(self, root: Path, name: str, text: str) -> str:
        (root / name).write_text(text, encoding="utf-8")
        return str(root / name)

    def test_embedded_text_needs_no_permission_and_no_fs(self):
        result, client = self.turn([
            self.text_block("how many rows and which columns have empty values?"),
            self.resource("file:///tmp/cities.csv", CSV),
        ])
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(client.read_files, [])
        self.assertIn("3 row(s) of data, 3 column(s)", result["text"])
        self.assertIn("columns with empty values: name, pop", result["text"])
        self.assertIn("Source: the text your editor embedded in the prompt.", result["text"])
        self.assertIn("No request left this machine", result["text"])

    def test_a_uri_only_attachment_is_read_over_acp_after_asking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.write(root, "cities.csv", CSV)
            result, client = self.turn([
                self.text_block("profile this csv"),
                self.resource("file://" + path),
            ], root=root)
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertEqual(len(client.permission_requests), 1)
        self.assertEqual(client.permission_requests[0]["toolCall"]["title"],
                         f"Allow attach to read {path}?")
        # The client resolves what it serves, so /var is /private/var on this machine.
        self.assertEqual([item["path"] for item in client.read_files], [str(Path(path).resolve())])
        self.assertIn("read with fs/read_text_file over ACP from " + path, result["text"])
        self.assertIn("  - how I got it: read over ACP", result["text"])

    def test_a_refused_read_is_reported_and_stops_with_a_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.write(root, "data.json", JSON_TEXT)
            result, client = self.turn([
                self.text_block("profile this file"),
                self.resource("file://" + path),
            ], permission="reject", root=root)
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertEqual(client.read_files, [])
        self.assertIn("you did not allow the read", result["text"])

    def test_a_client_that_cannot_read_files_is_told_so_without_being_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(Path(tmp), "data.json", JSON_TEXT)
            result, client = self.turn([
                self.text_block("profile this file"),
                self.resource("file://" + path),
            ], root=None)
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertEqual(client.permission_requests, [])
        self.assertIn("does not advertise fs.readTextFile", result["text"])
        self.assertIn("attach the text itself instead", result["text"])

    def test_a_missing_file_is_an_honest_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = str(root / "nope.csv")
            result, _ = self.turn([
                self.text_block("profile this file"),
                self.resource("file://" + missing),
            ], root=root)
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("could not read anything that was attached", result["text"])
        self.assertIn("nope.csv", result["text"])
        self.assertIn("no such file", result["text"])

    def test_nothing_attached_gets_the_help_text_and_touches_nothing(self):
        result, client = self.turn([self.text_block("what is in this file?")])
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("I read the file you attach to me in your editor", result["text"])
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(client.read_files, [])
        self.assertEqual(client.tool_calls(), [])

    def test_a_web_link_is_refused_in_words(self):
        result, client = self.turn([
            self.text_block("what is in this file?"),
            {"type": "resource_link", "uri": "https://example.test/report.pdf",
             "name": "report.pdf"},
        ])
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertEqual(client.permission_requests, [])
        self.assertIn("report.pdf", result["text"])
        self.assertIn("I read files, not web links", result["text"])

    def test_two_attachments_are_both_profiled(self):
        result, client = self.turn([
            self.text_block("outline both files"),
            self.resource("file:///tmp/notes.md", MARKDOWN),
            self.resource("file:///tmp/data.json", JSON_TEXT),
        ])
        self.assertIn("notes.md  (markdown", result["text"])
        self.assertIn("data.json  (json", result["text"])
        self.assertEqual(client.permission_requests, [])
        titles = [call["title"] for call in client.tool_calls()]
        self.assertIn("Profile 2 attached file(s)", titles)

    def test_a_binary_attachment_is_reported_as_not_text(self):
        result, _ = self.turn([
            self.text_block("what is in this file?"),
            self.resource("file:///tmp/blob.bin", "a\x00b"),
        ])
        self.assertIn("blob.bin  (binary", result["text"])
        self.assertIn("only its size and hash are reported", result["text"])

    def test_too_many_attachments_names_the_ones_it_did_not_read(self):
        blocks = [self.text_block("profile all of these")]
        for index in range(MAX_FILES + 2):
            blocks.append(self.resource(f"file:///tmp/f{index}.txt", f"file {index}\n"))
        result, _ = self.turn(blocks)
        self.assertIn("2 more attachment(s) were not read", result["text"])
        self.assertIn(f"at most {MAX_FILES} at a time", result["text"])
        self.assertIn("f0.txt", result["text"])

    def test_a_file_longer_than_the_read_limit_says_the_counts_are_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.write(root, "big.txt", "".join(f"line {n}\n" for n in range(MAX_LINES + 50)))
            result, _ = self.turn([
                self.text_block("how many lines in this file?"),
                self.resource("file://" + path),
            ], root=root)
        self.assertIn(f"only the first {MAX_LINES} lines were read", result["text"])
        self.assertIn("not of a longer file", result["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
