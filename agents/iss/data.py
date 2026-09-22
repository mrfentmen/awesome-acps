"""Read-only space-station and sky-condition reader for the iss agent.

  http://api.open-notify.org/iss-now.json           ISS ground position
  https://api.sunrise-sunset.org/json               sunrise/sunset/twilight
  https://api.open-meteo.com/v1/forecast            cloud cover

All three are keyless, verified live on 2026-09-22 (ISS over the South Pacific at
-24.2244,-148.5018; sunset 2026-09-22T22:54:52Z for New York).

open-notify is called over plain HTTP on purpose: its HTTPS endpoint did not answer on
2026-09-22 (25 s timeouts on every try) while HTTP answered in 0.2 s. The payload is a
public position, nothing private, and this is the only feed in the repo where that holds.

Honest about what this is: open-notify gives the station's current ground point, not a
pass prediction. The reader therefore computes the great-circle distance to a point and
returns the sky conditions; the agent says plainly that it does not forecast passes.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from urllib import parse, request

ISS_URL = "http://api.open-notify.org/iss-now.json"
SUN_URL = "https://api.sunrise-sunset.org/json"
CLOUD_URL = "https://api.open-meteo.com/v1/forecast"

DATASET_ISS = "api.open-notify.org/iss-now.json"
DATASET_SUN = "api.sunrise-sunset.org/json"
DATASET_CLOUD = "api.open-meteo.com/v1/forecast (cloud_cover)"

DEFAULT_USER_AGENT = "awesome-acps-iss/1.0 (+https://github.com/mrfentmen/awesome-acps)"

EARTH_RADIUS_KM = 6371.0

_POINT_RE = re.compile(r"\b(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\b")


class IssError(RuntimeError):
    """A space-station or sky feed could not be read."""


def distance_km(point_a: str, point_b: str) -> float:
    """Great-circle distance between two 'lat,lon' points, in kilometres."""
    lat1, lon1 = (float(part) for part in point_a.split(","))
    lat2, lon2 = (float(part) for part in point_b.split(","))
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


class IssData:
    """ISS position plus the sky conditions at a point, with caching and validation."""

    def __init__(self, fetch=None, cache_ttl: float = 300.0, timeout: float = 45.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("ISS_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("ISS_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("ISS_USER_AGENT") or DEFAULT_USER_AGENT
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
            raise IssError(f"request failed: {exc}") from exc

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

    # -- readings ----------------------------------------------------------

    def position(self) -> dict:
        payload = self._get(ISS_URL, {}, ttl=60)
        position = payload.get("iss_position") if isinstance(payload, dict) else None
        if not isinstance(position, dict) or "latitude" not in position or "longitude" not in position:
            raise IssError("open-notify returned an unexpected payload (no iss_position)")
        return {
            "dataset": DATASET_ISS,
            "latitude": float(position["latitude"]),
            "longitude": float(position["longitude"]),
            "point": f"{float(position['latitude'])},{float(position['longitude'])}",
            "timestamp": payload.get("timestamp"),
        }

    def sky(self, point: str, now: datetime | None = None) -> dict:
        location = self.check_point(point)
        latitude, longitude = location.split(",")
        sun_payload = self._get(SUN_URL, {
            "lat": latitude, "lng": longitude, "formatted": "0",
        }, ttl=3600)
        results = sun_payload.get("results") if isinstance(sun_payload, dict) else None
        if not isinstance(results, dict) or "sunset" not in results:
            raise IssError("sunrise-sunset returned an unexpected payload (no results)")
        cloud_payload = self._get(CLOUD_URL, {
            "latitude": latitude, "longitude": longitude, "current": "cloud_cover", "timezone": "UTC",
        }, ttl=600)
        current = cloud_payload.get("current") if isinstance(cloud_payload, dict) else None
        if not isinstance(current, dict) or "cloud_cover" not in current:
            raise IssError("Open-Meteo returned an unexpected payload (no cloud_cover)")

        moment = now or datetime.now(timezone.utc)
        dark: bool | None = None
        try:
            sunset = datetime.fromisoformat(results["sunset"])
            sunrise = datetime.fromisoformat(results["sunrise"])
            if sunset.tzinfo is None:
                sunset = sunset.replace(tzinfo=timezone.utc)
            if sunrise.tzinfo is None:
                sunrise = sunrise.replace(tzinfo=timezone.utc)
            dark = moment >= sunset or moment < sunrise
        except (KeyError, ValueError):
            dark = None
        return {
            "dataset": DATASET_SUN,
            "point": location,
            "sunrise": results.get("sunrise"),
            "sunset": results.get("sunset"),
            "solar_noon": results.get("solar_noon"),
            "cloud_cover": current.get("cloud_cover"),
            "cloud_dataset": DATASET_CLOUD,
            "cloud_time": current.get("time"),
            "evaluated_at": moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "dark": dark,
        }
