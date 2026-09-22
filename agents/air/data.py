"""Read-only air quality reader for the air agent.

Self-contained on purpose: the Open-Meteo transport lives here with one small cache.

  https://air-quality-api.open-meteo.com/v1/air-quality   CAMS/Open-Meteo air quality

Keyless, verified live on 2026-09-22 (Kansas returned AQI 144 for a smoke plume that
morning). The API is a weather-style forecast endpoint: `current=` gives one reading,
`hourly=` gives the next days, both for a latitude/longitude point.

Two different AQI scales come back and they are not comparable: `us_aqi` is the US EPA
scale, `european_aqi` is the EEA scale. This reader keeps the provider's own numbers and
only labels them; the band tables below are the published EPA categories, spelled out so
the answer can say what the number means.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from urllib import parse, request

BASE_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
DATASET = "air-quality-api.open-meteo.com/v1/air-quality"

DEFAULT_USER_AGENT = "awesome-acps-air/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: Current reading fields, exactly as Open-Meteo names them.
CURRENT_FIELDS = (
    "pm2_5", "pm10", "us_aqi", "european_aqi", "ozone", "nitrogen_dioxide",
    "sulphur_dioxide", "carbon_monoxide", "uv_index",
)
HOURLY_FIELDS = ("pm2_5", "pm10", "us_aqi")

#: US EPA AQI categories (upper bound inclusive, label).
AQI_BANDS = (
    (50, "Good"),
    (100, "Moderate"),
    (150, "Unhealthy for Sensitive Groups"),
    (200, "Unhealthy"),
    (300, "Very Unhealthy"),
    (500, "Hazardous"),
)

#: PM2.5 in ug/m3, against the WHO 24-hour guideline of 15.
PM25_BANDS = ((15, "within the WHO guideline"), (35, "above the WHO guideline"),
              (55, "well above the WHO guideline"), (float("inf"), "very high"))

_POINT_RE = re.compile(r"\b(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\b")

#: Metro centres, so a place name works without coordinates.
CITY_POINTS = {
    "atlanta": (33.75, -84.39), "bangkok": (13.76, 100.50), "beijing": (39.90, 116.41),
    "berlin": (52.52, 13.40), "bogota": (-4.71, -74.07), "boston": (42.36, -71.06),
    "buenos aires": (-34.60, -58.38), "cairo": (30.04, 31.24), "chicago": (41.88, -87.63),
    "delhi": (28.61, 77.21), "denver": (39.74, -104.99), "detroit": (42.33, -83.05),
    "houston": (29.76, -95.37), "istanbul": (41.01, 28.98), "jakarta": (-6.21, 106.85),
    "johannesburg": (-26.20, 28.05), "karachi": (24.86, 67.01), "lagos": (6.52, 3.38),
    "lahore": (31.55, 74.34), "las vegas": (36.17, -115.14), "lima": (-12.05, -77.04),
    "london": (51.51, -0.13), "los angeles": (34.05, -118.24), "madrid": (40.42, -3.70),
    "manila": (14.60, 120.98), "mexico city": (19.43, -99.13), "miami": (25.76, -80.19),
    "milan": (45.46, 9.19), "minneapolis": (44.98, -93.27), "moscow": (55.76, 37.62),
    "mumbai": (19.08, 72.88), "nairobi": (-1.29, 36.82), "new york": (40.71, -74.01),
    "paris": (48.86, 2.35), "philadelphia": (39.95, -75.17), "phoenix": (33.45, -112.07),
    "portland": (45.52, -122.68), "rome": (41.90, 12.50), "salt lake city": (40.76, -111.89),
    "san francisco": (37.77, -122.42), "santiago": (-33.45, -70.67), "sao paulo": (-23.55, -46.63),
    "seattle": (47.61, -122.33), "seoul": (37.57, 126.98), "shanghai": (31.23, 121.47),
    "singapore": (1.35, 103.82), "sydney": (-33.87, 151.21), "tokyo": (35.68, 139.69),
    "toronto": (43.65, -79.38), "vancouver": (49.28, -123.12), "warsaw": (52.23, 21.01),
}


class AirQualityError(RuntimeError):
    """The air quality feed could not be read."""


def aqi_band(aqi: float | None) -> str:
    """The EPA category name for a US AQI number."""
    if aqi is None:
        return "not reported"
    for ceiling, label in AQI_BANDS:
        if aqi <= ceiling:
            return label
    return "beyond the scale"


def pm25_band(value: float | None) -> str:
    """A plain reading of a PM2.5 number against the WHO guideline."""
    if value is None:
        return "not reported"
    for ceiling, label in PM25_BANDS:
        if value <= ceiling:
            return label
    return "very high"


class AirQualityData:
    """Open-Meteo air quality, read over HTTP with caching and validation."""

    def __init__(self, fetch=None, cache_ttl: float = 600.0, timeout: float = 20.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("AIR_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("AIR_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("AIR_USER_AGENT") or DEFAULT_USER_AGENT
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
            raise AirQualityError(f"air quality request failed: {exc}") from exc

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
    def point_from_place(place: str) -> str | None:
        """A point for a place name in the built-in list, else None."""
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

    @staticmethod
    def check_hours(value) -> int:
        try:
            hours = int(value)
        except (TypeError, ValueError):
            raise ValueError("hours must be a whole number") from None
        if not 1 <= hours <= 72:
            raise ValueError("hours must be between 1 and 72")
        return hours

    # -- readings ----------------------------------------------------------

    def current(self, point: str) -> dict:
        location = self.check_point(point)
        payload = self._get(BASE_URL, {
            "latitude": location.split(",")[0],
            "longitude": location.split(",")[1],
            "current": ",".join(CURRENT_FIELDS),
            "timezone": "UTC",
        }, ttl=min(self.cache_ttl, 600))
        current = payload.get("current") if isinstance(payload, dict) else None
        if not isinstance(current, dict):
            raise AirQualityError("Open-Meteo returned an unexpected payload (no current block)")
        reading = {field: current[field] for field in CURRENT_FIELDS if field in current}
        return {
            "dataset": DATASET,
            "point": location,
            "time": current.get("time"),
            "current": reading,
            "us_aqi": reading.get("us_aqi"),
            "us_aqi_band": aqi_band(reading.get("us_aqi")),
            "european_aqi": reading.get("european_aqi"),
            "pm2_5_band": pm25_band(reading.get("pm2_5")),
        }

    def hourly(self, point: str, hours: int = 24) -> dict:
        location = self.check_point(point)
        window = self.check_hours(hours)
        payload = self._get(BASE_URL, {
            "latitude": location.split(",")[0],
            "longitude": location.split(",")[1],
            "hourly": ",".join(HOURLY_FIELDS),
            "forecast_days": str(max(1, min(3, (window + 23) // 24))),
            "timezone": "UTC",
        }, ttl=min(self.cache_ttl, 900))
        hourly = payload.get("hourly") if isinstance(payload, dict) else None
        if not isinstance(hourly, dict) or not hourly.get("time"):
            raise AirQualityError("Open-Meteo returned an unexpected payload (no hourly block)")
        rows = []
        for index, stamp in enumerate(hourly["time"]):
            row = {"time": stamp}
            for field in HOURLY_FIELDS:
                values = hourly.get(field) or []
                row[field] = values[index] if index < len(values) else None
            rows.append(row)
        rows = rows[:window]
        if not rows:
            raise AirQualityError("Open-Meteo returned no hourly rows")
        peak = max(rows, key=lambda row: row.get("pm2_5") if row.get("pm2_5") is not None else -1)
        dirty_hours = [row for row in rows if (row.get("us_aqi") or 0) > 100]
        return {
            "dataset": DATASET,
            "point": location,
            "window_hours": len(rows),
            "rows": rows,
            "peak_pm2_5": peak.get("pm2_5"),
            "peak_pm2_5_time": peak.get("time"),
            "worst_aqi": max((row.get("us_aqi") or 0) for row in rows),
            "hours_above_aqi_100": len(dirty_hours),
            "first_dirty_hour": dirty_hours[0]["time"] if dirty_hours else None,
        }
