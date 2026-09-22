"""Read-only bike-share reader for the bikes agent.

  https://gbfs.citibikenyc.com/gbfs/en/station_information.json
  https://gbfs.citibikenyc.com/gbfs/en/station_status.json

GBFS is the open General Bikeshare Feed Specification; Citi Bike (Lyft NYC) publishes it
keyless. Verified live on 2026-09-22 (system_id lyft_nyc, station statuses returned).

One system, stated plainly: this is Citi Bike - New York City, Jersey City and Hoboken.
The two files are joined by station id, and the join is cached for a minute because the
status file changes constantly and is a couple of megabytes.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from urllib import parse, request

INFO_URL = "https://gbfs.citibikenyc.com/gbfs/en/station_information.json"
STATUS_URL = "https://gbfs.citibikenyc.com/gbfs/en/station_status.json"

DATASET = "gbfs.citibikenyc.com/gbfs/en (station_information + station_status)"
SYSTEM_NAME = "Citi Bike (New York City, Jersey City, Hoboken)"

DEFAULT_USER_AGENT = "awesome-acps-bikes/1.0 (+https://github.com/mrfentmen/awesome-acps)"

EARTH_RADIUS_KM = 6371.0

_POINT_RE = re.compile(r"\b(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\b")

#: Citi Bike service-area centres, so a borough name works without coordinates.
AREA_POINTS = {
    "manhattan": (40.7834, -73.9663), "brooklyn": (40.6782, -73.9442),
    "queens": (40.7282, -73.7949), "bronx": (40.8448, -73.8648),
    "jersey city": (40.7178, -74.0431), "hoboken": (40.7439, -74.0324),
}


class BikesError(RuntimeError):
    """The GBFS feed could not be read."""


def distance_km(point_a: str, point_b: str) -> float:
    """Great-circle distance between two 'lat,lon' points, in kilometres."""
    lat1, lon1 = (float(part) for part in point_a.split(","))
    lat2, lon2 = (float(part) for part in point_b.split(","))
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


class BikesData:
    """Citi Bike station information and live status, joined and cached."""

    def __init__(self, fetch=None, cache_ttl: float = 60.0, timeout: float = 30.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("BIKES_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("BIKES_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("BIKES_USER_AGENT") or DEFAULT_USER_AGENT
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
            raise BikesError(f"GBFS request failed: {exc}") from exc

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
    def check_point(value: str) -> str:
        match = _POINT_RE.search(str(value))
        if not match:
            raise ValueError("a point is 'lat,lon', for example 40.71,-74.01")
        latitude, longitude = float(match.group(1)), float(match.group(2))
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError("latitude must be -90..90 and longitude -180..180")
        return f"{round(latitude, 4)},{round(longitude, 4)}"

    @staticmethod
    def check_query(value: str) -> str:
        query = " ".join(str(value).strip().split())
        if len(query) < 3:
            raise ValueError("a station name is at least three characters, like 'Grand Central'")
        return query

    @staticmethod
    def point_from_area(place: str) -> str | None:
        lowered = str(place).strip().lower()
        key = next((name for name in sorted(AREA_POINTS, key=len, reverse=True)
                    if lowered == name or lowered.endswith(f" {name}") or lowered.startswith(f"{name} ")),
                   None)
        if not key:
            return None
        latitude, longitude = AREA_POINTS[key]
        return f"{latitude},{longitude}"

    @staticmethod
    def area_from_text(text: str) -> str | None:
        lowered = " " + re.sub(r"[^a-z ]+", " ", text.lower()).strip() + " "
        for name in sorted(AREA_POINTS, key=len, reverse=True):
            if f" {name} " in re.sub(r"\s+", " ", lowered):
                return name
        return None

    # -- readings ----------------------------------------------------------

    @staticmethod
    def _stations_block(payload) -> list[dict]:
        data = payload.get("data") if isinstance(payload, dict) else None
        stations = data.get("stations") if isinstance(data, dict) else None
        if not isinstance(stations, list):
            raise BikesError("GBFS returned an unexpected payload (no stations list)")
        return stations

    def stations(self) -> list[dict]:
        info = self._stations_block(self._get(INFO_URL, {}))
        status = self._stations_block(self._get(STATUS_URL, {}))
        # Station ids are mixed: some are short numbers, most are UUIDs, so the join keys on
        # the raw id as a string. (Keying on digits only silently drops ~70% of the system.)
        # If the status file has a station the info file does not, it is dropped - so every
        # row always has a name and coordinates.
        by_id = {str(row.get("station_id")): row for row in info if row.get("station_id") is not None}
        merged: list[dict] = []
        for row in status:
            station = by_id.get(str(row.get("station_id")))
            if not station:
                continue
            merged.append({
                "id": str(row["station_id"]),
                "name": station.get("name"),
                "lat": station.get("lat"),
                "lon": station.get("lon"),
                "capacity": station.get("capacity"),
                "bikes": row.get("num_bikes_available"),
                "ebikes": row.get("num_ebikes_available"),
                "docks": row.get("num_docks_available"),
                "renting": row.get("is_renting"),
                "last_reported": row.get("last_reported"),
            })
        if not merged:
            raise BikesError("GBFS returned no usable stations (the join came back empty)")
        return merged

    def station(self, query: str) -> dict:
        name = self.check_query(query)
        words = [word for word in re.split(r"\W+", name.lower()) if word]
        rows = self.stations()
        exact = [row for row in rows if (row["name"] or "").lower() == name.lower()]
        contains = [row for row in rows
                    if all(word in (row["name"] or "").lower() for word in words)]
        matches = exact or contains
        if not matches:
            raise BikesError(f"no Citi Bike station matches {name!r}")
        best = sorted(matches, key=lambda row: len(row["name"] or ""))[0]
        return {"dataset": DATASET, "match_count": len(matches), "station": best}

    def nearby(self, point: str, limit: int = 3) -> dict:
        location = self.check_point(point)
        rows = max(1, min(10, int(limit)))
        stations = self.stations()
        ranked = sorted(
            ({"station": row, "distance_km": round(distance_km(location, f"{row['lat']},{row['lon']}"), 3)}
             for row in stations if row.get("lat") is not None and row.get("lon") is not None),
            key=lambda item: item["distance_km"],
        )[:rows]
        return {"dataset": DATASET, "point": location, "rows": ranked}

    def system(self) -> dict:
        rows = self.stations()
        return {
            "dataset": DATASET,
            "system": SYSTEM_NAME,
            "stations": len(rows),
            "bikes": sum(row.get("bikes") or 0 for row in rows),
            "ebikes": sum(row.get("ebikes") or 0 for row in rows),
            "docks": sum(row.get("docks") or 0 for row in rows),
            "renting": sum(1 for row in rows if row.get("renting")),
        }
