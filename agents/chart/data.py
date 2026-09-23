"""The real data behind a charted series, and the windows that make them honest.

Four keyless sources, all verified live on 2026-09-23:

  geocoding-api.open-meteo.com        a place name to coordinates
  api.open-meteo.com/v1/forecast      hourly temperature and precipitation
  air-quality-api.open-meteo.com      hourly US AQI
  api.fiscaldata.treasury.gov         the national debt, day by day (40.1 trillion today)
  earthquake.usgs.gov                 quakes above M2.5 in the past day

Two decisions here are about not faking a chart:

  1. **The window starts at the place's own now.** Open-Meteo returns `utc_offset_seconds`, so
     the first charted hour is the first hour at or after the local time *there*, not here.
     Without that, a chart of 'the next 24 hours' in Tokyo starts eight hours off.
  2. **Long windows are thinned, not truncated.** A week of hourly data is 168 points, which
     is more than 640 pixels can show honestly, so every Nth point is kept and the subtitle
     says the chart is sampled rather than pretending it is every hour.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import threading
import time
from urllib import parse, request

GEOCODING = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY = "https://air-quality-api.open-meteo.com/v1/air-quality"
TREASURY = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2/accounting/od/debt_to_penny"
QUAKES = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"

WEATHER_DATASET = "Open-Meteo (open-meteo.com)"
AIR_DATASET = "Open-Meteo air quality (air-quality-api.open-meteo.com)"
DEBT_DATASET = "US Treasury Fiscal Data, debt to the penny (fiscaldata.treasury.gov)"
QUAKE_DATASET = "USGS earthquakes (earthquake.usgs.gov)"

DEFAULT_USER_AGENT = "awesome-acps-chart/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: More points than this and a 640px wide chart is drawing lies, so the series is sampled.
MAX_POINTS = 120

KINDS = ("temperature", "rain", "air", "debt", "quakes")


class ChartError(RuntimeError):
    """A chart's data could not be read."""


def local_now(utc_offset_seconds) -> datetime.datetime:
    """The wall clock at a place, from its UTC offset. Falls back to this machine's time."""
    try:
        offset = int(utc_offset_seconds)
    except (TypeError, ValueError):
        return datetime.datetime.now()
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(seconds=offset)


def sample(labels: list[str], values: list[float], limit: int = MAX_POINTS,
           align_last: bool = False) -> tuple[list[str], list[float], int]:
    """Thin a series to at most `limit` points, evenly. Returns (labels, values, step)."""
    count = len(values)
    if count <= limit:
        return list(labels), list(values), 1
    step = max(1, math.ceil(count / limit))
    # With align_last the series ends on its real last point, which is the one a reader looks
    # for; otherwise it starts on the first.
    start = (count - 1) % step if align_last else 0
    kept_labels = labels[start::step]
    kept_values = values[start::step]
    if align_last and kept_values and kept_values[-1] != values[-1]:
        kept_labels, kept_values = kept_labels + [labels[-1]], kept_values + [values[-1]]
    return kept_labels, kept_values, step


