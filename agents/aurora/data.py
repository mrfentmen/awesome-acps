"""Read-only reader for NOAA's Space Weather Prediction Center (SWPC) products.

Self-contained on purpose: the transport and its small cache live here. Every endpoint
is keyless and was verified live on 2026-09-22:

  /products/noaa-planetary-k-index.json           3-hourly planetary Kp, last ~7 days
  /products/noaa-planetary-k-index-forecast.json  3-hourly Kp, mixing observed and predicted
  /products/alerts.json                           SWPC watches, warnings, alerts and summaries
  /json/planetary_k_index_1m.json                 the 1-minute estimated Kp (right now)
  /json/ovation_aurora_latest.json                aurora probability on a 1-degree global grid

The quirks, all handled here:

1. Four of the five products are bare JSON arrays, not objects with a results wrapper.
2. The forecast file mixes observed and predicted rows in one list; only rows whose
   `observed` field says "predicted" are futures, and only those are forecast.
3. The 1-minute Kp file keeps its latest estimate under `estimated_kp`.
4. OVATION is a ~900 KB grid of [longitude, latitude, probability] triples for the whole
   planet every degree, so it is cached hard and only the neighbourhood around the
   asked-for point is read.
5. SWPC message text is CRLF-separated with the code on the first line and the headline
   after "Issue Time"; both are parsed out here so callers get fields, not a blob.
6. The alerts feed is newest-first today, but file order is not trusted: messages are
   sorted on their fixed-width issue timestamp.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from urllib import error as urlerror
from urllib import parse, request

BASE_URL = "https://services.swpc.noaa.gov"

KP_3H = "/products/noaa-planetary-k-index.json"
KP_FORECAST = "/products/noaa-planetary-k-index-forecast.json"
ALERTS = "/products/alerts.json"
KP_1M = "/json/planetary_k_index_1m.json"
OVATION = "/json/ovation_aurora_latest.json"

DATASET_KP = "swpc.noaa.gov/planetary-k-index"
DATASET_FORECAST = "swpc.noaa.gov/planetary-k-index-forecast"
DATASET_ALERTS = "swpc.noaa.gov/alerts"
DATASET_KP_1M = "swpc.noaa.gov/planetary-k-index-1m"
DATASET_OVATION = "swpc.noaa.gov/ovation-aurora"
DEFAULT_USER_AGENT = "acp-aurora/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: NOAA's geomagnetic storm scale: Kp 5 and up is a storm, graded G1-G5.
STORM_SCALE = {
    5: "G1 (minor)",
    6: "G2 (moderate)",
    7: "G3 (strong)",
    8: "G4 (severe)",
    9: "G5 (extreme)",
}

#: Message kinds SWPC issues, longest first so "EXTENDED WARNING" wins over "WARNING".
MESSAGE_KINDS = ("EXTENDED WARNING", "WARNING", "WATCH", "ALERT", "SUMMARY", "CANCELLATION", "CANCEL")

#: Coordinates for places people ask about, so callers need not know their latitude.
#: Reference geography shipped with the agent, not data from NOAA.
CITY_COORDS: dict[str, tuple[float, float]] = {
    "new york": (40.71, -74.01), "boston": (42.36, -71.06), "philadelphia": (39.95, -75.17),
    "washington": (38.91, -77.04), "atlanta": (33.75, -84.39), "chicago": (41.88, -87.63),
    "detroit": (42.33, -83.05), "minneapolis": (44.98, -93.27), "st louis": (38.63, -90.20),
    "kansas city": (39.10, -94.58), "cleveland": (41.50, -81.69), "pittsburgh": (40.44, -79.99),
    "buffalo": (42.89, -78.88), "burlington": (44.48, -73.21), "denver": (39.74, -104.99),
    "salt lake city": (40.76, -111.89), "boise": (43.62, -116.20), "billings": (45.78, -108.50),
    "fargo": (46.88, -96.79), "duluth": (46.79, -92.10), "seattle": (47.61, -122.33),
    "portland": (45.52, -122.68), "san francisco": (37.77, -122.42), "los angeles": (34.05, -118.24),
    "phoenix": (33.45, -112.07), "dallas": (32.78, -96.80), "miami": (25.76, -80.19),
    "honolulu": (21.31, -157.86), "anchorage": (61.22, -149.90), "fairbanks": (64.84, -147.72),
    "toronto": (43.65, -79.38), "montreal": (45.50, -73.57), "vancouver": (49.28, -123.12),
    "winnipeg": (49.90, -97.14), "saskatoon": (52.13, -106.67), "yellowknife": (62.45, -114.37),
    "reykjavik": (64.15, -21.94), "tromso": (69.65, -18.96), "oslo": (59.91, 10.75),
    "stockholm": (59.33, 18.07), "helsinki": (60.17, 24.94), "copenhagen": (55.68, 12.57),
    "edinburgh": (55.95, -3.19), "dublin": (53.35, -6.26), "london": (51.51, -0.13),
    "berlin": (52.52, 13.40), "warsaw": (52.23, 21.01), "kyiv": (50.45, 30.52),
    "moscow": (55.76, 37.62), "murmansk": (68.97, 33.08), "tokyo": (35.68, 139.69),
    "beijing": (39.90, 116.41), "sapporo": (43.06, 141.35), "sydney": (-33.87, 151.21),
    "melbourne": (-37.81, 144.96), "auckland": (-36.85, 174.76), "dunedin": (-45.87, 170.50),
    "cape town": (-33.92, 18.42), "buenos aires": (-34.60, -58.38), "ushuaia": (-54.80, -68.30),
}

_CODE_RE = re.compile(r"Space Weather Message Code:\s*(\S+)")
_SERIAL_RE = re.compile(r"Serial Number:\s*(\d+)")
_ISSUE_RE = re.compile(r"Issue Time:\s*([^\r\n]+)")
_KIND_RE = re.compile(r"\b(" + "|".join(kind.replace(" ", r"\s+") for kind in MESSAGE_KINDS) + r")\b")
_GSCALE_RE = re.compile(r"\bG([1-5])\b")


class SpaceWeatherError(RuntimeError):
    """SWPC could not be read."""


def kp_band(kp: float | None) -> str:
    """Plain words for a Kp value, using NOAA's own storm grading from 5 up."""
    if kp is None:
        return "unknown"
    if kp >= 5:
        return STORM_SCALE.get(int(min(round(kp), 9)), "G5 (extreme)")
    if kp >= 4:
        return "active"
    if kp >= 3:
        return "unsettled"
    return "quiet"


