"""How dry a place is, from the US Drought Monitor's own API (keyless).

Verified live on 2026-09-23:

  .../api/StateStatistics/GetDroughtSeverityStatisticsByAreaPercent?aoi=06&startdate=9/1/2026&...
      -> MapDate,StateAbbreviation,None,D0,D1,D2,D3,D4,ValidStart,ValidEnd,StatisticFormatID
         20260915,CA,34.77,65.23,22.32,0.04,0.00,0.00,...
  .../api/CountyStatistics/...?aoi=48201 -> adds FIPS,County,State

Three things the payload makes plain and this reader keeps. The response is **CSV, not JSON**
(test the shape, not the hope). The columns are **cumulative**: D1 is "D1 or worse", not "exactly
D1", and the column the CSV heads with the literal word `None` is the share with no drought at
all - so D0 + None is 100. And there is no national endpoint (the NationalStatistics path answers
404), so this agent answers for a state or a county and does not invent a country-wide number.
"""

from __future__ import annotations

import csv
import datetime
import io
import os
import re
import time
from urllib import parse, request

BASE = "https://usdmdataservices.unl.edu/api/"

DATASET = "US Drought Monitor (usdmdataservices.unl.edu)"

DEFAULT_USER_AGENT = "awesome-acps-drought/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: State name -> the 2-digit FIPS code the API wants as `aoi`.
STATE_FIPS = {
    "alabama": "01", "alaska": "02", "arizona": "04", "arkansas": "05", "california": "06",
    "colorado": "08", "connecticut": "09", "delaware": "10", "district of columbia": "11",
    "florida": "12", "georgia": "13", "hawaii": "15", "idaho": "16", "illinois": "17",
    "indiana": "18", "iowa": "19", "kansas": "20", "kentucky": "21", "louisiana": "22",
    "maine": "23", "maryland": "24", "massachusetts": "25", "michigan": "26", "minnesota": "27",
    "mississippi": "28", "missouri": "29", "montana": "30", "nebraska": "31", "nevada": "32",
    "new hampshire": "33", "new jersey": "34", "new mexico": "35", "new york": "36",
    "north carolina": "37", "north dakota": "38", "ohio": "39", "oklahoma": "40", "oregon": "41",
    "pennsylvania": "42", "puerto rico": "72", "rhode island": "44", "south carolina": "45",
    "south dakota": "46", "tennessee": "47", "texas": "48", "utah": "49", "vermont": "50",
    "virginia": "51", "washington": "53", "west virginia": "54", "wisconsin": "55",
    "wyoming": "56",
}

#: Two-letter codes, so "how dry is CA" works without a lookup table of names.
STATE_CODES = {"al": "01", "ak": "02", "az": "04", "ar": "05", "ca": "06", "co": "08",
               "ct": "09", "de": "10", "dc": "11", "fl": "12", "ga": "13", "hi": "15",
               "id": "16", "il": "17", "in": "18", "ia": "19", "ks": "20", "ky": "21",
               "la": "22", "me": "23", "md": "24", "ma": "25", "mi": "26", "mn": "27",
               "ms": "28", "mo": "29", "mt": "30", "ne": "31", "nv": "32", "nh": "33",
               "nj": "34", "nm": "35", "ny": "36", "nc": "37", "nd": "38", "oh": "39",
               "ok": "40", "or": "41", "pa": "42", "pr": "72", "ri": "44", "sc": "45",
               "sd": "46", "tn": "47", "tx": "48", "ut": "49", "vt": "50", "va": "51",
               "wa": "53", "wv": "54", "wi": "55", "wy": "56"}

#: The five categories, worst last. D0+ means "at least abnormally dry".
CATEGORIES = (("d0", "abnormally dry (D0+)"), ("d1", "moderate drought (D1+)"),
              ("d2", "severe drought (D2+)"), ("d3", "extreme drought (D3+)"),
              ("d4", "exceptional drought (D4+)"))

#: The API's "no data" markers.
NO_DATA = ("", "-99", "-99.0", "None")


def fips_for(name: str) -> str | None:
    """A state name, a two-letter code, or a 5-digit county FIPS."""
    text = str(name or "").strip().lower().strip(".")
    if re.fullmatch(r"\d{5}", text):
        return text
    if text in STATE_FIPS:
        return STATE_FIPS[text]
    return STATE_CODES.get(text)


def number(value) -> float | None:
    """A CSV cell as a float, or None when the monitor has nothing for that week."""
    text = str(value or "").strip()
    if text in NO_DATA:
        return None
    try:
        return float(text)
    except ValueError:
        return None


