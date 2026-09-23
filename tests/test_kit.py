"""Tests for acp_kit: transport, initialize, sessions, prompt turns, permissions, cancel.

    python3 tests/test_kit.py

The agent and the client are wired to each other in memory, so the whole ACP
conversation runs for real (ndjson framing, threads, deferred responses) with no
subprocess and no network.
"""

from __future__ import annotations

import base64
import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import (  # noqa: E402
    AcpAgent,
    AcpClient,
    Connection,
    JsonRpcError,
    STOP_CANCELLED,
    STOP_END_TURN,
    STOP_REFUSAL,
    prompt_text,
    text_of,
)


class QueueReader:
    """Blocking line reader that a test can push into."""

    def __init__(self) -> None:
        self._items: queue.Queue = queue.Queue()

    def push(self, text: str) -> None:
        self._items.put(text)

    def close(self) -> None:
        self._items.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        item = self._items.get()
        if item is None:
            raise StopIteration
        return item


class WiredWriter:
    def __init__(self, reader: QueueReader) -> None:
        self.reader = reader

    def write(self, text: str) -> None:
        self.reader.push(text)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.reader.close()


def connected_pair():
    to_agent, to_client = QueueReader(), QueueReader()
    agent_conn = Connection(to_agent, WiredWriter(to_client), name="agent")
    client_conn = Connection(to_client, WiredWriter(to_agent), name="client")
    return agent_conn, client_conn


class EchoAgent(AcpAgent):
    name = "echo"
    title = "Echo Agent"
    version = "1.2.3"

    def new_session(self, session):
        session.remember("agent", f"session ready in {session.cwd}")
        return {"models": []}

    def prompt(self, ctx, prompt):
        ctx.plan([("read the request", "high"), ("answer it", "medium")])
        tool = "call_echo"
        ctx.tool_call(tool, "Echo the request", kind="other", name="echo", raw_input={"text": prompt_text(prompt)})
        if not ctx.ask_permission(tool, "Allow echo?", remember_key="echo"):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission before I can answer.")
            return STOP_REFUSAL
        ctx.check_cancelled()
        ctx.tool_call_update(tool, status="in_progress")
        ctx.stream_text(f"echo: {prompt_text(prompt)}", chunk_size=6)
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content("done"))
        ctx.usage(10, 200000, {"amount": 0.0, "currency": "USD"})
        session_note = ""
        self.maybe_extra(session_note)
        return STOP_END_TURN

    def maybe_extra(self, note: str) -> None:
        return None


class SlowAgent(EchoAgent):
    """Waits for cancellation instead of finishing."""

    def prompt(self, ctx, prompt):
        ctx.message("working…")
        for _ in range(600):
            ctx.check_cancelled()
            time.sleep(0.01)
        return STOP_END_TURN


