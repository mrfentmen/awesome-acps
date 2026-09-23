"""Read-only NDBC reader for the buoys agent.

Self-contained on purpose: the NDBC transport lives here with one small cache.

  https://www.ndbc.noaa.gov/activestations.xml        every active station, with coordinates
  https://www.ndbc.noaa.gov/data/realtime2/<ID>.txt   that station's last ~45 days of reports

NDBC is the US National Data Buoy Center: moored buoys and coastal stations reporting wind,
waves, water temperature and pressure. Keyless, and verified live on 2026-09-22 (station
41025, the Diamond Shoals buoy off North Carolina, was reporting 1.6 m waves, 11.0 m/s wind
at 2026-09-22T23:50Z).

Two things shape this reader:

  1. The realtime file is text, not JSON. Row 1 is the column names, row 2 the units, row 3
     onward the reports, newest first, all times UTC. Every column is whitespace separated.
  2. A missing reading is the literal `MM`. That is not zero, and this reader keeps it as
     None so an answer can say "not reported" instead of "calm".
"""

from __future__ import annotations

import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from urllib import request

BASE_URL = "https://www.ndbc.noaa.gov"
DATASET = "ndbc.noaa.gov (National Data Buoy Center)"

DEFAULT_USER_AGENT = "awesome-acps-buoys/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: What a person actually asks about, in the order the file lists them.
FIELD_NOTES = {
    "WDIR": "wind direction (degrees true, the direction wind comes FROM)",
    "WSPD": "wind speed",
    "GST": "wind gust",
    "WVHT": "significant wave height (average of the highest third of waves)",
    "DPD": "dominant wave period",
    "APD": "average wave period",
    "MWD": "mean wave direction (degrees true)",
    "PRES": "sea level pressure",
    "ATMP": "air temperature",
    "WTMP": "water temperature",
    "DEWP": "dew point",
    "VIS": "visibility",
    "PTDY": "pressure tendency",
    "TIDE": "tide",
}

#: NDBC station ids: five digits for buoys (41025), or four letters plus a digit for fixed
#: stations (meyc1, which is Monterey's shore station).
_STATION_RE = re.compile(r"\b(\d{5}|[A-Za-z]{4}\d)\b")
_MISSING = {"MM", ""}

_CACHE_STATIONS_TTL = 86400.0  # the station list changes about as often as a harbor does


class BuoysError(RuntimeError):
    """An NDBC feed could not be read."""


def station_id(value) -> str:
    """A station id from text: '41025' stays, 'meyc1' lowercases, anything else raises."""
    token = str(value or "").strip()
    match = _STATION_RE.search(token)
    if not match:
        raise ValueError("give me an NDBC station id, for example 41025 or meyc1")
    return match.group(1).lower()


def reading(value) -> float | None:
    """One cell of the realtime file: 'MM' means not reported, so it becomes None."""
    text = str(value or "").strip()
    if text in _MISSING:
        return None
    try:
        return float(text)
    except ValueError:
        return None


