"""Reads for the `watch` agent: a snapshot of a live feed, taken on demand, over and over.

Three keyless feeds, all verified live on 2026-09-22/23:

  earthquakes: https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson
  geomagnetic: https://services.swpc.noaa.gov/json/planetary_k_index_1m.json
  space station: http://api.open-notify.org/iss-now.json

Two quirks worth knowing:

1. The USGS feed is *not* sorted by time, so the newest event is found by comparing
   timestamps, never by trusting file order.
2. open-notify's HTTPS endpoint hangs (the same finding the `iss` agent records), so the
   plain HTTP endpoint is used deliberately, with a short timeout.

Nothing here sleeps, loops or schedules. It answers "what does the feed say right now" and
the agent decides how often to ask, so the pacing lives in exactly one place.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from urllib import parse, request

USGS_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson"
KP_URL = "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"
ISS_URL = "http://api.open-notify.org/iss-now.json"

DATASET = "earthquake.usgs.gov all_hour + swpc.noaa.gov Kp-1m + api.open-notify.org/iss-now"

#: What can be watched, and what each one is.
FEEDS = {
    "quakes": "every earthquake in the past hour (USGS)",
    "aurora": "the one-minute planetary K index, NOAA SWPC",
    "iss": "where the International Space Station is",
}

DEFAULT_USER_AGENT = "awesome-acps-watch/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: Hard limits so a watch can never become an all-night poll.
MAX_ROUNDS = 20
MAX_INTERVAL = 120.0
MAX_SECONDS = 180.0

#: NOAA's geomagnetic storm scale: Kp 5 and up is a storm, graded G1-G5.
STORM_SCALE = {5: "G1 (minor)", 6: "G2 (moderate)", 7: "G3 (strong)",
               8: "G4 (severe)", 9: "G5 (extreme)"}

#: How fast the station travels, for a sanity check on movement between snapshots (km/s).
ISS_SPEED_KM_S = 7.66


class WatchError(RuntimeError):
    """A feed could not be read or did not look like the feed it claims to be."""


def storm_level(kp: float) -> str | None:
    """Kp -> 'G3 (strong)', or None when it is below storm level."""
    if kp is None:
        return None
    for step in sorted(STORM_SCALE, reverse=True):
        if float(kp) >= step:
            return STORM_SCALE[step]
    return None


def utc(epoch: float | None) -> str | None:
    """An epoch second as `2026-09-23T01:11:52Z`."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between two (latitude, longitude) points in km."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def bounds(rounds=None, interval=None) -> tuple[int, float]:
    """Rounds and seconds, clamped to what this agent is allowed to do."""
    try:
        count = int(rounds) if rounds is not None else int(os.environ.get("WATCH_AGENT_ROUNDS", 3))
    except (TypeError, ValueError):
        count = 3
    try:
        gap = float(interval) if interval is not None else float(os.environ.get("WATCH_AGENT_INTERVAL", 15.0))
    except (TypeError, ValueError):
        gap = 15.0
    count = max(1, min(count, MAX_ROUNDS))
    gap = max(0.0, min(gap, MAX_INTERVAL))
    return count, min(gap, MAX_SECONDS / count)


