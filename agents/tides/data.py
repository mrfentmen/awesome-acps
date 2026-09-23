"""Read-only NOAA tide reader for the tides agent.

Self-contained on purpose: the NOAA Tides and Currents transport lives here with one cache.

  mdapi/prod/webapi/stations.json?type=tidepredictions   every station with tide predictions
  api/prod/datagetter?product=water_level&date=latest    what the gauge reads right now
  api/prod/datagetter?product=predictions&interval=hilo  the hi/lo events NOAA predicts

Tides and Currents is the US National Ocean Service's own gauge network. Keyless, and verified
live on 2026-09-23 (3,499 stations with tide predictions; The Battery's gauge, 8518750, read
3.753 ft above MLLW while it predicted a 1.286 ft low).

Three things about this data shape the reader:

  1. The station list spells longitude `lng`, not `lon`. Reading the wrong key silently loses
     every coordinate, which is why `nearest()` exists and is tested.
  2. Predictions are harmonic. They are not observations: a prediction is where the water will
     be, a water level is where it is. This reader keeps them apart and never mixes them.
  3. Both feeds can be asked for `time_zone=lst_ldt`, which is the station's own wall clock
     with daylight saving applied. The gauge's newest reading is therefore the station's
     current local time, and hi/lo events can be filtered against it by plain string
     comparison - no timezone arithmetic, no DST guesses.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import re
import threading
import time
from urllib import parse, request

MD_API = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi"
DATA_API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
DATASET = "NOAA Tides and Currents (tidesandcurrents.noaa.gov)"

#: NOAA asks every caller to identify itself; this is how this repo does it.
APPLICATION = "awesome-acps-tides"
DEFAULT_USER_AGENT = "awesome-acps-tides/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: A plain reference to the datum every height here is measured against.
DATUM = "MLLW"
DATUM_NOTE = "MLLW (mean lower low water, the chart datum US tide tables use)"

#: Station ids are seven digits (8518750 is The Battery).
_STATION_RE = re.compile(r"\b(\d{7})\b")

#: State and territory names to the two-letter code NOAA stores, so 'tides in Florida' works.
STATE_CODES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC", "puerto rico": "PR",
    "virgin islands": "VI", "guam": "GU", "american samoa": "AS", "northern mariana islands": "MP",
}

#: Station code to a readable name, so an answer can say 'Florida' instead of 'FL'.
STATE_NAMES = {code: name.title() for name, code in STATE_CODES.items()}
STATE_NAMES.update({"DC": "District of Columbia", "PR": "Puerto Rico", "VI": "U.S. Virgin Islands",
                    "AS": "American Samoa", "GU": "Guam", "MP": "Northern Mariana Islands"})

_CACHE_STATIONS_TTL = 86400.0  # the gauge network changes about as often as a coastline does

#: Words that are never part of a place name in a tide question.
_NOISE = {
    "tide", "tides", "high", "low", "next", "when", "is", "the", "at", "in", "near", "for",
    "of", "today", "tonight", "tomorrow", "now", "right", "table", "station", "stations",
    "gauge", "gages", "predictions", "prediction", "please", "what", "whats", "how", "highs",
    "lows", "time", "times", "levels", "level", "current", "currently", "observed", "above",
    "below", "and", "a", "an", "me", "show", "give", "list", "are", "there", "any", "would",
}


class TidesError(RuntimeError):
    """A NOAA tide feed could not be read."""


def station_id(value) -> str:
    """A seven digit station id from text, or a clear error."""
    match = _STATION_RE.search(str(value or ""))
    if not match:
        raise ValueError("give me a NOAA station id, for example 8518750 (The Battery)")
    return match.group(1)


def haversine_km(first, second) -> float:
    """Great-circle distance in km between two (latitude, longitude) pairs."""
    lat1, lon1 = float(first[0]), float(first[1])
    lat2, lon2 = float(second[0]), float(second[1])
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def place_words(text: str) -> list[str]:
    """The words in a question that could name a place, in order, noise removed."""
    return [word for word in re.findall(r"[A-Za-z][\w.'-]*", str(text or "")) if word.lower() not in _NOISE]


def state_from_words(words: list[str]) -> tuple[str | None, list[str]]:
    """(state code, the words left over) - 'florida tides' -> ('FL', [])."""
    lowered = [word.lower() for word in words]
    for size in (2, 1):
        for start in range(len(lowered) - size + 1):
            phrase = " ".join(lowered[start:start + size])
            if phrase in STATE_CODES:
                rest = words[:start] + words[start + size:]
                return STATE_CODES[phrase], rest
    return None, list(words)


def score_name(query_words: list[str], name: str) -> float:
    """How well a station name answers a query, 0 when it does not. Bigger is better.

    A name with words the query never mentioned is a worse match than one without them:
    NOAA has both "NEW YORK (The Battery)" and "Battery Creek, 4 mi. above entrance", and
    'the battery' has to reach the one in New York.
    """
    haystack = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    if not haystack or not query_words:
        return 0.0
    tokens = haystack.split()
    score = 0.0
    for word in query_words:
        token = word.lower().strip("\"'")
        if not token:
            continue
        if haystack == token:
            score += 6.0
        elif haystack.startswith(token):
            score += 4.0
        elif re.search(rf"\b{re.escape(token)}\b", haystack):
            score += 3.0
        elif len(token) > 3 and token in haystack:
            score += 1.0
    if not score:
        return 0.0
    matched = sum(1 for word in query_words if re.search(rf"\b{re.escape(word.lower())}\b", haystack))
    unexplained = sum(1 for token in tokens
                      if not any(token == word.lower() or (len(word) > 2 and word.lower() in token)
                                 for word in query_words))
    return score + matched - 0.75 * unexplained


class TidesData:
    """NOAA station metadata, live gauge readings and harmonic tide predictions."""

    def __init__(self, fetch=None, cache_ttl: float = 300.0, timeout: float = 25.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("TIDES_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("TIDES_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("TIDES_USER_AGENT") or DEFAULT_USER_AGENT
        self._fetch = fetch or self._http_text
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    # -- transport ---------------------------------------------------------

    def _http_text(self, url: str, params: dict) -> str:
        if params:
            url = f"{url}?{parse.urlencode(params)}"
        req = request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # urllib raises many types; callers see one error type
            raise TidesError(f"NOAA request failed: {exc}") from exc

    def _json(self, url: str, params: dict | None = None, ttl: float | None = None) -> dict:
        key = f"{url}?{parse.urlencode(params or {})}"
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        payload = self._fetch(url, params or {})
        if isinstance(payload, (str, bytes)):
            try:
                payload = json.loads(payload)
            except ValueError as exc:
                raise TidesError(f"NOAA returned something that is not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise TidesError("NOAA returned an unexpected payload")
        if payload.get("error"):
            message = (payload["error"] or {}).get("message") or payload["error"]
            raise TidesError(f"NOAA refused the request: {message}")
        with self._lock:
            self._cache[key] = (now + (ttl if ttl is not None else self.cache_ttl), payload)
        return payload

    # -- stations ----------------------------------------------------------

    def stations(self) -> list[dict]:
        """Every station NOAA publishes tide predictions for, with coordinates."""
        payload = self._json(f"{MD_API}/stations.json", {"type": "tidepredictions"},
                             ttl=_CACHE_STATIONS_TTL)
        rows = []
        for raw in payload.get("stations") or []:
            rows.append({
                "id": str(raw.get("id") or ""),
                "name": (raw.get("name") or "").strip(),
                "state": (raw.get("state") or "").strip(),
                "latitude": _number(raw.get("lat")),
                "longitude": _number(raw.get("lng")),
                "timezone_offset": _number(raw.get("timezonecorr")),
            })
        # Deliberately no reference/subordinate field: this endpoint reports tideType as an
        # empty string for all 3,499 stations (checked live 2026-09-23), so a flag built from
        # it would label every station 'subordinate' - a fabricated fact, not NOAA's.
        if not rows:
            raise TidesError("NOAA returned no tide stations")
        return rows

    def station(self, station: str) -> dict:
        """One station's metadata, or a clear error naming what is wrong."""
        wanted = station_id(station)
        for row in self.stations():
            if row["id"] == wanted:
                return row
        raise ValueError(f"NOAA has no tide station {wanted!r}")

    def find(self, text: str, limit: int = 5) -> list[dict]:
        """Stations a question is asking about, best match first."""
        words = place_words(text)
        wanted_state, words = state_from_words(words)
        if not words and not wanted_state:
            return []
        candidates = []
        for row in self.stations():
            if wanted_state and row["state"] != wanted_state:
                continue
            if words:
                score = score_name(words, row["name"])
                if not score:
                    continue
            else:
                score = 1.0
            candidates.append((score, row))
        candidates.sort(key=lambda pair: (-pair[0], pair[1]["name"]))
        return [row for _score, row in candidates[: max(1, min(int(limit), 25))]]

    def by_state(self, code: str) -> list[dict]:
        """Every station in one state or territory, by name - the order NOAA itself gives."""
        wanted = str(code or "").strip().upper()
        return sorted((row for row in self.stations() if row["state"] == wanted),
                      key=lambda row: row["name"].lower())

    def nearest(self, latitude: float, longitude: float, limit: int = 5) -> list[dict]:
        """The closest stations to a point, with the distance added to each row."""
        origin = (float(latitude), float(longitude))
        found = []
        for row in self.stations():
            if row["latitude"] is None or row["longitude"] is None:
                continue
            distance = haversine_km(origin, (row["latitude"], row["longitude"]))
            found.append((distance, dict(row, distance_km=round(distance, 1))))
        found.sort(key=lambda pair: pair[0])
        return [row for _distance, row in found[: max(1, min(int(limit), 25))]]

    # -- readings and predictions ------------------------------------------

    def _get(self, params: dict) -> dict:
        params = {"application": APPLICATION, "format": "json", "units": "english",
                  "time_zone": "lst_ldt", **params}
        return self._json(DATA_API, params)

    def observed(self, station: str) -> dict | None:
        """The newest real reading from a gauge, or None when that station has no sensor.

        None is not a failure: some stations publish predictions only. The caller decides
        what to say about it.
        """
        wanted = station_id(station)
        try:
            payload = self._get({"product": "water_level", "datum": DATUM, "date": "latest",
                                 "station": wanted})
        except TidesError:
            return None
        rows = payload.get("data") or []
        if not rows:
            return None
        newest = rows[-1]
        meta = payload.get("metadata") or {}
        return {
            "time_local": (newest.get("t") or "").strip(),
            "height_ft": _number(newest.get("v")),
            "sigma_ft": _number(newest.get("s")),
            "quality": (newest.get("q") or "").strip(),
            "station_name": (meta.get("name") or "").strip(),
        }

    def events(self, station: str, hours: int = 72) -> list[dict]:
        """The hi/lo tide events NOAA predicts, station local time, oldest first.

        `begin_date` is always sent. Asked for `range` alone, this endpoint silently starts
        three days in the past - verified live on 2026-09-23, when a `range=48` request for
        The Battery returned events from 2026-09-20, and Boston's answered 'next high tide'
        came back empty because every predicted event had already happened.
        """
        wanted = station_id(station)
        span = max(6, min(int(hours), 240))
        begin = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
        payload = self._get({"product": "predictions", "datum": DATUM, "station": wanted,
                             "interval": "hilo", "range": str(span), "begin_date": begin})
        if "predictions" not in payload:
            raise TidesError(f"NOAA returned no prediction table for station {wanted!r}")
        events = []
        for raw in payload.get("predictions") or []:
            kind = (raw.get("type") or "").strip().upper()
            events.append({
                "time_local": (raw.get("t") or "").strip(),
                "height_ft": _number(raw.get("v")),
                "kind": "high" if kind == "H" else "low" if kind == "L" else kind.lower(),
            })
        events.sort(key=lambda event: event["time_local"])
        return events

    def tide(self, station: str, count: int = 4, hours: int = 72) -> dict:
        """Everything one tide answer needs: the gauge, what it reads, and what comes next.

        The gauge's own newest reading is this station's current wall clock, so the events
        are filtered against it as text. When a station publishes no live level, the caller
        is told the list is anchored instead of being handed a vague 'next'.
        """
        info = self.station(station)
        reading = self.observed(info["id"])
        events = self.events(info["id"], hours=hours)
        anchor = reading["time_local"] if reading else None
        if anchor:
            upcoming = [event for event in events if event["time_local"] > anchor]
        else:
            upcoming = list(events)
        keep = max(1, min(int(count), 12))
        return {
            "dataset": DATASET,
            "station": info,
            "observed": reading,
            "anchored": bool(anchor),
            "events": events,
            "upcoming": upcoming[:keep],
            "total_events": len(events),
        }


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