class NdbcData:
    """NDBC station metadata and realtime reports, with caching and validation."""

    def __init__(self, fetch=None, cache_ttl: float = 300.0, timeout: float = 25.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("BUOYS_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("BUOYS_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("BUOYS_USER_AGENT") or DEFAULT_USER_AGENT
        self._fetch = fetch or self._http_text
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    # -- transport ---------------------------------------------------------

    def _http_text(self, url: str, params: dict) -> str:
        req = request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # urllib raises many types; callers see one error type
            raise BuoysError(f"NDBC request failed: {exc}") from exc

    def _cached(self, url: str, ttl: float | None = None) -> str:
        now = time.time()
        with self._lock:
            hit = self._cache.get(url)
            if hit and hit[0] > now:
                return hit[1]
        payload = self._fetch(url, {})
        with self._lock:
            self._cache[url] = (now + (ttl if ttl is not None else self.cache_ttl), payload)
        return payload

    # -- stations ----------------------------------------------------------

    def stations(self) -> list[dict]:
        """Every active station NDBC publishes: id, name, coordinates, type."""
        text = self._cached(f"{BASE_URL}/activestations.xml", ttl=_CACHE_STATIONS_TTL)
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise BuoysError(f"NDBC station list was not valid XML: {exc}") from exc
        rows = []
        for node in root.findall("station"):
            rows.append({
                "id": (node.get("id") or "").lower(),
                "name": node.get("name") or "",
                "latitude": _number(node.get("lat")),
                "longitude": _number(node.get("lon")),
                "type": (node.get("type") or "").lower(),
                "owner": node.get("owner") or "",
                "program": node.get("pgm") or "",
            })
        if not rows:
            raise BuoysError("NDBC returned no stations")
        return rows

    def find_stations(self, text: str, limit: int = 5) -> list[dict]:
        """Stations whose id or name matches the words given, best match first."""
        wanted = [word for word in re.findall(r"[a-z0-9]+", str(text or "").lower()) if len(word) > 2]
        if not wanted:
            return []
        matches = []
        for row in self.stations():
            haystack = f"{row['id']} {row['name']}".lower()
            hits = sum(1 for word in wanted if word in haystack)
            if hits:
                matches.append((hits, row))
        # More words matched wins; then a buoy over a shore station, then the shorter name.
        matches.sort(key=lambda pair: (-pair[0], pair[1]["type"] != "buoy", len(pair[1]["name"])))
        return [row for _hits, row in matches[: max(1, min(int(limit), 25))]]

    def station(self, station: str) -> dict:
        """One station's metadata, or a clear error naming what is wrong."""
        wanted = station_id(station)
        for row in self.stations():
            if row["id"] == wanted:
                return row
        raise ValueError(f"NDBC has no active station {wanted!r}")

    # -- reports -----------------------------------------------------------

    def latest(self, station: str, rows: int = 1) -> dict:
        """The newest report for a station, with NDBC's own column names and units."""
        wanted = station_id(station)
        text = self._cached(f"{BASE_URL}/data/realtime2/{wanted.upper()}.txt")
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) < 3 or not lines[0].startswith("#"):
            raise BuoysError(f"NDBC returned no realtime table for station {wanted!r}")
        columns = lines[0].lstrip("#").split()
        units = lines[1].lstrip("#").split()
        if len(units) != len(columns):
            units = units + [""] * (len(columns) - len(units))
        keep = max(1, min(int(rows), 10))
        reports = []
        for line in lines[2:2 + keep]:
            cells = line.split()
            values = {}
            raw = {}
            for index, column in enumerate(columns):
                cell = cells[index] if index < len(cells) else "MM"
                raw[column] = cell
                values[column] = reading(cell)
            reports.append({
                "time_utc": _timestamp(values),
                "values": values,
                "units": {column: units[index] for index, column in enumerate(columns)},
                "raw": raw,
            })
        if not reports:
            raise BuoysError(f"station {wanted!r} has no reports in the realtime file")
        return {
            "dataset": DATASET,
            "station": wanted,
            "columns": columns,
            "reports": reports,
            "total_reports": max(0, len(lines) - 2),
        }

    def conditions(self, station: str) -> dict:
        """The newest report, trimmed to the readings a person asked about, plus the station."""
        info = self.station(station)
        report = self.latest(station, rows=1)["reports"][0]
        values = report["values"]
        interesting = {key: values.get(key) for key in FIELD_NOTES if key in values}
        return {
            "dataset": DATASET,
            "station": info,
            "time_utc": report["time_utc"],
            "readings": interesting,
            "units": report["units"],
            "raw": report["raw"],
        }


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp(values: dict) -> str | None:
    """'#YY MM DD hh mm' -> ISO 8601 UTC, the way the file means it."""
    year = values.get("YY")
    if year is None:
        return None
    parts = []
    for key in ("YY", "MM", "DD", "hh", "mm"):
        cell = values.get(key)
        if cell is None:
            return None
        parts.append(int(cell))
    if year < 100:
        parts[0] = 2000 + int(year)
    return "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}Z".format(*parts)
