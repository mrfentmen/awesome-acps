"""Read-only Open Library reader for the books agent.

Self-contained on purpose: the Open Library transport lives here with one small cache.

  https://openlibrary.org/search.json                    books by title, author or subject
  https://openlibrary.org/search/authors.json            authors by name
  https://openlibrary.org/works/OL893414W.json           one work: description, subjects
  https://openlibrary.org/authors/OL31353A.json          one author: bio, dates
  https://openlibrary.org/authors/OL31353A/works.json    an author's works
  https://openlibrary.org/trending/daily.json            what people are reading today

Keyless, verified live on 2026-09-22 (search "dune" returned 48,208 works; the Dune work
  record carried a 1-paragraph description and subjects; Ursula K. Le Guin's author record
  carried a bio with her dates).

Two quirks of the real data are handled here rather than papered over:
  * a work's `description` is either a string or a {"type": ..., "value": ...} object by
    edition, and it is frequently absent;
  * an author's works list comes back in Open Library's own order, which is not
    chronological. The answer says so instead of pretending it is ranked.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from urllib import parse, request

BASE_URL = "https://openlibrary.org"
DATASET_SEARCH = "openlibrary.org/search.json"
DATASET_AUTHOR_SEARCH = "openlibrary.org/search/authors.json"
DATASET_WORK = "openlibrary.org/works"
DATASET_AUTHOR = "openlibrary.org/authors"
DATASET_TRENDING = "openlibrary.org/trending/daily.json"

DEFAULT_USER_AGENT = "awesome-acps-books/1.0 (+https://github.com/mrfentmen/awesome-acps)"

SEARCH_FIELDS = ("key", "title", "author_name", "first_publish_year", "edition_count", "language")

_WORK_RE = re.compile(r"(OL\d+W)", re.IGNORECASE)
_AUTHOR_KEY_RE = re.compile(r"(OL\d+A)", re.IGNORECASE)


class BooksError(RuntimeError):
    """An Open Library response could not be read."""


class BooksData:
    """Open Library, read over HTTP with caching and validation."""

    def __init__(self, fetch=None, cache_ttl: float = 900.0, timeout: float = 20.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("BOOKS_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("BOOKS_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("BOOKS_USER_AGENT") or DEFAULT_USER_AGENT
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
            raise BooksError(f"open library request failed: {exc}") from exc

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
    def check_work(value: str) -> str:
        """Accept 'OL893414W', '/works/OL893414W' or a full openlibrary.org work URL."""
        match = _WORK_RE.search(str(value))
        if not match:
            raise ValueError("a work id looks like OL893414W (the trailing W means work)")
        return f"/works/{match.group(1).upper()}"

    @staticmethod
    def check_author_key(value: str) -> str:
        match = _AUTHOR_KEY_RE.search(str(value))
        if not match:
            raise ValueError("an author id looks like OL31353A (the trailing A means author)")
        return f"/authors/{match.group(1).upper()}"

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
    def check_query(value: str) -> str:
        query = re.sub(r"\s+", " ", str(value)).strip(" ?.!,")
        if len(query) < 2:
            raise ValueError("a search needs at least two characters")
        if len(query) > 200:
            raise ValueError("that search is too long - keep it under 200 characters")
        return query

    # -- readers -----------------------------------------------------------

    def search(self, query: str, limit: int = 5) -> dict:
        wanted = self.check_query(query)
        size = self.check_limit(limit)
        payload = self._get(f"{BASE_URL}/search.json", {
            "q": wanted, "limit": str(size), "fields": ",".join(SEARCH_FIELDS),
        }, ttl=min(self.cache_ttl, 3600))
        docs = payload.get("docs") if isinstance(payload, dict) else None
        if docs is None:
            raise BooksError("Open Library returned an unexpected search payload (no docs)")
        # A result without a work key is an edition or a stub the caller cannot follow up on.
        books = [book for book in (self._one_book(doc) for doc in docs) if book["key"]]
        return {
            "dataset": DATASET_SEARCH,
            "query": wanted,
            "found": int(payload.get("numFound") or 0),
            "books": books,
        }

    def work(self, reference: str) -> dict:
        key = self.check_work(reference)
        payload = self._get(f"{BASE_URL}{key}.json", {}, ttl=min(self.cache_ttl, 86400))
        if not isinstance(payload, dict) or not payload.get("title"):
            raise BooksError(f"Open Library has no work record at {key}")
        return {
            "dataset": DATASET_WORK,
            "key": payload.get("key") or key,
            "title": payload.get("title"),
            "description": self._description(payload.get("description")),
            "subjects": (payload.get("subjects") or [])[:10],
            "first_publish_date": payload.get("first_publish_date"),
            "links": [link.get("url") for link in (payload.get("links") or []) if link.get("url")][:3],
        }

    def author(self, name: str, works: int = 5) -> dict:
        wanted = self.check_query(name)
        size = self.check_limit(works)
        found = self._get(f"{BASE_URL}/search/authors.json", {"q": wanted, "limit": "1"},
                          ttl=min(self.cache_ttl, 3600))
        docs = found.get("docs") if isinstance(found, dict) else None
        if not docs:
            raise BooksError(f"Open Library has no author matching {wanted!r}")
        first = docs[0]
        key = self.check_author_key(first.get("key") or "")
        detail = self._get(f"{BASE_URL}{key}.json", {}, ttl=min(self.cache_ttl, 86400))
        titles = self._get(f"{BASE_URL}{key}/works.json", {"limit": str(size)},
                           ttl=min(self.cache_ttl, 3600))
        entries = titles.get("entries") if isinstance(titles, dict) else None
        if entries is None:
            raise BooksError(f"Open Library returned no works list for {key}")
        return {
            "dataset": DATASET_AUTHOR,
            "key": key,
            "name": (detail or {}).get("name") or first.get("name"),
            "bio": self._description((detail or {}).get("bio")),
            "birth_date": (detail or {}).get("birth_date"),
            "death_date": (detail or {}).get("death_date"),
            "work_count": first.get("work_count"),
            "top_work": first.get("top_work"),
            "works": [{"key": entry.get("key"), "title": entry.get("title")}
                      for entry in entries[:size]],
        }

    def trending(self, limit: int = 5) -> dict:
        size = self.check_limit(limit)
        payload = self._get(f"{BASE_URL}/trending/daily.json", {"limit": str(size)},
                            ttl=min(self.cache_ttl, 3600))
        works = payload.get("works") if isinstance(payload, dict) else None
        if works is None:
            raise BooksError("Open Library returned an unexpected trending payload")
        return {
            "dataset": DATASET_TRENDING,
            "books": [{
                "key": item.get("key"),
                "title": item.get("title"),
                "authors": [author.get("name") for author in (item.get("authors") or [])
                            if author.get("name")][:3],
                "first_publish_year": item.get("first_publish_year"),
            } for item in works[:size]],
        }

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _description(value) -> str | None:
        """Open Library stores descriptions as a string sometimes, an object other times."""
        if value is None:
            return None
        text = value.get("value") if isinstance(value, dict) else value
        if not isinstance(text, str):
            return None
        cleaned = re.sub(r"\s+", " ", text).strip()
        return cleaned or None

    @staticmethod
    def _one_book(doc: dict) -> dict:
        key = doc.get("key")
        if key and not str(key).startswith("/works/"):
            key = None
        return {
            "key": key,
            "title": doc.get("title"),
            "authors": doc.get("author_name") or [],
            "first_publish_year": doc.get("first_publish_year"),
            "edition_count": doc.get("edition_count"),
            "languages": (doc.get("language") or [])[:4],
            "url": f"{BASE_URL}{key}" if key else None,
        }
