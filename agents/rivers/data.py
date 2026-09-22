"""Read-only stream gauge reader for the rivers agent.

Self-contained on purpose: the USGS transport lives here with one small cache.

  https://api.waterdata.usgs.gov/ogcapi/v0/collections/latest-continuous   newest value per parameter
  https://api.waterdata.usgs.gov/ogcapi/v0/collections/continuous          the time series itself
  https://api.waterdata.usgs.gov/ogcapi/v0/collections/monitoring-locations the gauge list

One real finding from building this (2026-09-22): the old NWIS endpoint everyone still
documents, `waterservices.usgs.gov/nwis/iv`, answers 503 now - USGS has moved to the OGC
API above. This reader only talks to the new one, which was verified live that day
(USGS-06730500 returned 0.30 ft^3/s on 2026-09-22T18:00Z).

Units come back as strings like `ft^3/s` and `ft`; the reader keeps USGS's own unit text
instead of converting, so nothing is silently changed.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib import parse, request

BASE_URL = "https://api.waterdata.usgs.gov/ogcapi/v0"
DATASET_LATEST = "api.waterdata.usgs.gov/ogcapi/v0/collections/latest-continuous"
DATASET_SERIES = "api.waterdata.usgs.gov/ogcapi/v0/collections/continuous"
DATASET_SITES = "api.waterdata.usgs.gov/ogcapi/v0/collections/monitoring-locations"

DEFAULT_USER_AGENT = "awesome-acps-rivers/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: USGS parameter codes this agent speaks.
PARAMETERS = {
    "00060": "discharge (streamflow)",
    "00065": "gage height (water level)",
    "00010": "water temperature",
    "63680": "turbidity",
    "00400": "pH",
}

_POINT_RE = re.compile(r"\b(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\b")
_SITE_RE = re.compile(r"\b(?:USGS-)?(\d{8,15})\b")


class RiversError(RuntimeError):
    """A USGS water feed could not be read."""


class RiversData:
    """USGS continuous water data, read over HTTP with caching and validation."""

    def __init__(self, fetch=None, cache_ttl: float = 300.0, timeout: float = 25.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("RIVERS_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("RIVERS_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("RIVERS_USER_AGENT") or DEFAULT_USER_AGENT
        self._fetch = fetch or self._http_get
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    # -- transport ---------------------------------------------------------

    def _http_get(self, url: str, params: dict):
        query = parse.urlencode(params) if params else ""
        full = f"{url}?{query}" if query else url
        req = request.Request(full, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # urllib raises many types; callers see one error type
            raise RiversError(f"USGS water request failed: {exc}") from exc

    def _get(self, url: str, params: dict, ttl: float | None = None):
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
    def check_site(value: str) -> str:
        """Accept '06730500', 'USGS-06730500' or a full monitoring location id."""
        text = str(value).strip()
        match = _SITE_RE.search(text)
        if not match:
            raise ValueError("a gauge is an 8-to-15 digit USGS site number, for example 06730500")
        return f"USGS-{match.group(1)}"

    @staticmethod
    def check_point(value: str) -> str:
        match = _POINT_RE.search(str(value))
        if not match:
            raise ValueError("a point is 'lat,lon', for example 40.71,-74.01")
        latitude, longitude = float(match.group(1)), float(match.group(2))
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError("latitude must be -90..90 and longitude -180..180")
        return f"{round(latitude, 4)},{round(longitude, 4)}"

    @staticmethod
    def check_radius_km(value) -> float:
        try:
            radius = float(value)
        except (TypeError, ValueError):
            raise ValueError("a radius must be a number of kilometres") from None
        if not 1 <= radius <= 500:
            raise ValueError("a radius must be between 1 and 500 km")
        return radius

    @staticmethod
    def check_parameter(value: str) -> str:
        code = str(value).strip()
        if code not in PARAMETERS:
            raise ValueError(f"parameter must be one of: {', '.join(sorted(PARAMETERS))}")
        return code

    @staticmethod
    def check_hours(value) -> int:
        try:
            hours = int(value)
        except (TypeError, ValueError):
            raise ValueError("hours must be a whole number") from None
        if not 1 <= hours <= 720:
            raise ValueError("hours must be between 1 and 720 (30 days)")
        return hours

    # -- readings ----------------------------------------------------------

    def latest(self, site: str) -> dict:
        location = self.check_site(site)
        payload = self._get(f"{BASE_URL}/collections/latest-continuous/items", {
            "f": "json",
            "monitoring_location_id": location,
            "limit": "20",
        }, ttl=min(self.cache_ttl, 300))
        observations = self._observations(payload)
        if not observations:
            raise RiversError(f"USGS has no current readings for {location}")
        for item in observations:
            item["parameter_name"] = PARAMETERS.get(item["parameter_code"] or "", "other")
        return {
            "dataset": DATASET_LATEST,
            "site": location,
            "site_name": self._site_name(payload),
            "observations": observations,
            "observed_at": max((item["time"] or "") for item in observations) or None,
        }

    def series(self, site: str, hours: int = 24, parameter: str = "00060") -> dict:
        location = self.check_site(site)
        code = self.check_parameter(parameter)
        window = self.check_hours(hours)
        payload = self._get(f"{BASE_URL}/collections/continuous/items", {
            "f": "json",
            "monitoring_location_id": location,
            "parameter_code": code,
            "limit": str(min(1000, max(10, window * 4))),
            "sortby": "-time",
        }, ttl=min(self.cache_ttl, 300))
        rows = self._observations(payload)
        if not rows:
            raise RiversError(
                f"USGS has no {PARAMETERS[code]} series for {location} in the window "
                "(the gauge may not measure that)")
        rows.sort(key=lambda row: row["time"] or "")
        values = [(row["value"], row["unit"]) for row in rows if row["value"] is not None]
        if not values:
            raise RiversError(f"USGS returned no numeric values for {location}")
        numbers = [value for value, _ in values]
        first, last = rows[0], rows[-1]
        change = round(numbers[-1] - numbers[0], 4)
        return {
            "dataset": DATASET_SERIES,
            "site": location,
            "site_name": self._site_name(payload),
            "parameter_code": code,
            "parameter_name": PARAMETERS[code],
            "unit": values[0][1],
            "rows": rows,
            "count": len(rows),
            "window_hours": window,
            "first": {"time": first["time"], "value": first["value"]},
            "last": {"time": last["time"], "value": last["value"]},
            "min": min(numbers),
            "max": max(numbers),
            "mean": round(sum(numbers) / len(numbers), 4),
            "change": change,
            "rising": change > 0,
        }

    def sites_near(self, point: str, radius_km: float = 25, limit: int = 5) -> dict:
        location = self.check_point(point)
        radius = self.check_radius_km(radius_km)
        latitude, longitude = (float(part) for part in location.split(","))
        # One degree of latitude is about 111 km; longitude shrinks with latitude.
        dlat = radius / 111.0
        dlon = radius / max(1.0, 111.0 * abs(math.cos(math.radians(latitude))))
        payload = self._get(f"{BASE_URL}/collections/monitoring-locations/items", {
            "f": "json",
            "bbox": f"{round(longitude - dlon, 5)},{round(latitude - dlat, 5)},"
                    f"{round(longitude + dlon, 5)},{round(latitude + dlat, 5)}",
            "limit": str(max(5, min(int(limit) * 10, 200))),
        }, ttl=min(self.cache_ttl, 3600))
        features = payload.get("features") if isinstance(payload, dict) else None
        if features is None:
            raise RiversError("USGS returned an unexpected payload (no features)")
        sites = []
        for feature in features:
            props = feature.get("properties") or {}
            site_id = props.get("id") or props.get("monitoring_location_number") or ""
            if not str(site_id).startswith("USGS-"):
                continue
            coordinates = (feature.get("geometry") or {}).get("coordinates") or []
            distance = None
            if len(coordinates) >= 2:
                distance = round(self._haversine_km(latitude, longitude, coordinates[1], coordinates[0]), 2)
            sites.append({
                "site": site_id,
                "name": props.get("monitoring_location_name"),
                "site_type": props.get("site_type_code") or props.get("monitoring_location_type"),
                "county": props.get("county_name"),
                "state": props.get("state_name"),
                "distance_km": distance,
            })
        sites.sort(key=lambda item: (item["distance_km"] is None, item["distance_km"]))
        return {
            "dataset": DATASET_SITES,
            "point": location,
            "radius_km": radius,
            "sites": sites[: max(1, min(int(limit), 50))],
            "matched": len(sites),
        }

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        radius = 6371.0
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        return 2 * radius * math.asin(min(1.0, math.sqrt(a)))

    @staticmethod
    def _observations(payload) -> list[dict]:
        features = payload.get("features") if isinstance(payload, dict) else None
        if features is None:
            raise RiversError("USGS returned an unexpected payload (no features)")
        rows = []
        for feature in features:
            props = feature.get("properties") or {}
            raw = props.get("value")
            try:
                value = float(raw)
            except (TypeError, ValueError):
                value = None
            rows.append({
                "time": props.get("time"),
                "value": value,
                "unit": props.get("unit_of_measure"),
                "parameter_code": props.get("parameter_code"),
                "statistic_id": props.get("statistic_id"),
                "site": props.get("monitoring_location_id"),
            })
        return rows

    @staticmethod
    def _site_name(payload) -> str | None:
        features = payload.get("features") if isinstance(payload, dict) else None
        for feature in features or []:
            name = (feature.get("properties") or {}).get("monitoring_location_name")
            if name:
                return name
        return None

    @staticmethod
    def window_start(hours: int) -> str:
        """The UTC timestamp `hours` before now, for callers that want to label a window."""
        return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
