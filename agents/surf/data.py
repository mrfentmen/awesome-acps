"""Read-only surf reader: Open-Meteo marine waves plus the matching wind.

Self-contained on purpose: both Open-Meteo transports live here with one small cache.

  https://marine-api.open-meteo.com/v1/marine    significant wave height, period, direction
  https://api.open-meteo.com/v1/forecast         the wind that is shaping those waves

Keyless, verified live on 2026-09-22 (Santa Cruz, 36.95/-122.03: 1.7 m waves, 9.5 s period,
292 degrees, wind 11.7 km/h from 281 degrees).

The interesting judgement in a surf report is wind: the same 1.5 m swell is clean in an
offshore wind and sloppy in an onshore one. Neither API knows where the beach faces, so this
reader does not pretend to. It compares the wind direction with the wave direction: waves
arrive FROM the sea, so wind from the same quarter is onshore, wind from the opposite quarter
is offshore. That is an inference from the data and the answer says so.

Wave-height bands and the groundswell threshold are rules of thumb, spelled out as rules of
thumb - not measurements, and not a promise about any particular beach.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from urllib import parse, request

MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
WIND_URL = "https://api.open-meteo.com/v1/forecast"
DATASET = "marine-api.open-meteo.com/v1/marine + api.open-meteo.com/v1/forecast"

DEFAULT_USER_AGENT = "awesome-acps-surf/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: Words that never belong to a spot name.
_SPOT_STOPWORDS = frozenset({
    "the", "a", "an", "it", "is", "are", "be", "today", "now", "here", "there", "this",
    "next", "hours", "hour", "days", "day", "week", "weekend", "waves", "wave", "surf",
    "surfing", "swell", "conditions", "spots", "spot", "weather", "morning", "afternoon",
    "evening", "night", "right", "doing", "going", "worth",
})

MARINE_CURRENT = "wave_height,wave_direction,wave_period,swell_wave_height,swell_wave_period,wind_wave_height,wind_wave_period"
MARINE_HOURLY = "wave_height,wave_period,wave_direction,swell_wave_height,wind_wave_height"
WIND_CURRENT = "wind_speed_10m,wind_direction_10m,wind_gusts_10m,temperature_2m"

#: Significant wave height bands in metres. Rules of thumb a surfer would recognise, kept
#: coarse on purpose: 1.2 m at a reef is not 1.2 m at a beach break.
WAVE_BANDS = (
    (0.4, "flat", "barely any swell"),
    (1.0, "small", "knee to waist high"),
    (2.0, "fun", "waist to chest high"),
    (3.5, "solid", "overhead"),
    (float("inf"), "big", "well overhead and heavy"),
)

#: A long period means organised groundswell rather than local wind chop.
GROUNDSWELL_PERIOD_S = 10.0

#: A leading \b would sit before the minus sign, not the digit, so '-122.03,37.7' would parse
#: as positive. Lookarounds keep the sign attached to its number.
_POINT_RE = re.compile(
    r"(?<![\d.])(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)(?![\d.])")

#: Well-known breaks and the coast each one sits on, so a name works without coordinates.
SURF_SPOTS = {
    "santa cruz": (36.95, -122.03), "mavericks": (37.49, -122.50), "malibu": (34.03, -118.68),
    "huntington beach": (33.66, -118.00), "san diego": (32.72, -117.17),
    "ocean beach": (37.76, -122.51), "pipeline": (21.66, -158.05), "waikiki": (21.27, -157.83),
    "honolulu": (21.31, -157.86), "jaws": (20.94, -156.30), "nazare": (39.60, -9.07),
    "peniche": (39.36, -9.38), "biarritz": (43.48, -1.56), "hossegor": (43.66, -1.44),
    "mundaka": (43.41, -2.70), "newquay": (50.42, -5.08), "thurso": (58.60, -3.52),
    "bundoran": (54.48, -8.28), "teahupoo": (-17.83, -149.27), "uluwatu": (-8.81, 115.09),
    "kuta": (-8.72, 115.17), "bondi": (-33.89, 151.28), "snapper rocks": (-28.16, 153.55),
    "byron bay": (-28.64, 153.61), "bells beach": (-38.37, 144.28), "jeffreys bay": (-34.05, 24.91),
    "cape town": (-33.92, 18.42), "durban": (-29.86, 31.03), "el sunzal": (13.49, -89.38),
    "punta roca": (-12.24, -76.87), "puerto escondido": (15.86, -97.07),
    "santa teresa": (9.64, -85.17), "sayulita": (20.87, -105.44), "miami": (25.76, -80.13),
    "jersey shore": (39.95, -74.02), "outer banks": (35.25, -75.53), "galveston": (29.30, -94.80),
    "bali": (-8.65, 115.22), "lisbon": (38.72, -9.14), "porto": (41.15, -8.61),
    "sydney": (-33.87, 151.21), "gold coast": (-28.02, 153.40), "tokyo": (35.68, 139.69),
    "los angeles": (33.99, -118.47), "san francisco": (37.77, -122.42), "seattle": (47.61, -122.33),
    "cornwall": (50.47, -5.03), "devon": (50.62, -3.40), "wales": (51.61, -4.00),
    "dublin": (53.35, -6.26), "galway": (53.27, -9.05),
}


class SurfError(RuntimeError):
    """An Open-Meteo marine or wind feed could not be read."""


def compass(degrees) -> str | None:
    """Degrees to a 16-point label: 292 -> 'WNW'. None when the value is missing."""
    labels = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
              "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")
    try:
        return labels[int(float(degrees) % 360 / 22.5 + 0.5) % 16]
    except (TypeError, ValueError):
        return None


def wave_band(height) -> tuple[str, str]:
    """(label, plain description) for a significant wave height in metres."""
    try:
        value = float(height)
    except (TypeError, ValueError):
        return "unknown", "no wave height reported"
    for limit, label, description in WAVE_BANDS:
        if value < limit:
            return label, description
    return "big", "well overhead and heavy"


def wind_relation(wind_from, wave_from) -> tuple[str, str, float | None]:
    """How the wind sits against the swell: (label, why, degrees between them).

    Waves arrive FROM the sea, so a wind out of the same quarter blows onshore and a wind
    from the opposite quarter blows offshore, grooming the wave faces. This is an inference
    from two directions, not knowledge of which way any given beach faces.
    """
    if wind_from is None or wave_from is None:
        return "unknown", "one of the directions was not reported", None
    difference = abs((float(wind_from) - float(wave_from) + 180.0) % 360.0 - 180.0)
    if difference <= 45.0:
        return "onshore", "wind is coming out of the same quarter as the swell, so it is pushing in", difference
    if difference >= 135.0:
        return "offshore", "wind is blowing against the swell, the direction that grooms wave faces", difference
    return "cross-shore", "wind is across the swell, which adds chop to some faces and cleans others", difference


class SurfData:
    """Marine waves and matching wind at a point, with caching and validation."""

    def __init__(self, fetch=None, cache_ttl: float = 900.0, timeout: float = 25.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("SURF_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("SURF_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("SURF_USER_AGENT") or DEFAULT_USER_AGENT
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
            raise SurfError(f"Open-Meteo request failed: {exc}") from exc

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
    def check_point(value) -> str:
        """'36.95,-122.03' or a known spot name -> 'lat,lon' rounded to 4 places."""
        text = str(value or "").strip()
        match = _POINT_RE.search(text)
        if match:
            latitude, longitude = float(match.group(1)), float(match.group(2))
        else:
            key = re.sub(r"[^a-z ]+", " ", text.lower())
            key = " ".join(key.split())
            if key in SURF_SPOTS:
                latitude, longitude = SURF_SPOTS[key]
            else:
                raise ValueError(
                    f"I do not know the spot {value!r}. Give me coordinates like 36.95,-122.03, "
                    "or one of: " + ", ".join(sorted(SURF_SPOTS)[:12]) + ", ...")
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError("latitude must be -90..90 and longitude -180..180")
        return f"{round(latitude, 4)},{round(longitude, 4)}"

    @staticmethod
    def spot_from_text(text: str) -> str | None:
        """A spot named in a sentence, or None. Longest name wins ('santa cruz' > 'cruz').

        When the sentence names something this reader does not know, that phrase is returned
        as typed, so `check_point` can refuse it by name instead of the agent claiming no spot
        was mentioned at all.
        """
        work = str(text)
        lowered = " " + re.sub(r"[^a-z ]+", " ", work.lower()).strip() + " "
        for name in sorted(SURF_SPOTS, key=len, reverse=True):
            if f" {name} " in lowered:
                return name
        match = _POINT_RE.search(work)
        if match:
            return f"{match.group(1)},{match.group(2)}"
        phrase = re.search(
            r"\b(?:at|near|off|for|around)\s+(?:the\s+)?(.+?)"
            r"(?=\s+(?:today|now|right now|tomorrow|this|next|in the next)\b|[?.!,]|$)",
            work, re.IGNORECASE)
        if phrase:
            words = [word for word in re.findall(r"[A-Za-z][\w'-]*", phrase.group(1))
                     if word.lower() not in _SPOT_STOPWORDS]
            if words:
                return " ".join(words[:4])
        return None

    # -- reads -------------------------------------------------------------

    def conditions(self, spot: str) -> dict:
        """Current waves and wind at a point, plus a derived read of the wind and the band."""
        point = self.check_point(spot)
        latitude, longitude = point.split(",")
        marine = self._cached(MARINE_URL, {
            "latitude": latitude, "longitude": longitude, "timezone": "auto",
            "current": MARINE_CURRENT, "forecast_days": 1,
        })
        wind = self._cached(WIND_URL, {
            "latitude": latitude, "longitude": longitude, "timezone": "auto",
            "current": WIND_CURRENT, "forecast_days": 1,
        })
        current = (marine or {}).get("current") or {}
        wind_now = (wind or {}).get("current") or {}
        if not current:
            raise SurfError("Open-Meteo marine returned no current reading for that point")
        relation, why, difference = wind_relation(wind_now.get("wind_direction_10m"),
                                                  current.get("wave_direction"))
        label, description = wave_band(current.get("wave_height"))
        period = current.get("wave_period")
        swell_type = None
        if period is not None:
            swell_type = ("groundswell" if float(period) >= GROUNDSWELL_PERIOD_S else "windswell")
        return {
            "dataset": DATASET,
            "point": point,
            "timezone": (marine or {}).get("timezone"),
            "observed_at": current.get("time"),
            "waves": {
                "height_m": current.get("wave_height"),
                "period_s": period,
                "direction_deg": current.get("wave_direction"),
                "swell_height_m": current.get("swell_wave_height"),
                "wind_wave_height_m": current.get("wind_wave_height"),
            },
            "band": {"label": label, "description": description},
            "swell_type": swell_type,
            "wind": {
                "speed_kmh": wind_now.get("wind_speed_10m"),
                "direction_deg": wind_now.get("wind_direction_10m"),
                "gust_kmh": wind_now.get("wind_gusts_10m"),
                "temperature_c": wind_now.get("temperature_2m"),
            },
            "wind_relation": {"label": relation, "why": why, "degrees_apart": difference},
            "units": (marine or {}).get("current_units") or {},
        }

    def forecast(self, spot: str, hours: int = 48) -> dict:
        """Hourly wave height and period ahead, with the biggest window called out."""
        point = self.check_point(spot)
        latitude, longitude = point.split(",")
        days = max(1, min(int(math.ceil(max(1, min(int(hours), 168)) / 24)), 7))
        marine = self._cached(MARINE_URL, {
            "latitude": latitude, "longitude": longitude, "timezone": "auto",
            "hourly": MARINE_HOURLY, "forecast_days": days,
        })
        hourly = (marine or {}).get("hourly") or {}
        times = hourly.get("time") or []
        heights = hourly.get("wave_height") or []
        periods = hourly.get("wave_period") or []
        directions = hourly.get("wave_direction") or []
        swell = hourly.get("swell_wave_height") or []
        rows = []
        for index, stamp in enumerate(times[: max(1, min(int(hours), 168))]):
            height = heights[index] if index < len(heights) else None
            rows.append({
                "time": stamp,
                "height_m": height,
                "period_s": periods[index] if index < len(periods) else None,
                "direction_deg": directions[index] if index < len(directions) else None,
                "swell_height_m": swell[index] if index < len(swell) else None,
                "band": wave_band(height)[0],
            })
        if not rows:
            raise SurfError("Open-Meteo marine returned no hourly forecast for that point")
        ranked = [row for row in rows if row["height_m"] is not None]
        ranked.sort(key=lambda row: row["height_m"], reverse=True)
        return {
            "dataset": DATASET,
            "point": point,
            "timezone": (marine or {}).get("timezone"),
            "hours": len(rows),
            "rows": rows,
            "biggest": ranked[:5],
        }
