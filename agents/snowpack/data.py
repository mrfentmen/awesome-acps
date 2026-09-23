"""How much water is in the snow, from NRCS's own snow network (keyless).

Verified live on 2026-09-23:

  .../awdbRestApi/services/v1/stations?stationTriplets=301:CA:SNTL
      -> 'Adin Mtn', Modoc county CA, elevation 6170 ft, network SNTL
  .../awdbRestApi/services/v1/data?stationTriplets=301:CA:SNTL&elements=WTEQ&periodRef=END
      -> {'date': '2026-09-21', 'value': 0.0, 'median': 0.0}

Three things the payloads make plain and this reader keeps. **Every value arrives with its own
median** ("median": 0.0), so percent-of-normal is a real calculation rather than a guess. The unit
of WTEQ is **inches of water**, not inches of snow - the depth is SNWD - so the two are never
added up or compared. And `stationNames=*sierra*` is not a name search (it answered three
unrelated stations), so a place question is asked as `*:CA:SNTL` - every snow station in a state -
and the stations that actually report are named from a single follow-up lookup.
"""

from __future__ import annotations

import json
import os
import time
from urllib import parse, request

BASE = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/"

DATASET = "NRCS Air and Water Database (wcc.sc.egov.usda.gov)"

DEFAULT_USER_AGENT = "awesome-acps-snowpack/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: Snow telemetry, the automated network that measures the mountain snowpack.
SNOW_NETWORK = "SNTL"

#: element code -> (label, unit, what it is)
ELEMENTS = {
    "WTEQ": ("snow water equivalent", "in", "the water in the snowpack, in inches"),
    "SNWD": ("snow depth", "in", "how deep the snow is on the ground, in inches"),
    "PREC": ("precipitation accumulation", "in", "season-to-date precipitation"),
}

#: The states the snow network covers, by code and by name.
STATE_CODES = ("AK", "AZ", "CA", "CO", "ID", "MT", "NM", "NV", "OR", "UT", "WA", "WY")

STATE_NAMES = {"alaska": "AK", "arizona": "AZ", "california": "CA", "colorado": "CO",
               "idaho": "ID", "montana": "MT", "new mexico": "NM", "nevada": "NV",
               "oregon": "OR", "utah": "UT", "washington": "WA", "wyoming": "WY"}

#: Snow water equivalent is the headline figure, so it is listed before snow depth.
ELEMENT_ORDER = ("WTEQ", "SNWD", "PREC")


class SnowpackError(RuntimeError):
    """The snow network could not be read."""


class SnowpackNotFound(SnowpackError):
    """The network has no such station, or no value in the window."""


def percent_of_median(value, median_value) -> float | None:
    """value as a percentage of the median, or None when the median is zero or missing."""
    try:
        value = float(value)
        median_value = float(median_value)
    except (TypeError, ValueError):
        return None
    if median_value <= 0:
        return None
    return value / median_value * 100.0


def last_value(values: list[dict]) -> dict | None:
    """The newest reading in a series, ignoring rows that carry no number."""
    for row in reversed(values or []):
        if row.get("value") is not None:
            return row
    return None


