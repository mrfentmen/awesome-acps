"""The conditions an alert can wait on, each one a single live check.

Five keyless sources, all verified live on 2026-09-23:

  air-quality-api.open-meteo.com              current US AQI for a place
  api.open-meteo.com/v1/forecast              current temperature for a place
  earthquake.usgs.gov all_hour                quakes above M2.5 in the past hour
  api.water.noaa.gov/nwps/v1/gauges/<id>      a river gauge's stage and flood category
  api.fiscaldata.treasury.gov                 the national debt

A condition is a question with a threshold: `check()` returns the current value *and* whether
the condition holds, so the agent can stay silent while it does not. Quakes are the odd one out
and are handled differently on purpose: "quakes above M5" is not a level, it is an event, so the
reader keeps the ids it has already seen and fires only on a new one.
"""

from __future__ import annotations

import datetime
import json
import os
import threading
import time
from urllib import parse, request

AIR_QUALITY = "https://air-quality-api.open-meteo.com/v1/air-quality"
FORECAST = "https://api.open-meteo.com/v1/forecast"
QUAKES = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_hour.geojson"
NWPS = "https://api.water.noaa.gov/nwps/v1/gauges"
TREASURY = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2/accounting/od/debt_to_penny"

AIR_DATASET = "Open-Meteo air quality"
TEMP_DATASET = "Open-Meteo forecast"
QUAKE_DATASET = "USGS earthquakes (earthquake.usgs.gov)"
FLOOD_DATASET = "NOAA National Water Prediction Service (api.water.noaa.gov)"
DEBT_DATASET = "US Treasury Fiscal Data"

DEFAULT_USER_AGENT = "awesome-acps-alert/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: The conditions this agent knows, and what a threshold means for each.
CONDITIONS = {
    "aqi": "the US Air Quality Index at a place",
    "temperature": "the air temperature at a place, in degrees Celsius",
    "quakes": "a new earthquake above a magnitude, worldwide",
    "flood": "a river gauge reaching or passing its flood stage",
    "debt": "the national debt in dollars",
}

#: Flood categories that count as flooding, worst last.
FLOOD_CATEGORIES = {"action": 1, "minor": 2, "moderate": 3, "major": 4}


class AlertError(RuntimeError):
    """A condition could not be checked."""


