"""Tsunami bulletins, from the two US warning centres' own CAP alerts (keyless).

Verified live on 2026-09-23:

  https://www.tsunami.gov/events/xml/PAAQCAP.xml  -> NTWC, 'Tsunami Information', Minor, expires
     2026-09-17T15:23:45-00:00, area '45 miles NE of Amukta Pass, Alaska', magnitude 6.3 Mwp
  https://www.tsunami.gov/events/xml/PHEBCAP.xml  -> PTWC, 'Tsunami Information', Minor, expires
     2026-09-18T14:25:30-00:00, area 'VICINITY OF PUERTO RICO'

Three things the payloads make plain and this reader keeps. There are exactly **two** live CAP
files - PAAQ (Palmer, AK, Pacific and the US west coast) and PHEB (Honolulu, HI) - and the other
names people expect (PTWCAtom.xml, WCATWCAtom.xml, PAAQ.json) answer 404, so only the real ones
are read. A CAP file **keeps its last alert after it expires**, so "is there a warning?" can only
be answered by comparing `expires` with now - reading the file's existence as a live warning would
cry wolf forever. And `expires` is the field that decides it; `severity` and `certainty` describe
the event, not whether it is still in force.
"""

from __future__ import annotations

import datetime
import os
import re
import time
import xml.etree.ElementTree as ET
from urllib import request
from urllib.error import HTTPError

DATASET = "tsunami.gov CAP alerts (NTWC Palmer + PTWC Honolulu)"

DEFAULT_USER_AGENT = "awesome-acps-tsunami/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: The two centre codes whose CAP files actually exist.
CENTRES = {"PAAQ": "NWS National Tsunami Warning Center (Palmer, Alaska)",
           "PHEB": "NWS Pacific Tsunami Warning Center (Honolulu, Hawaii)"}

CAP_URL = "https://www.tsunami.gov/events/xml/{code}CAP.xml"

CAP = "{urn:oasis:names:tc:emergency:cap:1.2}"

#: The categories a bulletin can carry, worst first. CAP calls them all "events".
EVENTS = {"Tsunami Warning": "warning", "Tsunami Advisory": "advisory",
          "Tsunami Watch": "watch", "Tsunami Information": "information",
          "Tsunami Information Statement": "information"}

#: Parameters that carry the earthquake itself.
PARAMETERS = {"EventPreliminaryMagnitude": "magnitude", "EventPreliminaryMagnitudeType": "scale",
              "EventOriginTime": "origin_time", "EventDepth": "depth",
              "EventLocationName": "location", "EventLatLon": "coordinates"}


class TsunamiError(RuntimeError):
    """The bulletin feeds could not be read."""


def parse_time(text: str | None) -> datetime.datetime | None:
    """A CAP timestamp as an aware UTC datetime, or None."""
    raw = str(text or "").strip()
    if not raw:
        return None
    for pattern in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            parsed = datetime.datetime.strptime(raw, pattern)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.astimezone(datetime.timezone.utc)
    return None


def hours_between(later: datetime.datetime, earlier: datetime.datetime) -> float:
    return (later - earlier).total_seconds() / 3600.0


def kind_of(event: str) -> str:
    """'Tsunami Warning' -> 'warning'; anything unfamiliar keeps its own words."""
    text = str(event or "").strip()
    for name, kind in EVENTS.items():
        if name.lower() in text.lower():
            return kind
    return text.lower() or "bulletin"


