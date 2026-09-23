"""The feeds behind a briefing, gathered independently so one dead source cannot ruin it.

Five keyless sources, all verified live on 2026-09-23:

  api.open-meteo.com/v1/forecast           today's high, low and rain chance
  air-quality-api.open-meteo.com           the current US AQI and PM2.5
  api.weather.gov/alerts/active?area=CO    active official alerts for a US state
  earthquake.usgs.gov 2.5_day              quakes above M2.5 in the past day
  http://api.open-notify.org/iss-now.json  where the space station is (plain HTTP on purpose:
                                           the HTTPS endpoint hangs - see the note below)

Every section is gathered in its own try/except and returns what it got plus an error string,
so a briefing with one dead source still arrives with the other four. That is the whole point
of a briefing: it is more useful to know the air-quality feed is down than to lose the weather
with it.
"""

from __future__ import annotations

import datetime
import json
import os
import threading
import time
from urllib import parse, request

FORECAST = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY = "https://air-quality-api.open-meteo.com/v1/air-quality"
ALERTS = "https://api.weather.gov/alerts/active"
QUAKES = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"
#: open-notify's HTTPS endpoint hangs on this machine (25s timeouts); HTTP answers in 0.2s.
ISS = "http://api.open-notify.org/iss-now.json"

GEOCODING_DATASET = "Open-Meteo geocoding"
WEATHER_DATASET = "Open-Meteo forecast (open-meteo.com)"
AIR_DATASET = "Open-Meteo air quality (air-quality-api.open-meteo.com)"
ALERT_DATASET = "US National Weather Service (api.weather.gov)"
QUAKE_DATASET = "USGS earthquakes (earthquake.usgs.gov)"
ISS_DATASET = "open-notify (api.open-notify.org)"

DEFAULT_USER_AGENT = "awesome-acps-brief/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: The sections a briefing always tries, in the order they are written.
SECTIONS = ("weather", "air", "alerts", "quakes", "station")


class BriefError(RuntimeError):
    """A whole briefing could not be put together."""


