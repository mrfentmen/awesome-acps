"""Read-only OpenStreetMap reader for the osm agent.

Two keyless services, both verified live on 2026-09-23:

  nominatim.openstreetmap.org/search   a place name to a latitude and longitude
  overpass-api.de/api/interpreter      what is mapped near a point (Postel's own query API)

Nominatim has one rule this reader respects: it wants a real User-Agent identifying the
caller, so this agent sends one and caches results. Overpass occasionally returns 429/504
under load, so there is one fallback mirror (kumi.systems) and a clear error after that.

Nothing here invents a place. If OSM has no matching POI, the answer says so - OpenStreetMap
is volunteer-mapped, and an empty result means "no mapper has added it", not "it is not there".
That distinction is printed with every answer rather than guessed away.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from urllib import parse, request

NOMINATIM = "https://nominatim.openstreetmap.org"
DATASET = "OpenStreetMap (nominatim.openstreetmap.org and overpass-api.de)"

#: Overpass asks callers to identify themselves and to use the nearest of its mirrors.
OVERPASS_MIRRORS = ("https://overpass-api.de/api/interpreter",
                    "https://overpass.kumi.systems/api/interpreter")

DEFAULT_USER_AGENT = "awesome-acps-osm/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: The request cap: a wildcard search near a big city can match thousands of objects.
MAX_EXAMPLES = 300

#: Question words mapped to the OSM tag they mean, in the order they are checked.
KINDS: tuple[tuple[re.Pattern, tuple[str, str], str], ...] = (
    (re.compile(r"\b(drinking water|water fountains?|fountains?|water taps?|bubblers?)\b", re.I),
     ("amenity", "drinking_water"), "drinking water"),
    (re.compile(r"\b(ev chargers?|charging stations?|electric vehicle chargers?|superchargers?)\b", re.I),
     ("amenity", "charging_station"), "EV charger"),
    (re.compile(r"\b(atms?|cash machines?)\b", re.I), ("amenity", "atm"), "ATM"),
    (re.compile(r"\b(coffee|cafes?|espresso)\b", re.I), ("amenity", "cafe"), "cafe"),
    (re.compile(r"\b(pharmac(?:y|ies)|chemists?)\b", re.I), ("amenity", "pharmacy"), "pharmacy"),
    (re.compile(r"\b(toilets?|restrooms?|bathrooms?|loos?|\bwc\b)\b", re.I), ("amenity", "toilets"), "public toilet"),
    (re.compile(r"\b(benches?)\b", re.I), ("amenity", "bench"), "bench"),
    (re.compile(r"\b(playgrounds?)\b", re.I), ("leisure", "playground"), "playground"),
    (re.compile(r"\b(picnic (?:tables?|sites?))\b", re.I), ("tourism", "picnic_site"), "picnic site"),
    (re.compile(r"\b(parks?|gardens?)\b", re.I), ("leisure", "park"), "park"),
    (re.compile(r"\b(viewpoints?|scenic spots?)\b", re.I), ("tourism", "viewpoint"), "viewpoint"),
    (re.compile(r"\b(supermarkets?|grocer(?:y|ies)|grocery stores?)\b", re.I), ("shop", "supermarket"), "supermarket"),
    (re.compile(r"\b(baker(?:y|ies))\b", re.I), ("shop", "bakery"), "bakery"),
    (re.compile(r"\b(book ?stores?|bookshops?)\b", re.I), ("shop", "books"), "bookshop"),
    (re.compile(r"\b(laund(?:ry|ries)|laundromats?)\b", re.I), ("shop", "laundry"), "laundry"),
    (re.compile(r"\b(bars?|pubs?)\b", re.I), ("amenity", "bar"), "bar"),
    (re.compile(r"\b(fast food|burgers?)\b", re.I), ("amenity", "fast_food"), "fast food"),
    (re.compile(r"\b(ice cream|gelato)\b", re.I), ("amenity", "ice_cream"), "ice cream"),
    (re.compile(r"\b(restaurants?|places? to eat)\b", re.I), ("amenity", "restaurant"), "restaurant"),
    (re.compile(r"\b(banks?)\b", re.I), ("amenity", "bank"), "bank"),
    (re.compile(r"\b(post offices?|post boxes?|mailboxes?)\b", re.I), ("amenity", "post_box"), "post box"),
    (re.compile(r"\b(librar(?:y|ies))\b", re.I), ("amenity", "library"), "library"),
    (re.compile(r"\b(hospitals?|emergency rooms?)\b", re.I), ("amenity", "hospital"), "hospital"),
    (re.compile(r"\b(doctors?|clinics?|urgent care)\b", re.I), ("amenity", "doctors"), "doctor"),
    (re.compile(r"\b(dentists?)\b", re.I), ("amenity", "dentist"), "dentist"),
    (re.compile(r"\b(vets?|veterinar(?:y|ies))\b", re.I), ("amenity", "veterinary"), "vet"),
    (re.compile(r"\b(museums?)\b", re.I), ("tourism", "museum"), "museum"),
    (re.compile(r"\b(hotels?|hostels?)\b", re.I), ("tourism", "hotel"), "hotel"),
    (re.compile(r"\b(fuel|gas stations?|petrol|petrol stations?)\b", re.I), ("amenity", "fuel"), "fuel station"),
    (re.compile(r"\b(bike parking|bicycle parking)\b", re.I), ("amenity", "bicycle_parking"), "bike parking"),
    (re.compile(r"\b(gyms?|fitness (?:centres?|centers?))\b", re.I), ("leisure", "fitness_centre"), "gym"),
    (re.compile(r"\b(police)\b", re.I), ("amenity", "police"), "police station"),
    (re.compile(r"\b(fire stations?)\b", re.I), ("amenity", "fire_station"), "fire station"),
    (re.compile(r"\b(parking|car parks?)\b", re.I), ("amenity", "parking"), "parking"),
    (re.compile(r"\b(cinemas?|movie theaters?|movie theatres?)\b", re.I), ("amenity", "cinema"), "cinema"),
    (re.compile(r"\b(bus stops?)\b", re.I), ("highway", "bus_stop"), "bus stop"),
)

#: Three good tags to show per POI when OSM has them, in the order they are printed.
_TAG_ORDER = ("opening_hours", "wheelchair", "fee", "operator", "brand", "access", "cuisine",
              "addr:housenumber", "addr:street")

_KM_RE = re.compile(r"\bwithin\s+(\d+(?:\.\d+)?)\s*(km|kilomet(?:er|re)s?|m|meters?|metres?|mi|miles?)\b",
                    re.I)
_METRES_RE = re.compile(r"\b(\d+)\s*(m|meters?|metres?)\s+(?:of|from|around)\b", re.I)

DEFAULT_RADIUS_M = 800
MAX_RADIUS_M = 20000


class OsmError(RuntimeError):
    """An OpenStreetMap service could not be read."""


def haversine_km(first, second) -> float:
    """Great-circle distance in km between two (latitude, longitude) pairs."""
    lat1, lon1 = float(first[0]), float(first[1])
    lat2, lon2 = float(second[0]), float(second[1])
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi, d_lambda = phi2 - phi1, math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def kinds_from_text(text: str) -> list[tuple[str, str, str]]:
    """Every POI kind a question mentions: [(key, value, label)]."""
    found = []
    for pattern, (key, value), label in KINDS:
        if pattern.search(text) and (key, value, label) not in found:
            found.append((key, value, label))
    return found


def radius_from_text(text: str) -> int | None:
    """A radius in metres if the question states one, capped, or None."""
    match = _KM_RE.search(text) or _METRES_RE.search(text)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith("km") or unit.startswith("kilomet"):
        metres = amount * 1000
    elif unit.startswith("mi"):
        metres = amount * 1609.34
    else:
        metres = amount
    return int(max(50, min(metres, MAX_RADIUS_M)))


class OsmData:
    """Geocoding through Nominatim, POI search through Overpass."""

    def __init__(self, fetch=None, cache_ttl: float = 3600.0, timeout: float = 30.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("OSM_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("OSM_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("OSM_USER_AGENT") or DEFAULT_USER_AGENT
        self._fetch = fetch or self._http_text
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    # -- transport ---------------------------------------------------------

    def _http_text(self, url: str, params: dict, data: bytes | None = None,
                   content_type: str | None = None) -> str:
        target = f"{url}?{parse.urlencode(params)}" if params and data is None else url
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        req = request.Request(target, data=data, headers=headers)
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # urllib raises many types; callers see one error type
            raise OsmError(f"OpenStreetMap request failed: {exc}") from exc

    def _json(self, key: str, call, ttl: float | None = None):
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        payload = call()
        if isinstance(payload, (str, bytes)):
            try:
                payload = json.loads(payload)
            except ValueError as exc:
                raise OsmError(f"OpenStreetMap returned something that is not JSON: {exc}") from exc
        with self._lock:
            self._cache[key] = (now + (ttl if ttl is not None else self.cache_ttl), payload)
        return payload

    # -- geocoding ---------------------------------------------------------

    def geocode(self, place: str) -> dict:
        """A place name to its best match, or a clear error."""
        query = str(place or "").strip()
        if not query:
            raise ValueError("give me a place to look up, for example Bryant Park, New York")
        payload = self._json(
            f"geocode:{query.lower()}",
            lambda: self._fetch(f"{NOMINATIM}/search",
                                {"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 0},
                                None, None),
            ttl=86400.0,
        )
        if not isinstance(payload, list) or not payload:
            raise ValueError(f"OpenStreetMap has no place called {query!r}")
        best = payload[0]
        try:
            latitude, longitude = float(best["lat"]), float(best["lon"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OsmError(f"Nominatim returned no coordinates for {query!r}") from exc
        return {
            "query": query,
            "name": best.get("name") or (best.get("display_name") or "").split(",")[0],
            "display_name": best.get("display_name") or query,
            "latitude": latitude,
            "longitude": longitude,
            "kind": best.get("type") or best.get("category") or "",
        }

    # -- points of interest ------------------------------------------------

    @staticmethod
    def overpass_query(latitude: float, longitude: float, key: str, value: str, radius: int,
                       limit: int = MAX_EXAMPLES) -> str:
        """The Overpass QL for 'everything tagged key=value within radius of a point'."""
        return (f"[out:json][timeout:25];\n"
                f'nwr(around:{int(radius)},{float(latitude)},{float(longitude)})["{key}"="{value}"];\n'
                f"out center tags {int(limit)};")

    def pois(self, latitude: float, longitude: float, key: str, value: str, radius: int,
             limit: int = 8) -> dict:
        """Points tagged key=value near a point, nearest first."""
        origin = (float(latitude), float(longitude))
        span = int(max(50, min(int(radius), MAX_RADIUS_M)))
        query = self.overpass_query(origin[0], origin[1], key, value, span)
        body = parse.urlencode({"data": query}).encode()
        last_error: Exception | None = None
        payload = None
        for mirror in OVERPASS_MIRRORS:
            try:
                payload = self._fetch(mirror, {}, body, "application/x-www-form-urlencoded")
                if isinstance(payload, (str, bytes)):
                    payload = json.loads(payload)
                break
            except Exception as exc:  # try the next mirror, then report
                last_error = exc
                payload = None
        if payload is None:
            raise OsmError(f"Overpass did not answer on either mirror: {last_error}")
        if not isinstance(payload, dict) or "elements" not in payload:
            raise OsmError("Overpass returned an unexpected payload")

        found = []
        for element in payload.get("elements") or []:
            tags = element.get("tags") or {}
            centre = element.get("center") or {}
            lat = element.get("lat", centre.get("lat"))
            lon = element.get("lon", centre.get("lon"))
            if lat is None or lon is None:
                continue
            distance = haversine_km(origin, (float(lat), float(lon)))
            found.append({
                "name": tags.get("name") or "",
                "kind": tags.get(key) or value,
                "latitude": float(lat),
                "longitude": float(lon),
                "distance_km": round(distance, 3),
                "osm_id": f"{element.get('type', 'node')}/{element.get('id')}",
                "tags": {tag: tags[tag] for tag in _TAG_ORDER if tag in tags},
            })
        found.sort(key=lambda row: row["distance_km"])
        # One real place is often mapped twice (a node and a way). Keep the nearest of each
        # name inside 50 m instead of printing 'Diana Ross Playground' twice.
        deduped: list[dict] = []
        for row in found:
            twin = next((kept for kept in deduped
                         if row["name"] and kept["name"].lower() == row["name"].lower()
                         and abs(kept["distance_km"] - row["distance_km"]) < 0.05), None)
            if twin is None:
                deduped.append(row)
        found = deduped
        return {
            "dataset": DATASET,
            "key": key,
            "value": value,
            "radius_m": span,
            "total": len(found),
            "capped": len(found) >= MAX_EXAMPLES,
            "pois": found[: max(1, min(int(limit), 25))],
        }