class AlertData:
    """One live check per condition, with the last quake ids remembered."""

    def __init__(self, fetch=None, timeout: float = 25.0, user_agent: str | None = None) -> None:
        self.timeout = float(os.environ.get("ALERT_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("ALERT_USER_AGENT") or DEFAULT_USER_AGENT
        self._fetch = fetch or self._http_text
        self.seen_quakes: set[str] = set()

    # -- transport ---------------------------------------------------------

    def _http_text(self, url: str, params: dict) -> str:
        query = f"{url}?{parse.urlencode(params)}" if params else url
        req = request.Request(query, headers={"User-Agent": self.user_agent, "Accept": "*/*"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # urllib raises many types; callers see one error type
            raise AlertError(f"request failed: {exc}") from exc

    def _json(self, url: str, params: dict | None = None) -> dict:
        payload = self._fetch(url, params or {})
        if isinstance(payload, (str, bytes)):
            try:
                payload = json.loads(payload)
            except ValueError as exc:
                raise AlertError(f"the source returned something that is not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise AlertError("the source returned an unexpected payload")
        return payload

    def geocode(self, place: str) -> dict:
        payload = self._json("https://geocoding-api.open-meteo.com/v1/search",
                             {"name": str(place or "").strip(), "count": 1, "language": "en"})
        results = payload.get("results") or []
        if not results:
            raise ValueError(f"no place called {place!r} was found")
        best = results[0]
        return {"name": best.get("name") or place, "country": best.get("country") or "",
                "admin": best.get("admin1") or "",
                "latitude": float(best["latitude"]), "longitude": float(best["longitude"])}

    # -- checks ------------------------------------------------------------

    def aqi(self, place: str) -> dict:
        spot = self.geocode(place)
        payload = self._json(AIR_QUALITY, {"latitude": spot["latitude"],
                                           "longitude": spot["longitude"],
                                           "current": "us_aqi", "timezone": "auto"})
        value = (payload.get("current") or {}).get("us_aqi")
        if value is None:
            raise AlertError("the air quality model has no current reading for that place")
        return {"condition": "aqi", "value": float(value), "unit": " AQI", "place": spot,
                "label": f"US AQI in {spot['name']}", "source": AIR_DATASET}

    def temperature(self, place: str) -> dict:
        spot = self.geocode(place)
        payload = self._json(FORECAST, {"latitude": spot["latitude"],
                                        "longitude": spot["longitude"],
                                        "current": "temperature_2m", "timezone": "auto"})
        value = (payload.get("current") or {}).get("temperature_2m")
        if value is None:
            raise AlertError("the forecast has no current temperature for that place")
        return {"condition": "temperature", "value": float(value), "unit": " degC", "place": spot,
                "label": f"temperature in {spot['name']}", "source": TEMP_DATASET}

    def floods(self, gauge: str) -> dict:
        """One river gauge: observed stage, flow and how it compares with flood stage."""
        wanted = str(gauge or "").strip()
        if not wanted.isdigit():
            raise ValueError("give me a river gauge id, for example 07010000 (the gauge ids are "
                             "the numbers NOAA prints on its water pages)")
        payload = self._json(f"{NWPS}/{wanted}")
        status = (payload.get("status") or {}).get("observed") or {}
        category = str(status.get("floodCategory") or "no_flooding").replace("_", " ")
        thresholds = payload.get("floodThresholds") or {}
        stage = status.get("primary")
        value = None
        try:
            value = float(stage)
        except (TypeError, ValueError):
            value = None
        return {
            "condition": "flood",
            "value": value,
            "unit": " ft" if value is not None else "",
            "label": f"{payload.get('name') or wanted} (gauge {wanted})",
            "category": category,
            "category_rank": FLOOD_CATEGORIES.get(str(status.get("floodCategory") or "").lower(), 0),
            "thresholds": thresholds,
            "source": FLOOD_DATASET,
            "note": str(status.get("floodCategory") or "no_flooding"),
        }

    def quakes(self, min_magnitude: float) -> dict:
        """The newest quake at or above a magnitude, and whether it is one we have not seen."""
        payload = self._json(QUAKES)
        if payload.get("type") != "FeatureCollection":
            raise AlertError("USGS returned an unexpected payload")
        features = payload.get("features") or []
        considered = []
        for feature in features:
            properties = feature.get("properties") or {}
            magnitude = properties.get("mag")
            if not isinstance(magnitude, (int, float)) or magnitude < float(min_magnitude):
                continue
            considered.append({
                "id": str(feature.get("id") or properties.get("code") or ""),
                "magnitude": float(magnitude),
                "place": properties.get("place") or "unlocated",
                "time": properties.get("time"),
                "url": properties.get("url"),
            })
        considered.sort(key=lambda row: row["id"])
        newest = considered[0] if considered else None
        fresh = None
        for event in considered:
            if event["id"] and event["id"] not in self.seen_quakes:
                fresh = event
                break
        for event in considered:
            if event["id"]:
                self.seen_quakes.add(event["id"])
        when = ""
        if fresh and isinstance(fresh["time"], (int, float)):
            when = datetime.datetime.fromtimestamp(fresh["time"] / 1000,
                                                   datetime.timezone.utc).strftime("%H:%M UTC")
        return {
            "condition": "quakes",
            "value": fresh["magnitude"] if fresh else None,
            "unit": " M" if fresh else "",
            "label": f"earthquakes at or above M{min_magnitude}",
            "event": fresh,
            "newest": newest,
            "watched": len(considered),
            "seen": len(self.seen_quakes),
            "when": when,
            "source": QUAKE_DATASET,
        }

    def debt(self) -> dict:
        payload = self._json(TREASURY, {"sort": "-record_date", "page[size]": 1,
                                        "fields": "record_date,tot_pub_debt_out_amt"})
        rows = payload.get("data") or []
        if not rows:
            raise AlertError("the Treasury returned no debt rows")
        return {"condition": "debt", "value": float(rows[0]["tot_pub_debt_out_amt"]),
                "unit": " dollars", "label": "the national debt",
                "as_of": rows[0].get("record_date"), "source": DEBT_DATASET}

    def check(self, condition: str, direction: str = "above", threshold: float | None = None,
              place: str | None = None, gauge: str | None = None) -> dict:
        """One check: the current reading plus whether the condition is met right now."""
        if condition == "aqi":
            reading = self.aqi(place)
        elif condition == "temperature":
            reading = self.temperature(place)
        elif condition == "flood":
            reading = self.floods(gauge)
        elif condition == "quakes":
            reading = self.quakes(threshold if threshold is not None else 4.0)
        elif condition == "debt":
            reading = self.debt()
        else:
            raise ValueError(f"I can watch: {', '.join(CONDITIONS)}")

        if condition == "quakes":
            reading["met"] = reading["event"] is not None
            return reading
        if condition == "flood":
            reading["met"] = reading["category_rank"] >= 1
            return reading
        if reading["value"] is None:
            reading["met"] = False
            return reading
        if condition == "debt" and threshold is None:
            reading["met"] = False
            return reading
        if threshold is None:
            reading["met"] = False
            return reading
        if direction == "below":
            reading["met"] = reading["value"] <= float(threshold)
        else:
            reading["met"] = reading["value"] >= float(threshold)
        return reading