def is_storm(kp: float | None) -> bool:
    return kp is not None and kp >= 5


def parse_message(row: dict) -> dict | None:
    """One raw SWPC product -> fields. Returns None when the row has no message text."""
    text = str(row.get("message") or "")
    if not text.strip():
        return None
    code_match = _CODE_RE.search(text)
    serial_match = _SERIAL_RE.search(text)
    kind_match = _KIND_RE.search(text)
    scale = _GSCALE_RE.search(text)
    lines = [line.strip() for line in text.replace("\r\n", "\n").split("\n")]
    headline = ""
    for index, line in enumerate(lines):
        if line.lower().startswith("issue time"):
            headline = next((candidate for candidate in lines[index + 1:] if candidate), "")
            break
    return {
        "dataset": DATASET_ALERTS,
        "product_id": row.get("product_id"),
        "issue_datetime": row.get("issue_datetime"),
        "code": code_match.group(1) if code_match else None,
        "serial": serial_match.group(1) if serial_match else None,
        "kind": kind_match.group(1).upper() if kind_match else None,
        "g_scale": f"G{scale.group(1)}" if scale else None,
        "headline": (headline or lines[0])[:200],
        "text": text,
    }


class SpaceWeatherData:
    """SWPC products over HTTP: injectable fetch, cached per query, threadsafe."""

    def __init__(self, fetch=None, base_url: str | None = None, cache_ttl: float | None = None,
                 timeout: float | None = None, user_agent: str | None = None) -> None:
        env = os.environ
        self.base_url = (base_url or env.get("AURORA_BASE_URL") or BASE_URL).rstrip("/")
        self.cache_ttl = float(cache_ttl if cache_ttl is not None else env.get("AURORA_CACHE_TTL", "300"))
        self.timeout = float(timeout if timeout is not None else env.get("AURORA_HTTP_TIMEOUT", "25"))
        self.user_agent = user_agent or env.get("AURORA_USER_AGENT") or DEFAULT_USER_AGENT
        self._fetch = fetch or self._http_get
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.RLock()

    # -- transport ---------------------------------------------------------

    def _http_get(self, path: str, params: dict):
        query = parse.urlencode(params) if params else ""
        full = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        req = request.Request(full, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urlerror.HTTPError as exc:
            raise SpaceWeatherError(f"SWPC answered {exc.code} for {path}") from exc
        except Exception as exc:  # urllib raises many types; callers see one
            raise SpaceWeatherError(f"SWPC request failed: {exc}") from exc

    def _get(self, path: str, params: dict | None = None, ttl: float | None = None):
        params = params or {}
        key = f"{path}:{json.dumps({k: str(v) for k, v in sorted(params.items())})}"
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        payload = self._fetch(path, params)
        with self._lock:
            self._cache[key] = (now + (ttl if ttl is not None else self.cache_ttl), payload)
        return payload

    @staticmethod
    def _rows(payload) -> list[dict]:
        if not isinstance(payload, list):
            raise SpaceWeatherError("SWPC returned a payload that is not a list of rows")
        return [row for row in payload if isinstance(row, dict)]

    # -- validation --------------------------------------------------------

    @staticmethod
    def check_lat(value, name: str = "latitude") -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number") from None
        if not -90 <= number <= 90:
            raise ValueError(f"{name} must be between -90 and 90")
        return number

    @staticmethod
    def check_lon(value, name: str = "longitude") -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number") from None
        if not -180 <= number <= 180:
            raise ValueError(f"{name} must be between -180 and 180")
        return number

    @staticmethod
    def check_point(value) -> tuple[float, float]:
        parts = re.sub(r"\s+", "", str(value or "")).split(",")
        if len(parts) != 2:
            raise ValueError("a point must look like 64.84,-147.72")
        return SpaceWeatherData.check_lat(parts[0]), SpaceWeatherData.check_lon(parts[1])

    @staticmethod
    def city(name: str) -> tuple[float, float, str] | None:
        key = " ".join(str(name or "").lower().replace("-", " ").strip().split())
        if key in CITY_COORDS:
            lat, lon = CITY_COORDS[key]
            return lat, lon, key.title()
        for known, coords in CITY_COORDS.items():
            if len(key) >= 4 and (key in known or known in key):
                return coords[0], coords[1], known.title()
        return None

    # -- reads -------------------------------------------------------------

    def kp_now(self) -> dict:
        """The current 1-minute estimated Kp, with the latest 3-hourly row alongside it."""
        minute = self._get(KP_1M, ttl=min(self.cache_ttl, 120))
        rows = self._rows(minute)
        latest_minute = rows[-1] if rows else {}
        kp = latest_minute.get("estimated_kp")
        kp = float(kp) if kp is not None else None
        three_hourly = self.kp_rows(limit=1)
        latest_3h = three_hourly[0] if three_hourly else {}
        return {
            "dataset": DATASET_KP_1M,
            "time_tag": latest_minute.get("time_tag"),
            "estimated_kp": kp,
            "band": kp_band(kp),
            "storm": is_storm(kp),
            "three_hourly": {
                "dataset": DATASET_KP,
                "time_tag": latest_3h.get("time_tag"),
                "kp": latest_3h.get("Kp"),
                "a_running": latest_3h.get("a_running"),
                "station_count": latest_3h.get("station_count"),
            },
        }

    def kp_rows(self, limit: int = 8) -> list[dict]:
        """The most recent 3-hourly Kp rows (8 rows is 24 hours)."""
        rows = self._rows(self._get(KP_3H, ttl=min(self.cache_ttl, 300)))
        return rows[-max(1, int(limit)):]

    def recent(self, hours: int = 24) -> dict:
        """Current conditions plus the peak of the last `hours`, from the 3-hourly rows."""
        wanted = self._rows(self._get(KP_3H, ttl=min(self.cache_ttl, 300)))[-max(1, int(hours) // 3):]
        peak = max((float(row.get("Kp") or 0) for row in wanted), default=None)
        peak_row = next((row for row in reversed(wanted) if float(row.get("Kp") or 0) >= (peak or 0)), None)
        return {
            "now": self.kp_now(),
            "window_hours": hours,
            "peak_kp": peak,
            "peak_band": kp_band(peak),
            "peak_time_tag": (peak_row or {}).get("time_tag"),
            "rows": wanted,
            "dataset": DATASET_KP,
        }

    def forecast(self, days: int = 3) -> dict:
        """Predicted Kp for the next days, grouped by UTC day, highest first per day."""
        rows = self._rows(self._get(KP_FORECAST, ttl=min(self.cache_ttl, 900)))
        predicted = [row for row in rows if str(row.get("observed") or "").lower() == "predicted"]
        by_day: dict[str, dict] = {}
        for row in predicted:
            day = str(row.get("time_tag") or "")[:10]
            if not day:
                continue
            kp = float(row.get("kp") or 0)
            entry = by_day.setdefault(day, {"date": day, "max_kp": kp, "rows": []})
            entry["rows"].append({"time_tag": row.get("time_tag"), "kp": kp})
            if kp > entry["max_kp"]:
                entry["max_kp"] = kp
        ordered = [by_day[day] for day in sorted(by_day)][: max(1, int(days))]
        for entry in ordered:
            entry["band"] = kp_band(entry["max_kp"])
            entry["storm"] = is_storm(entry["max_kp"])
        peak = max((entry["max_kp"] for entry in ordered), default=None)
        return {
            "dataset": DATASET_FORECAST,
            "days": ordered,
            "predicted_rows": len(predicted),
            "observed_rows": len(rows) - len(predicted),
            "peak_kp": peak,
            "peak_band": kp_band(peak),
        }

    def messages(self, limit: int = 5, contains: str | None = None) -> dict:
        """SWPC watches, warnings, alerts and summaries, newest first, parsed into fields."""
        rows = self._rows(self._get(ALERTS, ttl=min(self.cache_ttl, 300)))
        parsed = [item for item in (parse_message(row) for row in rows) if item]
        if contains:
            needle = str(contains).lower()
            parsed = [item for item in parsed
                      if needle in item["text"].lower() or needle in (item["headline"] or "").lower()
                      or needle in (item["kind"] or "").lower()]
        parsed.sort(key=lambda item: str(item.get("issue_datetime") or ""), reverse=True)
        keep = max(1, int(limit))
        return {"dataset": DATASET_ALERTS, "count": len(parsed), "messages": parsed[:keep]}

    def aurora_probability(self, lat: float, lon: float, radius: float = 2.0) -> dict:
        """The OVATION model's aurora probability at (or near) a point, right now."""
        lat = self.check_lat(lat)
        lon = self.check_lon(lon)
        grid = self._get(OVATION, ttl=min(self.cache_ttl, 900))
        coordinates = grid.get("coordinates") if isinstance(grid, dict) else None
        if not coordinates:
            raise SpaceWeatherError("the OVATION grid was empty")
        lon_norm = lon % 360
        best: tuple[float, int, int, int] | None = None  # distance, lon step, lat step, probability
        near: list[int] = []
        for lon_step, lat_step, probability in coordinates:
            if abs(lat_step - lat) > radius:
                continue
            delta_lon = abs(lon_step - lon_norm)
            delta_lon = min(delta_lon, 360 - delta_lon)
            if delta_lon > radius:
                continue
            near.append(int(probability))
            distance = (lat_step - lat) ** 2 + delta_lon ** 2
            if best is None or distance < best[0]:
                best = (distance, int(lon_step), int(lat_step), int(probability))
        if best is None:
            raise SpaceWeatherError("the OVATION grid had no cell near that point")
        return {
            "dataset": DATASET_OVATION,
            "observation_time": grid.get("Observation Time"),
            "forecast_time": grid.get("Forecast Time"),
            "probability": best[3],
            "nearest_cell": {"latitude": best[2], "longitude": best[1]},
            "radius_degrees": radius,
            "max_probability_nearby": max(near) if near else best[3],
            "cells_read": len(coordinates),
        }
