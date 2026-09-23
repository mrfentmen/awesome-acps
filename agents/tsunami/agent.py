"""Tsunami - is a warning in force, and what the last bulletin said.

The one thing that makes this agent worth having is that it will tell you **no**. Both US warning
centres keep their last CAP alert on the web after it expires, so anything that reports "warning
found" from the file's existence would be wrong every calm day. This reads the expiry and says
when nothing is in force, and how long ago the last bulletin lapsed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from data import CENTRES, DATASET, TsunamiData, TsunamiError  # noqa: E402

HELP = (
    "I read both US tsunami warning centres' live CAP bulletins (keyless): NTWC in Palmer, Alaska\n"
    "and PTWC in Honolulu. Ask me:\n"
    "  - is there a tsunami warning right now?\n"
    "  - any tsunami advisory for the west coast?\n"
    "  - what was the last tsunami bulletin?\n"
    "  - is there a tsunami warning for Hawaii?\n"
    "The bulletins stay on the web after they expire, so I check each one's expiry and tell you\n"
    "plainly when nothing is in force - and how long ago the last one lapsed. That is why this\n"
    "agent can answer 'no', which a feed-shaped answer cannot."
)

PERMISSION_KEY = "tsunami-read-warning-centre-bulletins"

SKILLS = ("tsunami-status", "tsunami-place", "help")

_PLACE_RE = re.compile(r"\b(?:for|near|at|around|affect(?:ing)?)\s+([A-Za-z][A-Za-z .'\-]{2,40})",
                       re.IGNORECASE)
_STATUS_WORDS = re.compile(r"\b(warning|warning?s?|advisory|advisories|watch|watches|bulletin|"
                           r"info|information|statement|active|right now|happening|current|"
                           r"latest|last|any)\b", re.IGNORECASE)
_PLACES = ("hawaii", "alaska", "california", "oregon", "washington", "puerto rico", "guam",
           "american samoa", "japan", "chile", "indonesia", "new zealand", "mexico", "canada",
           "british columbia", "philippines", "vancouver island")


def place_from_text(text: str) -> str | None:
    """A place the question names, or None. Only the basin's own places, so a stray word is not
    treated as a location."""
    lowered = str(text or "").lower()
    for place in sorted(_PLACES, key=len, reverse=True):
        if place in lowered:
            return place
    match = _PLACE_RE.search(str(text or ""))
    if match:
        candidate = re.sub(r"^(?:the|a|an)\s+", "", match.group(1).strip(" ."), flags=re.I)
        if len(candidate.split()) <= 3 and candidate.lower() not in ("me", "us", "any"):
            return candidate
    return None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    stripped = str(text or "").strip()
    if not stripped or re.search(r"\b(help|what can you do|commands?)\b", stripped, re.IGNORECASE):
        return "help", {}
    if not _STATUS_WORDS.search(stripped) and "tsunami" not in stripped.lower():
        return "help", {}
    place = place_from_text(stripped)
    if place:
        return "tsunami-place", {"place": place}
    return "tsunami-status", {}


def render_status(found: dict) -> str:
    """The verdict first, then the bulletins behind it."""
    if found["active"]:
        lines = [f"{len(found['active'])} tsunami bulletin(s) are in force right now:"]
        for alert in found["active"]:
            lines.append(f"  {alert['event']} - {alert['centre_name']}")
            lines.append(f"    area: {'; '.join(alert['areas']) or 'not named'}")
            lines.append(f"    severity {alert['severity']}, urgency {alert['urgency']}, "
                         f"certainty {alert['certainty']}")
            lines.append(f"    expires {alert['expires']}")
    else:
        lines = ["No tsunami bulletin is in force right now."]
    newest = found["newest"]
    lines.append(f"  newest bulletin: {newest['event']} from {newest['centre_name']}")
    lines.append(f"    sent {newest['sent']}"
                 + (f" ({newest['sent_hours_ago']:g} h ago)"
                    if newest["sent_hours_ago"] is not None else ""))
    if newest["has_end_time"]:
        if newest["active"]:
            lines.append(f"    in force until {newest['expires']}")
        else:
            lines.append(f"    expired {newest['expires']}"
                         + (f" - {newest['expired_hours_ago']:g} h ago"
                            if newest["expired_hours_ago"] is not None else ""))
    else:
        lines.append("    the bulletin carries no expiry, so I cannot say whether it is still in "
                     "force")
    lines.append(f"    area: {'; '.join(newest['areas']) or 'not named'}")
    for alert in found["alerts"][1:]:
        lines.append(f"  other centre - {alert['centre_name']}: {alert['event']}, "
                     f"sent {alert['sent']}"
                     + (f", expired {alert['expires']}" if alert["has_end_time"]
                        and not alert["active"] else ""))
    if found["unavailable"]:
        lines.append(f"  (no answer from: "
                     + ", ".join(row["centre"] for row in found["unavailable"]) + ")")
    return "\n".join(lines)


def render_place(found: dict) -> str:
    """What the bulletins say about one named place."""
    #: Places are matched in lower case but should not be *shown* in it.
    place = found["place"].title()
    if not found["matches"]:
        lines = [f"No current or last bulletin names {place}."]
        newest = found["newest"]
        lines.append(f"  The newest bulletin overall: {newest['event']}, area "
                     f"{'; '.join(newest['areas']) or 'not named'}, sent {newest['sent']}")
        lines.append("  A bulletin only mentions the places it affects, so a place that is not "
                     "named is not under any bulletin.")
        return "\n".join(lines)
    lines = [f"{place} - {len(found['matches'])} bulletin(s) name it:"]
    for alert in found["matches"]:
        lines.append(f"  {alert['event']} from {alert['centre_name']}"
                     + (" (IN FORCE)" if alert["active"] else " (expired)"))
        lines.append(f"    sent {alert['sent']}"
                     + (f", expired {alert['expires']}" if alert["has_end_time"] else ""))
        lines.append(f"    area: {'; '.join(alert['areas']) or 'not named'}"
                     + (f" (named in the {alert['matched_in']})"
                        if alert.get("matched_in") == "bulletin text" else ""))
    return "\n".join(lines)


def render_bulletin(alert: dict) -> str:
    """One bulletin in full, earthquake parameters included."""
    lines = [f"{alert['event']} - {alert['centre_name']}"]
    lines.append("  status: " + ("IN FORCE" if alert["active"] else
                                 ("expired" if alert["has_end_time"] else "no expiry given")))
    lines.append(f"  sent {alert['sent']}"
                 + (f", expires {alert['expires']}" if alert["has_end_time"] else ""))
    lines.append(f"  severity {alert['severity']}, urgency {alert['urgency']}, "
                 f"certainty {alert['certainty']}")
    lines.append(f"  area: {'; '.join(alert['areas']) or 'not named'}")
    parameters = alert["parameters"]
    if parameters:
        bits = []
        if parameters.get("magnitude"):
            bits.append(f"magnitude {parameters['magnitude']} "
                        f"{parameters.get('scale') or ''}".strip())
        if parameters.get("depth"):
            bits.append(f"depth {parameters['depth']}")
        if parameters.get("origin_time"):
            bits.append(f"origin {parameters['origin_time']}")
        if parameters.get("coordinates"):
            bits.append(f"at {parameters['coordinates'].split()[0]}")
        lines.append("  earthquake: " + ", ".join(bits))
    if alert["headline"]:
        lines.append(f"  {alert['headline']}")
    if alert["description"]:
        lines.append(f"  {alert['description'][:600]}")
    if alert["instruction"]:
        lines.append(f"  instruction: {alert['instruction'][:400]}")
    if alert["product"]:
        lines.append(f"  bulletin: {alert['product']}")
    return "\n".join(lines)


class TsunamiAgent(AcpAgent):
    name = "tsunami"
    title = "Tsunami - whether a bulletin is in force, and what the last one said"
    version = "1.0.0"

    def __init__(self, connection=None, data: TsunamiData | None = None) -> None:
        super().__init__(connection)
        self.data = data or TsunamiData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Tsunami session opened. " + HELP.splitlines()[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read both warning centres' CAP bulletins", "medium"),
            ("Check each bulletin's expiry before calling anything active", "high"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("I check the expiry on every bulletin, so 'nothing is in force' is a "
                        "real answer here.")
            return STOP_END_TURN

        tool = f"call_{skill.replace('-', '_')}"
        ctx.tool_call(tool, "Read the warning centres' bulletins", kind="fetch", name=skill,
                      raw_input=dict(params))
        if not ctx.ask_permission(tool, "Allow Tsunami to read the US tsunami warning centres' "
                                        "public bulletins?", remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the warning centres' bulletins first.")
            return STOP_REFUSAL
        ctx.tool_call_update(tool, status="in_progress")

        try:
            if skill == "tsunami-place":
                found = self.data.place(params["place"])
                body = render_place(found)
                summary = (f"{params['place']}: {len(found['matches'])} bulletin(s) name it, "
                           f"{len([m for m in found['matches'] if m['active']])} in force")
            else:
                found = self.data.alerts()
                if found["active"]:
                    body = (render_status(found) + "\n\n"
                            + render_bulletin(found["active"][0]))
                else:
                    body = render_status(found) + "\n\n" + render_bulletin(found["newest"])
                summary = (f"{len(found['active'])} bulletin(s) in force; newest is "
                           f"{found['newest']['event']}")
        except (TsunamiError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the bulletins: {exc}")
            return STOP_END_TURN

        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(summary))
        ctx.stream_text(body + "\n")
        ctx.message(f"\nSource: {DATASET}, read live. These are the two US centres only "
                    "(Palmer AK and Honolulu HI); other basins issue their own bulletins, which "
                    "this does not read. A bulletin left on the web after its expiry is still "
                    "shown here, marked expired, because it is the last thing that was said.")
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        return None  # one read, one answer


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        TsunamiAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