class KitTests(unittest.TestCase):
    def setUp(self):
        self.agent_conn, self.client_conn = connected_pair()
        self.agent = EchoAgent(self.agent_conn)
        threading.Thread(target=self.agent_conn.serve, name="agent-reader", daemon=True).start()
        self.client = AcpClient(connection=self.client_conn)
        self.client.start()
        self.result = self.client.initialize()
        self.session_id = self.client.new_session(cwd="/tmp/project")

    def tearDown(self):
        self.client.stop()

    # -- handshake ---------------------------------------------------------

    def test_initialize_shape(self):
        self.assertEqual(self.result["protocolVersion"], 1)
        self.assertEqual(self.result["agentInfo"]["name"], "echo")
        self.assertEqual(self.result["agentInfo"]["version"], "1.2.3")
        self.assertEqual(self.result["authMethods"], [])
        capabilities = self.result["agentCapabilities"]
        self.assertTrue(capabilities["loadSession"])
        self.assertTrue(capabilities["promptCapabilities"]["embeddedContext"])
        self.assertFalse(capabilities["promptCapabilities"]["image"])

    def test_new_session_returns_id_and_extra(self):
        self.assertTrue(self.session_id.startswith("sess_"))
        self.assertIn(self.session_id, self.agent.sessions)

    def test_request_before_initialize_is_rejected(self):
        agent_conn, client_conn = connected_pair()
        agent = EchoAgent(agent_conn)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn)
        client.start()
        try:
            with self.assertRaises(JsonRpcError) as ctx:
                client.new_session(cwd="/tmp")
            self.assertEqual(ctx.exception.code, -32600)
        finally:
            client.stop()

    def test_unknown_method(self):
        with self.assertRaises(JsonRpcError) as ctx:
            self.client.conn.request("nope/at/all", {})
        self.assertEqual(ctx.exception.code, -32601)

    # -- prompt turn -------------------------------------------------------

    def test_prompt_turn_streams_updates(self):
        turn = self.client.prompt("hello there", self.session_id)
        self.assertEqual(turn["stopReason"], STOP_END_TURN)
        kinds = [update.get("sessionUpdate") for update in turn["updates"]]
        self.assertIn("plan", kinds)
        self.assertIn("tool_call", kinds)
        self.assertIn("tool_call_update", kinds)
        self.assertIn("usage_update", kinds)
        self.assertEqual(kinds.count("agent_message_chunk"), (len("echo: hello there") // 6) + 1)
        self.assertEqual(turn["text"], "echo: hello there")
        statuses = [u.get("status") for u in turn["updates"] if u.get("sessionUpdate") == "tool_call_update"]
        self.assertEqual(statuses, ["in_progress", "completed"])
        self.assertEqual(len(self.client.permission_requests), 1)
        self.assertEqual(turn["updates"][0]["entries"][0]["status"], "pending")

    def test_permission_denied_refuses_the_turn(self):
        client_conn, agent_conn = connected_pair()
        agent = EchoAgent(agent_conn)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission="reject")
        client.start()
        try:
            client.initialize()
            session_id = client.new_session(cwd="/tmp")
            turn = client.prompt("hello", session_id)
            self.assertEqual(turn["stopReason"], STOP_REFUSAL)
            failed = [u for u in turn["updates"] if u.get("status") == "failed"]
            self.assertEqual(len(failed), 1)
        finally:
            client.stop()

    def test_allow_always_is_remembered(self):
        client_conn, agent_conn = connected_pair()
        agent = EchoAgent(agent_conn)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission="allow-always")
        client.start()
        try:
            client.initialize()
            session_id = client.new_session(cwd="/tmp")
            client.prompt("first", session_id)
            client.prompt("second", session_id)
            self.assertEqual(len(client.permission_requests), 1)  # second turn used the remembered choice
        finally:
            client.stop()

    def test_session_load_replays_history(self):
        self.client.prompt("remember me", self.session_id)
        other_id = self.client.new_session(cwd="/tmp/project")

        def replays():
            return [
                update for update in self.client.updates
                if update.get("sessionUpdate") in ("user_message_chunk", "agent_message_chunk")
                and update.get("messageId", "").startswith("msg_replay_")
            ]

        # The fresh session only remembers the note EchoAgent left in new_session().
        self.client.load_session(other_id, "/tmp/project")
        fresh = replays()
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0]["sessionUpdate"], "agent_message_chunk")
        self.assertIn("session ready in /tmp/project", fresh[0]["content"]["text"])

        # The used session replays its note and the user's prompt, in order.
        self.client.updates.clear()
        self.client.load_session(self.session_id, "/tmp/project")
        used = replays()
        self.assertEqual([update["sessionUpdate"] for update in used],
                         ["agent_message_chunk", "user_message_chunk"])
        self.assertEqual(used[1]["content"]["text"], "remember me")

    def test_cancel_stops_the_turn(self):
        agent_conn, client_conn = connected_pair()
        agent = SlowAgent(agent_conn)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn)
        client.start()
        try:
            client.initialize()
            session_id = client.new_session(cwd="/tmp")
            holder: dict = {}

            def run_prompt():
                holder["turn"] = client.prompt("take your time", session_id)

            worker = threading.Thread(target=run_prompt, daemon=True)
            worker.start()
            time.sleep(0.1)
            client.cancel(session_id)
            worker.join(timeout=10)
            self.assertEqual(holder["turn"]["stopReason"], STOP_CANCELLED)
        finally:
            client.stop()

    def test_close_session_removes_it(self):
        self.assertEqual(self.client.close_session(self.session_id), {})
        self.assertNotIn(self.session_id, self.agent.sessions)
        with self.assertRaises(JsonRpcError) as ctx:
            self.client.close_session(self.session_id)
        self.assertEqual(ctx.exception.code, -32602)

    def test_unknown_session_prompt(self):
        with self.assertRaises(JsonRpcError) as ctx:
            self.client.conn.request("session/prompt", {"sessionId": "sess_nope", "prompt": [{"type": "text", "text": "x"}]})
        self.assertEqual(ctx.exception.code, -32602)


