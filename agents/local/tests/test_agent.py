"""Tests for the local agent: disk, git and port reads, routing, and live turns.

    python3 agents/local/tests/test_agent.py

Nothing here touches the real machine. The command runner and the disk-usage call are both
injected, and the fixtures are shaped like the real output of `shutil.disk_usage`,
`git status --porcelain=v2 --branch` and `lsof -nP -iTCP -sTCP:LISTEN`.
"""

from __future__ import annotations

import queue
import sys
import threading
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for path in (str(REPO_ROOT), str(AGENT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from acp_kit import AcpClient, Connection, STOP_END_TURN, STOP_REFUSAL  # noqa: E402

from agent import HELP, LocalAgent, route  # noqa: E402
from data import (  # noqa: E402
    DATASET,
    LOW_FREE_BYTES,
    LocalData,
    LocalError,
    _parse_lsof,
    _parse_status,
    check_path,
    human_bytes,
    port_filter,
)

GIT_STATUS = "\n".join([
    "# branch.oid 57df985abc",
    "# branch.head main",
    "# branch.upstream origin/main",
    "# branch.ab +2 -1",
    "1 M. N... 100644 100644 100644 aaa bbb src/app.py",
    "1 .M N... 100644 100644 100644 aaa bbb docs/readme.md",
    "1 MM N... 100644 100644 100644 aaa bbb src/both.py",
    "? notes.txt",
    "u UU N... 100644 100644 100644 aaa bbb src/conflict.py",
    "2 R. N... 100644 100644 100644 aaa bbb R100 src/new_name.py\tsrc/old_name.py",
]) + "\n"

GIT_LOG = "57df985\t2026-09-22T19:04:11+00:00\tdtaxk\tfeat: add six new agents\n"

#: Real shape from `lsof -nP -iTCP -sTCP:LISTEN` on macOS.
LSOF = "\n".join([
    "COMMAND   PID   USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME",
    "node    12345 dtaxk   21u  IPv6 0x8a1b2c3d4e5f      0t0  TCP *:3000 (LISTEN)",
    "ssh      6541  root   3u   IPv4 0x1f2e3d4c5b6a      0t0  TCP *:22 (LISTEN)",
    "python3  9988 dtaxk   6u   IPv4 0x9a8b7c6d5e4f      0t0  TCP 127.0.0.1:8000 (LISTEN)",
]) + "\n"


class DiskUsage:
    """Stand-in for the object shutil.disk_usage returns."""

    def __init__(self, total: int, used: int, free: int) -> None:
        self.total, self.used, self.free = total, used, free


class FakeRunner:
    """Answers the allowed read-only commands and records every argv it was asked for.

    The fake repo root is this repo, because the reader checks that the root git reports is a
    folder that really exists.
    """

    def __init__(self, top_level: str = str(REPO_ROOT), status: str = GIT_STATUS,
                 log: str = GIT_LOG, lsof: str = LSOF, git_rc: int = 0, lsof_rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.top_level = top_level
        self.status = status
        self.log = log
        self.lsof = lsof
        self.git_rc = git_rc
        self.lsof_rc = lsof_rc

    def __call__(self, argv: list[str], cwd: str | None):
        self.calls.append(list(argv))
        if argv[:2] == ["git", "rev-parse"]:
            return self.git_rc, (self.top_level + "\n" if self.git_rc == 0 else "")
        if argv[:2] == ["git", "status"]:
            return 0, self.status
        if argv[:2] == ["git", "log"]:
            return 0, self.log
        if argv[0] == "lsof":
            lines = self.lsof.splitlines(keepends=True)
            for arg in argv:
                if arg.startswith("-i:"):
                    wanted = arg[3:]
                    lines = [line for line in lines
                             if not line.startswith("COMMAND") and line.rstrip().endswith(f":{wanted} (LISTEN)")]
            return self.lsof_rc, "".join(lines)
        raise AssertionError(f"unexpected command {argv}")


def make_data(runner: FakeRunner | None = None, usage: DiskUsage | None = None) -> tuple[LocalData, FakeRunner]:
    runner = runner or FakeRunner()
    usage = usage or DiskUsage(500_000_000_000, 100_000_000_000, 400_000_000_000)
    return LocalData(runner=runner, disk_usage=lambda path: usage), runner


class BytesTests(unittest.TestCase):
    def test_human_bytes(self):
        self.assertEqual(human_bytes(0), "0 B")
        self.assertEqual(human_bytes(999), "999 B")
        self.assertEqual(human_bytes(2048), "2 KB")
        self.assertEqual(human_bytes(1024 ** 3 * 3 // 2), "1.5 GB")
        self.assertEqual(human_bytes(None), "unknown")


class PathTests(unittest.TestCase):
    def test_check_path_rejects_a_missing_folder(self):
        with self.assertRaises(ValueError):
            check_path("/definitely/not/here")

    def test_check_path_expands_the_home_directory(self):
        self.assertTrue(check_path("~").startswith("/"))

    def test_check_path_defaults_to_the_cwd(self):
        self.assertEqual(check_path(""), check_path(None))


class PortFilterTests(unittest.TestCase):
    def test_port_filter_reads_a_port(self):
        self.assertEqual(port_filter("what is running on port 3000?"), 3000)
        self.assertEqual(port_filter("who holds :5432"), 5432)
        self.assertIsNone(port_filter("which ports are listening?"))
        self.assertIsNone(port_filter("port 99999 is not a port"))


class ParseTests(unittest.TestCase):
    def test_parse_lsof_skips_the_header(self):
        rows = _parse_lsof(LSOF)
        self.assertEqual([row["port"] for row in rows], [22, 3000, 8000])
        self.assertEqual(rows[0]["command"], "ssh")
        self.assertEqual(rows[0]["pid"], 6541)
        self.assertEqual(rows[1]["address"], "*")

    def test_parse_lsof_handles_ipv6_and_junk(self):
        rows = _parse_lsof("\n".join([
            "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME",
            "node 1 me 20u IPv6 0x1 0t0 TCP [::1]:5173 (LISTEN)",
            "garbled",
            "weird 2 me 20u IPv6 0x1 0t0 TCP not-a-port (LISTEN)",
        ]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["address"], "::1")
        self.assertEqual(rows[0]["port"], 5173)

    def test_parse_status_reads_branch_and_divergence(self):
        report = _parse_status(GIT_STATUS)
        self.assertEqual(report["branch"], "main")
        self.assertEqual(report["upstream"], "origin/main")
        self.assertEqual((report["ahead"], report["behind"]), (2, 1))

    def test_parse_status_counts_every_kind_of_change(self):
        report = _parse_status(GIT_STATUS)
        self.assertEqual(report["staged"], 3)
        self.assertEqual(report["modified"], 2)
        self.assertEqual(report["untracked"], 1)
        self.assertEqual(report["conflicted"], 1)

    def test_parse_status_keeps_the_current_name_of_a_rename(self):
        renamed = [row for row in _parse_status(GIT_STATUS)["changed"] if "from" in row]
        self.assertEqual(renamed, [{"state": "staged", "path": "src/new_name.py",
                                    "from": "src/old_name.py"}])

    def test_parse_status_on_empty_output(self):
        report = _parse_status("")
        self.assertIsNone(report["branch"])
        self.assertEqual(report["changed"], [])


class ReaderTests(unittest.TestCase):
    def test_disk_reports_free_space(self):
        data, _ = make_data()
        disk = data.disk("/tmp")
        self.assertEqual(disk["free"], 400_000_000_000)
        self.assertAlmostEqual(disk["percent_free"], 80.0)
        self.assertFalse(disk["low"])

    def test_disk_flags_a_nearly_full_volume(self):
        data, _ = make_data(usage=DiskUsage(LOW_FREE_BYTES * 4, LOW_FREE_BYTES * 3, LOW_FREE_BYTES - 1))
        self.assertTrue(data.disk("/tmp")["low"])

    def test_git_reports_the_repo_root_it_found(self):
        data, runner = make_data()
        report = data.git("/tmp")
        self.assertEqual(report["root"], str(REPO_ROOT))
        self.assertEqual(runner.calls[1][:2], ["git", "status"])
        self.assertIn("--porcelain=v2", runner.calls[1])
        self.assertEqual(report["last_commit"]["short"], "57df985")

    def test_git_refuses_a_folder_that_is_not_a_repo(self):
        data, _ = make_data(FakeRunner(top_level="", git_rc=128))
        with self.assertRaises(ValueError) as caught:
            data.git("/tmp")
        self.assertIn("not inside a git repository", str(caught.exception))

    def test_ports_lists_listeners(self):
        data, runner = make_data()
        found = data.ports()
        self.assertEqual(found["total"], 3)
        self.assertEqual(found["distinct_ports"], [22, 3000, 8000])
        self.assertEqual(runner.calls[0], ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"])

    def test_ports_filters_to_one_port(self):
        data, runner = make_data()
        data.ports(3000)
        self.assertIn("-i:3000", runner.calls[0])

    def test_ports_treats_nothing_listening_as_an_answer(self):
        data, _ = make_data(FakeRunner(lsof="", lsof_rc=1))
        self.assertEqual(data.ports()["listeners"], [])

    def test_a_real_lsof_failure_is_an_error(self):
        data, _ = make_data(FakeRunner(lsof="lsof: not permitted", lsof_rc=2))
        with self.assertRaises(LocalError):
            data.ports()

    def test_the_runner_refuses_a_command_outside_the_allowlist(self):
        data = LocalData()
        with self.assertRaises(LocalError):
            data._run(["rm", "-rf", "/"], "/tmp")

    def test_the_agent_only_ever_runs_git_and_lsof(self):
        data, runner = make_data()
        data.disk("/tmp")
        data.git("/tmp")
        data.ports()
        allowed = {"git", "lsof"}
        self.assertTrue({argv[0] for argv in runner.calls} <= allowed, runner.calls)


class RouteTests(unittest.TestCase):
    def test_disk_question(self):
        self.assertEqual(route("how much disk space is left?")[0], "disk")

    def test_disk_question_with_a_path(self):
        skill, params = route("how much space is free on /Volumes/Backup?")
        self.assertEqual(skill, "disk")
        self.assertEqual(params["path"], "/Volumes/Backup")

    def test_git_question(self):
        self.assertEqual(route("what is going on in this repo?")[0], "git")

    def test_git_question_with_a_path(self):
        skill, params = route("is ~/code/app dirty?")
        self.assertEqual(skill, "git")
        self.assertEqual(params["path"], "~/code/app")

    def test_port_question(self):
        self.assertEqual(route("which ports are listening?")[0], "ports")

    def test_port_question_names_the_port(self):
        skill, params = route("what is running on port 3000?")
        self.assertEqual(skill, "ports")
        self.assertEqual(params["port"], 3000)

    def test_a_bare_path_falls_back_to_disk(self):
        self.assertEqual(route("/Volumes/Backup")[0], "disk")

    def test_help(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("hello there")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")


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
    def turn(self, text: str, permission: str = "allow-once", runner=None, usage=None,
             cwd: str = "/tmp"):
        agent_conn, client_conn = connected_pair()
        data, fake = make_data(runner, usage)
        agent = LocalAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd=cwd)
        try:
            return client.prompt(text, session_id), client, fake
        finally:
            client.stop()

    def test_disk_question_answers_with_real_numbers(self):
        result, client, _ = self.turn("how much disk space is left?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("Total: 465.7 GB", result["text"])
        self.assertIn("Free: 372.5 GB (80.0% of the volume)", result["text"])
        self.assertIn(DATASET, result["text"])
        self.assertEqual(len(client.permission_requests), 1)

    def test_a_tight_disk_says_so_and_says_what_to_do(self):
        result, _, _ = self.turn("how much space is left on /tmp?",
                                 usage=DiskUsage(LOW_FREE_BYTES * 4, LOW_FREE_BYTES * 4 - 1, 1))
        self.assertIn("Tight: under the 10 GB free mark", result["text"])
        self.assertIn("not your source, config or databases", result["text"])

    def test_the_session_working_directory_is_the_default(self):
        result, _, fake = self.turn("how much space is left?", cwd="/usr")
        self.assertIn("/usr", result["text"])

    def test_git_question_reports_branch_changes_and_last_commit(self):
        result, _, _ = self.turn("what is going on in this repo?")
        self.assertIn(f"Repository {REPO_ROOT}", result["text"])
        self.assertIn("Branch: main, tracking origin/main, 2 ahead and 1 behind", result["text"])
        self.assertIn("Working tree: 3 staged, 2 modified, 1 untracked", result["text"])
        self.assertIn("[staged] src/app.py", result["text"])
        self.assertIn("[untracked] notes.txt", result["text"])
        self.assertIn("Last commit: 57df985 2026-09-22T19:04:11+00:00 by dtaxk - feat: add six new agents",
                      result["text"])
        self.assertIn("Nothing was staged, committed or restored", result["text"])

    def test_conflicts_are_called_out_first(self):
        result, _, _ = self.turn("is this repo okay?")
        self.assertIn("Conflicts: 1 path(s) need resolving", result["text"])
        self.assertIn("[conflicted] src/conflict.py", result["text"])

    def test_a_folder_that_is_not_a_repo_is_reported_plainly(self):
        result, _, _ = self.turn("what is going on in this repo?",
                                 runner=FakeRunner(top_level="", git_rc=128))
        self.assertIn("is not inside a git repository", result["text"])

    def test_port_question_lists_listeners(self):
        result, _, fake = self.turn("which ports are listening?")
        self.assertIn("3 TCP listener(s) on this machine", result["text"])
        self.assertIn("22 - ssh (pid 6541, user root) on *", result["text"])
        self.assertIn("8000 - python3 (pid 9988, user dtaxk) on 127.0.0.1", result["text"])
        self.assertIn("only TCP sockets in the LISTEN state", result["text"])

    def test_port_question_names_the_owner_of_one_port(self):
        result, _, fake = self.turn("what is running on port 3000?")
        self.assertIn("1 listener(s) on port 3000", result["text"])
        self.assertIn("Port 3000 is held by: node", result["text"])
        self.assertIn("-i:3000", fake.calls[0])

    def test_asking_about_a_closed_port_is_answered_not_guessed(self):
        result, _, _ = self.turn("what is running on port 3000?", runner=FakeRunner(lsof="", lsof_rc=1))
        self.assertIn("Nothing is listening on TCP port 3000 right now", result["text"])

    def test_a_missing_folder_is_refused(self):
        result, _, _ = self.turn("how much space is left on /definitely/not/here?")
        self.assertIn("is not a folder I can read", result["text"])

    def test_streaming_is_chunked_under_one_message_id(self):
        result, _, _ = self.turn("what is going on in this repo?")
        chunks = [u for u in result["updates"] if u.get("sessionUpdate") == "agent_message_chunk"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len({chunk["messageId"] for chunk in chunks}), 1)

    def test_help_does_not_ask_permission(self):
        result, client, _ = self.turn("what can you do?")
        self.assertIn("read-only", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _, _ = self.turn("how much disk space is left?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_a_failing_git_leaves_a_readable_answer(self):
        result, _, _ = self.turn("what is going on in this repo?", runner=FakeRunner(log=""))
        self.assertIn("Branch: main", result["text"])
        self.assertNotIn("Last commit:", result["text"])

    def test_the_help_text_is_the_agent_help_constant(self):
        result, _, _ = self.turn("help")
        self.assertIn(HELP.splitlines()[0], result["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
