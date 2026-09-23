"""How deep the water table is, from USGS's own water API (keyless).

Verified live on 2026-09-23, all on the OGC API v0:

  .../collections/monitoring-locations/items?id=OH015-395847084085500
      -> 'MI-3A OH', Miami County, Ohio, well depth 130 ft
  .../collections/daily/items?monitoring_location_id=OH015-395847084085500&parameter_code=72019
      -> 2026-09-22 9.95 ft, 2026-09-21 10.19 ft, 2026-09-20 10.31 ft
  .../collections/latest-daily/items?parameter_code=72019&state_name=Ohio&datetime=...

Three things the payloads make plain and this reader keeps. Parameter **72019 is depth to water
level in feet below land surface**, so a *bigger* number means a *deeper* water table - reading it
as a level would flip the meaning. `latest-daily` is the latest reading each site ever reported,
which for an abandoned well can be 1989, so a date window is always sent and stale rows are
dropped. And `daily` does not filter by state at all (it answers zero rows for
`state_name=Ohio&parameter_code=72019`), so a state question goes to `latest-daily` instead of
paging through every well.
"""

from __future__ import annotations

import json
import os
import re
import time
from urllib import parse, request
from urllib.error import HTTPError

COLLECTIONS = "https://api.waterdata.usgs.gov/ogcapi/v0/collections/"

DATASET = "USGS Water Data OGC API (api.waterdata.usgs.gov)"

DEFAULT_USER_AGENT = "awesome-acps-groundwater/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: Depth to water level, in feet below land surface. The parameter this agent is about.
DEPTH_PARAMETER = "72019"

STATE_NAMES = ("Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
               "Connecticut", "Delaware", "District of Columbia", "Florida", "Georgia", "Hawaii",
               "Idaho", "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
               "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
               "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey",
               "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio", "Oklahoma",
               "Oregon", "Pennsylvania", "Puerto Rico", "Rhode Island", "South Carolina",
               "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
               "West Virginia", "Wisconsin", "Wyoming")


class GroundwaterError(RuntimeError):
    """The USGS water API could not be read."""


class GroundwaterNotFound(GroundwaterError):
    """The API answered 404, or named no site - a real 'nothing here', not a failure."""


def state_from_text(text: str) -> str | None:
    """A full US state name, or None. Full names only: two-letter codes collide with words."""
    lowered = str(text or "").lower()
    for name in sorted(STATE_NAMES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name.lower())}\b", lowered):
            return name
    return None


def identifier_from_text(text: str) -> str | None:
    """A USGS site number (8-15 digits) or a location id like OH015-395847084085500."""
    work = str(text or "")
    match = re.search(r"\b([A-Z]{2}\d{3}-\d{10,16})\b", work)
    if match:
        return match.group(1)
    for token in re.findall(r"\d{8,16}", work):
        return token
    return None


def median(values: list[float]) -> float | None:
    """The middle value, without importing statistics for one line."""
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


