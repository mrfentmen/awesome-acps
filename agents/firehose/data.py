"""Read-only Wikimedia EventStreams reader for the firehose agent.

One service, and the only one in this repo that is a **push** channel rather than a feed you
poll. Verified live on 2026-09-23: connecting to the stream and reading lines returns real
edits as they happen.

  https://stream.wikimedia.org/v2/stream/recentchange   every edit on every Wikimedia wiki

The stream is Server-Sent Events: comment lines start with ':', fields are 'event:', 'id:' and
'data:', and each `data:` line is one JSON object. Wikimedia sends one `data:` line per event,
so this reader reads line by line and decodes those.

Because it is a stream, the reader is a generator and the caller owns the limits. Two rules
keep it honest: reading stops on the caller's count, on a wall-clock deadline, or when the
socket goes quiet for `timeout` seconds, and the filter never hides what it filtered - every
result carries how many events were seen so the answer can say "12 of 240" instead of "12".
"""

from __future__ import annotations

import json
import os
import time
from urllib import request

STREAM_URL = "https://stream.wikimedia.org/v2/stream/recentchange"
DATASET = "Wikimedia EventStreams (stream.wikimedia.org, public, keyless push stream)"

DEFAULT_USER_AGENT = "awesome-acps-firehose/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: Hard caps, so a stream can never hold a prompt turn open forever.
MAX_EVENTS = 25
MAX_SECONDS = 60.0

#: Fields worth keeping from the very large recentchange object.
KEPT_FIELDS = ("title", "title_url", "user", "bot", "comment", "type", "minor", "wiki",
               "server_name", "namespace", "timestamp", "id", "revision", "length")


class FirehoseError(RuntimeError):
    """The event stream could not be read."""


def parse_sse_line(line: str) -> dict | None:
    """One line of the stream to an event dict, or None when it is not a data line."""
    text = str(line or "").strip()
    if not text.startswith("data:"):
        return None
    payload = text[len("data:"):].strip()
    if not payload:
        return None
    try:
        decoded = json.loads(payload)
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


def normalise(event: dict) -> dict:
    """One raw recentchange object to the small shape the agent answers with."""
    revision = event.get("revision") or {}
    length = event.get("length") or {}
    old, new = length.get("old"), length.get("new")
    try:
        delta = int(new) - int(old)
    except (TypeError, ValueError):
        delta = None
    return {
        "title": str(event.get("title") or "(untitled)"),
        "user": str(event.get("user") or "(unknown)"),
        "bot": bool(event.get("bot")),
        "type": str(event.get("type") or ""),
        "minor": bool(event.get("minor")),
        "wiki": str(event.get("wiki") or ""),
        "server": str(event.get("server_name") or ""),
        "comment": str(event.get("comment") or ""),
        "delta": delta,
        "url": event.get("title_url") or "",
        "old_revision": revision.get("old"),
        "revision": revision.get("new"),
        "id": event.get("id"),
    }


def matches(event: dict, skip_bots: bool = True, only_new: bool = False,
            language: str | None = None, wiki: str | None = None,
            min_delta: int | None = None) -> bool:
    """Whether one normalised event is one the caller asked to see. Pure and testable."""
    if skip_bots and event.get("bot"):
        return False
    if only_new and event.get("type") != "new":
        return False
    if min_delta is not None:
        delta = event.get("delta")
        if delta is None or abs(delta) < int(min_delta):
            return False
    if wiki and event.get("wiki") != wiki:
        return False
    if language:
        wanted = language.lower().removesuffix("wiki")
        server = event.get("server") or ""
        code = server.split(".", 1)[0].lower()
        if code != wanted:
            return False
    return True


class FirehoseData:
    """The Wikimedia edit stream, read as a generator with caller-owned limits."""

    def __init__(self, fetch=None, timeout: float = 20.0, user_agent: str | None = None) -> None:
        self.timeout = float(os.environ.get("FIREHOSE_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("FIREHOSE_USER_AGENT") or DEFAULT_USER_AGENT
        #: fetch(url, timeout) -> an iterable of text lines from the open stream.
        self._fetch = fetch or self._http_lines

    def _http_lines(self, url: str, timeout: float):
        req = request.Request(url, headers={"User-Agent": self.user_agent,
                                           "Accept": "text/event-stream"})
        try:
            response = request.urlopen(req, timeout=timeout)
        except Exception as exc:  # urllib raises many types; callers see one error type
            raise FirehoseError(f"could not connect to the event stream: {exc}") from exc
        with response:
            for raw in response:
                yield raw.decode("utf-8", "replace")

    def events(self, skip_bots: bool = True, only_new: bool = False, language=None, wiki=None,
               min_delta=None, count: int = 10, seconds: float = 30.0):
        """Yield normalised events that match, up to a count or a deadline.

        Also yields a final {"seen": n} record through `stats` so the caller can report how
        much of the stream was filtered out instead of implying the stream was quiet.
        """
        self.stats = {"seen": 0, "matched": 0, "stopped": "count"}
        deadline = time.monotonic() + max(1.0, min(float(seconds), MAX_SECONDS))
        wanted = max(1, min(int(count), MAX_EVENTS))
        try:
            lines = self._fetch(STREAM_URL, self.timeout)
        except FirehoseError:
            raise
        try:
            for line in lines:
                event = parse_sse_line(line)
                if event is None:
                    continue
                self.stats["seen"] += 1
                row = normalise(event)
                if not matches(row, skip_bots=skip_bots, only_new=only_new, language=language,
                               wiki=wiki, min_delta=min_delta):
                    if time.monotonic() > deadline:
                        self.stats["stopped"] = "deadline"
                        return
                    continue
                self.stats["matched"] += 1
                yield row
                if self.stats["matched"] >= wanted:
                    self.stats["stopped"] = "count"
                    return
                if time.monotonic() > deadline:
                    self.stats["stopped"] = "deadline"
                    return
        except FirehoseError:
            raise
        except Exception as exc:
            raise FirehoseError(f"the event stream stopped: {exc}") from exc
