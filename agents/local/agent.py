"""Local — the machine you are sitting in: disk space, the git repo, listening ports.

ACP's whole point is that the editor launches the agent next to your code, and yet every
agent on the vendors' list is a chat that writes code and never once looks at the machine it
is running on. This one reads what the editor already knows: the session's working directory.

It is read-only, on purpose and by construction. The only commands it can run are `git`
(reporting) and `lsof` (listing), and it never writes, stages, commits, or kills anything. The
one question it asks permission for is reading your machine at all, and the answer is bound to
a single remember-key so you are asked once, not per turn.
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
    DATASET,
    LocalData,
    LocalError,
    check_path,
    human_bytes,
    port_filter,
)

HELP = (
    "I read this machine, read-only. Ask me:\n"
    "  • how much disk space is left?\n"
    "  • what is going on in this repo?\n"
    "  • what am I running on port 3000?\n"
    "  • which ports are listening on this machine?\n"
    "I run `git rev-parse`, `git status`, `git log` and `lsof`, nothing else. I never write, "
    "stage, commit, or kill a process. Paths I cannot read are reported, not guessed at."
)

PERMISSION_KEY = "local-read-this-machine"

SKILLS = ("disk", "git", "ports", "help")

_PATH_RE = re.compile(r"(~?/[^\s\"',;]+|\.\.?/[^\s\"',;]+)")

_DISK_WORDS = re.compile(
    r"\b(disk|storage|space|free space|drive|volume|gb|tb|out of space|full)\b", re.IGNORECASE)
_GIT_WORDS = re.compile(
    r"\b(git|repo|repository|branch|commit|commits|uncommitted|dirty|staged|untracked|"
    r"working tree|changes|diff|ahead|behind|main)\b", re.IGNORECASE)
_PORT_WORDS = re.compile(
    r"\b(port|ports|listening|listen|lsof|bound|bound to|serving|running on|socket)\b",
    re.IGNORECASE)


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    params: dict = {}
    port = port_filter(text)
    if port is not None:
        params["port"] = port
    match = _PATH_RE.search(text)
    if match:
        # Trailing sentence punctuation is not part of a path: 'on /tmp?' names /tmp.
        params["path"] = match.group(1).rstrip("?!,;:")

    if port is not None or _PORT_WORDS.search(text):
        return "ports", params
    if _GIT_WORDS.search(text):
        return "git", params
    if _DISK_WORDS.search(text):
        return "disk", params
    if "path" in params:
        # A bare path is a place people ask about, and disk space is the safe read.
        return "disk", params
    return "help", {}


class LocalAgent(AcpAgent):
    name = "local"
    title = "Local — disk, git and ports on this machine"
    version = "1.0.0"

    def __init__(self, connection=None, data: LocalData | None = None) -> None:
        super().__init__(connection)
        self.data = data or LocalData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Local session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Resolve the folder (the session's own working directory unless you named one)", "medium"),
            ("Read it locally, read-only", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("I only look at the machine this editor launched me on, and only "
                        "through `git` and `lsof`.")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read the local {skill} state", kind="read", name=skill,
                      raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Local to read this machine (disk space, git status, listening ports)?",
                                  remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to look at this machine before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params, ctx)
        except (LocalError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read that: {exc}")
            return STOP_END_TURN
        ctx.check_cancelled()
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(artifact["summary"]))
        ctx.stream_text(answer)
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        # Each skill is one short read; there is nothing long-running to interrupt.
        return None

    # -- skills ------------------------------------------------------------

    def _run_skill(self, skill: str, params: dict, ctx: SessionContext) -> tuple[str, dict]:
        if skill == "disk":
            return self._disk(params, ctx)
        if skill == "git":
            return self._git(params, ctx)
        if skill == "ports":
            return self._ports(params)
        return HELP, {"summary": "help", "dataset": None}

    @staticmethod
    def _where(params: dict, ctx: SessionContext) -> str:
        """The folder to read: what the question named, else the session's own cwd."""
        if params.get("path"):
            return check_path(params["path"])
        return check_path(ctx.session.cwd)

    def _disk(self, params: dict, ctx: SessionContext) -> tuple[str, dict]:
        where = self._where(params, ctx)
        disk = self.data.disk(where)
        artifact = {
            "summary": f"{DATASET}: {where} has {human_bytes(disk['free'])} free of "
                       f"{human_bytes(disk['total'])}",
            "dataset": DATASET,
            "path": where,
            "total": disk["total"],
            "used": disk["used"],
            "free": disk["free"],
            "percent_free": disk["percent_free"],
            "low": disk["low"],
        }
        lines = [
            f"{disk['path']} at {self.data.now()}:",
            f"  • Total: {human_bytes(disk['total'])}",
            f"  • Used: {human_bytes(disk['used'])}",
            f"  • Free: {human_bytes(disk['free'])} ({disk['percent_free']:.1f}% of the volume)",
        ]
        if disk["low"]:
            lines.append(f"  • Tight: under the 10 GB free mark I would flag. Cleaning caches "
                         "(package manager caches, build output) is the usual first move - not "
                         "your source, config or databases.")
        lines.append(
            f"\nSource: {DATASET}. This is the free space the operating system reports for the "
            "filesystem holding that folder, read locally. I do not scan folders or judge what "
            "is taking the space - I only ask the OS what is free."
        )
        return "\n".join(lines), artifact

    def _git(self, params: dict, ctx: SessionContext) -> tuple[str, dict]:
        where = self._where(params, ctx)
        report = self.data.git(where)
        changed = report["changed"]
        artifact = {
            "summary": f"{DATASET}: {report['root']} on {report['branch']}, "
                       f"{len(changed)} uncommitted path(s)",
            "dataset": DATASET,
            "root": report["root"],
            "branch": report["branch"],
            "upstream": report["upstream"],
            "ahead": report["ahead"],
            "behind": report["behind"],
            "staged": report["staged"],
            "modified": report["modified"],
            "untracked": report["untracked"],
            "conflicted": report["conflicted"],
            "changed": changed,
            "last_commit": report["last_commit"],
        }
        branch = report["branch"] or "(detached HEAD)"
        tracking = ""
        if report["upstream"]:
            tracking = f", tracking {report['upstream']}"
            if report["ahead"] is not None:
                tracking += f", {report['ahead']} ahead and {report['behind']} behind"
        lines = [f"Repository {report['root']} at {self.data.now()}:",
                 f"  • Branch: {branch}{tracking}"]
        if report["conflicted"]:
            lines.append(f"  • Conflicts: {report['conflicted']} path(s) need resolving before "
                         "any commit - nothing else on this branch is safe to judge until then")
        lines.append(f"  • Working tree: {report['staged']} staged, {report['modified']} modified, "
                     f"{report['untracked']} untracked")
        for row in changed[:12]:
            was = f" (was {row['from']})" if row.get("from") else ""
            lines.append(f"    - [{row['state']}] {row['path']}{was}")
        if len(changed) > 12:
            lines.append(f"    - ... and {len(changed) - 12} more")
        last = report["last_commit"]
        if last:
            lines.append(f"  • Last commit: {last['short']} {last['date']} by {last['author']} - "
                         f"{last['subject']}")
        lines.append(
            f"\nSource: {DATASET}. Read with `git status --porcelain=v2 --branch` and "
            "`git log -1`, locally. Nothing was staged, committed or restored to produce this."
        )
        return "\n".join(lines), artifact

    def _ports(self, params: dict) -> tuple[str, dict]:
        port = params.get("port")
        found = self.data.ports(port)
        listeners = found["listeners"]
        scope = f"port {port}" if port else "this machine"
        artifact = {
            "summary": f"{DATASET}: {found['total']} listening TCP port(s) on {scope}",
            "dataset": DATASET,
            "port": port,
            "listeners": listeners,
            "shown": found["shown"],
            "total": found["total"],
            "distinct_ports": found["distinct_ports"],
        }
        if not listeners:
            if port:
                lines = [f"Nothing is listening on TCP port {port} right now.",
                         "That means the server is not running, or it is bound to a socket I "
                         "cannot see as this user."]
            else:
                lines = ["Nothing is listening on TCP on this machine right now."]
        else:
            headline = (f"{found['total']} listener(s) on port {port}" if port
                        else f"{found['total']} TCP listener(s) on this machine")
            lines = [f"{headline} at {self.data.now()}:"]
            for row in listeners:
                pid = f"pid {row['pid']}, " if row["pid"] else ""
                lines.append(f"  • {row['port']} - {row['command']} ({pid}user {row['user']}) "
                             f"on {row['address']}")
            if found["total"] > found["shown"]:
                lines.append(f"  • ... and {found['total'] - found['shown']} more")
            if port:
                owners = sorted({row["command"] for row in listeners})
                lines.append(f"  • Port {port} is held by: {', '.join(owners)}")
        lines.append(
            f"\nSource: {DATASET}. Read from `lsof -nP -iTCP -sTCP:LISTEN`, locally, and only "
            "TCP sockets in the LISTEN state are listed - established connections, which are "
            "other people's traffic, are not shown. I do not stop or restart anything."
        )
        return "\n".join(lines), artifact


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        LocalAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
