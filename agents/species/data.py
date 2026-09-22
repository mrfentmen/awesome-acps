"""Read-only iNaturalist reader for the species agent.

  https://api.inaturalist.org/v1/observations   iNaturalist observations

Keyless, verified live on 2026-09-22 (548,321 observations matched Danaus plexippus).
The endpoint returns `total_results` for a taxon name plus the newest records when a
`per_page` is given, which is all this reader needs: a count, recent sightings, and what
has been seen around a point.

iNaturalist's grades are not equal: `research` is community-confirmed, `needs_id` is not.
This reader keeps the grade on every row instead of pretending they are the same thing.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from urllib import parse, request

BASE_URL = "https://api.inaturalist.org/v1/observations"
DATASET = "api.inaturalist.org/v1/observations"

DEFAULT_USER_AGENT = "awesome-acps-species/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: iNaturalist accepts a point + radius (km). Keep the radius sane for a public API.
MAX_RADIUS_KM = 25

_POINT_RE = re.compile(r"\b(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\b")

#: Metro centres, so a place name works without coordinates.
CITY_POINTS = {
    "amsterdam": (52.37, 4.90), "atlanta": (33.75, -84.39), "bangalore": (12.97, 77.59),
    "bangkok": (13.76, 100.50), "berlin": (52.52, 13.40), "boston": (42.36, -71.06),
    "chicago": (41.88, -87.63), "delhi": (28.61, 77.21), "denver": (39.74, -104.99),
    "hong kong": (22.32, 114.17), "london": (51.51, -0.13), "los angeles": (34.05, -118.24),
    "mexico city": (19.43, -99.13), "miami": (25.76, -80.19), "nairobi": (-1.29, 36.82),
    "new york": (40.71, -74.01), "paris": (48.86, 2.35), "san francisco": (37.77, -122.42),
    "sao paulo": (-23.55, -46.63), "seattle": (47.61, -122.33), "sydney": (-33.87, 151.21),
    "tokyo": (35.68, 139.69), "toronto": (43.65, -79.38), "vancouver": (49.28, -123.12),
}


class SpeciesError(RuntimeError):
    """The iNaturalist feed could not be read."""


class SpeciesData:
    """iNaturalist observations, read over HTTP with caching and validation."""

    def __init__(self, fetch=None, cache_ttl: float = 600.0, timeout: float = 20.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("SPECIES_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("SPECIES_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("SPECIES_USER_AGENT") or DEFAULT_USER_AGENT
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
            raise SpeciesError(f"iNaturalist request failed: {exc}") from exc

    def _get(self, params: dict, ttl: float | None = None):
        key = json.dumps({k: str(v) for k, v in sorted(params.items())})
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        payload = self._fetch(BASE_URL, params)
        with self._lock:
            self._cache[key] = (now + (ttl if ttl is not None else self.cache_ttl), payload)
        return payload

    # -- validation --------------------------------------------------------

    @staticmethod
    def check_taxon(value: str) -> str:
        taxon = " ".join(str(value).strip().split())
        if len(taxon) < 3 or not re.search(r"[A-Za-z]", taxon):
            raise ValueError("a taxon is a name like 'monarch butterfly' or 'Danaus plexippus'")
        return taxon

    @staticmethod
    def check_radius(value) -> int:
        try:
            radius = int(value)
        except (TypeError, ValueError):
            raise ValueError("radius must be a whole number of kilometres") from None
        if not 1 <= radius <= MAX_RADIUS_KM:
            raise ValueError(f"radius must be between 1 and {MAX_RADIUS_KM} km")
        return radius

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
    def point_from_place(place: str) -> str | None:
        lowered = str(place).strip().lower()
        key = next((name for name in sorted(CITY_POINTS, key=len, reverse=True)
                    if lowered == name or lowered.endswith(f" {name}") or lowered.startswith(f"{name} ")),
                   None)
        if not key:
            return None
        latitude, longitude = CITY_POINTS[key]
        return f"{latitude},{longitude}"

    @staticmethod
    def place_from_text(text: str) -> str | None:
        lowered = " " + re.sub(r"[^a-z ]+", " ", text.lower()).strip() + " "
        for name in sorted(CITY_POINTS, key=len, reverse=True):
            if f" {name} " in re.sub(r"\s+", " ", lowered):
                return name
        return None

    # -- rows --------------------------------------------------------------

    @staticmethod
    def _row(result: dict) -> dict:
        taxon = result.get("taxon") or {}
        user = result.get("user") or {}
        return {
            "name": taxon.get("name"),
            "common": taxon.get("preferred_common_name"),
            "observed_on": result.get("observed_on") or result.get("created_at"),
            "place": result.get("place_guess"),
            "user": user.get("login"),
            "quality": result.get("quality_grade"),
            "uri": result.get("uri"),
        }

    @staticmethod
    def _results(payload) -> tuple[int, list[dict]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise SpeciesError("iNaturalist returned an unexpected payload (no results list)")
        total = payload.get("total_results")
        if not isinstance(total, int):
            raise SpeciesError("iNaturalist returned an unexpected payload (no total_results)")
        return total, payload["results"]

    # -- readings ----------------------------------------------------------

    def count(self, taxon: str) -> dict:
        name = self.check_taxon(taxon)
        payload = self._get({"taxon_name": name, "per_page": "1"})
        total, results = self._results(payload)
        return {
            "dataset": DATASET,
            "taxon": name,
            "total": total,
            "sample": self._row(results[0]) if results else None,
        }

    def recent(self, taxon: str, limit: int = 5) -> dict:
        name = self.check_taxon(taxon)
        rows = max(1, min(20, int(limit)))
        payload = self._get({"taxon_name": name, "per_page": str(rows), "order": "desc",
                             "order_by": "observed_on"})
        total, results = self._results(payload)
        return {
            "dataset": DATASET,
            "taxon": name,
            "total": total,
            "rows": [self._row(result) for result in results[:rows]],
        }

    def nearby(self, point: str, radius_km: int = 10, limit: int = 5, taxon: str | None = None) -> dict:
        location = self.check_point(point)
        radius = self.check_radius(radius_km)
        rows = max(1, min(20, int(limit)))
        params = {
            "lat": location.split(",")[0],
            "lng": location.split(",")[1],
            "radius": str(radius),
            "per_page": str(rows),
            "order": "desc",
            "order_by": "observed_on",
        }
        if taxon:
            params["taxon_name"] = self.check_taxon(taxon)
        payload = self._get(params)
        total, results = self._results(payload)
        return {
            "dataset": DATASET,
            "point": location,
            "radius_km": radius,
            "taxon": taxon,
            "total": total,
            "rows": [self._row(result) for result in results[:rows]],
        }
