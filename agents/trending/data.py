"""What the world is reading, from Wikimedia's own pageview metrics.

Verified live on 2026-09-23 (keyless):

  https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/2026/09/20
  .../per-article/en.wikipedia/all-access/user/Lizzie_Borden/daily/20260915/20260921

Two things the payload makes plain and this reader keeps: the numbers are **yesterday's**
(these metrics lag by a day, UTC, and asking for today answers 404), and the top list is full of
navigation rather than reading - `Main_Page` is always first with millions of hits, and
`Special:Search` sits near the top. Those are dropped, and the count that was dropped is
reported, so "top 10" never quietly means "top 10 after I hid five rows".
"""

from __future__ import annotations

import datetime
import json
import os
import time
from urllib import parse, request

BASE = "https://wikimedia.org/api/rest_v1/metrics/pageviews"

DATASET = "Wikimedia pageview metrics (wikimedia.org/api/rest_v1)"

DEFAULT_USER_AGENT = "awesome-acps-trending/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: Projects worth asking about by short name.
PROJECTS = {"wikipedia": "en.wikipedia", "en": "en.wikipedia", "de": "de.wikipedia",
            "es": "es.wikipedia", "fr": "fr.wikipedia", "it": "it.wikipedia",
            "ja": "ja.wikipedia", "nl": "nl.wikipedia", "pl": "pl.wikipedia",
            "pt": "pt.wikipedia", "ru": "ru.wikipedia", "zh": "zh.wikipedia",
            "ar": "ar.wikipedia", "uk": "uk.wikipedia"}

#: Titles that are navigation, not reading.
NOT_READING = ("Main_Page", "Special:", "Wikipedia:", "Portal:", "Help:", "Template:")


class TrendingError(RuntimeError):
    """The pageview metrics could not be read."""


def yesterday(days_back: int = 1) -> datetime.date:
    return datetime.date.today() - datetime.timedelta(days=days_back)


def readable_title(article: str) -> str:
    """Lizzie_Borden -> 'Lizzie Borden'; leaves namespaces alone."""
    return str(article or "").replace("_", " ")


class TrendingData:
    """The top list and one article's daily views."""

    def __init__(self, fetch=None, cache_ttl: float = 1800.0, timeout: float = 25.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("TRENDING_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("TRENDING_HTTP_TIMEOUT", timeout))
        self.user_agent = (user_agent or os.environ.get("TRENDING_USER_AGENT")
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
            raise TrendingError(f"request failed: {exc}") from exc

    def _json(self, url: str, ttl: float | None = None) -> dict:
        now = time.time()
        hit = self._cache.get(url)
        if hit and hit[0] > now:
            return hit[1]
        payload = self._fetch(url, {})
        if isinstance(payload, (str, bytes)):
            try:
                payload = json.loads(payload)
            except ValueError as exc:
                raise TrendingError(f"the metrics returned something that is not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise TrendingError("the metrics returned an unexpected payload")
        self._cache[url] = (now + (ttl if ttl is not None else self.cache_ttl), payload)
        return payload

    def project(self, name: str | None) -> str:
        text = str(name or "wikipedia").strip().lower()
        if text.endswith(".wikipedia"):
            return text
        if text not in PROJECTS:
            raise ValueError(f"I do not know the project {name!r}; try one of: "
                             + ", ".join(sorted(set(PROJECTS))))
        return PROJECTS[text]

    # -- reads -------------------------------------------------------------

    def top(self, project: str | None = None, limit: int = 10, when=None) -> dict:
        """The most-read articles for a day (the metrics lag, so that day is yesterday)."""
        wanted = max(1, min(int(limit), 25))
        name = self.project(project)
        date = when or yesterday()
        url = f"{BASE}/top/{name}/all-access/{date.year:04d}/{date.month:02d}/{date.day:02d}"
        payload = self._json(url, ttl=1800.0)
        items = payload.get("items") or []
        if not items:
            raise TrendingError("no pageview item came back for that day")
        articles = items[0].get("articles") or []
        if not articles:
            raise TrendingError("the day's article list was empty")
        rows, skipped = [], 0
        for row in articles:
            article = str(row.get("article") or "")
            if any(article.startswith(prefix) for prefix in NOT_READING):
                skipped += 1
                continue
            rows.append({"rank": len(rows) + 1, "article": article,
                         "title": readable_title(article), "views": row.get("views")})
            if len(rows) >= wanted:
                break
        return {"project": name, "date": date.isoformat(), "rows": rows,
                "navigation_skipped": skipped, "listed": len(articles),
                "source": DATASET}

    def article(self, article: str, project: str | None = None, days: int = 7) -> dict:
        """One article's daily views over a window, and its total."""
        title = str(article or "").strip().replace(" ", "_")
        if not title:
            raise ValueError("tell me which article, for example Lizzie Borden")
        name = self.project(project)
        wanted = max(2, min(int(days), 60))
        end = yesterday()
        start = end - datetime.timedelta(days=wanted - 1)
        url = (f"{BASE}/per-article/{name}/all-access/user/{parse.quote(title)}/daily/"
               f"{start.strftime('%Y%m%d')}/{end.strftime('%Y%m%d')}")
        payload = self._json(url, ttl=1800.0)
        items = payload.get("items") or []
        if not items:
            raise TrendingError(f"no pageviews were recorded for {title!r} in that window")
        rows = [{"date": f"{str(row['timestamp'])[:4]}-{str(row['timestamp'])[4:6]}-"
                        f"{str(row['timestamp'])[6:8]}",
                 "views": row.get("views")}
                for row in items]
        return {"article": title, "title": readable_title(title), "project": name,
                "rows": rows, "total": sum(row["views"] or 0 for row in rows),
                "best": max(rows, key=lambda row: row["views"] or 0),
                "from": start.isoformat(), "to": end.isoformat(), "source": DATASET}
