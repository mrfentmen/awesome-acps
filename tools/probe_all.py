#!/usr/bin/env python3
"""Probe every agent in this repo the way an editor would: real stdio, real data, one turn.

    python3 tools/probe_all.py              # every agent
    python3 tools/probe_all.py air books    # only the named ones

Each agent is spawned as a child process, spoken to over ACP (initialize → new_session →
session/prompt), and its answer is printed with the tool call and the stop reason. Every
question in the table below hits a public, keyless dataset - there is no fixture and no
fake in this path. If an agent cannot answer, this exits non-zero.

The a2a-bridge agent needs a running A2A server to be meaningful, so it is probed only when
ACP_A2A_ENDPOINTS is set in the environment (for example nyc311=http://127.0.0.1:8787).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import AcpClient  # noqa: E402  (path setup must come first)

#: agent name -> (agent script, one live question). Every question reads a real feed.
AGENTS: dict[str, tuple[str, str]] = {
    "a2a_bridge": ("agents/a2a_bridge/bridge.py", "which agents can you reach?"),
    "air": ("agents/air/agent.py", "how is the air quality in Delhi right now?"),
    "alert": ("agents/alert/agent.py", "alert me when the air quality in Delhi passes 200 "
                                        "for 1 check every 5 seconds"),
    "archive": ("agents/archive/agent.py", "find Apollo 11 recordings"),
    "art": ("agents/art/agent.py", "paintings by Monet"),
    "aurora": ("agents/aurora/agent.py", "how are the geomagnetic conditions right now?"),
    "bikes": ("agents/bikes/agent.py", "how many citibikes are available right now?"),
    "books": ("agents/books/agent.py", "find books about urban foxes"),
    "brief": ("agents/brief/agent.py", "brief me on Denver"),
    "buoys": ("agents/buoys/agent.py", "what are the conditions at buoy 41025?"),
    "chart": ("agents/chart/agent.py", "chart the temperature in Seattle for the next 12 hours"),
    "chem": ("agents/chem/agent.py", "what is the formula for ibuprofen?"),
    "civic": ("agents/civic/agent.py", "did complaint 70483808 get fixed?"),
    "crypto": ("agents/crypto/agent.py", "what is the price of bitcoin right now?"),
    "domains": ("agents/domains/agent.py", "when does example.com expire?"),
    "drought": ("agents/drought/agent.py", "how dry is California?"),
    "firehose": ("agents/firehose/agent.py", "watch the wiki firehose for 2 edits"),
    "floodwatch": ("agents/floodwatch/agent.py", "what is the Mississippi River at St. Louis doing?"),
    "food": ("agents/food/agent.py", "how much sugar is in Nutella?"),
    "forecast": ("agents/forecast/agent.py", "when will it rain in Seattle today?"),
    "fx": ("agents/fx/agent.py", "what is 100 USD in EUR?"),
    "groundwater": ("agents/groundwater/agent.py", "water level at site 395847084085500"),
    "hazards": ("agents/hazards/agent.py", "any weather alerts in NY right now?"),
    "iss": ("agents/iss/agent.py", "could I see the ISS from Denver tonight?"),
    "labels": ("agents/labels/agent.py", "what is lipitor?"),
    "launches": ("agents/launches/agent.py", "what is the next launch?"),
    "ledger": ("agents/ledger/agent.py", "what is the national debt right now?"),
    "local": ("agents/local/agent.py", "what is going on in this repo?"),
    "moon": ("agents/moon/agent.py", "when is the next full moon?"),
    "nature": ("agents/nature/agent.py", "how many monarch butterflies are recorded in Canada?"),
    "osm": ("agents/osm/agent.py", "nearest drinking water to Bryant Park, New York"),
    "papers": ("agents/papers/agent.py", "papers about CRISPR sickle cell"),
    "pollen": ("agents/pollen/agent.py", "how bad is the pollen today in Denver?"),
    "rivers": ("agents/rivers/agent.py", "what is gauge 06730500 doing right now?"),
    "snowpack": ("agents/snowpack/agent.py", "how much water is in the California snowpack?"),
    "species": ("agents/species/agent.py", "how many observations of Danaus plexippus are there?"),
    "stocks": ("agents/stocks/agent.py", "what is AAPL doing right now?"),
    "surf": ("agents/surf/agent.py", "how are the waves at Pipeline right now?"),
    "tides": ("agents/tides/agent.py", "when is the next high tide in Boston?"),
    "trending": ("agents/trending/agent.py", "what is trending on Wikipedia today?"),
    "tsunami": ("agents/tsunami/agent.py", "is there a tsunami warning right now?"),
    "vehicles": ("agents/vehicles/agent.py", "recalls for the 2015 Honda Civic"),
    "watch": ("agents/watch/agent.py", "watch the aurora for 3 rounds every 3 seconds"),
    "wiki": ("agents/wiki/agent.py", "who was Ada Lovelace?"),
    "wildfire": ("agents/wildfire/agent.py", "what wildfires are burning in California right now?"),
}

#: Every agent's HELP block opens its list of examples with this, which makes it possible to
#: tell a real answer from an agent that quietly fell back to its own instructions. Without this
#: check an agent that routes a question to help "passes" the probe while answering nothing.
HELP_MARKER = "Ask me:"
HELP_QUESTIONS = ("help", "what can you do", "commands")

#: Every agent says this when the public feed behind it did not answer (see `upstream_error` in
#: the kit). Such a turn is not a failure of the agent - reporting the outage is the right answer -
#: but it is not proof of live data either, so the run names it instead of counting it as one.
UPSTREAM_MARKER = "I could not read "


def classify(text: str, prompt: str) -> tuple[bool, bool]:
    """(hollow, upstream) for one answer — what a green probe line still would not prove.

    hollow: the answer is the agent's own help text, so the question went unanswered.
    upstream: the agent reported that its feed did not answer, so this turn proves the
    outage is handled, not that live data was read.
    """
    asked_for_help = any(word in prompt.lower() for word in HELP_QUESTIONS)
    hollow = HELP_MARKER in text and not asked_for_help
    return hollow, UPSTREAM_MARKER in text


def probe(name: str, script: str, prompt: str, timeout: float, show_log: bool) -> dict:
    started = time.time()
    client = AcpClient(command=[sys.executable, str(REPO_ROOT / script)], permission="allow-once",
                       timeout=timeout, stderr=None if show_log else sys.stderr)
    report: dict = {"agent": name, "prompt": prompt, "ok": False, "title": None, "tool": None,
                    "stop": None, "text": "", "seconds": 0.0, "hollow": False, "upstream": False}
    try:
        client.start()
        info = client.initialize(name="probe-all")
        agent_info = info.get("agentInfo") or {}
        report["title"] = agent_info.get("title") or agent_info.get("name")
        session_id = client.new_session(cwd=os.getcwd())
        turn = client.prompt(prompt, session_id)
        for update in turn["updates"]:
            if update.get("sessionUpdate") == "tool_call" and not report["tool"]:
                report["tool"] = f"{update.get('name')} ({update.get('status')})"
        report["stop"] = turn["stopReason"]
        report["text"] = turn["text"] or ""
        report["hollow"], report["upstream"] = classify(report["text"], prompt)
        report["ok"] = (bool(report["text"].strip()) and turn["stopReason"] == "end_turn"
                        and not report["hollow"])
    except Exception as exc:  # a crash in the agent is a failed probe, not a crashed tool
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        client.stop()
    report["seconds"] = round(time.time() - started, 2)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="live ACP probe of every agent")
    parser.add_argument("names", nargs="*", help="agents to probe (default: all)")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--show-server-log", action="store_true", help="let agent logs through")
    parser.add_argument("--json", action="store_true", help="machine-readable summary")
    args = parser.parse_args(argv)

    wanted = args.names or list(AGENTS)
    unknown = [name for name in wanted if name not in AGENTS]
    if unknown:
        print(f"unknown agent(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    endpoints = os.environ.get("ACP_A2A_ENDPOINTS", "").strip()
    reports = []
    for name in wanted:
        if name == "a2a_bridge":
            if endpoints:
                reports.append(probe(name, *AGENTS[name], args.timeout, args.show_server_log))
            else:
                print("skipping a2a_bridge: set ACP_A2A_ENDPOINTS to a running A2A server "
                      "(for example nyc311=http://127.0.0.1:8787)\n", file=sys.stderr)
            continue
        reports.append(probe(name, *AGENTS[name], args.timeout, args.show_server_log))

    if args.json:
        print(json.dumps(reports, indent=2))
    else:
        print(f"== probing {len(reports)} agent(s) over real ACP stdio, live data\n")
        for report in reports:
            mark = "PASS" if report["ok"] else "FAIL"
            print(f"[{mark}] {report['agent']} ({report['seconds']}s) - {report['prompt']}")
            if report.get("title"):
                print(f"       agent: {report['title']}")
            if report.get("tool"):
                print(f"       tool : {report['tool']}")
            if report.get("error"):
                print(f"       error: {report['error']}")
            if report.get("hollow"):
                print("       hollow: the answer is the agent's own help text, so the question "
                      "was not actually answered")
            if report.get("upstream"):
                print("       upstream: the feed did not answer, so this turn proves the agent "
                      "reports the outage, not the dataset")
            print(f"       stop : {report['stop']}")
            for line in report["text"].splitlines():
                print(f"       | {line}")
            print()
        passed = sum(1 for report in reports if report["ok"])
        hollow = sum(1 for report in reports if report.get("hollow"))
        failed_upstream = [report["agent"] for report in reports if report.get("upstream")]
        notes = [f"{hollow} fell back to help"] if hollow else []
        if failed_upstream:
            notes.append("upstream down for " + ", ".join(failed_upstream))
        print(f"== {passed}/{len(reports)} agent(s) answered a live question"
              + (f"  ({'; '.join(notes)})" if notes else ""))

    return 1 if any(not report["ok"] for report in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
