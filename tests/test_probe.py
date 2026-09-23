"""Tests for tools/probe_all.py: what a green probe line still would not prove.

    python3 tests/test_probe.py

Only the classifier is tested here - the probe itself spawns agents and reads the
network, which the rest of the suite must not do. These cases are the two answers that
can look like a pass and are not one: an agent that fell back to its own help text, and
an agent that reported its feed was down.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.probe_all import AGENTS, HELP_MARKER, classify  # noqa: E402


class ClassifyTests(unittest.TestCase):
    def test_help_text_for_a_real_question_is_hollow(self):
        answer = f"{HELP_MARKER}\n- what is Apple trading at?\n- how did NVDA move?"
        hollow, upstream = classify(answer, "what is AAPL doing right now?")
        self.assertTrue(hollow)
        self.assertFalse(upstream)

    def test_help_text_asked_for_on_purpose_is_not_hollow(self):
        answer = f"Air quality agent.\n{HELP_MARKER}\n- how is the air in Delhi?"
        hollow, upstream = classify(answer, "help")
        self.assertFalse(hollow)
        self.assertFalse(upstream)

    def test_help_question_variants_count(self):
        answer = f"{HELP_MARKER}\n- one example"
        for prompt in ("what can you do?", "what CAN you do", "commands"):
            with self.subTest(prompt=prompt):
                self.assertFalse(classify(answer, prompt)[0])

    def test_upstream_outage_is_flagged(self):
        answer = ("I could not read OpenStreetMap: Overpass did not answer on either mirror: "
                  "The read operation timed out")
        hollow, upstream = classify(answer, "nearest drinking water to Bryant Park, New York")
        self.assertFalse(hollow)
        self.assertTrue(upstream)

    def test_normal_answer_is_neither(self):
        answer = "BOSTON, MA (8443970): next high tide 2026-09-23 14:06 local."
        self.assertEqual(classify(answer, "when is the next high tide in Boston?"),
                         (False, False))

    def test_agent_reporting_a_missing_record_is_not_an_outage(self):
        answer = ("10 active wildfire(s) matching POOState = 'US-CA'.\n"
                  "Containment not reported for 3 of them.")
        self.assertEqual(classify(answer, "what is burning in California?"), (False, False))

    def test_every_probed_agent_has_a_real_question(self):
        for name, (script, prompt) in AGENTS.items():
            with self.subTest(agent=name):
                self.assertTrue((REPO_ROOT / script).is_file(), f"{script} is missing")
                self.assertTrue(prompt.strip(), f"{name} has no probe question")
                self.assertFalse(classify(prompt, prompt)[0],
                                 f"{name}'s probe question is itself a help request")


if __name__ == "__main__":
    unittest.main(verbosity=2)
