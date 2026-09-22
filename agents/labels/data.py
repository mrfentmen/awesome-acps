"""Read-only FDA drug-label reader for the labels agent.

  https://api.fda.gov/drug/label.json   openFDA Structured Product Labeling

Keyless, verified live on 2026-09-22 (LIPITOR (ATORVASTATIN CALCIUM) came back with its
sections). The label text is what the manufacturer filed with the FDA - this reader keeps
the sections verbatim and truncates long ones with a note, instead of paraphrasing.

openFDA answers a search with HTTP 404 when nothing matches; that is not an error here, it
is an empty result, so the reader turns it into one.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from urllib import parse, request

BASE_URL = "https://api.fda.gov/drug/label.json"
DATASET = "api.fda.gov/drug/label.json"

DEFAULT_USER_AGENT = "awesome-acps-labels/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: Label sections the reader exposes, with the name used in answers.
SECTIONS = (
    ("indications_and_usage", "Indications and usage"),
    ("dosage_and_administration", "Dosage and administration"),
    ("warnings", "Warnings"),
    ("contraindications", "Contraindications"),
    ("adverse_reactions", "Adverse reactions"),
    ("drug_interactions", "Drug interactions"),
)

#: Long label sections are cut here so one answer stays readable.
SECTION_LIMIT = 600


class LabelsError(RuntimeError):
    """The openFDA label feed could not be read."""


class LabelsData:
    """openFDA drug labels, read over HTTP with caching and validation."""

    def __init__(self, fetch=None, cache_ttl: float = 3600.0, timeout: float = 20.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("LABELS_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("LABELS_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("LABELS_USER_AGENT") or DEFAULT_USER_AGENT
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
                return {"meta": {"results": {"total": 0}}, "results": []}
            raise LabelsError(f"openFDA request failed: {exc}") from exc

    def _get(self, params: dict, ttl: float | None = None):
        key = json.dumps({k: str(v) for k, v in sorted(params.items())})
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        payload = self._fetch(BASE_URL, params)
        with self._lock:
            self._cache[key] = (now + (ttl if ttl is not None else self.cache_ttl), payload)
        return payload

    # -- validation --------------------------------------------------------

    @staticmethod
    def check_drug(value: str) -> str:
        name = " ".join(str(value).strip().split())
        name = re.sub(r'["\\]', "", name)
        if len(name) < 2 or not re.search(r"[A-Za-z0-9]", name):
            raise ValueError("a drug name is at least two characters, like 'lipitor' or 'metformin'")
        if len(name) > 60:
            raise ValueError("that is too long for a drug name - use the brand or generic name")
        return name

    # -- rows --------------------------------------------------------------

    @staticmethod
    def _first_list(value) -> str | None:
        if isinstance(value, list) and value:
            return str(value[0])
        if isinstance(value, str):
            return value
        return None

    @classmethod
    def _sections(cls, result: dict) -> dict:
        sections: dict[str, str] = {}
        for key, _ in SECTIONS:
            text = cls._first_list(result.get(key))
            if text:
                sections[key] = text
        return sections

    @staticmethod
    def _openfda(result: dict) -> dict:
        block = result.get("openfda") or {}

        def first(key: str):
            value = block.get(key)
            return value[0] if isinstance(value, list) and value else None

        return {
            "brand_name": first("brand_name"),
            "generic_name": first("generic_name"),
            "manufacturer": first("manufacturer_name"),
            "substance_name": first("substance_name"),
        }

    # -- readings ----------------------------------------------------------

    def _search_payload(self, name: str, limit: int) -> dict:
        query = f'(openfda.brand_name:"{name}" OR openfda.generic_name:"{name}")'
        return self._get({"search": query, "limit": str(limit)})

    def search(self, name: str, limit: int = 3) -> dict:
        drug = self.check_drug(name)
        rows = max(1, min(10, int(limit)))
        payload = self._search_payload(drug, rows)
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise LabelsError("openFDA returned an unexpected payload (no results list)")
        return {
            "dataset": DATASET,
            "query": drug,
            "rows": [{"openfda": self._openfda(result),
                      "has_sections": sorted(self._sections(result))} for result in results],
        }

    def label(self, name: str) -> dict:
        drug = self.check_drug(name)
        payload = self._search_payload(drug, 1)
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise LabelsError("openFDA returned an unexpected payload (no results list)")
        if not results:
            return {"dataset": DATASET, "query": drug, "found": False, "label": None, "sections": {}}
        result = results[0]
        sections = self._sections(result)
        return {
            "dataset": DATASET,
            "query": drug,
            "found": True,
            "openfda": self._openfda(result),
            "effective_time": result.get("effective_time"),
            "sections": sections,
        }

    @staticmethod
    def trim(text: str, limit: int = SECTION_LIMIT) -> str:
        """A section cut to a readable size, with a note when it was cut."""
        collapsed = " ".join(str(text).split())
        if len(collapsed) <= limit:
            return collapsed
        return collapsed[:limit].rstrip() + f" ... [cut at {limit} characters]"