class ChartData:
    """The series this agent can draw, with one small cache."""

    def __init__(self, fetch=None, cache_ttl: float = 600.0, timeout: float = 25.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("CHART_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("CHART_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("CHART_USER_AGENT") or DEFAULT_USER_AGENT
        self._fetch = fetch or self._http_text
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    # -- transport ---------------------------------------------------------

    def _http_text(self, url: str, params: dict) -> str:
        query = f"{url}?{parse.urlencode(params)}" if params else url
        req = request.Request(query, headers={"User-Agent": self.user_agent, "Accept": "*/*"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # urllib raises many types; callers see one error type
            raise ChartError(f"request failed: {exc}") from exc

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
                raise ChartError(f"the data source returned something that is not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ChartError("the data source returned an unexpected payload")
        with self._lock:
            self._cache[key] = (now + (ttl if ttl is not None else self.cache_ttl), payload)
        return payload

    # -- places ------------------------------------------------------------

    def geocode(self, place: str) -> dict:
        """A place name to coordinates, for the weather and air series."""
        query = str(place or "").strip()
        if not query:
            raise ValueError("give me a place to chart, for example Seattle")
        payload = self._json(GEOCODING, {"name": query, "count": 1, "language": "en",
                                         "format": "json"}, ttl=86400.0)
        results = payload.get("results") or []
        if not results:
            raise ValueError(f"no place called {query!r} was found")
        best = results[0]
        return {
            "query": query,
            "name": best.get("name") or query,
            "country": best.get("country") or "",
            "admin": best.get("admin1") or "",
            "latitude": float(best["latitude"]),
            "longitude": float(best["longitude"]),
            "timezone": best.get("timezone") or "",
        }

    # -- series ------------------------------------------------------------

    def _hourly(self, url: str, place: dict, fields: str, hours: int) -> tuple[dict, list[str]]:
        days = max(2, min(16, math.ceil(max(1, int(hours)) / 24) + 1))
        payload = self._json(url, {"latitude": place["latitude"], "longitude": place["longitude"],
                                   "hourly": fields, "forecast_days": days, "timezone": "auto"})
        times = ((payload.get("hourly") or {}).get("time")) or []
        if not times:
            raise ChartError("the hourly forecast came back empty")
        return payload, times

    @staticmethod
    def _window(times: list[str], offset_seconds, hours: int) -> tuple[int, int]:
        """(start, end) indexes for the next `hours` hours, starting with the current one.

        The current hour is included rather than skipped: hourly data at 20:00 describes
        20:00-21:00, and a chart of 'the next 24 hours' that begins at 21:00 quietly drops one.
        """
        now = local_now(offset_seconds).strftime("%Y-%m-%dT%H:00")
        start = 0
        for index, stamp in enumerate(times):
            if stamp >= now:
                start = index
                break
        return start, min(len(times), start + max(1, int(hours)))

    def weather(self, place: dict, metric: str = "temperature", hours: int = 24) -> dict:
        """Hourly temperature or precipitation, from the place's own current hour."""
        payload, times = self._hourly(FORECAST, place, "temperature_2m,precipitation", hours)
        start, end = self._window(times, payload.get("utc_offset_seconds"), hours)
        hourly = payload["hourly"]
        times, values = times[start:end], hourly["temperature_2m"][start:end]
        if metric == "rain":
            values = hourly["precipitation"][start:end]
            unit, title = "mm", f"Rain per hour in {self._where(place)}"
            note = ("Millimetres of precipitation per hour. A gap in the line is dry weather, "
                    "not missing data.")
        else:
            unit, title = "\u00b0C", f"Temperature in {self._where(place)}"
            note = "Air temperature two metres above the ground, in the place's own local time."
        labels = [_short_time(stamp) for stamp in times]
        return {
            "kind": metric,
            "chart": "bar" if metric == "rain" else "line",
            "title": title,
            "subtitle": f"Open-Meteo hourly forecast, {times[0][:10]} {times[0][11:16]} to "
                        f"{times[-1][11:16]} local ({len(values)} hours)",
            "unit": unit,
            "labels": labels,
            "values": [float(value) for value in values],
            "source": WEATHER_DATASET,
            "place": place,
            "note": note,
            "raw_times": times,
        }

    def air(self, place: dict, hours: int = 48) -> dict:
        """Hourly US AQI for a place, from its own current hour."""
        payload, times = self._hourly(AIR_QUALITY, place, "us_aqi,pm2_5", hours)
        start, end = self._window(times, payload.get("utc_offset_seconds"), hours)
        hourly = payload["hourly"]
        times = times[start:end]
        aqi = [None if value is None else float(value) for value in hourly["us_aqi"][start:end]]
        if all(value is None for value in aqi):
            raise ChartError("the air quality forecast came back empty for this place")
        filled = [value if value is not None else 0.0 for value in aqi]
        return {
            "kind": "air",
            "chart": "line",
            "title": f"Air quality in {self._where(place)}",
            "subtitle": f"Open-Meteo hourly US AQI, {times[0][:10]} {times[0][11:16]} to "
                        f"{times[-1][11:16]} local ({len(filled)} hours)",
            "unit": " AQI",
            "labels": [_short_time(stamp) for stamp in times],
            "values": filled,
            "source": AIR_DATASET,
            "place": place,
            "note": ("US AQI: 0-50 good, 51-100 moderate, 101-150 unhealthy for sensitive groups, "
                     "151-200 unhealthy. Hours the model had no value for are drawn as zero, "
                     "which is why the answer names the hours it does have."),
            "raw_times": times,
        }

    def debt(self, days: int = 30) -> dict:
        """The national debt, day by day, oldest first."""
        span = max(5, min(int(days), 365))
        payload = self._json(TREASURY, {"sort": "-record_date", "page[size]": span,
                                        "fields": "record_date,tot_pub_debt_out_amt"})
        rows = payload.get("data") or []
        if not rows:
            raise ChartError("the Treasury returned no debt rows")
        rows = list(reversed(rows))
        dates = [str(row.get("record_date") or "") for row in rows]
        values = []
        for row in rows:
            try:
                values.append(float(row.get("tot_pub_debt_out_amt")))
            except (TypeError, ValueError):
                values.append(0.0)
        return {
            "kind": "debt",
            "chart": "line",
            "title": "US national debt",
            "subtitle": f"Treasury debt to the penny, {dates[0]} to {dates[-1]} "
                        f"({len(values)} daily records)",
            "unit": "",
            "labels": [date[5:] for date in dates],
            "values": values,
            "source": DEBT_DATASET,
            "place": None,
            "note": ("Total public debt outstanding, in dollars, as the Treasury publishes it "
                     "each business day - weekends and holidays have no row."),
            "raw_times": dates,
        }

    def quakes(self, hours: int = 24) -> dict:
        """Earthquakes above M2.5 in the past day, counted per hour."""
        span = max(3, min(int(hours), 24))
        payload = self._json(QUAKES, ttl=120.0)
        features = payload.get("features") or []
        if payload.get("type") != "FeatureCollection":
            raise ChartError("USGS returned an unexpected payload")
        now = datetime.datetime.now(datetime.timezone.utc)
        buckets: dict[str, int] = {}
        order = []
        for index in range(span - 1, -1, -1):
            stamp = (now - datetime.timedelta(hours=index)).strftime("%Y-%m-%dT%H")
            buckets[stamp] = 0
            order.append(stamp)
        counted = 0
        for feature in features:
            properties = feature.get("properties") or {}
            milliseconds = properties.get("time")
            if not isinstance(milliseconds, (int, float)):
                continue
            stamp = datetime.datetime.fromtimestamp(milliseconds / 1000,
                                                    datetime.timezone.utc).strftime("%Y-%m-%dT%H")
            if stamp in buckets:
                buckets[stamp] += 1
                counted += 1
        values = [buckets[stamp] for stamp in order]
        return {
            "kind": "quakes",
            "chart": "bar",
            "title": f"Earthquakes above M2.5 in the past {span} hours",
            "subtitle": f"USGS all-earthquakes feed, counted per hour UTC ({counted} events)",
            "unit": "",
            "labels": [stamp[11:] for stamp in order],
            "values": [float(value) for value in values],
            "source": QUAKE_DATASET,
            "place": None,
            "note": ("Counts per hour, in UTC, from the USGS M2.5+ feed - which is itself a "
                     "subset: smaller quakes are not in this feed at all."),
            "raw_times": order,
        }

    def series(self, kind: str, place: str | None = None, hours: int = 24,
               days: int = 30) -> dict:
        """One series by name, with the labels and values the renderer needs."""
        if kind not in KINDS:
            raise ValueError(f"I can chart: {', '.join(KINDS)}")
        if kind == "debt":
            return self.debt(days=days)
        if kind == "quakes":
            return self.quakes(hours=hours)
        if not place:
            raise ValueError("that chart needs a place, for example 'chart the temperature in "
                             "Seattle for the next 24 hours'")
        resolved = self.geocode(place)
        if kind == "air":
            return self.air(resolved, hours=hours)
        return self.weather(resolved, metric=kind, hours=hours)

    @staticmethod
    def _where(place: dict) -> str:
        bits = [place["name"]]
        if place.get("admin") and place["admin"] != place["name"]:
            bits.append(place["admin"])
        if place.get("country"):
            bits.append(place["country"])
        return ", ".join(bits)


def _short_time(stamp: str) -> str:
    """An ISO hour to something that fits under an axis: '14:00', or '09-24' past a day."""
    text = str(stamp or "")
    if "T" not in text:
        return text[5:10] or text
    return text[11:16] if len(text) >= 16 else text
