"""Domains - who owns a name, when it expires, and whether it is taken.

Every ACP agent on the vendors' list writes code. This one answers what WHOIS never answered
cleanly: when does this domain expire, who is the registrar, is any registration behind this
name at all, and who holds this IP block. It reads RDAP, the JSON protocol that replaced
WHOIS, keyless, and it is the same shape for every TLD.

Two things it refuses to do:

  * It will not guess. A 404 from the registry is reported as 'no registration found', which
    is what the registry actually said - not 'available', which only a registrar can promise.
  * It will not invent a registrar. Plenty of RDAP records omit the registrar entity (or
    redact it after GDPR), and then the answer says so instead of printing a blank field.
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
    DomainsError,
    RdapData,
    days_until,
    display_date,
    normalise_domain,
)

HELP = (
    "I read RDAP, the registration protocol that replaced WHOIS (keyless). Ask me:\n"
    "  - when does github.com expire?\n"
    "  - who is the registrar for example.org?\n"
    "  - is popsnax.com taken?\n"
    "  - is this-domain-is-free-12345.net registered?\n"
    "  - who owns 8.8.8.8?\n"
    "A 404 from a registry means no registration was found; I will not call that 'available', "
    "because only a registrar can promise that."
)

PERMISSION_KEY = "domains-read-public-rdap"

SKILLS = ("domain-info", "domain-available", "ip-info", "help")

_DOMAIN_RE = re.compile(r"\b((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})\b", re.IGNORECASE)
_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
_AVAILABLE_WORDS = re.compile(r"\b(available|taken|free|registered|unregistered|for sale|"
                              r"can i (?:buy|register|get)|is it taken|is .* taken|still open)\b",
                              re.IGNORECASE)
_INFO_WORDS = re.compile(r"\b(expire|expires|expiry|expiration|registrar|registered by|who owns|"
                         r"whois|who is|created|registration|renew|renewal|nameservers?|dns|"
                         r"status|age|old)\b", re.IGNORECASE)
_IP_WORDS = re.compile(r"\b(ip|address|network|block|cidr|whose)\b", re.IGNORECASE)


def domain_from_text(text: str) -> str | None:
    """A domain name written in a question, or None."""
    for match in _DOMAIN_RE.finditer(str(text or "")):
        candidate = match.group(1)
        # Skip things that look like file names or version numbers rather than domains.
        if candidate.lower().endswith((".md", ".py", ".js", ".json", ".txt", ".html", ".csv")):
            continue
        try:
            return normalise_domain(candidate)
        except ValueError:
            continue
    return None


def ip_from_text(text: str) -> str | None:
    """An IPv4 address written in a question, or None."""
    match = _IP_RE.search(str(text or ""))
    if not match:
        return None
    octets = match.group(1).split(".")
    if any(int(part) > 255 for part in octets):
        return None
    return match.group(1)


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    stripped = str(text or "").strip()
    if not stripped or re.search(r"\b(help|what can you do|commands?)\b", stripped, re.IGNORECASE):
        return "help", {}

    domain = domain_from_text(stripped)
    address = ip_from_text(stripped)

    if address and (_IP_WORDS.search(stripped) or not domain):
        return "ip-info", {"address": address}
    if domain:
        if _AVAILABLE_WORDS.search(stripped) and not _INFO_WORDS.search(stripped):
            return "domain-available", {"domain": domain}
        if _AVAILABLE_WORDS.search(stripped) and re.search(r"\b(taken|available|registered|free)\b",
                                                          stripped, re.IGNORECASE):
            return "domain-available", {"domain": domain}
        return "domain-info", {"domain": domain}
    return "help", {}


class DomainsAgent(AcpAgent):
    name = "domains"
    title = "Domains - RDAP registration, expiry and IP owners"
    version = "1.0.0"

    def __init__(self, connection=None, data: RdapData | None = None) -> None:
        super().__init__(connection)
        self.data = data or RdapData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Domains session opened. " + HELP.splitlines()[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Ask the registry over RDAP", "medium"),
            ("Report the fields the registry actually published", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("I report what RDAP published, including the fields it withheld, and I "
                        "never turn 'no record' into 'available'.")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read RDAP ({skill.replace('-', ' ')})", kind="fetch", name=skill,
                      raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow Domains to query public RDAP servers?",
                                  remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to query the public RDAP servers first.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (DomainsError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read RDAP: {exc}")
            return STOP_END_TURN
        ctx.check_cancelled()
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(artifact["summary"]))
        ctx.stream_text(answer)
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        return None  # every skill is one request

    # -- skills ------------------------------------------------------------

    def _run_skill(self, skill: str, params: dict) -> tuple[str, dict]:
        if skill == "domain-info":
            return self._info(params)
        if skill == "domain-available":
            return self._available(params)
        if skill == "ip-info":
            return self._ip(params)
        return HELP, {"summary": "help", "dataset": None}

    def _info(self, params: dict) -> tuple[str, dict]:
        record = self.data.domain(params["domain"])
        artifact = {"summary": f"{DATASET}: {record['domain']} "
                               f"{'registered' if record['registered'] else 'no record'}",
                    "dataset": DATASET, **record}
        if not record["registered"]:
            return self._unregistered(record, asked_for_availability=False)
        left = days_until(record["expiration"])
        lines = [f"{record['domain']} is registered (a {record['tld']} domain):"]
        if record.get("registrar"):
            lines.append(f"  - registrar: {record['registrar']}")
        else:
            lines.append("  - registrar: not published in this registry's RDAP record (some "
                         "registries redact it)")
        if record.get("registration"):
            lines.append(f"  - created: {display_date(record['registration'])}")
        if record.get("expiration"):
            when = display_date(record["expiration"])
            if left is None:
                lines.append(f"  - expires: {when}")
            elif left < 0:
                lines.append(f"  - expired: {when} ({abs(left)} days ago)")
            else:
                lines.append(f"  - expires: {when} (in {left} days)")
        else:
            lines.append("  - expires: not published in this record")
        last_changed = (record.get("events") or {}).get("last changed")
        if last_changed:
            lines.append(f"  - last changed: {display_date(last_changed)}")
        if record.get("status"):
            lines.append(f"  - status: {', '.join(record['status'][:6])}")
        if record.get("nameservers"):
            lines.append(f"  - nameservers: {', '.join(record['nameservers'][:6])}")
        if record.get("dnssec") is not None:
            lines.append(f"  - DNSSEC: {'signed' if record['dnssec'] else 'not signed'}")
        if record.get("handle"):
            lines.append(f"  - registry id: {record['handle']}")
        lines.append("")
        lines.append(f"Source: {DATASET}, read live. Registration dates are the registry's own; "
                     "I do not estimate or round them.")
        return "\n".join(lines), artifact

    def _available(self, params: dict) -> tuple[str, dict]:
        record = self.data.domain(params["domain"])
        artifact = {"summary": f"{DATASET}: {record['domain']} "
                               f"{'has a registration' if record['registered'] else 'no record found'}",
                    "dataset": DATASET, **record}
        if record["registered"]:
            left = days_until(record["expiration"])
            when = display_date(record["expiration"]) if record.get("expiration") else "unknown"
            note = f", expiring {when}" + (f" in {left} days" if left is not None and left >= 0 else "")
            lines = [f"{record['domain']} is taken - the registry has a registration for it{note}.",
                     f"  - registrar: {record.get('registrar') or 'not published'}",
                     "",
                     "Ask me when it expires for the full record.",
                     f"Source: {DATASET}, read live."]
            return "\n".join(lines), artifact
        return self._unregistered(record, asked_for_availability=True)

    @staticmethod
    def _unregistered(record: dict, asked_for_availability: bool) -> tuple[str, dict]:
        lines = [
            f"{record['domain']} has no registration record: the {record['tld']} registry answered "
            "that it holds nothing for this name.",
            "",
            "What that does and does not mean:",
            "  - it does mean no registration exists at this moment, which is the state a "
            "registrar looks for when you want to buy a name",
            "  - it does NOT mean the name is buyable: registries reserve and block names that "
            "never appear in RDAP, and only a registrar can confirm a purchase",
            "  - it can change at any moment: RDAP is read live, and a name can be registered "
            "one second after this answer",
            "",
            f"Source: {DATASET}, read live. Check with the registrar before relying on it.",
        ]
        if not asked_for_availability:
            lines.insert(1, "That is why I am describing it rather than calling it available - "
                            "ask me 'is it taken?' and I will answer that question directly.")
        return "\n".join(lines), {"summary": f"{DATASET}: {record['domain']} no registration record",
                                 "dataset": DATASET, **record}

    def _ip(self, params: dict) -> tuple[str, dict]:
        record = self.data.network(params["address"])
        artifact = {"summary": f"{DATASET}: {record['address']} "
                               f"{record.get('name') or record.get('handle') or 'no record'}",
                    "dataset": DATASET, **record}
        if not record["found"]:
            return (f"RDAP has no record for {record['address']}. That happens when the block has "
                    "no registration object registered with its regional internet registry.",
                    artifact)
        lines = [f"{record['address']} - {record.get('name') or 'unnamed block'}"
                 + (f" ({record['country']})" if record.get("country") else "") + ":"]
        if record.get("start") and record.get("end"):
            lines.append(f"  - block: {record['start']} - {record['end']}")
        if record.get("cidr"):
            lines.append("  - prefix lengths: /" + ", /".join(record["cidr"]))
        if record.get("type"):
            lines.append(f"  - type: {record['type']}")
        if record.get("entities"):
            lines.append(f"  - holder: {', '.join(record['entities'][:4])}")
        registered = (record.get("events") or {}).get("registration")
        if registered:
            lines.append(f"  - registered: {display_date(registered)}")
        if record.get("handle"):
            lines.append(f"  - registry handle: {record['handle']}")
        lines.append("")
        lines.append(f"Source: {DATASET}, read live. Registration data for IP blocks is held by "
                     "the regional internet registry that owns the range.")
        return "\n".join(lines), artifact


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        DomainsAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