class ImageAgent(AcpAgent):
    """Answers with an image content block, from raw bytes and from base64."""

    name = "pics"
    title = "Picture Agent"
    supports_images = True

    def prompt(self, ctx, prompt):
        ctx.image(b"\x89PNG\r\n\x1a\n", "image/png")
        ctx.image("QUJD", "image/png", message_id="msg_second")
        return STOP_END_TURN


class FileAgent(AcpAgent):
    """Calls the client's own fs methods: "write <path> <content>" or "read <path>"."""

    name = "files"
    title = "File Agent"

    def prompt(self, ctx, prompt):
        text = prompt_text(prompt)
        if text.startswith("write "):
            path, _, content = text[len("write "):].partition(" ")
            ctx.conn.request("fs/write_text_file",
                             {"sessionId": ctx.session.id, "path": path, "content": content})
            ctx.message("wrote")
            return STOP_END_TURN
        path = text[len("read "):].strip() if text.startswith("read ") else text
        result = ctx.conn.request("fs/read_text_file",
                                  {"sessionId": ctx.session.id, "path": path})
        ctx.message(result["content"])
        return STOP_END_TURN


def wired(agent, client_kwargs=None):
    """A served agent plus a started client already past initialize/new_session."""
    agent_conn, client_conn = connected_pair()
    instance = agent(agent_conn)
    threading.Thread(target=agent_conn.serve, daemon=True).start()
    client = AcpClient(connection=client_conn, **(client_kwargs or {}))
    client.start()
    client.initialize()
    return instance, client, client.new_session(cwd="/tmp")


class ImageBlockTests(unittest.TestCase):
    def test_an_image_capable_agent_says_so_at_initialize(self):
        _agent, client, _session = wired(ImageAgent)
        try:
            capabilities = client.initialized_payload["agentCapabilities"]
            self.assertTrue(capabilities["promptCapabilities"]["image"])
        finally:
            client.stop()

    def test_bytes_are_base64_encoded_and_a_string_passes_through(self):
        _agent, client, session_id = wired(ImageAgent)
        try:
            turn = client.prompt("draw", session_id)
            images = [update["content"] for update in turn["updates"]
                      if update.get("content", {}).get("type") == "image"]
            self.assertEqual(len(images), 2)
            self.assertEqual(images[0]["mimeType"], "image/png")
            self.assertEqual(base64.b64decode(images[0]["data"]), b"\x89PNG\r\n\x1a\n")
            self.assertEqual(images[1]["data"], "QUJD")  # already encoded, left alone
            self.assertEqual(images[0]["data"], base64.b64encode(b"\x89PNG\r\n\x1a\n").decode())
            # an image carries no text, so the turn's text stays empty
            self.assertEqual(turn["text"], "")
        finally:
            client.stop()