class GroundwaterData:
    """One well's depth to water, and a state's newest readings."""

    def __init__(self, fetch=None, cache_ttl: float = 1800.0, timeout: float = 40.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("GROUNDWATER_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("GROUNDWATER_HTTP_TIMEOUT", timeout))
        self.user_agent = (user_agent or os.environ.get("GROUNDWATER_USER_AGENT")
                           or DEFAULT_USER_AGENT)
        self._fetch = fetch or self._http_text
        self._cache: dict[str, tuple[float, object]] = {}

    # -- transport ---------------------------------------------------------

    def _http_text(self, url: str, params: dict | None = None) -> str:
        query = f"{url}?{parse.urlencode(params, doseq=True)}" if params else url
        req = request.Request(query, headers={"User-Agent": self.user_agent, "Accept": "*/*"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except HTTPError as exc:
            raise GroundwaterNotFound(f"request failed: {exc}") from exc
        except Exception as exc:  # urllib raises many other types too
            raise GroundwaterError(f"request failed: {exc}") from exc

    def _items(self, collection: str, params: dict, ttl: float | None = None) -> list[dict]:
        query = f"{COLLECTIONS}{collection}/items?" + parse.urlencode(params, doseq=True)
        now = time.time()
        hit = self._cache.get(query)
        if hit and hit[0] > now:
            return hit[1]
        payload = self._fetch(f"{COLLECTIONS}{collection}/items", params)
        if isinstance(payload, (str, bytes)):
            try:
                payload = json.loads(payload)
            except ValueError as exc:
                raise GroundwaterError(
                    f"the USGS API returned something that is not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise GroundwaterError("the USGS API returned an unexpected payload")
        features = payload.get("features") or []
        self._cache[query] = (now + (ttl if ttl is not None else self.cache_ttl), features)
        return features

    # -- reads -------------------------------------------------------------

    def site(self, identifier: str) -> dict:
        """One monitoring location's own record, by site number or location id."""
        text = str(identifier or "").strip()
        if not text:
            raise ValueError("tell me which well, for example site 395847084085500")
        key = "id" if "-" in text else "monitoring_location_number"
        features = self._items("monitoring-locations", {key: text, "limit": "1"}, ttl=3600.0)
        if not features:
            raise GroundwaterNotFound(f"USGS has no monitoring location {text!r}")
        properties = features[0].get("properties") or {}
        geometry = features[0].get("geometry") or {}
        coordinates = geometry.get("coordinates") or []
        return {
            "id": properties.get("id"),
            "number": str(properties.get("monitoring_location_number") or text),
            "name": str(properties.get("monitoring_location_name") or "(unnamed site)"),
            "state": properties.get("state_name"),
            "county": properties.get("county_name"),
            "site_type": properties.get("site_type"),
            "well_depth": properties.get("well_constructed_depth"),
            "aquifer": properties.get("aquifer_code"),
            "latitude": coordinates[1] if len(coordinates) > 1 else None,
            "longitude": coordinates[0] if len(coordinates) > 0 else None,
            "url": f"https://waterdata.usgs.gov/monitoring-location/{properties.get('id')}/",
            "source": DATASET,
        }

    def readings(self, location_id: str, days: int = 14) -> dict:
        """A well's daily depth-to-water series, oldest first."""
        wanted = max(1, min(int(days), 120))
        features = self._items("daily", {"monitoring_location_id": location_id,
                                         "parameter_code": DEPTH_PARAMETER,
                                         "limit": str(wanted), "sortby": "-time"}, ttl=900.0)
        rows = []
        for feature in features:
            properties = feature.get("properties") or {}
            value = properties.get("value")
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            rows.append({"date": str(properties.get("time") or "")[:10], "depth_ft": value,
                         "unit": properties.get("unit_of_measure") or "ft",
                         "status": properties.get("approval_status")})
        rows.sort(key=lambda row: row["date"])
        if not rows:
            raise GroundwaterNotFound(f"no depth-to-water reading came back for {location_id!r} "
                                      "(many wells report only in the growing season)")
        return {"location_id": location_id, "rows": rows, "latest": rows[-1],
                "oldest": rows[0], "source": DATASET}

    def well(self, identifier: str, days: int = 14) -> dict:
        """A well and its recent depth readings, in one answer."""
        site = self.site(identifier)
        readings = self.readings(site["id"], days=days)
        site["readings"] = readings["rows"]
        site["latest"] = readings["latest"]
        site["oldest"] = readings["oldest"]
        site["days"] = len(readings["rows"])
        return site

    def state(self, state: str, days: int = 7, limit: int = 200) -> dict:
        """The newest depth reading from each well in a state, over a date window."""
        name = state_from_text(state) or str(state or "").strip()
        if not name:
            raise ValueError("tell me which state, for example Kansas")
        wanted = max(1, min(int(limit), 500))
        features = self._items("latest-daily", {
            "parameter_code": DEPTH_PARAMETER, "state_name": name,
            "datetime": self._window(days), "limit": str(wanted)}, ttl=1800.0)
        rows = []
        for feature in features:
            properties = feature.get("properties") or {}
            try:
                value = float(properties.get("value"))
            except (TypeError, ValueError):
                continue
            rows.append({"location_id": properties.get("monitoring_location_id"),
                         "date": str(properties.get("time") or "")[:10], "depth_ft": value})
        if not rows:
            raise GroundwaterNotFound(f"no well in {name} reported a depth to water in the last "
                                      f"{days} day(s)")
        depths = [row["depth_ft"] for row in rows]
        rows.sort(key=lambda row: row["depth_ft"])
        return {"state": name, "rows": rows, "wells": len(rows),
                "median_ft": median(depths), "shallowest": rows[0], "deepest": rows[-1],
                "days": max(1, min(int(days), 60)), "asked_for": wanted,
                "truncated": len(rows) >= wanted, "source": DATASET}

    # -- dates -------------------------------------------------------------

    def _window(self, days: int) -> str:
        import datetime

        span = max(1, min(int(days), 60))
        today = datetime.date.today()
        start = today - datetime.timedelta(days=span)
        return f"{start.isoformat()}/{today.isoformat()}"
