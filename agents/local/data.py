"""Reads for the `local` agent: disk space, the git repo you are sitting in, and listening ports.

All read-only. Nothing here writes, stages, commits, kills a process or opens a socket: the
only commands run are `git rev-parse`, `git status`, `git log` and `lsof` (to list listeners).
Every command is a fixed argv list - no shell, so no quoting surprises - and each one is
injected through a runner so the tests never touch the real machine.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime, timezone

DATASET = "your machine (read-only: disk space, git repo, listening TCP ports)"

#: Free space below this is worth warning about, in bytes (10 GB). The repo's own house rule.
LOW_FREE_BYTES = 10 * 1024**3

#: Above this many entries a listing is summarised instead of printed in full.
MAX_ROWS = 40

#: Commands this agent is allowed to run. Anything else is a bug, not a permission problem.
_ALLOWED = ("git", "lsof")


class LocalError(RuntimeError):
    """A read failed: missing binary, not a repo, a timeout, or nonsense output."""


def _run(argv: list[str], cwd: str | None, timeout: float) -> tuple[int, str]:
    """Run one read-only command. Returns (returncode, stdout+stderr)."""
    if not argv or argv[0] not in _ALLOWED:
        raise LocalError(f"refusing to run {argv[0]!r}: not one of {', '.join(_ALLOWED)}")
    try:
        proc = subprocess.run(
            argv, cwd=cwd, timeout=timeout, capture_output=True, text=True, check=False,
        )
    except FileNotFoundError as exc:
        raise LocalError(f"{argv[0]} is not installed on this machine") from exc
    except subprocess.TimeoutExpired as exc:
        raise LocalError(f"{argv[0]} did not answer within {timeout:.0f}s") from exc
    except OSError as exc:
        raise LocalError(f"could not run {argv[0]}: {exc}") from exc
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def human_bytes(value) -> str:
    """1234567890 -> '1.15 GB'. None stays honest."""
    if value is None:
        return "unknown"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(size) < 1024 or unit == "PB":
            digits = 0 if unit in ("B", "KB") else 1
            return f"{size:.{digits}f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def check_path(value) -> str:
    """A directory to read. Must exist, so a typo cannot look like an empty repo."""
    text = str(value or "").strip()
    if not text:
        return os.getcwd()
    path = os.path.abspath(os.path.expanduser(text))
    if not os.path.isdir(path):
        raise ValueError(f"{text!r} is not a folder I can read")
    return path


def port_filter(text) -> int | None:
    """A port number named in a question ('port 3000', ':5432'), or None.

    A digit must not come straight before the colon, so a clock time like 12:30 is not read
    as a port.
    """
    match = re.search(r"(?:\bport\b\s*[:#]?\s*|(?<!\d):)(\d{2,5})(?!\d)", str(text), re.IGNORECASE)
    if match:
        port = int(match.group(1))
        if 0 < port <= 65535:
            return port
    return None


def _parse_lsof(output: str) -> list[dict]:
    """Rows of `lsof -nP -iTCP -sTCP:LISTEN` -> listeners, ignoring the header.

    NAME looks like `*:3000`, `127.0.0.1:5432` or `[::1]:8080` - the port is whatever
    follows the last colon, and the address is what comes before it.
    """
    listeners: list[dict] = []
    for line in (output or "").splitlines():
        if not line or line.startswith("COMMAND"):
            continue
        fields = line.split()
        if len(fields) < 9:
            continue
        # The last column is the state, written as a parenthesised word like '(LISTEN)'.
        name = fields[-2] if fields[-1].startswith("(") else fields[-1]
        if ":" not in name:
            continue
        address, _, port_text = name.rpartition(":")
        port_text = port_text.strip()
        if not port_text.isdigit():
            continue
        listeners.append({
            "command": fields[0],
            "pid": int(fields[1]) if fields[1].isdigit() else None,
            "user": fields[2],
            "address": address.strip("[]") or "*",
            "port": int(port_text),
        })
    listeners.sort(key=lambda row: (row["port"], row["command"]))
    return listeners


def _parse_status(output: str) -> dict:
    """`git status --porcelain=v2 --branch` -> branch, ahead/behind and changed files."""
    report = {"branch": None, "upstream": None, "ahead": None, "behind": None,
              "staged": 0, "modified": 0, "untracked": 0, "conflicted": 0, "changed": []}
    for line in (output or "").splitlines():
        if line.startswith("# branch.head "):
            report["branch"] = line.split(" ", 2)[2].strip()
        elif line.startswith("# branch.upstream "):
            report["upstream"] = line.split(" ", 2)[2].strip()
        elif line.startswith("# branch.ab "):
            # Written as '+2 -1': the signs say which way, the counts are always positive.
            counts = re.findall(r"([+-])(\d+)", line.split(" ", 2)[2])
            if len(counts) == 2:
                report["ahead"], report["behind"] = int(counts[0][1]), int(counts[1][1])
        elif line.startswith("? "):
            report["untracked"] += 1
            report["changed"].append({"state": "untracked", "path": line[2:]})
        elif line.startswith("u "):
            report["conflicted"] += 1
            report["changed"].append({"state": "conflicted", "path": line.rsplit(" ", 1)[-1]})
        elif line[:2] in ("1 ", "2 "):
            # A rename line is `2 <xy> ... <score> <path>\t<original path>`: the current name
            # is the useful one, and the old name is kept alongside it.
            head, _, origin = line.partition("\t")
            parts = head.split()
            xy = parts[1] if len(parts) > 1 else ".."
            staged = xy[0] not in (".", " ")
            modified = xy[1] not in (".", " ")
            report["staged"] += 1 if staged else 0
            report["modified"] += 1 if modified else 0
            entry = {
                "state": "staged+modified" if staged and modified else
                         "staged" if staged else "modified",
                "path": parts[-1] if parts else "",
            }
            if origin:
                entry["from"] = origin
            report["changed"].append(entry)
    return report


class LocalData:
    """Read-only views of this machine. Every method is a single read, injected for tests."""

    def __init__(self, runner=None, disk_usage=None, timeout: float | None = None) -> None:
        self.timeout = float(os.environ.get("LOCAL_AGENT_TIMEOUT", timeout or 10.0))
        self._run = runner or (lambda argv, cwd: _run(argv, cwd, self.timeout))
        self._disk_usage = disk_usage or shutil.disk_usage

    # -- disk --------------------------------------------------------------

    def disk(self, path=None) -> dict:
        """Space on the filesystem that holds a path."""
        target = check_path(path)
        try:
            usage = self._disk_usage(target)
        except OSError as exc:
            raise LocalError(f"could not read disk usage for {target!r}: {exc}") from exc
        free, total = usage.free, usage.total
        percent_free = (free / total * 100) if total else 0.0
        return {
            "path": target,
            "total": total,
            "used": usage.used,
            "free": free,
            "percent_free": percent_free,
            "low": free < LOW_FREE_BYTES,
        }

    # -- git ---------------------------------------------------------------

    def git(self, path=None) -> dict:
        """The repo containing a path: branch, divergence, changed files, last commit."""
        start = check_path(path)
        code, output = self._run(["git", "rev-parse", "--show-toplevel"], start)
        root = (output or "").strip().splitlines()[0] if code == 0 and output.strip() else ""
        if code != 0 or not root or not os.path.isdir(root):
            raise ValueError(f"{start!r} is not inside a git repository")

        status_code, status_out = self._run(
            ["git", "status", "--porcelain=v2", "--branch", "--untracked-files=normal"], root)
        if status_code != 0:
            raise LocalError(f"git status failed in {root!r}: {status_out.strip()[:200]}")
        report = _parse_status(status_out)
        report.update({"root": root, "asked": start})

        log_code, log_out = self._run(
            ["git", "log", "-1", "--format=%h%x09%aI%x09%an%x09%s"], root)
        if log_code == 0 and log_out.strip():
            parts = log_out.strip().splitlines()[0].split("\t")
            if len(parts) == 4:
                report["last_commit"] = {
                    "short": parts[0], "date": parts[1], "author": parts[2], "subject": parts[3],
                }
        report.setdefault("last_commit", None)
        return report

    # -- ports -------------------------------------------------------------

    def ports(self, port: int | None = None) -> dict:
        """TCP ports in LISTEN state, optionally only one port."""
        argv = ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]
        if port is not None:
            argv += [f"-i:{int(port)}"]
        code, output = self._run(argv, None)
        # lsof exits 1 when nothing matches, which is an answer, not a failure.
        if code not in (0, 1):
            raise LocalError(f"lsof failed: {output.strip()[:200]}")
        listeners = _parse_lsof(output)
        return {
            "port": port,
            "listeners": listeners[:MAX_ROWS],
            "shown": min(len(listeners), MAX_ROWS),
            "total": len(listeners),
            "distinct_ports": sorted({row["port"] for row in listeners}),
        }

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
