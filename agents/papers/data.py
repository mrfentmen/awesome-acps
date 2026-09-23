"""Read-only research paper reader for the papers agent.

Three keyless services, all verified live on 2026-09-23:

  eutils.ncbi.nlm.nih.gov/entrez/eutils   PubMed: 492 papers for 'CRISPR sickle cell'
  export.arxiv.org/api/query              arXiv: preprints, Atom XML
  api.crossref.org/works                  Crossref: DOI metadata for ~150M works

Each one answers a different question, which is why all three are here rather than one:
PubMed indexes the biomedical literature with a stable PMID, arXiv carries preprints with a
version history and no journal, and Crossref is the DOI registry - the only one of the three
that can resolve a DOI on its own.

Two details are load-bearing. PubMed expects callers to identify themselves (`tool` and
`email`), and it rate-limits to three requests a second without an API key, so this reader
sends both. And arXiv's full-text search is not relevance ranked the way PubMed's is: it is
asked for newest first, which is what a person reading 'recent papers' wants - and `all:a b`
is an AND of two words, not a phrase, so its newest match can be only loosely related. That is
said in the answer rather than hidden behind a ranking the API does not do.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from urllib import error, parse, request

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
ARXIV = "http://export.arxiv.org/api/query"
CROSSREF = "https://api.crossref.org/works"
DATASET = "PubMed, arXiv and Crossref (all keyless)"

#: Both tools ask callers to say who they are; this is that, and it is not a secret.
TOOL_NAME = "awesome-acps-papers"
TOOL_EMAIL = os.environ.get("PAPERS_CONTACT_EMAIL") or "noreply@example.com"

DEFAULT_USER_AGENT = "awesome-acps-papers/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: How long to wait before the single retry after a throttle.
THROTTLE_PAUSE_S = 2.0

SOURCES = ("pubmed", "arxiv", "crossref")

_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")


class PapersError(RuntimeError):
    """A paper service could not be read."""


def doi_from_text(text: str) -> str | None:
    """A DOI written in a question, trimmed of trailing punctuation."""
    match = _DOI_RE.search(str(text or ""))
    return match.group(0).rstrip(".,;)") if match else None


def _clean(text) -> str:
    """Whitespace collapsed - arXiv's titles and abstracts arrive wrapped across lines."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