class FileSystemTests(unittest.TestCase):
    def test_the_filesystem_capability_is_advertised_only_when_it_is_served(self):
        agent, client, _session = wired(FileAgent)
        try:
            self.assertEqual(agent.client_capabilities,
                             {"fs": {"readTextFile": False, "writeTextFile": False}})
        finally:
            client.stop()
        with tempfile.TemporaryDirectory() as root:
            agent, client, _session = wired(FileAgent, {"fs_root": root})
            try:
                self.assertEqual(agent.client_capabilities,
                                 {"fs": {"readTextFile": True, "writeTextFile": True}})
            finally:
                client.stop()

    def test_a_write_lands_on_disk_and_is_recorded(self):
        with tempfile.TemporaryDirectory() as root:
            _agent, client, session_id = wired(FileAgent, {"fs_root": root})
            try:
                target = Path(root) / "nested" / "BRIEFING.md"
                turn = client.prompt(f"write {target} hello there", session_id)
                self.assertEqual(turn["stopReason"], STOP_END_TURN)
                self.assertEqual(target.read_text(encoding="utf-8"), "hello there")
                self.assertEqual(len(client.written_files), 1)
                self.assertEqual(Path(client.written_files[0]["path"]).resolve(),
                                 target.resolve())
            finally:
                client.stop()

    def test_a_read_returns_the_file_and_only_what_was_asked_for(self):
        with tempfile.TemporaryDirectory() as root:
            _agent, client, session_id = wired(FileAgent, {"fs_root": root})
            try:
                target = Path(root) / "note.txt"
                client.prompt(f"write {target} first line", session_id)
                turn = client.prompt(f"read {target}", session_id)
                self.assertEqual(turn["text"], "first line")
                self.assertEqual(Path(client.read_files[0]["path"]).resolve(), target.resolve())
            finally:
                client.stop()

    def test_a_path_outside_the_root_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            _agent, client, session_id = wired(FileAgent, {"fs_root": root})
            try:
                outside = Path(root).parent / "escaped.txt"
                with self.assertRaises(JsonRpcError) as ctx:
                    client.prompt(f"write {outside} nope", session_id)
                message = str(ctx.exception.message)
                self.assertIn("outside this client's fs_root", message)
                self.assertFalse(outside.exists())
                self.assertEqual(client.written_files, [])
            finally:
                client.stop()

    def test_without_a_root_the_fs_methods_are_not_implemented(self):
        _agent, client, session_id = wired(FileAgent)
        try:
            with self.assertRaises(JsonRpcError) as ctx:
                client.prompt("write /tmp/whatever.txt hi", session_id)
            self.assertEqual(ctx.exception.code, -32601)
            self.assertEqual(client.written_files, [])
        finally:
            client.stop()

    def test_a_read_of_something_missing_is_named(self):
        with tempfile.TemporaryDirectory() as root:
            _agent, client, session_id = wired(FileAgent, {"fs_root": root})
            try:
                with self.assertRaises(JsonRpcError) as ctx:
                    client.prompt(f"read {Path(root) / 'nope.txt'}", session_id)
                self.assertEqual(ctx.exception.code, -32002)
                self.assertIn("no such file", str(ctx.exception.message))
            finally:
                client.stop()


class ContentBlockTests(unittest.TestCase):
    def test_text_of(self):
        self.assertEqual(text_of({"type": "text", "text": " hi "}), " hi ")
        self.assertEqual(text_of({"type": "resource", "resource": {"text": "file body"}}), "file body")
        self.assertEqual(text_of({"type": "resource_link", "name": "n", "uri": "u"}), "n")
        self.assertEqual(text_of({"type": "image", "data": "..."}), "")

    def test_prompt_text_joins_blocks(self):
        blocks = [
            {"type": "text", "text": "look at this"},
            {"type": "resource", "resource": {"uri": "file:///x", "text": "body"}},
        ]
        self.assertEqual(prompt_text(blocks), "look at this\nbody")
        self.assertEqual(prompt_text([]), "")


if __name__ == "__main__":
    unittest.main()