class BriefData:
    """Each section gathered on its own, with the failures kept as text."""

    def __init__(self, fetch=None, cache_ttl: float = 300.0, timeout: float = 25.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("BRIEF_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("BRIEF_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("BRIEF_USER_AGENT") or DEFAULT_USER_AGENT
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
            raise BriefError(f"request failed: {exc}") from exc

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
                raise BriefError(f"the source returned something that is not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise BriefError("the source returned an unexpected payload")
        with self._lock:
            self._cache[key] = (now + (ttl if ttl is not None else self.cache_ttl), payload)
        return payload

    # -- places ------------------------------------------------------------

    def geocode(self, place: str) -> dict:
        query = str(place or "").strip()
        if not query:
            raise ValueError("tell me which place the briefing is for, for example Denver")
        payload = self._json("https://geocoding-api.open-meteo.com/v1/search",
                             {"name": query, "count": 1, "language": "en"}, ttl=86400.0)
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
            "country_code": best.get("country_code") or "",
            "timezone": best.get("timezone") or "",
        }

    # -- sections ----------------------------------------------------------

    def weather(self, place: dict) -> dict:
        payload = self._json(FORECAST, {
            "latitude": place["latitude"], "longitude": place["longitude"],
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "current": "temperature_2m,wind_speed_10m", "forecast_days": 1, "timezone": "auto"})
        daily = payload.get("daily") or {}
        current = payload.get("current") or {}
        highs = daily.get("temperature_2m_max") or []
        lows = daily.get("temperature_2m_min") or []
        rain = daily.get("precipitation_probability_max") or []
        if not highs or not lows:
            raise BriefError("the forecast came back without a daily high or low")
        lines = []
        if current.get("temperature_2m") is not None:
            lines.append(f"now {current['temperature_2m']} degC"
                         + (f", wind {current['wind_speed_10m']} km/h"
                            if current.get("wind_speed_10m") is not None else ""))
        lines.append(f"today's high {highs[0]} degC, low {lows[0]} degC"
                     + (f", rain chance {rain[0]}%" if rain else ""))
        return {"title": f"Weather in {self._where(place)}",
                "lines": lines, "source": WEATHER_DATASET, "error": None}

    def air(self, place: dict) -> dict:
        payload = self._json(AIR_QUALITY, {
            "latitude": place["latitude"], "longitude": place["longitude"],
            "current": "us_aqi,pm2_5", "timezone": "auto"})
        current = payload.get("current") or {}
        aqi = current.get("us_aqi")
        if aqi is None:
            raise BriefError("the air quality model has no current reading for this place")
        band = ("good" if aqi <= 50 else "moderate" if aqi <= 100
                else "unhealthy for sensitive groups" if aqi <= 150
                else "unhealthy" if aqi <= 200 else "very unhealthy")
        lines = [f"US AQI {aqi} ({band})"]
        if current.get("pm2_5") is not None:
            lines.append(f"PM2.5 {current['pm2_5']} ug/m3")
        return {"title": f"Air quality in {self._where(place)}",
                "lines": lines, "source": AIR_DATASET, "error": None}

    def alerts(self, place: dict) -> dict:
        """Active official alerts at the place's own coordinates.

        Asked by point rather than by state: a state code is not in the geocoding answer, and
        guessing one from a place name would put the wrong state's alerts in the file.
        """
        # No `limit` here: combined with `point` the NWS API answers HTTP 400 (found live on
        # 2026-09-23), so the first few alerts are taken from the answer instead.
        payload = self._json(ALERTS, {"point": f"{place['latitude']},{place['longitude']}",
                                     "status": "actual"})
        features = payload.get("features") or []
        lines = [f"{len(features)} active alert(s) at this point"]
        for feature in features[:3]:
            properties = feature.get("properties") or {}
            event = properties.get("event") or "alert"
            area = properties.get("areaDesc") or ""
            lines.append(f"{event}: {area[:80]}")
        if not features:
            lines = ["no active weather alerts at this point"]
        return {"title": "Official alerts", "lines": lines,
                "source": ALERT_DATASET, "error": None}

    def quakes(self) -> dict:
        payload = self._json(QUAKES, ttl=120.0)
        features = payload.get("features") or []
        biggest = None
        for feature in features:
            properties = feature.get("properties") or {}
            magnitude = properties.get("mag")
            if not isinstance(magnitude, (int, float)):
                continue
            if biggest is None or magnitude > biggest["magnitude"]:
                biggest = {"magnitude": magnitude, "place": properties.get("place") or "unlocated",
                           "time": properties.get("time")}
        lines = [f"{len(features)} quake(s) above M2.5 in the past day"]
        if biggest:
            when = ""
            if isinstance(biggest["time"], (int, float)):
                when = datetime.datetime.fromtimestamp(biggest["time"] / 1000,
                                                       datetime.timezone.utc).strftime(" at %H:%M UTC")
            lines.append(f"biggest M{biggest['magnitude']} - {biggest['place']}{when}")
        return {"title": "Earthquakes in the past day", "lines": lines,
                "source": QUAKE_DATASET, "error": None}

    def station(self) -> dict:
        payload = self._json(ISS, ttl=5.0)
        position = payload.get("iss_position") or {}
        if not position:
            raise BriefError("the station feed came back without a position")
        latitude, longitude = float(position["latitude"]), float(position["longitude"])
        return {"title": "International Space Station",
                "lines": [f"over {abs(latitude):.2f} deg {'N' if latitude >= 0 else 'S'}, "
                          f"{abs(longitude):.2f} deg {'E' if longitude >= 0 else 'W'}"],
                "source": ISS_DATASET, "error": None}

    def briefing(self, place: str, sections=None) -> dict:
        """Every section, each one gathered independently. Rails never raise for one source."""
        resolved = self.geocode(place)
        wanted = tuple(sections or SECTIONS)
        out = []
        for name in wanted:
            try:
                if name == "weather":
                    section = self.weather(resolved)
                elif name == "air":
                    section = self.air(resolved)
                elif name == "alerts":
                    section = self.alerts(resolved)
                elif name == "quakes":
                    section = self.quakes()
                elif name == "station":
                    section = self.station()
                else:
                    raise ValueError(f"unknown briefing section {name!r}")
            except (BriefError, ValueError, KeyError, TypeError) as exc:
                section = {"title": name.capitalize(), "lines": [], "source": None,
                           "error": f"unavailable: {exc}"}
            section["name"] = name
            out.append(section)
        return {
            "place": resolved,
            "sections": out,
            "generated_utc": datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"),
            "ok": sum(1 for section in out if not section["error"]),
        }

    def markdown(self, briefing: dict) -> str:
        """The briefing as the Markdown file the agent writes."""
        place = briefing["place"]
        heading = ", ".join(bit for bit in (place["name"], place.get("admin"),
                                           place.get("country")) if bit)
        lines = [f"# Briefing - {heading}", "",
                 f"Generated {briefing['generated_utc']} from public keyless feeds "
                 f"({briefing['ok']} of {len(briefing['sections'])} sections available).", ""]
        for section in briefing["sections"]:
            lines.append(f"## {section['title']}")
            if section["error"]:
                lines.append(section["error"])
            for line in section["lines"]:
                lines.append(f"- {line}")
            if section["source"]:
                lines.append(f"- source: {section['source']}")
            lines.append("")
        lines.append("---")
        lines.append("Every line above is from a live public feed at generation time, or says "
                     "plainly that its source was unavailable. Nothing here is estimated.")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _where(place: dict) -> str:
        bits = [place["name"]]
        if place.get("admin") and place["admin"] != place["name"]:
            bits.append(place["admin"])
        if place.get("country"):
            bits.append(place["country"])
        return ", ".join(bits)
