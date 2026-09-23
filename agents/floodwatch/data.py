"""Read-only flood reader: NWS river gauges, with USGS used to find them by name.

Self-contained on purpose: both transports live here with one small cache.

  https://api.water.noaa.gov/nwps/v1/gauges/<id>          NWPS gauge: stage, flow, categories
  https://api.waterdata.usgs.gov/ogcapi/v0/collections/   USGS site search (CQL2 filter)

NWPS is the National Water Prediction Service: the river forecast centers' own gauges, with
observed stage, the forecast peak, and the published action / minor / moderate / major flood
stages for that specific gauge. Keyless, verified live on 2026-09-22 (EADM7, Mississippi
River at St. Louis: 13.08 ft observed, 16.6 ft forecast, no flooding, minor at 30 ft).

Two findings from building this, both live-checked that day:

1. NWPS's gauge list is 12,889 gauges and 13 MB, and took 44-56 seconds to come back. That is
   far too slow and impolite to fetch inside a turn, so this reader never touches it. Gauges
   are addressed by id (a 5-character LID like EADM7, or a USGS site number like 07010000),
   and a river name is turned into an id through USGS's small item search first.
2. The Open-Meteo flood API was tried and dropped: it answered 0.45 m3/s for the Mississippi
   at St. Louis and 1.4 m3/s for the Amazon at Manaus, which are not river discharges a
   person should be shown. NWPS carries the forecast centers' actual numbers, so it is used
   instead.

A published flow of -9999 means "not published here" and becomes None, never a number.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from urllib import parse, request

NWPS_URL = "https://api.water.noaa.gov/nwps/v1/gauges"
USGS_URL = "https://api.waterdata.usgs.gov/ogcapi/v0/collections/monitoring-locations/items"
DATASET = "api.water.noaa.gov/nwps/v1 (NWS river gauges) + USGS monitoring locations"

DEFAULT_USER_AGENT = "awesome-acps-floodwatch/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: -9999 is NWPS's "no value published" (it appears in the flow fields).
_NO_VALUE = -9999.0

#: NWPS LIDs are five characters (EADM7, MEMT1); USGS site numbers run 8-15 digits.
_LID_RE = re.compile(r"^[A-Za-z0-9]{5}$")
_USGS_RE = re.compile(r"^\d{8,15}$")
_ID_RE = re.compile(r"\b([A-Za-z]{2,4}\d{1,3}|\d{8,15})\b")
_NAME_RE = re.compile(r"[A-Za-z]")

#: Worst first, so "how bad is it" has an order.
CATEGORY_ORDER = ("action", "minor", "moderate", "major")

CATEGORY_LABELS = {
    "no_flooding": "no flooding",
    "action": "action stage (near flood stage)",
    "minor": "minor flooding",
    "moderate": "moderate flooding",
    "major": "major flooding",
    "low_water": "low water",
    "not_defined": "no categories published for this gauge",
}


class FloodError(RuntimeError):
    """An NWPS or USGS feed could not be read."""


def number(value):
    """NWPS numbers, with -9999 recognised as 'not published'."""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result == _NO_VALUE:
        return None
    return result


def gauge_id(value) -> str:
    """An NWPS gauge id: a 5-character LID or a USGS site number."""
    token = str(value or "").strip().upper()
    if _USGS_RE.match(token):
        return token
    if _LID_RE.match(token) and any(char.isdigit() for char in token):
        return token
    raise ValueError(f"{value!r} is not a gauge id - use a LID like EADM7 or a USGS number like 07010000")


def gauge_id_from_text(text: str) -> str | None:
    """A gauge id written in a sentence, or None."""
    for match in _ID_RE.finditer(str(text)):
        try:
            return gauge_id(match.group(1))
        except ValueError:
            continue
    return None


def river_query(text: str) -> str | None:
    """The river or town a question names, for the USGS site search, or None.

    Only the ends are trimmed of question words: the middle of a USGS site name has to
    survive intact, because the search is a substring match - dropping 'River at' from
    'Mississippi River at St. Louis' would look for a name that does not exist.
    """
    work = str(text).strip()
    # A '.' is deliberately not a stop character: it would cut 'St. Louis' down to 'St'.
    for pattern in (
        r"\b(?:what(?:'s| is)?|how high is|how full is|is|status of|level of|report for)\s+"
        r"(?:the\s+)?(.+?)(?=\s+(?:doing|right now|now|today|flooding|forecast|cresting|"
        r"rising|falling|at flood)\b|[?!,]|$)",
        r"\b(?:of|for|at|on|near)\s+(?:the\s+)?(.+?)(?=\s+(?:right now|now|today|doing|"
        r"flooding|forecast)\b|[?!,]|$)",
    ):
        match = re.search(pattern, work, re.IGNORECASE)
        if match:
            words = re.findall(r"[A-Za-z][\w.'-]*", re.sub(r"\s+", " ", match.group(1)))
            while words and words[0].lower() in _QUERY_STOPWORDS:
                words.pop(0)
            while words and words[-1].lower() in _QUERY_STOPWORDS:
                words.pop()
            if words:
                return " ".join(words[:8]).strip(" .,?!")
    return None


#: Connector words that carry no search information when matching a USGS site name.
_CONNECTOR = frozenset({"at", "near", "nr", "the", "of", "in", "on", "and", "by", "above", "below"})


def _cql_literal(text: str) -> str:
    """A value escaped for a CQL2 single-quoted string literal."""
    cleaned = re.sub(r"[%_\\]", "", str(text))
    return cleaned.replace("'", "''")


def _name_contains(word: str) -> str:
    """A CQL2 clause matching a USGS site name that contains a word, ignoring case."""
    return f"LOWER(monitoring_location_name) LIKE '%{_cql_literal(word).lower()}%'"


def _search_filters(name: str) -> list[str]:
    """CQL2 filters to try for a river name, tightest first.

    The whole phrase is exact but brittle ('Colorado River at Austin' is stored as
    'Colorado River @ 59 nr Wharton' style names). The later filters keep every word but
    join them with AND, which still requires all of the river's words to appear.
    """
    filters = [_name_contains(name)]
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in _CONNECTOR]
    for count in (len(words), 2, 1):
        if 0 < count <= len(words):
            clause = " AND ".join(_name_contains(word) for word in words[:count])
            if clause not in filters:
                filters.append(clause)
    return filters


def _match_rank(row: dict, query: str) -> tuple:
    """Sort key: readable gauges first, then the closest and shortest name match."""
    name = (row.get("name") or "").lower()
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in _CONNECTOR]
    hits = -sum(1 for word in words if word in name)
    return (not row.get("usable"), hits, len(name), name)


#: Words that never belong to a river or town name. 'river' and 'creek' are NOT here: they are
#: part of most USGS site names ('Willamette River'), and dropping them makes a weaker search.
_QUERY_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "how", "what", "which", "be", "it", "there",
    "gauge", "stage", "level", "flood", "flooding", "water",
    "right", "now", "today", "doing", "reading", "status", "report", "show", "tell", "me",
    "please", "any", "near", "at", "in", "on", "of", "for", "and",
})


class FloodData:
    """NWPS gauge status and USGS site lookup, with caching and validation."""

    def __init__(self, fetch=None, cache_ttl: float = 300.0, timeout: float = 25.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("FLOODWATCH_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("FLOODWATCH_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("FLOODWATCH_USER_AGENT") or DEFAULT_USER_AGENT
        self._fetch = fetch or self._http_get
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    # -- transport ---------------------------------------------------------

    def _http_get(self, url: str, params: dict):
        full = f"{url}?{parse.urlencode(params)}" if params else url
        req = request.Request(full, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # urllib raises many types; callers see one error type
            raise FloodError(f"water feed request failed: {exc}") from exc

    def _cached(self, url: str, params: dict, ttl: float | None = None):
        key = f"{url}:{json.dumps({k: str(v) for k, v in sorted(params.items())})}"
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        payload = self._fetch(url, params)
        with self._lock:
            self._cache[key] = (now + (ttl if ttl is not None else self.cache_ttl), payload)
        return payload

    # -- validation --------------------------------------------------------

    @staticmethod
    def check_river(value) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(text) < 3 or not _NAME_RE.search(text):
            raise ValueError("a river or town name is required, for example 'Mississippi River at St. Louis'")
        if len(text) > 80:
            raise ValueError("that name is too long for a river search")
        return text

    # -- reads -------------------------------------------------------------

    def find_gauges(self, river, limit: int = 5) -> list[dict]:
        """USGS sites whose name contains the words given, best-looking first.

        USGS stores names in upper case ('COLORADO RIVER AT AUSTIN, TX'), so the filter has
        to lower-case both sides. Names also spell connectors differently from people
        ('at' vs '@' vs 'NR'), so the search widens step by step until it finds gauges NWPS
        can actually forecast, then ranks what it found.
        """
        name = self.check_river(river)
        wanted = max(1, min(int(limit), 50))
        found: list[dict] = []
        seen: set[str] = set()
        for cql in _search_filters(name):
            payload = self._cached(USGS_URL, {
                "f": "json",
                "filter": cql,
                "limit": str(max(wanted * 6, 30)),
            }, ttl=self.cache_ttl)
            features = payload.get("features") if isinstance(payload, dict) else None
            if features is None:
                raise FloodError("USGS returned an unexpected payload (no features)")
            for feature in features:
                props = feature.get("properties") or {}
                site = str(props.get("monitoring_location_number") or "")
                if site in seen:
                    continue
                seen.add(site)
                found.append({
                    "site": site,
                    "name": props.get("monitoring_location_name"),
                    "state": props.get("state_name"),
                    "county": props.get("county_name"),
                    # Only 8-digit USGS numbers are stream gauges NWPS forecasts; the long
                    # lat-lon style site numbers are wells and springs with no forecast point.
                    "usable": bool(re.match(r"^\d{8}$", site)),
                })
            # A tighter name match is a better answer than a looser one, so stop as soon as
            # the sites found can be read from NWPS.
            if any(row["usable"] for row in found):
                break
        found.sort(key=lambda row: _match_rank(row, name))
        return found[:wanted]

    def gauge(self, gauge: str) -> dict:
        """One NWPS gauge: observed, forecast, and the published flood stages."""
        wanted = gauge_id(gauge)
        try:
            payload = self._cached(f"{NWPS_URL}/{wanted}", {}, ttl=self.cache_ttl)
        except FloodError as exc:
            if "404" in str(exc):
                raise ValueError(f"NWPS has no gauge with the id {wanted!r}") from exc
            raise
        if not isinstance(payload, dict) or not payload.get("name"):
            raise FloodError(f"NWPS returned an unexpected payload for gauge {wanted!r}")
        return self._normalise(payload)

    def flood_status(self, target) -> dict:
        """A gauge report from an id, or from a river name resolved through USGS first."""
        text = str(target or "").strip()
        if not text:
            raise ValueError("give me a gauge id like EADM7 or a river name like 'Mississippi River at St. Louis'")
        try:
            as_id = gauge_id(text)
        except ValueError:
            as_id = None
        if as_id:
            report = self.gauge(as_id)
            report["via"] = "gauge id"
            report["candidates"] = []
            return report

        candidates = self.find_gauges(text, limit=10)
        tried = []
        for candidate in candidates:
            if not candidate["usable"]:
                continue
            tried.append(candidate["site"])
            try:
                report = self.gauge(candidate["site"])
            except ValueError:
                continue  # a USGS site with no NWPS forecast point; try the next one
            report["via"] = f"name match on {candidate['name']!r}"
            report["candidates"] = candidates
            return report
        raise ValueError(
            f"I found no NWPS gauge for {text!r}"
            + (f" (USGS sites tried: {', '.join(tried)})" if tried else " (USGS had no matching site)")
            + " - try a bigger river, or a gauge id like EADM7")

    # -- shaping -----------------------------------------------------------

    @staticmethod
    def _normalise(payload: dict) -> dict:
        status = payload.get("status") or {}
        observed = status.get("observed") or {}
        forecast = status.get("forecast") or {}
        categories = ((payload.get("flood") or {}).get("categories") or {})
        stages = {}
        for key, entry in categories.items():
            stage = number((entry or {}).get("stage"))
            if stage is not None:
                stages[str(key).lower()] = stage
        observed_stage = number(observed.get("primary"))
        next_up = None
        for key in CATEGORY_ORDER:
            stage = stages.get(key)
            if stage is not None and observed_stage is not None and observed_stage < stage:
                next_up = {"category": key, "stage": stage,
                           "feet_to_go": round(stage - observed_stage, 2)}
                break
        return {
            "dataset": DATASET,
            "name": payload.get("name"),
            "lid": payload.get("lid"),
            "usgs_id": payload.get("usgsId"),
            "state": (payload.get("state") or {}).get("abbreviation") if isinstance(payload.get("state"), dict) else None,
            "county": payload.get("county") if isinstance(payload.get("county"), str) else (payload.get("county") or {}).get("name"),
            "forecast_office": ((payload.get("rfc") or {}).get("abbreviation")),
            "timezone": payload.get("timeZone"),
            "observed": {
                "stage": observed_stage,
                "stage_unit": observed.get("primaryUnit"),
                "flow": number(observed.get("secondary")),
                "flow_unit": observed.get("secondaryUnit"),
                "category": observed.get("floodCategory") or payload.get("ObservedFloodCategory"),
                "at": observed.get("validTime"),
            },
            "forecast": {
                "stage": number(forecast.get("primary")),
                "stage_unit": forecast.get("primaryUnit"),
                "flow": number(forecast.get("secondary")),
                "flow_unit": forecast.get("secondaryUnit"),
                "category": forecast.get("floodCategory") or payload.get("ForecastFloodCategory"),
                "at": forecast.get("validTime"),
            },
            "categories": stages,
            "next_threshold": next_up,
        }
