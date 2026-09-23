"""Read-only GBIF reader for the nature agent.

  https://api.gbif.org/v1   — the Global Biodiversity Information Facility

GBIF is the worldwide occurrence index: museums, survey programs and citizen science
publishers, 2.5+ billion records, keyless and free. It is a different shelf from the
`species` agent in this repo: iNaturalist answers "what was seen near me", GBIF answers
"how many records of this taxon exist on Earth, where, and in which months" — published
datasets, not just observations.

One wrinkle drives the design: GBIF's name matcher (`species/match`) only speaks scientific
names. "monarch butterfly" is `NONE` there, and GBIF's own full-text search answers "Bald
eagle poxvirus" for "bald eagle" and ranks a clam above the tiger. So `resolve()` does not
guess from GBIF text search at all. It is a chain, each rung checked live on 2026-09-22:

  1. `species/match?name=`      — exact/fuzzy scientific names, and anything GBIF recognises
                                  outright ("Quercus alba", "Squalus carcharias")
  2. iNaturalist `taxa?q=`      — common names. iNaturalist's taxon search exists for this:
                                  "monarch butterflies", "bald eagle" and "house cat" all
                                  land on the right species. The best match is then passed
                                  back through GBIF's matcher for the accepted taxon key.
  3. `species/{key}` for synonyms — a synonym resolves to its accepted key

Matching is deliberately strict rather than confident: a candidate only wins when the common
name or matched term lines up exactly (plurals folded), with rank as a tiebreak so "red fox"
picks the species while "penguin" picks the family. When nothing lines up — "tiger", "cactus"
— the agent says it could not pin the name down and offers what it found, instead of
returning a moth family or a fungus as if it were sure.

Everything else is one occurrence search with a facet: country for "where", month for
"when". Counts are catalog records, not population sizes, and the answer says so.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from urllib import parse, request

BASE_URL = "https://api.gbif.org/v1"
INAT_URL = "https://api.inaturalist.org/v1"
DATASET = "GBIF occurrence index (api.gbif.org)"

#: Rank nudges the pick toward the organism a person means: "red fox" -> the species,
#: "penguin" -> the family, "shark" -> the infraclass. Broad ranks score low on purpose.
INAT_RANK_BONUS = {
    "species": 6, "subspecies": 4, "genus": 3, "family": 1,
    "infraclass": 1, "class": 1, "phylum": 1, "kingdom": 1,
}
#: An exact common-name or matched-term hit scores 40 or more; anything less is a guess.
INAT_MIN_SCORE = 46
INAT_PAGE = 30

DEFAULT_USER_AGENT = "awesome-acps-nature/1.0 (+https://github.com/mrfentmen/awesome-acps)"

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")

#: Country names and shorthands -> ISO 3166-1 alpha-2, which is what GBIF wants.
COUNTRY_CODES = {
    "united states": "US", "united states of america": "US", "usa": "US", "us": "US",
    "america": "US", "canada": "CA", "mexico": "MX", "brazil": "BR", "argentina": "AR",
    "chile": "CL", "peru": "PE", "colombia": "CO", "ecuador": "EC", "bolivia": "BO",
    "venezuela": "VE", "uruguay": "UY", "paraguay": "PY", "costa rica": "CR", "panama": "PA",
    "cuba": "CU", "jamaica": "JM", "dominican republic": "DO", "guatemala": "GT",
    "honduras": "HN", "nicaragua": "NI", "el salvador": "SV",
    "united kingdom": "GB", "uk": "GB", "britain": "GB", "great britain": "GB",
    "england": "GB", "scotland": "GB", "wales": "GB", "ireland": "IE",
    "france": "FR", "germany": "DE", "spain": "ES", "portugal": "PT", "italy": "IT",
    "netherlands": "NL", "belgium": "BE", "switzerland": "CH", "austria": "AT",
    "poland": "PL", "czechia": "CZ", "czech republic": "CZ", "hungary": "HU",
    "romania": "RO", "bulgaria": "BG", "greece": "GR", "croatia": "HR", "serbia": "RS",
    "slovakia": "SK", "slovenia": "SI", "denmark": "DK", "sweden": "SE", "norway": "NO",
    "finland": "FI", "iceland": "IS", "estonia": "EE", "latvia": "LV", "lithuania": "LT",
    "ukraine": "UA", "russia": "RU", "turkey": "TR", "türkiye": "TR",
    "china": "CN", "japan": "JP", "south korea": "KR", "korea": "KR", "taiwan": "TW",
    "hong kong": "HK", "india": "IN", "pakistan": "PK", "bangladesh": "BD", "sri lanka": "LK",
    "nepal": "NP", "bhutan": "BT", "thailand": "TH", "vietnam": "VN", "laos": "LA",
    "cambodia": "KH", "myanmar": "MM", "malaysia": "MY", "singapore": "SG",
    "indonesia": "ID", "philippines": "PH", "brunei": "BN",
    "australia": "AU", "new zealand": "NZ", "fiji": "FJ", "papua new guinea": "PG",
    "south africa": "ZA", "kenya": "KE", "tanzania": "TZ", "uganda": "UG", "rwanda": "RW",
    "ethiopia": "ET", "nigeria": "NG", "ghana": "GH", "senegal": "SN", "mali": "ML",
    "egypt": "EG", "morocco": "MA", "algeria": "DZ", "tunisia": "TN", "libya": "LY",
    "sudan": "SD", "madagascar": "MG", "mozambique": "MZ", "zimbabwe": "ZW",
    "botswana": "BW", "namibia": "NA", "angola": "AO", "cameroon": "CM",
    "democratic republic of the congo": "CD", "congo": "CG", "gabon": "GA",
    "saudi arabia": "SA", "united arab emirates": "AE", "qatar": "QA", "kuwait": "KW",
    "israel": "IL", "jordan": "JO", "lebanon": "LB", "iran": "IR", "iraq": "IQ",
    "kazakhstan": "KZ", "uzbekistan": "UZ", "mongolia": "MN", "georgia": "GE",
    "armenia": "AM", "azerbaijan": "AZ",
}

_CODE_RE = re.compile(r"^[A-Za-z]{2}$")
_NAME_RE = re.compile(r"[A-Za-z]")

_DISPLAY_STOPWORDS = frozenset({"of", "and", "the"})


def _display_name(name: str) -> str:
    """'united states of america' -> 'United States of America'."""
    return " ".join(word if word in _DISPLAY_STOPWORDS else word.capitalize()
                    for word in name.split())


#: GBIF's country facet answers in ISO codes ('ES'), which nobody wants to read. Built from
#: COUNTRY_CODES by keeping the longest name per code, so 'us' loses to 'united states'.
COUNTRY_NAMES = {
    code: _display_name(name)
    for code, name in (
        (code, max((n for n, c in COUNTRY_CODES.items() if c == code), key=len))
        for code in set(COUNTRY_CODES.values())
    )
}


def country_label(value) -> str:
    """A country name for a facet value: 'ES' -> 'Spain', 'Canada' -> 'Canada'."""
    text = str(value or "").strip()
    return COUNTRY_NAMES.get(text.upper(), text)


def singular(word: str) -> str:
    """'butterflies' -> 'butterfly', 'oaks' -> 'oak'. For matching only, never displayed."""
    low = word.lower()
    if low.endswith("ies") and len(low) > 4:
        return low[:-3] + "y"
    if low.endswith(("ches", "shes", "xes", "zes", "sses")) and len(low) > 4:
        return low[:-2]
    if low.endswith("s") and not low.endswith("ss") and len(low) > 3:
        return low[:-1]
    return low


def name_words(text) -> list[str]:
    """Words of a name, plural-folded: 'Monarch Butterflies' -> ['monarch', 'butterfly']."""
    return [singular(word) for word in re.findall(r"[a-z]+", str(text or "").lower())]


def month_name(value) -> str | None:
    """'8' -> 'August'; already-a-name passes through; None for junk."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        text = str(value).strip()
        return text if text in MONTHS else None
    if 1 <= number <= 12:
        return MONTHS[number - 1]
    return None