class SnowData:
    """Snow stations, and the water in their snow."""

    def __init__(self, fetch=None, cache_ttl: float = 3600.0, timeout: float = 40.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("SNOWPACK_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("SNOWPACK_HTTP_TIMEOUT", timeout))
        self.user_agent = (user_agent or os.environ.get("SNOWPACK_USER_AGENT")
                           or DEFAULT_USER_AGENT)
        self._fetch = fetch or self._http_text
        self._cache: dict[str, tuple[float, object]] = {}

    # -- transport ---------------------------------------------------------

    def _http_text(self, url: str, params: dict) -> str:
        query = f"{url}?{parse.urlencode(params, doseq=True)}" if params else url
        req = request.Request(query, headers={"User-Agent": self.user_agent, "Accept": "*/*"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # urllib raises many types; callers see one error type
            raise SnowpackError(f"request failed: {exc}") from exc

    def _json(self, path: str, params: dict, ttl: float | None = None):
        query = f"{BASE}{path}?" + parse.urlencode(params, doseq=True)
        now = time.time()
        hit = self._cache.get(query)
        if hit and hit[0] > now:
            return hit[1]
        text = self._fetch(f"{BASE}{path}", params)
        if isinstance(text, (bytes, bytearray)):
            text = text.decode("utf-8", "replace")
        try:
            payload = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise SnowpackError(f"the snow network returned something that is not JSON: "
                                f"{exc}") from exc
        self._cache[query] = (now + (ttl if ttl is not None else self.cache_ttl), payload)
        return payload

    # -- reads -------------------------------------------------------------

    def stations(self, pattern: str) -> list[dict]:
        """Station records for a triplet, or every snow station in a state via '*:XX:SNTL'."""
        payload = self._json("stations", {"stationTriplets": pattern}, ttl=7200.0)
        rows = payload if isinstance(payload, list) else payload.get("stations") or []
        return [{"triplet": str(row.get("stationTriplet") or ""),
                 "name": str(row.get("name") or "").strip(),
                 "state": row.get("stateCode"),
                 "county": row.get("countyName"),
                 "elevation_ft": row.get("elevation"),
                 "latitude": row.get("latitude"),
                 "longitude": row.get("longitude")} for row in rows if row.get("stationTriplet")]

    def station(self, triplet: str, days: int = 30,
                elements: tuple[str, ...] = ("WTEQ", "SNWD")) -> dict:
        """One station: its record, and its snow series with each value's own median."""
        wanted = max(1, min(int(days), 120))
        payload = self._json("data", {"stationTriplets": triplet, "elements": ",".join(elements),
                                      "periodRef": "END", "centralTendencyType": "MEDIAN"},
                            ttl=3600.0)
        if not payload:
            raise SnowpackNotFound(f"the snow network has no station {triplet!r}")
        block = payload[0]
        series = []
        for element in block.get("data") or []:
            code = str((element.get("stationElement") or {}).get("elementCode") or "")
            values = [row for row in element.get("values") or [] if row.get("value") is not None]
            newest = last_value(values)
            if not code or newest is None:
                continue
            label, unit, _what = ELEMENTS.get(code, (code, "", ""))
            series.append({"code": code, "label": label, "unit": unit or "in",
                           "rows": values[-wanted:], "latest": newest,
                           "percent_of_median": percent_of_median(newest.get("value"),
                                                                  newest.get("median"))})
        if not series:
            raise SnowpackNotFound(f"station {triplet!r} reported no snow values in the last "
                                   f"{wanted} day(s)")
        series.sort(key=lambda row: ELEMENT_ORDER.index(row["code"])
                    if row["code"] in ELEMENT_ORDER else len(ELEMENT_ORDER))
        records = self.stations(triplet)
        record = records[0] if records else {"triplet": triplet, "name": triplet}
        return {"station": record, "series": series, "source": DATASET,
                "url": f"https://wcc.sc.egov.usda.gov/nwcc/site?sitenum={triplet.split(':')[0]}"
                       f"&state={triplet.split(':')[1]}"}

    def state(self, state_code: str, days: int = 7, limit: int = 40,
              element: str = "WTEQ") -> dict:
        """What the snow stations of a state are reporting, and how they compare.

        One data request for the whole state, then one name lookup for the stations that came up
        with snow - two requests that do not grow with the number of stations.
        """
        code = str(state_code or "").strip().upper()
        if code not in STATE_CODES:
            raise ValueError(f"I do not know the snow state {state_code!r}; the snow network "
                             f"covers: {', '.join(STATE_CODES)}")
        wanted = max(1, min(int(limit), 400))
        payload = self._json("data", {"stationTriplets": f"*:{code}:{SNOW_NETWORK}",
                                      "elements": element, "periodRef": "END",
                                      "centralTendencyType": "MEDIAN"}, ttl=3600.0)
        rows, reporting = [], []
        for block in payload if isinstance(payload, list) else []:
            triplet = str(block.get("stationTriplet") or "")
            newest = None
            for element_block in block.get("data") or []:
                if str((element_block.get("stationElement") or {}).get("elementCode")) == element:
                    newest = last_value(element_block.get("values") or [])
            if newest is None:
                continue
            rows.append({"triplet": triplet, "date": newest.get("date"),
                         "value": newest.get("value"), "median": newest.get("median"),
                         "percent_of_median": percent_of_median(newest.get("value"),
                                                                newest.get("median"))})
            if float(newest.get("value") or 0) > 0:
                reporting.append(rows[-1])
        if not rows:
            raise SnowpackNotFound(f"no snow station in {code} reported a {element} value")
        reporting.sort(key=lambda row: float(row["value"]), reverse=True)
        top = reporting[:wanted]
        names = {}
        if top:
            #: One lookup for every station worth naming, instead of one per station.
            records = self.stations(",".join(row["triplet"] for row in top))
            names = {record["triplet"]: record for record in records}
        for row in top:
            record = names.get(row["triplet"]) or {}
            row["name"] = record.get("name") or row["triplet"]
            row["elevation_ft"] = record.get("elevation_ft")
        label, unit, what = ELEMENTS.get(element, (element, "in", ""))
        with_median = [row for row in rows if row["percent_of_median"] is not None]
        average = (sum(row["percent_of_median"] for row in with_median) / len(with_median)
                   if with_median else None)
        medians = [row["median"] for row in rows if row.get("median") is not None]
        return {"state": code, "element": element, "label": label, "unit": unit, "about": what,
                "stations": len(rows), "with_snow": len(reporting), "top": top,
                "average_percent_of_median": average,
                "stations_with_a_median": len(with_median),
                #: Off season every median is 0, and 0/0 is not "normal" - it is undefined.
                "medians_are_all_zero": bool(medians) and all(float(m) == 0 for m in medians),
                "source": DATASET}
