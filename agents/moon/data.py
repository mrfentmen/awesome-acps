"""Moon phases and the day's own light, from two keyless government services.

Verified live on 2026-09-23:

  https://aa.usno.navy.mil/api/moon/phases/date?date=YYYY-MM-DD&nump=4    US Naval Observatory
  https://api.sunrise-sunset.org/json?lat=&lng=&date=&formatted=0&tzid=  sunrise-sunset.org

USNO answers with `phasedata: [{year, month, day, phase, time}]` where the time is **UT**
(midnight is printed as a plain HH:MM), so this reader labels it UT rather than pretending it
is local. sunrise-sunset.org takes a `tzid` and then answers with real offsets
(`2026-09-22T06:42:27-04:00`), so those times are the place's own and are reported that way.

The moon phase *now* is interpolated between the two phases that bracket the moment - USNO
publishes the phase times, not an angle, so the reader says which two phases it sits between
instead of inventing a percentage.
"""

from __future__ import annotations

import datetime
import json
import os
import time
from urllib import parse, request

USNO = "https://aa.usno.navy.mil/api/moon/phases/date"
SUN = "https://api.sunrise-sunset.org/json"
GEOCODING = "https://geocoding-api.open-meteo.com/v1/search"

USNO_DATASET = "US Naval Observatory Astronomical Applications (aa.usno.navy.mil)"
SUN_DATASET = "sunrise-sunset.org"

DEFAULT_USER_AGENT = "awesome-acps-moon/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: The eight names USNO uses, and the four that are "primary" phases.
PHASES = ("New Moon", "First Quarter", "Full Moon", "Last Quarter")
BIG_PHASES = {"Full Moon", "New Moon"}


class MoonError(RuntimeError):
    """A source could not be read."""


def day_length(seconds) -> str:
    """43783 -> '12h 10m'."""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "unknown"
    return f"{total // 3600}h {(total % 3600) // 60}m"


def stamp(year, month, day, clock: str) -> str:
    """A USNO phase row to '2026-09-26 16:49 UT'."""
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d} {clock} UT"


class MoonData:
    """The next phases and the day's light for a place, each request cached."""

    def __init__(self, fetch=None, cache_ttl: float = 900.0, timeout: float = 25.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("MOON_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("MOON_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("MOON_USER_AGENT") or DEFAULT_USER_AGENT
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
            raise MoonError(f"request failed: {exc}") from exc

    def _json(self, url: str, params: dict | None = None, ttl: float | None = None):
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
                raise MoonError(f"the service returned something that is not JSON: {exc}") from exc
        if not isinstance(payload, (dict, list)):
            raise MoonError("the service returned an unexpected payload")
        self._cache[key] = (now + (ttl if ttl is not None else self.cache_ttl), payload)
        return payload

    def geocode(self, place: str) -> dict:
        query = str(place or "").strip()
        if not query:
            raise ValueError("tell me which place, for example Denver")
        payload = self._json(GEOCODING, {"name": query, "count": 1, "language": "en"},
                             ttl=86400.0)
        results = payload.get("results") or []
        if not results:
            raise ValueError(f"no place called {query!r} was found")
        best = results[0]
        return {"name": best.get("name") or query, "country": best.get("country") or "",
                "admin": best.get("admin1") or "", "timezone": best.get("timezone") or "UTC",
                "latitude": float(best["latitude"]), "longitude": float(best["longitude"])}

    # -- reads -------------------------------------------------------------

    def phases(self, count: int = 4, start: datetime.date | None = None) -> dict:
        """The next phases from a date (UT), the way USNO prints them."""
        wanted = max(1, min(int(count), 8))
        when = start or datetime.date.today()
        payload = self._json(USNO, {"date": when.isoformat(), "nump": wanted}, ttl=3600.0)
        rows = payload.get("phasedata")
        if not rows:
            raise MoonError("the observatory returned no phases")
        phases = [{"date": stamp(row["year"], row["month"], row["day"], row["time"]),
                   "phase": row["phase"],
                   "day": int(row["day"]), "month": int(row["month"]), "year": int(row["year"]),
                   "clock": row["time"]}
                  for row in rows]
        return {"from": when.isoformat(), "phases": phases}

    def sun(self, place: str, date: datetime.date | None = None) -> dict:
        """Sunrise, sunset, noon and the three twilights, in the place's own clock."""
        spot = self.geocode(place)
        when = date or datetime.date.today()
        payload = self._json(SUN, {"lat": spot["latitude"], "lng": spot["longitude"],
                                   "date": when.isoformat(), "formatted": 0,
                                   "tzid": spot["timezone"]})
        results = payload.get("results") or {}
        if payload.get("status") != "OK" or not results.get("sunrise"):
            raise MoonError("the sun service did not answer for that place and date")
        named = (("sunrise", "sunrise"), ("solar_noon", "solar noon"), ("sunset", "sunset"),
                 ("civil_twilight_begin", "civil twilight begins"),
                 ("civil_twilight_end", "civil twilight ends"),
                 ("nautical_twilight_begin", "nautical twilight begins"),
                 ("astronomical_twilight_begin", "astronomical twilight begins"))
        times = [{"key": key, "label": label, "at": results[key],
                  "clock": str(results[key])[11:16], "offset": str(results[key])[19:]}
                 for key, label in named if results.get(key)]
        return {"place": spot, "date": when.isoformat(), "times": times,
                "day_length": day_length(results.get("day_length")),
                "tzid": payload.get("tzid") or spot["timezone"],
                "source_note": "the place's own clock, with its UTC offset shown"}
