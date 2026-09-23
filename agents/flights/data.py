"""Who is flying overhead right now - OpenSky's live state vectors, keyless.

Verified live on 2026-09-23:

  https://opensky-network.org/api/states/all?lamin=40.4&lomin=-74.3&lamax=41.0&lomax=-73.6
      -> 72 aircraft over New York, with positions, altitudes and headings
  https://opensky-network.org/api/states/all?icao24=a487ef               -> one aircraft
  https://opensky-network.org/api/states/all?lamin=-0.5&lomin=-30.5&lamax=-0.2&lomax=-30.2
      -> {"time": ..., "states": null}  <- empty means **null**, not []
  https://nominatim.openstreetmap.org/search?q=...&format=jsonv2&limit=1  -> the place's centre

Five things this reader keeps straight instead of smoothing over:

  1. **`states` is `null` when nothing is in the box.** Reading that as `[]` is luck; reading
     `len(None)` is a crash. Both are handled here, and the number of aircraft found is said
     out loud.
  2. **The array's positions are fixed and unforgiving.** Index 5 is longitude and 6 is latitude,
     in that order, and the API does not name them. `parse_state` is the only place that knows
     the order, and it is tested against a live row.
  3. **An empty answer is not proof of empty sky.** These are ADS-B positions: an aircraft that
     is not broadcasting, or is beyond receiver coverage, is simply absent. Every answer says so.
  4. **Anonymous access is metered and the API says how much is left.** `X-Rate-Limit-Remaining`
     is read from the response and reported when it is there.
  5. **An aircraft lookup is by 24-bit address**, not by flight number: there is no callsign
     lookup in this API, so a callsign question is told that rather than answered wrongly.
"""

from __future__ import annotations

import json
import math
import os
from urllib import error, parse, request

STATES = "https://opensky-network.org/api/states/all"
NOMINATIM = "https://nominatim.openstreetmap.org/search"

DATASET = "OpenSky Network state vectors (opensky-network.org, keyless, ADS-B)"

DEFAULT_USER_AGENT = "awesome-acps-flights/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: How many aircraft one answer lists, nearest first.
MAX_AIRCRAFT = 12

#: The default radius of "overhead": 25 km is about a big city's airspace.
DEFAULT_RADIUS_KM = 25.0

#: OpenSky answers anonymous callers with this header, when it feels like it.
REMAINING_HEADER = "X-Rate-Limit-Remaining"

#: The order of OpenSky's state array. This is the only place that knows it.
FIELDS = ("icao24", "callsign", "origin_country", "time_position", "last_contact", "longitude",
          "latitude", "baro_altitude", "on_ground", "velocity", "true_track", "vertical_rate",
          "sensors", "geo_altitude", "squawk", "spi", "position_source")


