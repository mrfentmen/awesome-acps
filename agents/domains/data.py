"""Read-only RDAP reader for the domains agent.

RDAP is the registration data access protocol that replaced WHOIS. It is keyless, it returns
JSON, and it answers the same question for every TLD, so this reader asks one URL and gets
consistent fields. Verified live on 2026-09-23:

  https://rdap.org/domain/github.com    registration, expiration, status, nameservers, DNSSEC
  https://rdap.org/ip/8.8.8.8           the network block and who holds it

Two things shape this reader:

  1. **404 is an answer.** A registry that has no record replies 404, and that is how you ask
     'is this domain taken?' - so a 404 becomes {registered: False}, not an error.
  2. **The bootstrap is not always the answer.** rdap.org redirects to each TLD's own server;
     when it is unavailable this reader falls back to the registry's own endpoint for the TLDs
     it knows (.com, .net, .org).
"""

from __future__ import annotations

import datetime
import json
import os
import time
from urllib import error, parse, request

BOOTSTRAP = "https://rdap.org"
DATASET = "RDAP (rdap.org, the registration data access protocol that replaced WHOIS)"

#: Registry endpoints used only when the bootstrap will not answer.
FALLBACKS = {
    ".com": "https://rdap.verisign.com/com/v1/domain/",
    ".net": "https://rdap.verisign.com/net/v1/domain/",
    ".org": "https://rdap.publicinterestregistry.org/rdap/domain/",
}

DEFAULT_USER_AGENT = "awesome-acps-domains/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: How long to wait before the single retry after a 429/503 from the RDAP bootstrap.
RETRY_PAUSE_S = 1.0

#: Event actions, in the order they matter to a person.
EVENT_ORDER = ("registration", "expiration", "last changed", "last update of RDAP database",
               "transfer", "reregistration", "last purge of RDAP database")


class DomainsError(RuntimeError):
    """An RDAP service could not be read."""


