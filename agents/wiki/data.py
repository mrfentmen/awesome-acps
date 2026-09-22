"""Read-only Wikipedia/Wikidata reader for the wiki agent.

  https://en.wikipedia.org/api/rest_v1/page/summary/{title}   article summaries
  https://www.wikidata.org/w/api.php                          entity search
  https://api.wikimedia.org/feed/v1/.../onthisday/...         selected anniversaries

All keyless, verified live on 2026-09-22 (Wikidata returned Q42 for Douglas Adams; the
on-this-day feed carried the MAVEN Mars-orbit entry for 09/22).

Wikipedia asks for an identifying User-Agent (the repo sets one) and returns plain prose;
this reader keeps the prose and the licence pointer instead of rewriting either.
"""

from __future__ import annotations

import json
import os
import threading
import time
from urllib import parse, request

SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/"
WIKIDATA_URL = "https://www.wikidata.org/w/api.php"
ONTHISDAY_URL = "https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/selected/"

DATASET_WIKI = "en.wikipedia.org/api/rest_v1/page/summary"
DATASET_WIKIDATA = "wikidata.org/w/api.php (wbsearchentities)"
DATASET_ONTHISDAY = "api.wikimedia.org/feed/v1/wikipedia/en/onthisday/selected"

DEFAULT_USER_AGENT = "awesome-acps-wiki/1.0 (+https://github.com/mrfentmen/awesome-acps)"


class WikiError(RuntimeError):
    """A Wikipedia or Wikidata feed could not be read."""


class WikiNotFound(WikiError):
    """Wikipedia has no page under that title."""


class WikiData:
    """Wikipedia summaries, Wikidata entity search and on-this-day, with caching."""

    def __init__(self, fetch=None, cache_ttl: float = 3600.0, timeout: float = 20.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("WIKI_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("WIKI_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("WIKI_USER_AGENT") or DEFAULT_USER_AGENT
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
            if getattr(exc, "code", None) == 404:
                raise WikiNotFound(f"no Wikipedia page for {url.rsplit('/', 1)[-1]!r}") from exc
            raise WikiError(f"request failed: {exc}") from exc

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
    def check_title(value: str) -> str:
        title = " ".join(str(value).strip().split())
        if len(title) < 2:
            raise ValueError("a title is at least two characters, like 'Ada Lovelace'")
        return title

    # -- readings ----------------------------------------------------------

    def page(self, title: str) -> dict:
        name = self.check_title(title)
        url = SUMMARY_URL + parse.quote(name.replace(" ", "_"), safe="")
        payload = self._get(url, {}, ttl=self.cache_ttl)
        if not isinstance(payload, dict) or not payload.get("extract"):
            raise WikiError("Wikipedia returned an unexpected payload (no extract)")
        urls = payload.get("content_urls") or {}
        return {
            "dataset": DATASET_WIKI,
            "title": payload.get("title") or name,
            "description": payload.get("description"),
            "extract": payload.get("extract"),
            "wikibase_id": payload.get("wikibase_item"),
            "url": (urls.get("desktop") or {}).get("page"),
            "timestamp": payload.get("timestamp"),
        }

    def entity(self, query: str, limit: int = 3) -> dict:
        text = self.check_title(query)
        rows = max(1, min(10, int(limit)))
        payload = self._get(WIKIDATA_URL, {
            "action": "wbsearchentities", "search": text, "language": "en",
            "uselang": "en", "format": "json", "limit": str(rows),
        }, ttl=self.cache_ttl)
        search = payload.get("search") if isinstance(payload, dict) else None
        if not isinstance(search, list):
            raise WikiError("Wikidata returned an unexpected payload (no search list)")
        return {
            "dataset": DATASET_WIKIDATA,
            "query": text,
            "rows": [{
                "id": row.get("id"),
                "label": row.get("label"),
                "description": row.get("description"),
                "url": row.get("concepturi") or row.get("url"),
            } for row in search[:rows]],
        }

    def get_entity(self, qid: str) -> dict:
        identifier = str(qid).strip().upper()
        payload = self._get(WIKIDATA_URL, {
            "action": "wbgetentities", "ids": identifier,
            "props": "labels|descriptions|sitelinks", "languages": "en",
            "sitefilter": "enwiki", "format": "json",
        }, ttl=self.cache_ttl)
        entities = payload.get("entities") if isinstance(payload, dict) else None
        if not isinstance(entities, dict) or not entities:
            raise WikiError("Wikidata returned an unexpected payload (no entities)")
        entity = next(iter(entities.values()))
        if entity.get("missing") is not None:
            raise WikiNotFound(f"Wikidata has no entity {identifier}")
        labels = entity.get("labels") or {}
        descriptions = entity.get("descriptions") or {}
        sitelinks = entity.get("sitelinks") or {}
        return {
            "dataset": DATASET_WIKIDATA,
            "id": identifier,
            "label": (labels.get("en") or {}).get("value"),
            "description": (descriptions.get("en") or {}).get("value"),
            "article": (sitelinks.get("enwiki") or {}).get("title"),
        }

    def on_this_day(self, month: int, day: int) -> dict:
        stamp = f"{int(month):02d}/{int(day):02d}"
        payload = self._get(ONTHISDAY_URL + stamp, {}, ttl=self.cache_ttl)
        selected = payload.get("selected") if isinstance(payload, dict) else None
        if not isinstance(selected, list):
            raise WikiError("the Wikimedia feed returned an unexpected payload (no selected list)")
        rows = []
        for item in selected[:5]:
            pages = item.get("pages") or []
            rows.append({
                "year": item.get("year"),
                "text": item.get("text"),
                "page": (pages[0] or {}).get("title") if pages else None,
            })
        return {"dataset": DATASET_ONTHISDAY, "date": stamp, "rows": rows}