class WatchData:
    """One snapshot per call, from a keyless feed. Never sleeps; the agent owns the pacing."""

    def __init__(self, fetch=None, timeout: float | None = None, user_agent: str | None = None) -> None:
        self.timeout = float(os.environ.get("WATCH_AGENT_TIMEOUT", timeout or 20.0))
        self.user_agent = user_agent or os.environ.get("WATCH_AGENT_USER_AGENT") or DEFAULT_USER_AGENT
        self._fetch = fetch or self._http_get
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    # -- transport ---------------------------------------------------------

    def _http_get(self, url: str, params: dict):
        full = url if not params else f"{url}?{parse.urlencode(params)}"
        req = request.Request(full, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # urllib raises many types; callers see one error type
            raise WatchError(f"feed request failed: {exc}") from exc

    def _get(self, url: str, params: dict | None = None, ttl: float = 0.0):
        """A feed read. `ttl=0` means always fresh: a watch is only useful if it is current."""
        key = f"{url}:{json.dumps({k: str(v) for k, v in sorted((params or {}).items())})}"
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        payload = self._fetch(url, params or {})
        with self._lock:
            self._cache[key] = (time.time() + ttl, payload)
        return payload

    # -- feeds -------------------------------------------------------------

    def snapshot(self, feed: str, min_magnitude=None) -> dict:
        """A feed reading as {'feed', 'at', 'line', 'value', 'data'}.

        `value` is the part that changes - what the agent compares between rounds to say
        whether anything actually moved.
        """
        name = str(feed or "").strip().lower()
        if name not in FEEDS:
            raise ValueError(f"I can watch {', '.join(FEEDS)} - not {feed!r}")
        if name == "quakes":
            return self.quakes(min_magnitude)
        if name == "aurora":
            return self.aurora()
        return self.iss()

    def quakes(self, min_magnitude=None) -> dict:
        """The past hour of earthquakes, newest first."""
        payload = self._get(USGS_URL)
        features = payload.get("features") if isinstance(payload, dict) else None
        if features is None:
            raise WatchError("USGS returned a payload with no features")
        events = []
        for feature in features:
            props = feature.get("properties") or {}
            events.append({
                "id": feature.get("id"),
                "magnitude": props.get("mag"),
                "place": props.get("place"),
                "time": props.get("time"),
                "epoch": (props.get("time") or 0) / 1000 if props.get("time") else None,
                "url": props.get("url"),
            })
        # File order is not chronological; the newest event is the one with the newest stamp.
        events.sort(key=lambda event: event["time"] or 0, reverse=True)
        matched = [event for event in events
                   if min_magnitude is None or (event["magnitude"] is not None
                                                and event["magnitude"] >= min_magnitude)]
        newest = events[0] if events else None
        biggest = max(matched, key=lambda event: event["magnitude"] or -99, default=None)
        at = utc(newest["epoch"]) if newest else None
        label = (f"M{newest['magnitude']:.1f} {newest['place']}" if newest and newest["magnitude"] is not None
                 else "nothing recorded in the past hour")
        count = len(matched)
        line = f"{count} quake(s) in the past hour"
        if min_magnitude is not None:
            line += f" at or above M{float(min_magnitude):.1f}"
        line += f" - newest {label}"
        return {
            "feed": "quakes",
            "at": at,
            "line": line,
            "value": newest["id"] if newest else None,
            "data": {"count": count, "total": len(events), "newest": newest, "biggest": biggest,
                     "events": matched[:10]},
        }

    def aurora(self) -> dict:
        """The latest one-minute planetary K index."""
        payload = self._get(KP_URL)
        if not isinstance(payload, list):
            raise WatchError("NOAA SWPC returned a payload that is not a list")
        rows = [row for row in payload
                if isinstance(row, dict) and row.get("estimated_kp") is not None]
        if not rows:
            raise WatchError("NOAA SWPC sent no Kp readings")
        # The file grows in time order, but the newest reading is picked by timestamp anyway.
        latest = max(rows, key=lambda row: str(row.get("time_tag") or ""))
        kp = float(latest["estimated_kp"])
        level = storm_level(kp)
        line = f"Kp {kp:.2f}" + (f" - {level}" if level else " - below storm level")
        return {
            "feed": "aurora",
            "at": str(latest.get("time_tag") or "").replace(" ", "T") + "Z",
            "line": line,
            "value": round(kp, 2),
            "data": {"kp": kp, "kp_index": latest.get("kp_index"), "storm": level,
                     "readings": len(rows), "time_tag": latest.get("time_tag")},
        }

    def iss(self) -> dict:
        """Where the station is right now.

        HTTP on purpose: the HTTPS endpoint of this service accepts the connection and then
        never answers, which is the same finding the `iss` agent records.
        """
        payload = self._get(ISS_URL)
        position = payload.get("iss_position") if isinstance(payload, dict) else None
        if not isinstance(position, dict) or position.get("latitude") is None:
            raise WatchError("open-notify returned no position")
        latitude, longitude = float(position["latitude"]), float(position["longitude"])
        stamp = payload.get("timestamp")
        hemisphere = f"{abs(latitude):.2f} {'N' if latitude >= 0 else 'S'}, " \
                     f"{abs(longitude):.2f} {'E' if longitude >= 0 else 'W'}"
        return {
            "feed": "iss",
            "at": utc(stamp),
            "line": f"station at {hemisphere}",
            "value": (round(latitude, 3), round(longitude, 3), stamp),
            "data": {"latitude": latitude, "longitude": longitude, "timestamp": stamp,
                     "speed_km_s": ISS_SPEED_KM_S},
        }

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).strftime("%H:%M:%SZ")