def format_number(value) -> str:
    """1234567 -> '1,234,567'. Plain and testable."""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "unknown"


class NatureError(RuntimeError):
    """The GBIF feed could not be read."""


class NameNotFound(NatureError):
    """GBIF does not recognise the name; `candidates` holds what it suggests instead."""

    def __init__(self, message: str, candidates: list[dict] | None = None) -> None:
        super().__init__(message)
        self.candidates = candidates or []


class GbifData:
    """GBIF species resolution and occurrence reads, with caching and validation."""

    def __init__(self, fetch=None, cache_ttl: float = 1800.0, timeout: float = 20.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("NATURE_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("NATURE_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("NATURE_USER_AGENT") or DEFAULT_USER_AGENT
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
            raise NatureError(f"GBIF request failed: {exc}") from exc

    def _cached(self, url: str, params: dict, ttl: float | None = None):
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

    def _get(self, path: str, params: dict, ttl: float | None = None):
        return self._cached(f"{BASE_URL}{path}", params, ttl)

    def _inat(self, query: str):
        """iNaturalist taxon search, which is the one that actually knows common names."""
        return self._cached(f"{INAT_URL}/taxa",
                            {"q": query, "per_page": INAT_PAGE, "locale": "en"},
                            ttl=self.cache_ttl * 6)

    # -- validation --------------------------------------------------------

    @staticmethod
    def check_name(value) -> str:
        name = str(value or "").strip().strip("?.!,")
        if not _NAME_RE.search(name):
            raise ValueError("a taxon name is required, for example 'Danaus plexippus' or 'monarch butterfly'")
        if len(name) > 80:
            raise ValueError("that name is too long to be a taxon name")
        return name

    @staticmethod
    def country_code(value) -> str | None:
        """ISO alpha-2 for a country name or code. Raises ValueError for an unknown one."""
        text = re.sub(r"\s+", " ", str(value or "").strip().lower())
        if not text:
            return None
        code = COUNTRY_CODES.get(text)
        if code is not None:
            return code
        if _CODE_RE.match(text):
            return text.upper()
        raise ValueError(f"I do not know the country {value!r}; give me an ISO code like US or CA")

    @staticmethod
    def country_from_text(text: str) -> str | None:
        """A country named in a sentence, or None. Longest name wins ('costa rica' > 'rica')."""
        code, _phrase = GbifData.country_phrase_from_text(text)
        return code

    @staticmethod
    def country_phrase_from_text(text: str) -> tuple[str | None, str | None]:
        """The country named in a sentence and the exact phrase that named it.

        Returning the phrase lets the caller subtract only those words before looking for a
        taxon name (stripping any trailing words would eat 'of Danaus plexippus').
        """
        lowered = " " + re.sub(r"[^a-z ]+", " ", str(text).lower()).strip() + " "
        for name in sorted(COUNTRY_CODES, key=len, reverse=True):
            if f" {name} " in lowered:
                return COUNTRY_CODES[name], name
        return None, None

    @staticmethod
    def check_limit(value, maximum: int = 10) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError):
            raise ValueError("limit must be a whole number") from None
        if not 1 <= limit <= maximum:
            raise ValueError(f"limit must be between 1 and {maximum}")
        return limit

    # -- name resolution ---------------------------------------------------

    @staticmethod
    def _taxon_from_match(payload: dict, via: str) -> dict:
        status = payload.get("status")
        key = payload.get("acceptedUsageKey") if status == "SYNONYM" and payload.get("acceptedUsageKey") else payload.get("usageKey")
        return {
            "resolved": True,
            "query": None,
            "key": key,
            "scientific_name": payload.get("scientificName"),
            "common_name": None,
            "rank": payload.get("rank"),
            "status": status,
            "match_type": payload.get("matchType"),
            "confidence": payload.get("confidence"),
            "kingdom": payload.get("kingdom"),
            "family": payload.get("family"),
            "genus": payload.get("genus"),
            "via": via,
            "synonym_of": payload.get("scientificName") if status == "SYNONYM" else None,
            "candidates": [],
        }

    @staticmethod
    def _inat_score(query: str, item: dict) -> int:
        """How well one iNaturalist candidate matches what was asked for, 0 when shaky."""
        want = name_words(query)
        if not want:
            return 0
        phrase = " ".join(want)
        common = name_words(item.get("preferred_common_name"))
        matched = name_words(item.get("matched_term"))
        score = 0
        if common and " ".join(common) == phrase:
            score += 50
        elif matched and " ".join(matched) == phrase:
            score += 40
        pool = set(common) | set(matched)
        if set(want) <= pool:
            # every word asked for appears, minus a nudge for every extra word it drags in
            score += 10 - min(4, abs(len(pool) - len(want)))
        score += INAT_RANK_BONUS.get(str(item.get("rank") or "").lower(), 0)
        return score

    def resolve(self, name) -> dict:
        """The accepted taxon for a name, or a NameNotFound carrying close candidates."""
        query = self.check_name(name)
        match = self._get("/species/match", {"name": query}, ttl=self.cache_ttl * 6)
        if isinstance(match, dict) and match.get("matchType") not in (None, "NONE"):
            taxon = self._taxon_from_match(match, "match")
            taxon["query"] = query
            if taxon["status"] == "SYNONYM" and taxon["key"] != match.get("usageKey"):
                taxon = self._follow_synonym(taxon, match)
            return taxon

        # A common name. iNaturalist matches those natively; GBIF's text search does not.
        # Try the phrase as asked, then with plurals folded ("monarch butterflies").
        scored: list[tuple[int, dict]] = []
        for attempt in dict.fromkeys((query, " ".join(singular(word) for word in query.split()))):
            results = (self._inat(attempt) or {}).get("results") or []
            scored.extend((self._inat_score(attempt, item), item) for item in results)
            if scored and max(score for score, _item in scored) >= INAT_MIN_SCORE:
                break
        scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("name"))))
        candidates = [{
            "scientific_name": item.get("name"),
            "canonical_name": item.get("name"),
            "common_name": item.get("preferred_common_name"),
            "rank": item.get("rank"),
        } for _score, item in scored[:5]]

        if scored and scored[0][0] >= INAT_MIN_SCORE:
            taxon = self._taxon_from_inat(scored[0][1], query, candidates)
            if taxon is not None:
                return taxon

        raise NameNotFound(
            f"I could not pin down the name {query!r}",
            candidates=candidates,
        )

    def _taxon_from_inat(self, best: dict, query: str, candidates: list[dict]) -> dict | None:
        """iNaturalist's pick, turned into a GBIF taxon the occurrence reads can use.

        Usually the scientific name goes straight through GBIF's matcher. Broad ranks are
        the exception: `match` has no entry for an infraclass like Selachii, so the backbone
        search is asked instead and the row whose rank iNaturalist named is taken.
        """
        scientific_name = best.get("name")
        if not scientific_name:
            return None
        second = self._get("/species/match", {"name": scientific_name}, ttl=self.cache_ttl * 6)
        if isinstance(second, dict) and second.get("matchType") not in (None, "NONE"):
            taxon = self._taxon_from_match(second, "inat")
        else:
            record = self._backbone_record(scientific_name, best.get("rank"))
            if not record or not self._has_occurrences(record.get("key")):
                return None
            taxon = self._taxon_from_match({
                "usageKey": record.get("key"),
                "scientificName": record.get("scientificName") or record.get("canonicalName"),
                "rank": record.get("rank"),
                "status": record.get("status") or "ACCEPTED",
                "matchType": "BACKBONE_SEARCH",
                "confidence": None,
                "kingdom": record.get("kingdom"),
                "family": record.get("family"),
                "genus": record.get("genus"),
            }, "inat")
        taxon["query"] = query
        taxon["common_name"] = best.get("preferred_common_name") or None
        taxon["candidates"] = candidates
        if taxon["status"] == "SYNONYM" and taxon.get("key") != second.get("usageKey"):
            taxon = self._follow_synonym(taxon, second)
        return taxon

    def _has_occurrences(self, taxon_key) -> bool:
        """Whether any record is filed under a taxon key.

        GBIF holds name-only nodes for broad ranks — every Selachii row above is an infraclass
        with zero records, because shark records sit under lower taxa. Answering from one of
        those would print "0 records" as if it were a fact about sharks, so they are refused.
        """
        if not taxon_key:
            return False
        payload = self._get("/occurrence/search", {"taxonKey": taxon_key, "limit": 0},
                            ttl=self.cache_ttl * 6)
        return int((payload or {}).get("count") or 0) > 0

    def _backbone_record(self, scientific_name: str, want_rank: str | None = None) -> dict | None:
        """The backbone row for an exact scientific name, preferring the rank asked for."""
        payload = self._get("/species/search",
                            {"q": scientific_name, "nameType": "SCIENTIFIC", "limit": 10},
                            ttl=self.cache_ttl * 6)
        target = scientific_name.strip().lower()
        rows = [row for row in ((payload or {}).get("results") or [])
                if str(row.get("canonicalName") or "").strip().lower() == target]
        if not rows:
            return None
        if want_rank:
            same_rank = [row for row in rows
                         if str(row.get("rank") or "").lower() == str(want_rank).lower()]
            if same_rank:
                rows = same_rank
        return rows[0]  # GBIF returns these by relevance; the rest is decided by the count check

    def _follow_synonym(self, taxon: dict, match: dict) -> dict:
        """A synonym resolves to its accepted key; fetch that record for the accepted name."""
        accepted_key = match.get("acceptedUsageKey")
        if not accepted_key:
            return taxon
        record = self._get(f"/species/{accepted_key}", {}, ttl=self.cache_ttl * 12)
        if isinstance(record, dict) and record.get("canonicalName"):
            taxon["key"] = accepted_key
            taxon["scientific_name"] = record.get("canonicalName")
            taxon["rank"] = record.get("rank") or taxon["rank"]
            taxon["status"] = "ACCEPTED"
            taxon["kingdom"] = record.get("kingdom") or taxon["kingdom"]
            taxon["family"] = record.get("family") or taxon["family"]
            taxon["genus"] = record.get("genus") or taxon["genus"]
        return taxon

    def _taxon(self, name) -> dict:
        return self.resolve(name)

    # -- occurrence reads --------------------------------------------------

    def count(self, name, country=None) -> dict:
        """Occurrence records for a taxon, worldwide or in one country, with top countries."""
        taxon = self._taxon(name)
        code = self.country_code(country)
        world = self._get("/occurrence/search", {
            "taxonKey": taxon["key"], "limit": 0, "facet": "country", "facetLimit": 5,
        })
        total = int((world or {}).get("count") or 0)
        facets = ((world or {}).get("facets") or [{}])
        top = [{"country": country_label(entry.get("name")), "count": entry.get("count")}
               for entry in (facets[0].get("counts") or [])]
        result = {
            "dataset": DATASET,
            "taxon": taxon,
            "total": total,
            "top_countries": top,
            "country": code,
            "country_count": None,
        }
        if code:
            scoped = self._get("/occurrence/search", {"taxonKey": taxon["key"], "country": code, "limit": 0})
            result["country_count"] = int((scoped or {}).get("count") or 0)
        return result

    def season(self, name) -> dict:
        """Occurrence records by calendar month: when this taxon is recorded most."""
        taxon = self._taxon(name)
        payload = self._get("/occurrence/search", {
            "taxonKey": taxon["key"], "limit": 0, "facet": "month", "facetLimit": 12,
        })
        total = int((payload or {}).get("count") or 0)
        facets = ((payload or {}).get("facets") or [{}])
        rows = []
        for entry in (facets[0].get("counts") or []):
            label = month_name(entry.get("name"))
            if label:
                rows.append({"month": label, "count": entry.get("count")})
        rows.sort(key=lambda row: row["count"], reverse=True)
        return {"dataset": DATASET, "taxon": taxon, "total": total, "months": rows}

    def recent(self, name, limit: int = 3, country=None) -> dict:
        """The newest records GBIF holds for a taxon, by event date."""
        taxon = self._taxon(name)
        window = self.check_limit(limit)
        params = {
            "taxonKey": taxon["key"], "limit": window,
            "order_by": "eventDate", "sort": "desc",
        }
        code = self.country_code(country)
        if code:
            params["country"] = code
        payload = self._get("/occurrence/search", params, ttl=min(self.cache_ttl, 900))
        records = []
        for item in (payload or {}).get("results") or []:
            records.append({
                "date": item.get("eventDate") or item.get("year"),
                "country": country_label(item.get("country")),
                "locality": item.get("locality") or item.get("stateProvince"),
                "recorded_by": item.get("recordedBy"),
                "basis": item.get("basisOfRecord"),
                "dataset": item.get("datasetTitle") or item.get("publishingOrgKey"),
                "lat": item.get("decimalLatitude"),
                "lon": item.get("decimalLongitude"),
                "scientific_name": item.get("scientificName"),
            })
        return {
            "dataset": DATASET,
            "taxon": taxon,
            "total": int((payload or {}).get("count") or 0),
            "records": records,
            "country": code,
        }
