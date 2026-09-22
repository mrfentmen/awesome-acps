"""Read-only weather reader for the forecast agent.

Self-contained on purpose: the Open-Meteo transport lives here with one small cache.

  https://api.open-meteo.com/v1/forecast   hourly + daily weather for any point

Keyless, verified live on 2026-09-22. The rain part matters most: instead of "60% chance
today", this reader turns the hourly probability series into rain windows - the actual
hours the model expects rain - so the answer can say "roughly 14:00 to 17:00, peak 70%".

Weather codes come back as WMO numbers; the table below is the WMO 4677 interpretation,
copied out so the answer can print words instead of a code.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from urllib import parse, request

BASE_URL = "https://api.open-meteo.com/v1/forecast"
DATASET = "api.open-meteo.com/v1/forecast"

DEFAULT_USER_AGENT = "awesome-acps-forecast/1.0 (+https://github.com/mrfentmen/awesome-acps)"

CURRENT_FIELDS = ("temperature_2m", "apparent_temperature", "relative_humidity_2m",
                  "precipitation", "weather_code", "wind_speed_10m", "wind_direction_10m",
                  "is_day")
HOURLY_FIELDS = ("temperature_2m", "precipitation", "precipitation_probability", "wind_speed_10m")
DAILY_FIELDS = ("temperature_2m_max", "temperature_2m_min", "precipitation_sum",
                "precipitation_probability_max", "wind_speed_10m_max", "weather_code")

#: WMO 4677 weather codes, as Open-Meteo documents them.
WMO_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    56: "light freezing drizzle", 57: "dense freezing drizzle",
    61: "slight rain", 63: "moderate rain", 65: "heavy rain",
    66: "light freezing rain", 67: "heavy freezing rain",
    71: "slight snowfall", 73: "moderate snowfall", 75: "heavy snowfall", 77: "snow grains",
    80: "slight rain showers", 81: "moderate rain showers", 82: "violent rain showers",
    85: "slight snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with slight hail", 99: "thunderstorm with heavy hail",
}

#: WMO codes that mean rain or snow is falling.
WET_CODES = frozenset({51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99})

_POINT_RE = re.compile(r"\b(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\b")

#: Metro centres, so a place name works without coordinates.
CITY_POINTS = {
    "atlanta": (33.75, -84.39), "bangkok": (13.76, 100.50), "beijing": (39.90, 116.41),
    "berlin": (52.52, 13.40), "bogota": (-4.71, -74.07), "boston": (42.36, -71.06),
    "buenos aires": (-34.60, -58.38), "cairo": (30.04, 31.24), "chicago": (41.88, -87.63),
    "delhi": (28.61, 77.21), "denver": (39.74, -104.99), "dublin": (53.35, -6.26),
    "edinburgh": (55.95, -3.19), "houston": (29.76, -95.37), "istanbul": (41.01, 28.98),
    "johannesburg": (-26.20, 28.05), "lagos": (6.52, 3.38), "lima": (-12.05, -77.04),
    "london": (51.51, -0.13), "los angeles": (34.05, -118.24), "madrid": (40.42, -3.70),
    "manila": (14.60, 120.98), "mexico city": (19.43, -99.13), "miami": (25.76, -80.19),
    "milan": (45.46, 9.19), "minneapolis": (44.98, -93.27), "moscow": (55.76, 37.62),
    "mumbai": (19.08, 72.88), "nairobi": (-1.29, 36.82), "new york": (40.71, -74.01),
    "paris": (48.86, 2.35), "phoenix": (33.45, -112.07), "rome": (41.90, 12.50),
    "san francisco": (37.77, -122.42), "seattle": (47.61, -122.33), "seoul": (37.57, 126.98),
    "singapore": (1.35, 103.82), "stockholm": (59.33, 18.07), "sydney": (-33.87, 151.21),
    "tokyo": (35.68, 139.69), "toronto": (43.65, -79.38), "vancouver": (49.28, -123.12),
}


class ForecastError(RuntimeError):
    """The weather feed could not be read."""


class ForecastData:
    """Open-Meteo weather, read over HTTP with caching and validation."""

    def __init__(self, fetch=None, cache_ttl: float = 300.0, timeout: float = 20.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("FORECAST_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("FORECAST_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("FORECAST_USER_AGENT") or DEFAULT_USER_AGENT
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
            raise ForecastError(f"weather request failed: {exc}") from exc

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
            raise ValueError("a point is 'lat,lon', for example 51.51,-0.13")
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

    @staticmethod
    def check_hours(value) -> int:
        try:
            hours = int(value)
        except (TypeError, ValueError):
            raise ValueError("hours must be a whole number") from None
        if not 1 <= hours <= 72:
            raise ValueError("hours must be between 1 and 72")
        return hours

    @staticmethod
    def check_days(value) -> int:
        try:
            days = int(value)
        except (TypeError, ValueError):
            raise ValueError("days must be a whole number") from None
        if not 1 <= days <= 16:
            raise ValueError("days must be between 1 and 16")
        return days

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
            raise ForecastError("Open-Meteo returned an unexpected payload (no current block)")
        reading = {field: current[field] for field in CURRENT_FIELDS if field in current}
        code = reading.get("weather_code")
        return {
            "dataset": DATASET,
            "point": location,
            "time": current.get("time"),
            "current": reading,
            "weather": WMO_CODES.get(code, "unknown weather code" if code is not None else "not reported"),
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
            raise ForecastError("Open-Meteo returned an unexpected payload (no hourly block)")
        rows = []
        for index, stamp in enumerate(hourly["time"]):
            row = {"time": stamp}
            for field in HOURLY_FIELDS:
                values = hourly.get(field) or []
                row[field] = values[index] if index < len(values) else None
            rows.append(row)
        rows = rows[:window]
        if not rows:
            raise ForecastError("Open-Meteo returned no hourly rows")
        return {"dataset": DATASET, "point": location, "window_hours": len(rows), "rows": rows}

    def daily(self, point: str, days: int = 3) -> dict:
        location = self.check_point(point)
        window = self.check_days(days)
        payload = self._get(BASE_URL, {
            "latitude": location.split(",")[0],
            "longitude": location.split(",")[1],
            "daily": ",".join(DAILY_FIELDS),
            "forecast_days": str(window),
            "timezone": "UTC",
        }, ttl=min(self.cache_ttl, 900))
        daily = payload.get("daily") if isinstance(payload, dict) else None
        if not isinstance(daily, dict) or not daily.get("time"):
            raise ForecastError("Open-Meteo returned an unexpected payload (no daily block)")
        rows = []
        for index, day in enumerate(daily["time"]):
            row = {"date": day}
            for field in DAILY_FIELDS:
                values = daily.get(field) or []
                row[field] = values[index] if index < len(values) else None
            code = row.get("weather_code")
            row["weather"] = WMO_CODES.get(code, "unknown weather code" if code is not None else "not reported")
            rows.append(row)
        return {"dataset": DATASET, "point": location, "days": rows[:window]}

    # -- rain windows ------------------------------------------------------

    @staticmethod
    def rain_windows(rows: list[dict], threshold: int = 40) -> list[dict]:
        """Group consecutive hours that look wet into windows.

        An hour counts as wet when the model gives it at least `threshold` percent
        probability or any measurable amount. Windows shorter than two hours still count:
        a single-hour downpour is exactly what people want to be told about.
        """
        if not 1 <= int(threshold) <= 100:
            raise ValueError("a rain probability threshold is 1..100 percent")
        windows: list[dict] = []
        current: list[dict] = []
        for row in rows:
            probability = row.get("precipitation_probability")
            amount = row.get("precipitation")
            wet = (probability is not None and probability >= threshold) or (amount or 0) > 0.1
            if wet:
                current.append(row)
            elif current:
                windows.append(ForecastData._window(current))
                current = []
        if current:
            windows.append(ForecastData._window(current))
        return windows

    @staticmethod
    def _window(hours: list[dict]) -> dict:
        probabilities = [hour.get("precipitation_probability") or 0 for hour in hours]
        amounts = [hour.get("precipitation") or 0 for hour in hours]
        return {
            "start": hours[0]["time"],
            "end": hours[-1]["time"],
            "hours": len(hours),
            "peak_probability": max(probabilities),
            "total_mm": round(sum(amounts), 2),
        }
