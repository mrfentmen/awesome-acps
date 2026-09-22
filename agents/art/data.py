"""Read-only museum reader for the art agent.

  https://api.artic.edu/api/v1/artworks/search          Art Institute of Chicago
  https://openaccess-api.clevelandart.org/api/artworks  Cleveland Museum of Art
  https://collectionapi.metmuseum.org/public/collection/v1  The Met

All three keyless, verified live on 2026-09-22 (AIC 132,828 works, Cleveland 28 matches for
'monet', the Met 328 object ids). Search tries the Art Institute first, then Cleveland,
then the Met, and reports which one answered - so one museum being down does not empty the
answer.

Random picks come from the Met's public-domain objects, because that endpoint states
`isPublicDomain` explicitly, which a museum agent should not guess at.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from urllib import parse, request

AIC_URL = "https://api.artic.edu/api/v1/artworks/search"
CLEVELAND_URL = "https://openaccess-api.clevelandart.org/api/artworks"
MET_SEARCH_URL = "https://collectionapi.metmuseum.org/public/collection/v1/search"
MET_OBJECT_URL = "https://collectionapi.metmuseum.org/public/collection/v1/objects"

DATASET_AIC = "api.artic.edu/api/v1/artworks/search"
DATASET_CLEVELAND = "openaccess-api.clevelandart.org/api/artworks"
DATASET_MET = "collectionapi.metmuseum.org/public/collection/v1"

DEFAULT_USER_AGENT = "awesome-acps-art/1.0 (+https://github.com/mrfentmen/awesome-acps)"

AIC_IIIF = "https://www.artic.edu/iiif/2/"

#: How many random object ids the Met fallback may try before giving up.
RANDOM_ATTEMPTS = 5


class ArtError(RuntimeError):
    """No museum feed could be read."""


class ArtData:
    """Search and random picks across three keyless museum APIs."""

    def __init__(self, fetch=None, cache_ttl: float = 3600.0, timeout: float = 20.0,
                 seed: int | None = None, user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("ART_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("ART_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("ART_USER_AGENT") or DEFAULT_USER_AGENT
        self._fetch = fetch or self._http_get
        self._seed = seed
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
            raise ArtError(f"museum request failed: {exc}") from exc

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
    def check_query(value: str) -> str:
        query = " ".join(str(value).strip().split())
        if len(query) < 2 or not any(ch.isalnum() for ch in query):
            raise ValueError("a search is a word or two, like 'monet' or 'the sea'")
        if len(query) > 60:
            raise ValueError("keep the search to a few words")
        return query

    # -- museums -----------------------------------------------------------

    def _aic_search(self, query: str, limit: int) -> list[dict] | None:
        payload = self._get(AIC_URL, {
            "q": query, "limit": str(limit),
            "fields": "id,title,artist_display,date_display,medium_display,image_id",
        }, ttl=self.cache_ttl)
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return None
        return [{
            "title": row.get("title"),
            "artist": (row.get("artist_display") or "").split("\n")[0] or None,
            "date": row.get("date_display"),
            "medium": row.get("medium_display"),
            "museum": "Art Institute of Chicago",
            "url": f"https://www.artic.edu/artworks/{row.get('id')}" if row.get("id") else None,
            "image": f"{AIC_IIIF}{row['image_id']}/full/843,/0/default.jpg" if row.get("image_id") else None,
        } for row in rows]

    def _cleveland_search(self, query: str, limit: int) -> list[dict] | None:
        payload = self._get(CLEVELAND_URL, {"q": query, "limit": str(limit)}, ttl=self.cache_ttl)
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return None
        out = []
        for row in rows:
            creators = row.get("creators") or []
            artist = (creators[0] or {}).get("description") if creators else None
            images = ((row.get("images") or {}).get("web") or {}).get("url")
            out.append({
                "title": row.get("title"),
                "artist": artist,
                "date": row.get("creation_date"),
                "medium": row.get("technique"),
                "museum": "Cleveland Museum of Art",
                "url": row.get("url"),
                "image": images,
            })
        return out

    def _met_object(self, object_id: int) -> dict | None:
        payload = self._get(f"{MET_OBJECT_URL}/{object_id}", {}, ttl=self.cache_ttl)
        if not isinstance(payload, dict) or not payload.get("title"):
            return None
        return {
            "title": payload.get("title"),
            "artist": payload.get("artistDisplayName") or None,
            "date": payload.get("objectDate"),
            "medium": payload.get("medium"),
            "museum": "The Met",
            "url": payload.get("objectURL"),
            "image": payload.get("primaryImageSmall") or payload.get("primaryImage") or None,
            "public_domain": bool(payload.get("isPublicDomain")),
            "department": payload.get("department"),
        }

    def _met_search(self, query: str, limit: int) -> list[dict] | None:
        payload = self._get(MET_SEARCH_URL, {"q": query}, ttl=self.cache_ttl)
        ids = payload.get("objectIDs") if isinstance(payload, dict) else None
        if not isinstance(ids, list):
            return None
        rows = []
        for object_id in ids[:limit]:
            piece = self._met_object(object_id)
            if piece:
                rows.append(piece)
        return rows

    def search(self, query: str, limit: int = 3) -> dict:
        name = self.check_query(query)
        count = max(1, min(10, int(limit)))
        for dataset, reader in ((DATASET_AIC, self._aic_search),
                                (DATASET_CLEVELAND, self._cleveland_search),
                                (DATASET_MET, self._met_search)):
            try:
                rows = reader(name, count)
            except ArtError:
                continue
            if rows:
                return {"dataset": dataset, "query": name, "rows": rows}
        raise ArtError("no museum answered for that search (all three feeds failed or matched nothing)")

    def random_piece(self, seed: int | None = None) -> dict:
        payload = self._get(MET_OBJECT_URL, {}, ttl=86400)
        ids = payload if isinstance(payload, list) else None
        if not ids:
            raise ArtError("The Met returned an unexpected payload (no object id list)")
        chooser = random.Random(self._seed if seed is None else seed)
        for _ in range(RANDOM_ATTEMPTS):
            piece = self._met_object(chooser.choice(ids))
            if piece and piece.get("public_domain"):
                return {"dataset": DATASET_MET, "piece": piece, "pool": len(ids)}
        raise ArtError("could not land on a public-domain object in a few tries")