class TsunamiData:
    """The newest bulletin from each centre, and whether it is still in force."""

    def __init__(self, fetch=None, cache_ttl: float = 300.0, timeout: float = 30.0,
                 user_agent: str | None = None, now=None) -> None:
        self.cache_ttl = float(os.environ.get("TSUNAMI_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("TSUNAMI_HTTP_TIMEOUT", timeout))
        self.user_agent = (user_agent or os.environ.get("TSUNAMI_USER_AGENT")
                           or DEFAULT_USER_AGENT)
        self._fetch = fetch or self._http_text
        self._cache: dict[str, tuple[float, object]] = {}
        self._now = now

    # -- transport ---------------------------------------------------------

    def _http_text(self, url: str, params: dict | None = None) -> str:
        req = request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "*/*"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except HTTPError as exc:
            raise TsunamiError(f"request failed: {exc}") from exc
        except Exception as exc:  # urllib raises many other types too
            raise TsunamiError(f"request failed: {exc}") from exc

    def now(self) -> datetime.datetime:
        """The clock the caller gave, or UTC now - injected in tests."""
        return self._now or datetime.datetime.now(datetime.timezone.utc)

    def _text(self, code: str, ttl: float | None = None) -> str:
        url = CAP_URL.format(code=code)
        stamp = time.time()
        hit = self._cache.get(url)
        if hit and hit[0] > stamp:
            return hit[1]
        text = self._fetch(url, {})
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        if not isinstance(text, str) or "<alert" not in text:
            raise TsunamiError(f"the {code} bulletin feed returned no CAP alert")
        self._cache[url] = (stamp + (ttl if ttl is not None else self.cache_ttl), text)
        return text

    # -- parsing -----------------------------------------------------------

    def _alert(self, code: str, text: str) -> dict:
        """One CAP alert as a flat record, with the earthquake's own parameters pulled out."""
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise TsunamiError(f"the {code} bulletin is not valid XML: {exc}") from exc
        info = root.find(CAP + "info")
        if info is None:
            raise TsunamiError(f"the {code} bulletin carries no alert information")
        parameters = {}
        for parameter in info.findall(CAP + "parameter"):
            name = (parameter.findtext(CAP + "valueName") or "").strip()
            value = (parameter.findtext(CAP + "value") or "").strip()
            if name in PARAMETERS:
                parameters[PARAMETERS[name]] = value
        areas = [str(area.findtext(CAP + "areaDesc") or "").strip()
                 for area in info.findall(CAP + "area")]
        circles = [str(circle.text or "").strip()
                   for area in info.findall(CAP + "area")
                   for circle in area.findall(CAP + "circle")]
        sent = parse_time(root.findtext(CAP + "sent"))
        expires = parse_time(info.findtext(CAP + "expires"))
        now = self.now()
        active = bool(expires and expires > now)
        return {
            "centre": code,
            "centre_name": CENTRES.get(code, code),
            "identifier": root.findtext(CAP + "identifier"),
            "sender": root.findtext(CAP + "sender"),
            "source": root.findtext(CAP + "source") or code,
            "sent": sent.isoformat() if sent else None,
            "expires": expires.isoformat() if expires else None,
            "event": str(info.findtext(CAP + "event") or "").strip(),
            "kind": kind_of(info.findtext(CAP + "event") or ""),
            "severity": str(info.findtext(CAP + "severity") or "").strip(),
            "urgency": str(info.findtext(CAP + "urgency") or "").strip(),
            "certainty": str(info.findtext(CAP + "certainty") or "").strip(),
            "headline": str(info.findtext(CAP + "headline") or "").strip(),
            "description": re.sub(r"\s{2,}", " ", str(info.findtext(CAP + "description") or "")).strip(),
            "instruction": re.sub(r"\s{2,}", " ", str(info.findtext(CAP + "instruction") or "")).strip(),
            "areas": [area for area in areas if area],
            "circles": circles,
            "product": str(info.findtext(CAP + "web") or "").strip(),
            "parameters": parameters,
            "active": active,
            #: How long ago it was in force - the number that decides "is this still on?".
            "expired_hours_ago": (round(hours_between(now, expires), 1)
                                  if expires and not active else None),
            "sent_hours_ago": (round(hours_between(now, sent), 1) if sent else None),
            "has_end_time": expires is not None,
        }

    # -- reads -------------------------------------------------------------

    def alerts(self) -> dict:
        """Every centre's newest bulletin, live, with what is actually still in force."""
        records, skipped = [], []
        for code in CENTRES:
            try:
                records.append(self._alert(code, self._text(code)))
            except TsunamiError as exc:
                skipped.append({"centre": code, "reason": str(exc)})
        if not records:
            raise TsunamiError("no tsunami centre answered: "
                               + "; ".join(f"{row['centre']}: {row['reason']}" for row in skipped))
        records.sort(key=lambda row: row["sent"] or "", reverse=True)
        active = [row for row in records if row["active"]]
        return {"alerts": records, "active": active, "now": self.now().isoformat(),
                "unavailable": skipped, "newest": records[0], "source": DATASET}

    def place(self, name: str) -> dict:
        """Do any of the newest bulletins name this place?"""
        text = str(name or "").strip().lower()
        if not text:
            raise ValueError("tell me which place, for example Hawaii")
        found = self.alerts()
        matches = []
        for row in found["alerts"]:
            #: Areas first, but the prose names regions the areas do not ("AK, BC, and US West
            #: Coast"), so the whole bulletin is searched before saying a place is not named.
            areas = " ".join(row["areas"] + [row["parameters"].get("location", "")]).lower()
            everything = " ".join([areas, row["headline"], row["description"],
                                   row["instruction"]]).lower()
            if text in areas:
                matches.append(dict(row, matched_in="area"))
            elif text in everything:
                matches.append(dict(row, matched_in="bulletin text"))
        return dict(found, matches=matches, place=str(name).strip())