class PapersData:
    """PubMed, arXiv and Crossref behind one small cache."""

    def __init__(self, fetch=None, cache_ttl: float = 900.0, timeout: float = 25.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("PAPERS_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("PAPERS_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("PAPERS_USER_AGENT") or DEFAULT_USER_AGENT
        self._fetch = fetch or self._http_text
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    # -- transport ---------------------------------------------------------

    def _http_text(self, url: str, params: dict) -> str:
        """One GET, with a single retry when a service throttles.

        arXiv answers HTTP 406 to a burst of requests - it is its throttle, not a bad query:
        reproduced live on 2026-09-23, where the same request succeeded alone and 406'd after
        a handful of calls, then stayed 406 for minutes. A pause and one retry clears the short
        cases; when it does not, the error says what it is instead of blaming the search.
        """
        req = request.Request(f"{url}?{parse.urlencode(params)}",
                              headers={"User-Agent": self.user_agent, "Accept": "*/*"})
        for attempt in range(2):
            try:
                with request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read().decode("utf-8", "replace")
            except error.HTTPError as exc:
                if exc.code in (406, 429, 503) and attempt == 0:
                    time.sleep(THROTTLE_PAUSE_S)
                    continue
                if exc.code == 406:
                    raise PapersError(f"{url.split('/')[2]} is throttling requests right now "
                                      "(HTTP 406); it usually clears within a minute") from exc
                raise PapersError(f"paper service answered HTTP {exc.code} for {url}") from exc
            except Exception as exc:  # urllib raises many types; callers see one error type
                raise PapersError(f"paper service request failed: {exc}") from exc
        raise PapersError(f"paper service did not answer for {url}")

    def _payload(self, url: str, params: dict, ttl: float | None = None, expect="json"):
        key = f"{expect}:{url}?{parse.urlencode(params)}"
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        payload = self._fetch(url, params)
        if expect == "json":
            if isinstance(payload, (str, bytes)):
                try:
                    payload = json.loads(payload)
                except ValueError as exc:
                    raise PapersError(f"service returned something that is not JSON: {exc}") from exc
            if not isinstance(payload, dict):
                raise PapersError("service returned an unexpected payload")
        else:
            if not isinstance(payload, (str, bytes)):
                raise PapersError("service returned an unexpected payload")
            payload = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
        with self._lock:
            self._cache[key] = (now + (ttl if ttl is not None else self.cache_ttl), payload)
        return payload

    # -- PubMed ------------------------------------------------------------

    def pubmed(self, query: str, limit: int = 5, sort: str = "relevance") -> list[dict]:
        """PubMed results, newest first when asked, each with its PMID and DOI."""
        term = _clean(query)
        if not term:
            raise ValueError("give me something to search for, for example papers about CRISPR")
        keep = max(1, min(int(limit), 20))
        order = "pub_date" if sort == "date" else "relevance"
        search = self._payload(f"{EUTILS}/esearch.fcgi",
                               {"db": "pubmed", "term": term, "retmode": "json", "retmax": keep,
                                "sort": order, "tool": TOOL_NAME, "email": TOOL_EMAIL})
        ids = ((search.get("esearchresult") or {}).get("idlist")) or []
        if not ids:
            return []
        summary = self._payload(f"{EUTILS}/esummary.fcgi",
                                {"db": "pubmed", "id": ",".join(ids), "retmode": "json",
                                 "tool": TOOL_NAME, "email": TOOL_EMAIL})
        result = summary.get("result") or {}
        papers = []
        for pmid in ids:
            record = result.get(pmid) or {}
            if not record:
                continue
            doi = None
            for identifier in record.get("articleids") or []:
                if identifier.get("idtype") == "doi":
                    doi = identifier.get("value")
            papers.append({
                "source": "pubmed",
                "pmid": pmid,
                "doi": doi,
                "title": _clean(record.get("title")) or "(untitled)",
                "journal": _clean(record.get("source")),
                "date": _clean(record.get("pubdate")) or _clean(record.get("epubdate")),
                "authors": [_clean(author.get("name")) for author in record.get("authors") or []
                            if author.get("name")],
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            })
        return papers

    # -- arXiv -------------------------------------------------------------

    def arxiv(self, query: str, limit: int = 5) -> list[dict]:
        """arXiv preprints, newest first, with their primary category and version link."""
        term = _clean(query)
        if not term:
            raise ValueError("give me something to search for, for example papers about protein folding")
        keep = max(1, min(int(limit), 20))
        text = self._payload(ARXIV, {"search_query": f"all:{term}", "max_results": keep,
                                     "sortBy": "submittedDate", "sortOrder": "descending"},
                             expect="text")
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise PapersError(f"arXiv returned something that is not XML: {exc}") from exc
        namespace = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
        papers = []
        for entry in root.findall("atom:entry", namespace):
            identifier = _clean(entry.findtext("atom:id", default="", namespaces=namespace))
            category = entry.find("arxiv:primary_category", namespace)
            papers.append({
                "source": "arxiv",
                "arxiv_id": identifier.rsplit("/", 1)[-1] if identifier else "",
                "doi": _clean(entry.findtext("arxiv:doi", default="", namespaces=namespace)) or None,
                "title": _clean(entry.findtext("atom:title", default="", namespaces=namespace)) or "(untitled)",
                "journal": _clean(entry.findtext("arxiv:journal_ref", default="", namespaces=namespace)) or "arXiv preprint",
                "date": _clean(entry.findtext("atom:published", default="", namespaces=namespace))[:10],
                "authors": [_clean(author.findtext("atom:name", default="", namespaces=namespace))
                            for author in entry.findall("atom:author", namespace)],
                "category": category.get("term") if category is not None else "",
                "url": identifier,
            })
        return papers

    # -- Crossref ----------------------------------------------------------

    def crossref(self, query: str, limit: int = 5) -> list[dict]:
        """Crossref works matching a phrase, most relevant first."""
        term = _clean(query)
        if not term:
            raise ValueError("give me something to search for")
        keep = max(1, min(int(limit), 20))
        payload = self._payload(CROSSREF, {"query.bibliographic": term, "rows": keep,
                                           "mailto": TOOL_EMAIL})
        return self._crossref_items(payload)

    def doi(self, identifier: str) -> dict | None:
        """One work by its DOI, or None when Crossref has no record for it."""
        wanted = doi_from_text(identifier) or _clean(identifier)
        if not wanted:
            raise ValueError("give me a DOI, for example 10.1038/nature12373")
        try:
            payload = self._payload(f"{CROSSREF}/{parse.quote(wanted)}", {"mailto": TOOL_EMAIL})
        except PapersError:
            return None
        message = payload.get("message")
        if not isinstance(message, dict):
            return None
        return self._crossref_item(message)

    @staticmethod
    def _crossref_item(item: dict) -> dict:
        parts = ((item.get("issued") or {}).get("date-parts") or [[]])[0]
        authors = []
        for author in item.get("author") or []:
            name = " ".join(bit for bit in (author.get("given"), author.get("family")) if bit)
            if name:
                authors.append(_clean(name))
        return {
            "source": "crossref",
            "doi": item.get("DOI"),
            "title": _clean((item.get("title") or ["(untitled)"])[0]),
            "journal": _clean((item.get("container-title") or [""])[0]),
            "publisher": _clean(item.get("publisher")),
            "date": "-".join(str(part) for part in parts) if parts else "",
            "authors": authors,
            "citations": item.get("is-referenced-by-count"),
            "type": _clean(item.get("type")),
            "url": item.get("URL") or (f"https://doi.org/{item.get('DOI')}" if item.get("DOI") else ""),
        }

    def _crossref_items(self, payload: dict) -> list[dict]:
        message = payload.get("message") or {}
        items = message.get("items") if isinstance(message, dict) else None
        if items is None:
            raise PapersError("Crossref returned an unexpected payload")
        return [self._crossref_item(item) for item in items]