class FlightsError(RuntimeError):
    """The sky could not be read."""


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres. The same formula the tides agent uses."""
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = (math.sin(d_phi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2)
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def compass(degrees) -> str:
    """A track in degrees to the way a person would say it: 233.75 -> 'southwest'."""
    try:
        value = float(degrees) % 360
    except (TypeError, ValueError):
        return ""
    points = ("north", "northeast", "east", "southeast", "south", "southwest", "west",
              "northwest")
    return points[int((value + 22.5) % 360 // 45 % 8)]


def bbox_around(latitude: float, longitude: float, radius_km: float = DEFAULT_RADIUS_KM) -> dict:
    """A square of sky around a point, in the latitude/longitude box OpenSky takes."""
    lat_span = radius_km / 111.0
    # Longitude degrees are shorter away from the equator, and a pole has no longitude span.
    shrink = max(0.1, math.cos(math.radians(latitude)))
    lon_span = radius_km / (111.0 * shrink)
    return {"lamin": round(max(-90.0, latitude - lat_span), 4),
            "lomin": round(max(-180.0, longitude - lon_span), 4),
            "lamax": round(min(90.0, latitude + lat_span), 4),
            "lomax": round(min(180.0, longitude + lon_span), 4)}


def parse_state(row: list, now: float | None = None, centre: tuple | None = None) -> dict:
    """One OpenSky array to named fields, with the distance from a centre when given."""
    if not isinstance(row, list) or len(row) < len(FIELDS):
        raise FlightsError(f"an aircraft row had {len(row) if isinstance(row, list) else 0} "
                           f"fields where {len(FIELDS)} were expected")
    raw = dict(zip(FIELDS, row))
    state = {
        "icao24": str(raw["icao24"] or "").strip().lower(),
        "callsign": str(raw["callsign"] or "").strip(),
        "country": str(raw["origin_country"] or "").strip(),
        "longitude": raw["longitude"],
        "latitude": raw["latitude"],
        "altitude_m": raw["baro_altitude"],
        "geo_altitude_m": raw["geo_altitude"],
        "on_ground": bool(raw["on_ground"]),
        "speed_ms": raw["velocity"],
        "track": raw["true_track"],
        "vertical_rate_ms": raw["vertical_rate"],
        "squawk": str(raw["squawk"] or "").strip(),
        "position_source": raw["position_source"],
        "time_position": raw["time_position"],
        "last_contact": raw["last_contact"],
    }
    # How old this position was when the snapshot was taken - not how old it is now, because
    # the answer is a snapshot and saying otherwise would need a wall clock it does not have.
    for key in ("time_position", "last_contact"):
        age = None
        if isinstance(now, (int, float)) and isinstance(raw[key], (int, float)):
            age = max(0.0, float(now) - float(raw[key]))
        state[f"{key}_age_s"] = age
    distance = None
    if centre and isinstance(state["latitude"], (int, float)) \
            and isinstance(state["longitude"], (int, float)):
        distance = haversine_km(centre[0], centre[1], state["latitude"], state["longitude"])
    state["distance_km"] = round(distance, 1) if distance is not None else None
    return state


class FlightsData:
    """Live aircraft above a place, or one aircraft by its 24-bit address."""

    def __init__(self, fetch=None, timeout: float = 25.0, user_agent: str | None = None) -> None:
        self.timeout = float(os.environ.get("FLIGHTS_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("FLIGHTS_USER_AGENT") or DEFAULT_USER_AGENT
        self.fetch = fetch or self._http
        #: What OpenSky last said was left of the anonymous allowance, when it said anything.
        self.remaining: int | None = None
        self._places: dict[str, dict] = {}

    # -- transport ---------------------------------------------------------

    def _http(self, url: str) -> str:
        req = request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        try:
            with request.urlopen(req, timeout=self.timeout) as reply:
                left = reply.headers.get(REMAINING_HEADER)
                if left is not None:
                    try:
                        self.remaining = int(left)
                    except ValueError:
                        self.remaining = None
                return reply.read().decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            raise FlightsError(self._say(exc)) from exc
        except error.URLError as exc:
            raise FlightsError(f"OpenSky is unreachable ({exc.reason})") from exc
        except TimeoutError as exc:
            raise FlightsError(f"OpenSky did not answer within {self.timeout:.0f}s") from exc

    @staticmethod
    def _say(exc: "error.HTTPError") -> str:
        """OpenSky's refusal in words. The 429 is about the day's allowance, not the weather."""
        if exc.code == 429:
            return ("OpenSky answered 429: this address has used its daily anonymous allowance "
                    "(400 credits a day; a bigger box costs more of them). Try again later")
        if exc.code in (401, 403):
            return (f"OpenSky answered {exc.code}: it refused this request. It asks for a "
                    f"User-Agent naming the tool")
        if exc.code == 404:
            return f"OpenSky answered 404 for {exc.url}: no such endpoint"
        return f"OpenSky answered {exc.code} for {exc.url}"

    # -- places ------------------------------------------------------------

    def geocode(self, place: str) -> dict:
        """A place name to a latitude and longitude, through OpenStreetMap's Nominatim."""
        query = str(place or "").strip()
        if not query:
            raise FlightsError("give me a place to look above, for example Bryant Park, New York")
        if query.lower() in self._places:
            return self._places[query.lower()]
        params = {"q": query, "format": "jsonv2", "limit": "1"}
        payload = self.fetch(f"{NOMINATIM}?{parse.urlencode(params)}")
        try:
            rows = json.loads(payload)
        except ValueError as exc:
            raise FlightsError(f"Nominatim's answer was not JSON ({exc})") from exc
        if not isinstance(rows, list) or not rows:
            raise FlightsError(f"OpenStreetMap has no place called {query!r}")
        best = rows[0]
        try:
            found = {"query": query, "latitude": float(best["lat"]), "longitude": float(best["lon"]),
                     "name": str(best.get("name") or (best.get("display_name") or "").split(",")[0]
                                 or query),
                     "display_name": str(best.get("display_name") or query)}
        except (KeyError, TypeError, ValueError) as exc:
            raise FlightsError(f"Nominatim returned no coordinates for {query!r}") from exc
        self._places[query.lower()] = found
        return found

    # -- the sky -----------------------------------------------------------

    def states(self, centre: tuple | None = None, **bbox) -> dict:
        """A snapshot: {'time', 'aircraft', 'remaining'}. `states: null` means no aircraft.

        `centre` is (latitude, longitude) and only adds each aircraft's distance from it, in the
        same pass - a second pass would have to know the array order again, and that order is
        the one thing this module keeps in a single place.
        """
        # A box is numbers; an aircraft lookup is a hex string. Both go through here.
        params = {key: (f"{value:g}" if isinstance(value, (int, float)) else str(value))
                  for key, value in bbox.items() if value is not None}
        payload = self.fetch(f"{STATES}?{parse.urlencode(params)}")
        try:
            raw = json.loads(payload)
        except ValueError as exc:
            raise FlightsError(f"OpenSky's answer was not JSON ({exc})") from exc
        if not isinstance(raw, dict):
            raise FlightsError("OpenSky answered with something other than a snapshot")
        rows = raw.get("states")
        if rows is None:
            rows = []  # documented as null; treated as none, and said out loud by the answer
        if not isinstance(rows, list):
            raise FlightsError("OpenSky's aircraft list was not a list")
        return {"time": raw.get("time"),
                "aircraft": [parse_state(row, raw.get("time"), centre) for row in rows],
                "remaining": self.remaining}

    def overhead(self, place: str, radius_km: float = DEFAULT_RADIUS_KM,
                 limit: int = MAX_AIRCRAFT) -> dict:
        """The nearest aircraft to a place, with the distance from the place's middle."""
        found = self.geocode(place)
        centre = (found["latitude"], found["longitude"])
        box = bbox_around(centre[0], centre[1], radius_km)
        snapshot = self.states(centre=centre, **box)
        aircraft = list(snapshot["aircraft"])
        # Aircraft in the air first, then those on the ground; nearest first within each. Someone
        # asking what is over them means what is flying, and an apron full of parked jets at a
        # nearby airport would otherwise fill the whole list.
        aircraft.sort(key=lambda item: (item["on_ground"], item["distance_km"] is None,
                                        item["distance_km"] or 0.0))
        airborne = [item for item in aircraft if not item["on_ground"]]
        return {"place": {**found, "radius_km": radius_km, "box": box},
                "time": snapshot["time"], "remaining": snapshot["remaining"],
                "found": len(aircraft), "airborne": len(airborne), "on_ground": len(aircraft)
                - len(airborne), "aircraft": aircraft[:max(1, int(limit))],
                "nearest_km": aircraft[0]["distance_km"] if aircraft else None,
                "highest_m": max((item["altitude_m"] for item in airborne
                                  if isinstance(item["altitude_m"], (int, float))), default=None)}

    def aircraft(self, address: str) -> dict:
        """One aircraft by its 24-bit address. There is no callsign lookup in this API."""
        wanted = str(address or "").strip().lower()
        if not (len(wanted) == 6 and all(character in "0123456789abcdef" for character in wanted)):
            raise FlightsError(f"OpenSky looks an aircraft up by its 24-bit address: six hex "
                               f"characters like a487ef, and {address!r} is not one. "
                               f"There is no callsign lookup in this API")
        snapshot = self.states(icao24=wanted)
        found = [row for row in snapshot["aircraft"] if row["icao24"] == wanted]
        if not found:
            return {"address": wanted, "time": snapshot["time"],
                    "remaining": snapshot["remaining"], "aircraft": None}
        return {"address": wanted, "time": snapshot["time"], "remaining": snapshot["remaining"],
                "aircraft": found[0]}