class DroughtError(RuntimeError):
    """The Drought Monitor could not be read."""


class DroughtData:
    """Weekly drought area percentages for a state or a county."""

    def __init__(self, fetch=None, cache_ttl: float = 1800.0, timeout: float = 30.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("DROUGHT_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("DROUGHT_HTTP_TIMEOUT", timeout))
        self.user_agent = (user_agent or os.environ.get("DROUGHT_USER_AGENT")
                           or DEFAULT_USER_AGENT)
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
            raise DroughtError(f"request failed: {exc}") from exc

    def _csv(self, url: str, params: dict, ttl: float | None = None) -> list[dict]:
        key = f"{url}?{parse.urlencode(params)}"
        now = time.time()
        hit = self._cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        text = self._fetch(url, params)
        if isinstance(text, (bytes, bytearray)):
            text = text.decode("utf-8", "replace")
        if not isinstance(text, str):
            raise DroughtError("the monitor returned an unexpected payload")
        rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
        if not rows:
            raise DroughtError("the monitor returned no rows for that place and window")
        self._cache[key] = (now + (ttl if ttl is not None else self.cache_ttl), rows)
        return rows

    # -- shaping -----------------------------------------------------------

    def _readings(self, rows: list[dict], weeks: int) -> list[dict]:
        out = []
        for row in rows:
            stamp = str(row.get("MapDate") or "").strip()
            if len(stamp) != 8 or not stamp.isdigit():
                continue
            reading = {"date": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}",
                       "week_ends": str(row.get("ValidEnd") or "").strip() or None}
            for key, _label in CATEGORIES:
                reading[key] = number(row.get(key.upper()))
            reading["none"] = number(row.get("None"))
            out.append(reading)
        if not out:
            raise DroughtError("the monitor's rows carried no usable dates")
        out.sort(key=lambda row: row["date"])
        return out[-max(1, min(int(weeks), 60)):]

    def state(self, state: str, weeks: int = 6) -> dict:
        """Weekly drought percentages for a US state."""
        code = fips_for(state)
        if not code or len(code) != 2:
            raise ValueError(f"I do not know the state {state!r}; give a state name or its "
                             "two-letter code")
        rows = self._csv(f"{BASE}StateStatistics/GetDroughtSeverityStatisticsByAreaPercent",
                         {"aoi": code, "startdate": self._start(weeks), "enddate": self._end(),
                          "statisticsType": "1"})
        readings = self._readings(rows, weeks)
        label = (rows[0].get("StateAbbreviation") or state).strip()
        return {"place": label, "kind": "state", "aoi": code, "rows": readings,
                "latest": readings[-1], "source": DATASET}

    def county(self, county_fips: str, weeks: int = 6) -> dict:
        """Weekly drought percentages for a US county, by its 5-digit FIPS code."""
        code = fips_for(county_fips)
        if not code or len(code) != 5:
            raise ValueError("a county is asked for by its 5-digit FIPS code, for example 48201 "
                             "(there are over three thousand counties, so I do not carry a name "
                             "table)")
        rows = self._csv(f"{BASE}CountyStatistics/GetDroughtSeverityStatisticsByAreaPercent",
                         {"aoi": code, "startdate": self._start(weeks), "enddate": self._end(),
                          "statisticsType": "1"})
        readings = self._readings(rows, weeks)
        first = rows[0]
        label = f"{str(first.get('County') or '').strip()}, {str(first.get('State') or '').strip()}"
        return {"place": label.strip(", "), "kind": "county", "aoi": code, "rows": readings,
                "latest": readings[-1], "source": DATASET}

    def read(self, place: str, weeks: int = 6) -> dict:
        """A state by name or code, or a county by FIPS."""
        code = fips_for(place)
        if code and len(code) == 5:
            return self.county(code, weeks=weeks)
        return self.state(place, weeks=weeks)

    # -- dates -------------------------------------------------------------

    def _today(self) -> datetime.date:
        """The monitor's weeks end on Tuesday, so the newest row is at most a few days old."""
        return datetime.date.today()

    def _end(self) -> str:
        return f"{self._today().month}/{self._today().day}/{self._today().year}"

    def _start(self, weeks: int) -> str:
        span = max(1, min(int(weeks), 60)) * 7 + 7
        start = self._today() - datetime.timedelta(days=span)
        return f"{start.month}/{start.day}/{start.year}"
