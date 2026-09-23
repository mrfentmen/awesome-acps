"""Pollen counts from the CAMS model, read through Open-Meteo's air-quality API.

Verified live on 2026-09-23 (both keyless):

  https://geocoding-api.open-meteo.com/v1/search        place -> coordinates
  https://air-quality-api.open-meteo.com/v1/air-quality current + hourly pollen

Species and payload, exactly as the API returns them::

  current: {time, interval, alder_pollen, birch_pollen, grass_pollen,
            mugwort_pollen, ragweed_pollen, olive_pollen}   grains/m3
  hourly:  same keys, one value per hour for every day requested

The CAMS pollen model covers **Europe only**. Asked on 2026-09-23, Berlin gave
`grass_pollen: 0.1` while New York and Sydney came back with `null` for every species and no
error at all. That is why this reader keeps a real zero (0.0) apart from "not modelled here"
(None): a place outside the model gets an answer that says so, never a table of zeros.
"""

from __future__ import annotations

import datetime
import json
import os
import time
from urllib import parse, request

GEOCODING = "https://geocoding-api.open-meteo.com/v1/search"
AIR = "https://air-quality-api.open-meteo.com/v1/air-quality"

DATASET = "Open-Meteo air quality, CAMS pollen model (air-quality-api.open-meteo.com)"

DEFAULT_USER_AGENT = "awesome-acps-pollen/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: (api key, what a person calls it). Order is the order they are printed in.
SPECIES = (
    ("alder_pollen", "alder"),
    ("birch_pollen", "birch"),
    ("grass_pollen", "grass"),
    ("mugwort_pollen", "mugwort"),
    ("ragweed_pollen", "ragweed"),
    ("olive_pollen", "olive"),
)

#: Everyday groupings, so "tree pollen" and "weed pollen" mean something.
GROUPS = {
    "tree": ("alder", "birch", "olive"),
    "weed": ("mugwort", "ragweed"),
    "grass": ("grass",),
}

#: Grains/m3 bands. This is the scale public health services publish per species, so the same
#: bands are applied to each one here and the label always travels with the number.
BANDS = ((0.0, "none"), (10.0, "low"), (50.0, "moderate"), (100.0, "high"))
VERY_HIGH = "very high"


class PollenError(RuntimeError):
    """The pollen model could not be read."""


def band(value: float | None) -> str:
    """The band a grains/m3 reading falls in, or 'not available' when the model has no value."""
    if value is None:
        return "not available"
    for limit, label in BANDS:
        if value <= limit:
            return label
    return VERY_HIGH


def species_keys(species: str | None = None, group: str | None = None) -> list[tuple[str, str]]:
    """The (api key, label) pairs a request is about: one species, one group, or all six."""
    if group:
        wanted = GROUPS.get(group)
        if not wanted:
            raise ValueError(f"unknown pollen group {group!r}; I know: {', '.join(GROUPS)}")
        return [(key, label) for key, label in SPECIES if label in wanted]
    if species:
        chosen = [(key, label) for key, label in SPECIES if key.startswith(species)]
        if not chosen:
            raise ValueError(f"unknown pollen species {species!r}; I know: "
                             + ", ".join(label for _key, label in SPECIES))
        return chosen
    return list(SPECIES)


def local_hour(offset_seconds) -> datetime.datetime:
    """Now, in the place's own clock (the API reports utc_offset_seconds)."""
    base = datetime.datetime.now(datetime.timezone.utc)
    try:
        return (base + datetime.timedelta(seconds=int(offset_seconds))).replace(tzinfo=None)
    except (TypeError, ValueError):
        return base.replace(tzinfo=None)


