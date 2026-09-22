"""Read-only Launch Library 2 reader for the launches agent.

Self-contained on purpose: the Launch Library transport lives here with one small cache.

  https://ll.thespacedevs.com/2.3.0/launches/upcoming/   the global launch schedule
  https://ll.thespacedevs.com/2.3.0/launches/previous/   launches that already flew
  https://ll.thespacedevs.com/2.3.0/launches/<uuid>/     one launch, in full

Keyless, verified live on 2026-09-22 (369 launches on the upcoming list; the newest entry
was a Long March 8A with a 2026-09-23T13:30:00Z window, and `search=Starlink` returned a
Starship flight for 2026-09-28).

Two real constraints are built in, not hidden:
  * the free host is rate limited (15 requests per hour per IP), so the default cache is
    15 minutes and repeated questions do not re-hit it;
  * a launch window is a plan. `net` is the no-earlier-than time and it moves - the answer
    prints the window and the status instead of pretending the time is fixed.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib import parse, request

BASE_URL = "https://ll.thespacedevs.com/2.3.0"
DATASET_UPCOMING = "ll.thespacedevs.com/2.3.0/launches/upcoming"
DATASET_PREVIOUS = "ll.thespacedevs.com/2.3.0/launches/previous"
DATASET_DETAIL = "ll.thespacedevs.com/2.3.0/launches"

DEFAULT_USER_AGENT = "awesome-acps-launches/1.0 (+https://github.com/mrfentmen/awesome-acps)"

_UUID_RE = re.compile(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b", re.IGNORECASE)


class LaunchesError(RuntimeError):
    """A Launch Library response could not be read."""


class LaunchesData:
    """Launch Library 2, read over HTTP with caching and validation."""

    def __init__(self, fetch=None, cache_ttl: float = 900.0, timeout: float = 20.0,
                 user_agent: str | None = None) -> None:
        # The free host allows 15 requests/hour per IP, so the cache default is deliberately long.
        self.cache_ttl = float(os.environ.get("LAUNCHES_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("LAUNCHES_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("LAUNCHES_USER_AGENT") or DEFAULT_USER_AGENT
        self._fetch = fetch or self._http_get
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    # -- transport ---------------------------------------------------------

    def _http_get(self, url: str, params: dict):
        query = parse.urlencode(params) if params else ""
        full = f"{url}?{query}" if query else url
        req = request.Request(full, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # urllib raises many types; callers see one error type
            raise LaunchesError(f"launch library request failed: {exc}") from exc

    def _get(self, url: str, params: dict, ttl: float | None = None):
        key = f"{url}:{json.dumps({k: str(v) for k, v in sorted(params.items())})}"
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        payload = self._fetch(url, params)
        with self._lock:
            self._cache[key] = (now + (ttl if ttl is not None else self.cache_ttl), payload)
        return payload

    # -- validation --------------------------------------------------------

    @staticmethod
    def check_limit(value, ceiling: int = 25) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError):
            raise ValueError("a limit must be a whole number") from None
        if not 1 <= limit <= ceiling:
            raise ValueError(f"a limit must be between 1 and {ceiling}")
        return limit

    @staticmethod
    def check_days(value) -> int:
        try:
            days = int(value)
        except (TypeError, ValueError):
            raise ValueError("days must be a whole number") from None
        if not 1 <= days <= 365:
            raise ValueError("days must be between 1 and 365")
        return days

    @staticmethod
    def check_id(value: str) -> str:
        match = _UUID_RE.search(str(value))
        if not match:
            raise ValueError("a launch id is a uuid, for example 63063b9d-ade9-4448-8717-6f910aa81188")
        return match.group(1).lower()

    @staticmethod
    def check_search(value: str) -> str:
        text = re.sub(r"\s+", " ", str(value)).strip(" ?.!,")
        if len(text) < 2:
            raise ValueError("a launch search needs at least two characters")
        if len(text) > 100:
            raise ValueError("that launch search is too long - keep it under 100 characters")
        return text

    # -- readers -----------------------------------------------------------

    def upcoming(self, limit: int = 5, search: str | None = None, days: int | None = None) -> dict:
        size = self.check_limit(limit)
        params: dict[str, str] = {"limit": str(size)}
        if search:
            params["search"] = self.check_search(search)
        payload = self._get(f"{BASE_URL}/launches/upcoming/", params)
        results = payload.get("results") if isinstance(payload, dict) else None
        if results is None:
            raise LaunchesError("Launch Library returned an unexpected payload (no results)")
        launches = [self._one_launch(row) for row in results]
        if days:
            horizon = datetime.now(timezone.utc) + timedelta(days=self.check_days(days))
            launches = [item for item in launches
                        if item["net"] and self._parse(item["net"])
                        and self._parse(item["net"]) <= horizon]
        return {
            "dataset": DATASET_UPCOMING,
            "count": int(payload.get("count") or len(launches)),
            "search": search,
            "days": days,
            "launches": launches,
        }

    def recent(self, limit: int = 5, search: str | None = None) -> dict:
        size = self.check_limit(limit)
        params: dict[str, str] = {"limit": str(size)}
        if search:
            params["search"] = self.check_search(search)
        payload = self._get(f"{BASE_URL}/launches/previous/", params)
        results = payload.get("results") if isinstance(payload, dict) else None
        if results is None:
            raise LaunchesError("Launch Library returned an unexpected payload (no results)")
        return {
            "dataset": DATASET_PREVIOUS,
            "count": int(payload.get("count") or 0),
            "search": search,
            "launches": [self._one_launch(row) for row in results],
        }

    def detail(self, launch_id: str) -> dict:
        identity = self.check_id(launch_id)
        payload = self._get(f"{BASE_URL}/launches/{identity}/", {})
        if not isinstance(payload, dict) or not payload.get("name"):
            raise LaunchesError(f"Launch Library has no launch at {identity}")
        launch = self._one_launch(payload)
        mission = payload.get("mission") or {}
        description = mission.get("description")
        launch.update({
            "dataset": DATASET_DETAIL,
            "mission_description": re.sub(r"\s+", " ", description).strip() if description else None,
            "failreason": payload.get("failreason") or None,
            "probability": payload.get("probability"),
            "webcast_live": payload.get("webcast_live"),
            "agency_launch_attempt_count": payload.get("agency_launch_attempt_count"),
        })
        return launch

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _parse(value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _one_launch(row: dict) -> dict:
        mission = row.get("mission") or {}
        pad = row.get("pad") or {}
        location = pad.get("location") or {}
        status = row.get("status") or {}
        provider = row.get("launch_service_provider") or {}
        rocket = (row.get("rocket") or {}).get("configuration") or {}
        return {
            "id": row.get("id"),
            "name": row.get("name"),
            "net": row.get("net"),
            "net_precision": (row.get("net_precision") or {}).get("name"),
            "status": status.get("abbrev") or status.get("name"),
            "provider": provider.get("name"),
            "rocket": rocket.get("full_name") or rocket.get("name"),
            "mission": mission.get("name"),
            "mission_type": mission.get("type"),
            "orbit": (mission.get("orbit") or {}).get("name"),
            "pad": pad.get("name"),
            "location": location.get("name"),
            "window_start": row.get("window_start"),
            "window_end": row.get("window_end"),
            "url": row.get("url"),
        }