class RdapData:
    """Domain and IP registrations, from any RDAP server."""

    def __init__(self, fetch=None, timeout: float = 20.0, user_agent: str | None = None) -> None:
        self.timeout = float(os.environ.get("DOMAINS_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("DOMAINS_USER_AGENT") or DEFAULT_USER_AGENT
        #: fetch(url) -> (status, payload). status 0 means the request itself failed.
        self._fetch = fetch or self._http_json

    # -- transport ---------------------------------------------------------

    def _http_json(self, url: str):
        """(status, payload) for one RDAP URL. A 404 is an answer; a 429 is worth one pause.

        The public bootstrap rate-limits a burst of lookups (hit live on 2026-09-23 asking for
        four domains and then two IPs back to back), so a 429 is retried once after a short
        pause before it becomes an error.
        """
        req = request.Request(url, headers={"User-Agent": self.user_agent,
                                           "Accept": "application/rdap+json, application/json"})
        for attempt in range(2):
            try:
                with request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
            except error.HTTPError as exc:
                if exc.code == 404:
                    return 404, None
                if exc.code in (429, 503) and attempt == 0:
                    time.sleep(RETRY_PAUSE_S)
                    continue
                if exc.code == 429:
                    raise DomainsError("the RDAP bootstrap is rate limiting this address right "
                                       "now; ask again in a moment") from exc
                raise DomainsError(f"RDAP answered HTTP {exc.code} for {url}") from exc
            except Exception as exc:  # urllib raises many types; callers see one error type
                raise DomainsError(f"RDAP request failed: {exc}") from exc
        raise DomainsError(f"RDAP did not answer for {url}")

    def _get(self, path: str, fallback_base: str | None = None, name: str = ""):
        """(status, payload) from the bootstrap, then from the registry's own server."""
        attempts = [f"{BOOTSTRAP}/{path}"]
        if fallback_base:
            attempts.append(f"{fallback_base}{parse.quote(name)}")
        last_error: Exception | None = None
        for url in attempts:
            try:
                status, payload = self._fetch(url)
            except Exception as exc:
                last_error = exc
                continue
            if status == 404:
                return 404, None
            if isinstance(payload, dict):
                return status, payload
            last_error = DomainsError(f"RDAP returned an unexpected payload from {url}")
        raise DomainsError(f"RDAP did not answer: {last_error}")

    # -- parsing -----------------------------------------------------------

    @staticmethod
    def _events(payload: dict) -> dict:
        events = {}
        for event in payload.get("events") or []:
            action = (event.get("eventAction") or "").strip().lower()
            date = (event.get("eventDate") or "").strip()
            if action and date:
                events[action] = date
        return events

    @staticmethod
    def _registrar(payload: dict) -> str | None:
        """The registrar's name out of the entity vCard, or None when RDAP omits it."""
        for entity in payload.get("entities") or []:
            roles = [str(role).lower() for role in entity.get("roles") or []]
            if "registrar" not in roles:
                continue
            card = entity.get("vcardArray") or []
            if len(card) < 2 or not isinstance(card[1], list):
                continue
            for field in card[1]:
                if isinstance(field, list) and field and field[0] == "fn" and len(field) > 3:
                    value = field[3]
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        return None

    @staticmethod
    def _entity_names(payload: dict) -> list[str]:
        """Every named organisation in the record, registrant first."""
        names = []
        for entity in payload.get("entities") or []:
            card = entity.get("vcardArray") or []
            if len(card) < 2 or not isinstance(card[1], list):
                continue
            for field in card[1]:
                if isinstance(field, list) and field and field[0] == "fn" and len(field) > 3:
                    value = field[3]
                    if isinstance(value, str) and value.strip() and value.strip() not in names:
                        names.append(value.strip())
        return names

    # -- lookups -----------------------------------------------------------

    def domain(self, name: str) -> dict:
        """Everything RDAP publishes about a domain name."""
        domain = normalise_domain(name)
        tld = "." + domain.rsplit(".", 1)[-1]
        status, payload = self._get(f"domain/{parse.quote(domain)}", FALLBACKS.get(tld), domain)
        if status == 404 or payload is None:
            # The same keys every time, so a caller never has to guess what is missing.
            return {"dataset": DATASET, "domain": domain, "registered": False, "tld": tld,
                    "handle": None, "registrar": None, "entities": [], "events": {},
                    "status": [], "nameservers": [], "dnssec": None, "expiration": None,
                    "registration": None}
        events = self._events(payload)
        nameservers = []
        for server in payload.get("nameservers") or []:
            label = server.get("ldhName") or server.get("unicodeName")
            if label:
                nameservers.append(str(label).lower())
        secure = (payload.get("secureDNS") or {}).get("delegationSigned")
        return {
            "dataset": DATASET,
            "domain": domain,
            "registered": True,
            "tld": tld,
            "handle": payload.get("handle"),
            "registrar": self._registrar(payload),
            "entities": self._entity_names(payload),
            "events": events,
            "status": [str(item) for item in payload.get("status") or []],
            "nameservers": nameservers,
            "dnssec": bool(secure) if secure is not None else None,
            "expiration": events.get("expiration"),
            "registration": events.get("registration"),
        }

    def network(self, address: str) -> dict:
        """The IP block a literal address sits in, and who holds it."""
        literal = str(address or "").strip()
        if not literal:
            raise ValueError("give me an IP address, for example 8.8.8.8")
        status, payload = self._get(f"ip/{parse.quote(literal)}")
        if status == 404 or payload is None:
            return {"dataset": DATASET, "address": literal, "found": False, "name": None,
                    "handle": None, "country": None, "type": None, "start": None, "end": None,
                    "cidr": [], "entities": [], "events": {}, "status": []}
        events = self._events(payload)
        return {
            "dataset": DATASET,
            "address": literal,
            "found": True,
            "name": payload.get("name"),
            "handle": payload.get("handle"),
            "country": payload.get("country"),
            "type": payload.get("type"),
            "start": payload.get("startAddress"),
            "end": payload.get("endAddress"),
            "cidr": [str(block.get("length")) for block in payload.get("cidr0_cidrs") or []
                     if isinstance(block, dict)],
            "entities": self._entity_names(payload),
            "events": events,
            "status": [str(item) for item in payload.get("status") or []],
        }


def normalise_domain(name: str) -> str:
    """A domain from whatever the user typed: strips scheme, path, 'www.' and a trailing dot."""
    text = str(name or "").strip().lower()
    text = text.split("//", 1)[-1]
    text = text.split("/", 1)[0]
    text = text.split("@", 1)[-1]
    text = text.split(":", 1)[0]
    text = text.rstrip(".")
    if text.startswith("www."):
        text = text[4:]
    if not text or "." not in text or " " in text:
        raise ValueError(f"{name!r} is not a domain name, for example github.com")
    if not all(part for part in text.split(".")):
        raise ValueError(f"{name!r} is not a domain name, for example github.com")
    return text


def days_until(date_text: str | None, now: datetime.datetime | None = None) -> int | None:
    """Whole days from now until an RDAP timestamp, or None when there is no timestamp."""
    stamp = parse_date(date_text)
    if stamp is None:
        return None
    reference = now or datetime.datetime.now(datetime.timezone.utc)
    return (stamp - reference).days


def parse_date(date_text: str | None) -> datetime.datetime | None:
    """An RDAP timestamp ('2028-10-09T18:20:50Z', milliseconds and all) to a datetime."""
    if not date_text:
        return None
    text = str(date_text).strip().replace("Z", "+00:00")
    if "." in text:
        # RDAP sends whole milliseconds and sometimes microseconds; fromisoformat accepts at
        # most six digits, so the leading digits are kept and the rest of the offset with them.
        head, _, tail = text.partition(".")
        digits, index = "", 0
        while index < len(tail) and tail[index].isdigit():
            digits += tail[index]
            index += 1
        rest = tail[index:]
        digits = (digits + "000000")[:6]
        text = f"{head}.{digits}{rest}" if digits.strip("0") else f"{head}{rest}"
    try:
        stamp = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp


def display_date(date_text: str | None) -> str:
    """An RDAP timestamp as a plain date, or the original text when it cannot be parsed."""
    stamp = parse_date(date_text)
    return stamp.strftime("%Y-%m-%d") if stamp else (date_text or "unknown")