class PollenData:
    """Current counts and the days ahead, one request each, cached."""

    def __init__(self, fetch=None, cache_ttl: float = 900.0, timeout: float = 25.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("POLLEN_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("POLLEN_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("POLLEN_USER_AGENT") or DEFAULT_USER_AGENT
        self._fetch = fetch or self._http_text
        self._cache: dict[str, tuple[float, object]] = {}

    # -- transport ---------------------------------------------------------

    def _http_text(self, url: str, params: dict) -> str:
        query = f"{url}?{parse.urlencode(params)}" if params else url
        req = request.Request(query, headers={"User-Agent": self.user_agent, "Accept": "*/*"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # urllib raises many types; callers see one error type
            raise PollenError(f"request failed: {exc}") from exc

    def _json(self, url: str, params: dict | None = None, ttl: float | None = None) -> dict:
        key = f"{url}?{parse.urlencode(params or {})}"
        now = time.time()
        hit = self._cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        payload = self._fetch(url, params or {})
        if isinstance(payload, (str, bytes)):
            try:
                payload = json.loads(payload)
            except ValueError as exc:
                raise PollenError(f"the service returned something that is not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise PollenError("the service returned an unexpected payload")
        self._cache[key] = (now + (ttl if ttl is not None else self.cache_ttl), payload)
        return payload

    def geocode(self, place: str) -> dict:
        query = str(place or "").strip()
        if not query:
            raise ValueError("tell me which place, for example Berlin")
        payload = self._json(GEOCODING, {"name": query, "count": 1, "language": "en"}, ttl=86400.0)
        results = payload.get("results") or []
        if not results:
            raise ValueError(f"no place called {query!r} was found")
        best = results[0]
        return {"name": best.get("name") or query, "country": best.get("country") or "",
                "admin": best.get("admin1") or "", "latitude": float(best["latitude"]),
                "longitude": float(best["longitude"])}

    # -- reads -------------------------------------------------------------

    def current(self, place: str, species: str | None = None, group: str | None = None) -> dict:
        """Today's counts at the place's own current hour, species by species."""
        wanted = species_keys(species, group)
        spot = self.geocode(place)
        payload = self._json(AIR, {"latitude": spot["latitude"], "longitude": spot["longitude"],
                                   "current": ",".join(key for key, _label in wanted),
                                   "timezone": "auto"})
        current = payload.get("current") or {}
        readings = [{"species": key, "label": label, "value": current.get(key),
                     "band": band(current.get(key))} for key, label in wanted]
        known = [row for row in readings if row["value"] is not None]
        return {
            "place": spot,
            "at": current.get("time") or "",
            "offset": payload.get("utc_offset_seconds"),
            "readings": readings,
            "total": round(sum(row["value"] for row in known), 1) if known else None,
            "worst": max(known, key=lambda row: row["value"]) if known else None,
            "covered": bool(known),
        }

    def forecast(self, place: str, days: int = 5, species: str | None = None,
                 group: str | None = None) -> dict:
        """Per-day peaks for the days ahead, for every species, one species or one group."""
        wanted = species_keys(species, group)
        spot = self.geocode(place)
        payload = self._json(AIR, {"latitude": spot["latitude"], "longitude": spot["longitude"],
                                   "hourly": ",".join(key for key, _label in wanted),
                                   "forecast_days": max(1, min(int(days), 7)), "timezone": "auto"})
        hourly = payload.get("hourly") or {}
        hours = hourly.get("time") or []
        start = local_hour(payload.get("utc_offset_seconds")).date()
        days_out = []
        for index, stamp in enumerate(hours):
            date = stamp[:10]
            if date < start.isoformat():
                continue
            day = next((row for row in days_out if row["date"] == date), None)
            if day is None:
                day = {"date": date, "peaks": [], "covered": False}
                days_out.append(day)
            for key, label in wanted:
                values = hourly.get(key) or []
                value = values[index] if index < len(values) else None
                if value is None:
                    continue
                day["covered"] = True
                row = next((item for item in day["peaks"] if item["species"] == key), None)
                if row is None:
                    day["peaks"] = day["peaks"] + [{"species": key, "label": label,
                                                    "value": value, "band": band(value)}]
                    row = day["peaks"][-1]
                elif value > row["value"]:
                    row["value"] = value
                    row["band"] = band(value)
        for day in days_out:
            # A species sitting at 0.0 all day is not a peak: keep the ones that actually rise,
            # so the table is about pollen rather than about six rows of nothing.
            day["peaks"] = [row for row in day["peaks"] if row["value"] > 0]
            day["peaks"].sort(key=lambda row: row["value"], reverse=True)
            day["worst"] = day["peaks"][0] if day["peaks"] else None
        return {"place": spot, "days": days_out,
                "covered": any(day["covered"] for day in days_out),
                "species": [label for _key, label in wanted]}
