"""Chemicals and drugs, from PubChem and RxNorm (both keyless).

Verified live on 2026-09-23:

  https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/ibuprofen/property/.../JSON
      -> CID 3672, C13H18O2, 206.28, SMILES CC(C)CC1=CC=C(C=C1)C(C)C(=O)O
  https://rxnav.nlm.nih.gov/REST/drugs.json?name=lipitor
      -> 4 products, all 'atorvastatin ... [Lipitor]'

Two things worth keeping. PubChem names the SMILES field **ConnectivitySMILES**, not
CanonicalSMILES (the older name in its own docs answers nothing), and it answers a miss with
HTTP 404 and a PUGREST.NotFound body - which is a real answer, "there is no such compound", so it
is turned into a plain message rather than an error the caller has to guess at. RxNorm returns
products whose names already carry the generic ingredient, so "what is Lipitor" can say
"atorvastatin" without a second lookup.
"""

from __future__ import annotations

import json
import os
import re
import time
from urllib import parse, request
from urllib.error import HTTPError

PUBCHEM_URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/"
RXNORM_URL = "https://rxnav.nlm.nih.gov/REST/drugs.json"

DATASET = "PubChem + RxNorm (NLM)"

DEFAULT_USER_AGENT = "awesome-acps-chem/1.0 (+https://github.com/mrfentmen/awesome-acps)"

PROPERTIES = ("MolecularFormula,MolecularWeight,IUPACName,ConnectivitySMILES,Title")

#: RxNorm term types, in the order they matter to a person asking "what is this drug".
TERM_TYPES = {"IN": "ingredient", "BN": "brand name", "SBD": "branded product",
              "SCD": "clinical product", "SCDG": "clinical dose form group",
              "SCDF": "clinical drug form", "GPCK": "pack", "BPCK": "branded pack",
              "DF": "dose form", "DFG": "dose form group"}

#: The words after which a name is a chemical rather than a brand.
CHEM_WORDS = ("formula", "molecular weight", "molar mass", "molecule", "compound", "atom",
              "atoms", "chemical", "smiles", "structure", "cid", "pubchem", "element",
              "mass", "weigh", "made of", "made from", "composed of", "made up of")
#: The words after which a name is a drug.
DRUG_WORDS = ("drug", "generic", "brand", "brand name", "medicine", "medication", "prescription",
              "pill", "tablet", "capsule", "dose", "dosage", "rxnorm", "treat", "treats",
              "used for", "indication", "sold as", "also called", "other names")


class ChemError(RuntimeError):
    """PubChem or RxNorm could not be read."""


class ChemNotFound(ChemError):
    """The service answered 404, which is a real answer: there is no such compound."""


def word_from(text: str, words: tuple[str, ...]) -> str:
    """The longest matching word or phrase, or ''."""
    lowered = str(text or "").lower()
    best = ""
    for word in words:
        if word in lowered and len(word) > len(best):
            best = word
    return best


def clean_name(text: str, extra: tuple[str, ...] = ()) -> str:
    """The chemical or drug being asked about, with the question words taken off."""
    words = ("how much", "how many", "what is the", "what is", "what's", "whats", "does",
             "do", "is", "are", "the", "of", "for", "in", "a", "an", "it", "please",
             "tell me", "about", "give me", "show me", "find", "look up", "name",
             "weight", "mass", "structure", "element", "compound", "chemical", "formula",
             "molecular", "molecule", "molar", "smiles", "cid", "pubchem", "rxnorm",
             "same as", "also called", "other names", "sold as", "used for", "used to",
             "used", "made of", "made from", "made up of", "composed of", "made",
             "as", "and", "or", "with", "that", "which", "what") + extra
    work = f" {str(text or '')} "
    for word in sorted(words, key=len, reverse=True):
        work = re.sub(rf"(?i)\b{re.escape(word)}\b", " ", work)
    return " ".join(work.split()).strip(" ?,.")


def ingredient_of(product_name: str) -> str | None:
    """'atorvastatin 80 MG Oral Tablet [Lipitor]' -> 'atorvastatin'.

    RxNorm writes a product as ingredient + strength + dose form + [brand], so the ingredient is
    everything before the first number.
    """
    name = str(product_name or "").split("[")[0]
    match = re.match(r"\s*([A-Za-z][A-Za-z0-9'\- ]*?)(?=\s+\d|\s*$)", name)
    return match.group(1).strip().lower() if match else None


class ChemData:
    """Compounds from PubChem, drugs from RxNorm."""

    def __init__(self, fetch=None, cache_ttl: float = 3600.0, timeout: float = 30.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("CHEM_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("CHEM_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("CHEM_USER_AGENT") or DEFAULT_USER_AGENT
        self._fetch = fetch or self._http_text
        self._cache: dict[str, tuple[float, object]] = {}

    # -- transport ---------------------------------------------------------

    def _http_text(self, url: str, params: dict | None = None) -> str:
        query = f"{url}?{parse.urlencode(params, doseq=True)}" if params else url
        req = request.Request(query, headers={"User-Agent": self.user_agent, "Accept": "*/*"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except HTTPError as exc:
            if exc.code == 404:
                raise ChemNotFound(f"request failed: {exc}") from exc
            raise ChemError(f"request failed: {exc}") from exc
        except Exception as exc:  # urllib raises many other types too
            raise ChemError(f"request failed: {exc}") from exc

    def _json(self, url: str, params: dict | None = None, ttl: float | None = None) -> dict:
        query = f"{url}?{parse.urlencode(params, doseq=True)}" if params else url
        now = time.time()
        hit = self._cache.get(query)
        if hit and hit[0] > now:
            return hit[1]
        payload = self._fetch(url, params or {})
        if isinstance(payload, (str, bytes)):
            try:
                payload = json.loads(payload)
            except ValueError as exc:
                raise ChemError(f"the service returned something that is not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ChemError("the service returned an unexpected payload")
        self._cache[query] = (now + (ttl if ttl is not None else self.cache_ttl), payload)
        return payload

    # -- reads -------------------------------------------------------------

    def compound(self, name: str) -> dict:
        """One compound's formula, mass and structure, from PubChem."""
        text = str(name or "").strip()
        if not text:
            raise ValueError("tell me which chemical, for example ibuprofen")
        url = PUBCHEM_URL + parse.quote(text, safe="") + f"/property/{PROPERTIES}/JSON"
        try:
            payload = self._json(url)
        except ChemNotFound as exc:
            raise ChemError(f"PubChem has no compound called {text!r}") from exc
        table = (payload.get("PropertyTable") or {}).get("Properties") or []
        if not table:
            raise ChemError(f"PubChem has no compound called {text!r}")
        row = table[0]
        return {
            "query": text,
            "title": row.get("Title") or text,
            "cid": row.get("CID"),
            "formula": row.get("MolecularFormula"),
            "mass": row.get("MolecularWeight"),
            "iupac": row.get("IUPACName"),
            "smiles": row.get("ConnectivitySMILES"),
            "url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{row.get('CID')}",
            "source": DATASET,
        }

    def drug(self, name: str, limit: int = 6) -> dict:
        """A drug's brand and generic names, from RxNorm."""
        text = str(name or "").strip()
        if not text:
            raise ValueError("tell me which drug, for example Lipitor")
        searched = text
        payload = self._json(RXNORM_URL, {"name": searched}, ttl=3600.0)
        groups = (payload.get("drugGroup") or {}).get("conceptGroup") or []
        #: RxNorm matches the whole string, so "Advil versus ibuprofen" finds nothing. Retrying the
        #: first name is a real search, and `searched` records which name answered.
        if not any(group.get("conceptProperties") for group in groups) and len(text.split()) > 1:
            first = text.split()[0]
            if first.lower() != text.lower():
                searched = first
                payload = self._json(RXNORM_URL, {"name": first}, ttl=3600.0)
                groups = (payload.get("drugGroup") or {}).get("conceptGroup") or []
        wanted = max(1, min(int(limit), 25))
        products, ingredients, brands, kinds = [], {}, {}, []
        for group in groups:
            tty = str(group.get("tty") or "")
            properties = group.get("conceptProperties") or []
            if not properties:
                continue
            kinds.append(TERM_TYPES.get(tty, tty))
            for property_ in properties:
                term = str(property_.get("name") or "")
                if not term:
                    continue
                if tty == "IN":
                    ingredients[term.lower()] = term
                elif tty == "BN":
                    brands[term] = term
                elif tty in ("SBD", "SCD", "GPCK", "BPCK"):
                    found = ingredient_of(term)
                    if found and tty == "SBD":
                        ingredients[found] = found
                    if tty == "SBD":
                        brand = re.search(r"\[([^\]]+)\]", term)
                        if brand:
                            brands[brand.group(1)] = brand.group(1)
                    if len(products) < wanted:
                        products.append({"name": term, "rxcui": property_.get("rxcui"), "tty": tty})
        if not products and not ingredients and not brands:
            raise ChemError(f"RxNorm does not know a drug called {text!r}")
        return {
            "query": text,
            "searched": searched,
            "name": (payload.get("drugGroup") or {}).get("name") or text,
            "products": products,
            "ingredients": sorted(ingredients.values()),
            "brands": sorted(brands.values()),
            "kinds": kinds,
            "url": f"https://mor.nlm.nih.gov/RxNav/search?searchBy=String&searchTerm="
                   f"{parse.quote(text)}",
            "source": DATASET,
        }
