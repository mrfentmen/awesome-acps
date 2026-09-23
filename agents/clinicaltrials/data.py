"""ClinicalTrials.gov - what studies exist for a condition, and where they are recruiting.

Verified live on 2026-09-23, keyless:

  https://clinicaltrials.gov/api/v2/studies?query.cond=melanoma&query.locn=Boston
      &pageSize=1&countTotal=true&filter.overallStatus=RECRUITING&sort=LastUpdatePostDate:desc
      -> 200, {'totalCount': 40, 'studies': [...]}
  https://clinicaltrials.gov/api/v2/studies/NCT01065467     -> one study, 200
  https://clinicaltrials.gov/api/v2/studies/NCT99999999     -> 404, plain text
  https://clinicaltrials.gov/api/v2/studies?pageSize=abc   -> 400, plain text

Four things this reader states instead of implying:

  1. **A place filter matches a study, not a site.** `query.locn=Boston` returns studies that
     list *any* site in Boston - the first result for melanoma in Boston has its first two sites
     in Arizona and Arkansas. So the sites that actually match the place are found and named,
     and the count of all sites is given.
  2. **`totalCount` counts studies, not sites and not patients.** It is called studies here.
  3. **The registry holds what a sponsor filed.** A trial that was never registered, or whose
     record is stale, is not in it, and a status is the sponsor's own word, not a verification.
  4. **This is a registry, not medical advice.** Every answer says so.
"""

from __future__ import annotations

import json
import os
from urllib import error, parse, request

BASE = "https://clinicaltrials.gov/api/v2/studies"
STUDY_PAGE = "https://clinicaltrials.gov/study/{nct}"

DATASET = "ClinicalTrials.gov API v2 (clinicaltrials.gov/api/v2/studies)"

DEFAULT_USER_AGENT = ("awesome-acps-clinicaltrials/1.0 "
                      "(+https://github.com/mrfentmen/awesome-acps)")

#: The statuses the API filters on, in the words a person uses for them.
STATUSES = {
    "recruiting": "RECRUITING",
    "not yet recruiting": "NOT_YET_RECRUITING",
    "enrolling by invitation": "ENROLLING_BY_INVITATION",
    "active": "ACTIVE_NOT_RECRUITING",
    "active not recruiting": "ACTIVE_NOT_RECRUITING",
    "completed": "COMPLETED",
    "terminated": "TERMINATED",
    "withdrawn": "WITHDRAWN",
    "suspended": "SUSPENDED",
    "unknown": "UNKNOWN",
}

#: How many studies one answer lists, and how many sites or drugs per study.
MAX_STUDIES = 10
MAX_LOCATIONS = 4
MAX_INTERVENTIONS = 4
MAX_COLLABORATORS = 3

#: The API refuses a page size above this.
MAX_PAGE = 200


class TrialsError(RuntimeError):
    """The registry could not be read, or the question had nothing to look up."""


def phase_text(phases) -> str:
    """['PHASE2', 'PHASE3'] to 'phase 2, phase 3', and NA to 'not applicable'."""
    if not phases:
        return "phase not stated"
    labels = []
    for phase in phases:
        text = str(phase).upper()
        if text == "NA":
            labels.append("not applicable (often a device or behavioural study)")
        elif text.startswith("PHASE"):
            labels.append(f"phase {text[5:]}")
        elif text.startswith("EARLY_PHASE"):
            labels.append(f"early phase {text[12:]}")
        else:
            labels.append(text.lower())
    return ", ".join(labels)


def status_text(status: str) -> str:
    """RECRUITING to 'recruiting'."""
    return str(status or "").replace("_", " ").lower() or "status not stated"


def enrollment_text(info: dict) -> str:
    """{'count': 150, 'type': 'ESTIMATED'} to '150 (estimated)'."""
    if not isinstance(info, dict) or not info.get("count"):
        return "not stated"
    return f"{info['count']:,} ({str(info.get('type') or 'type not stated').lower()})"


def date_text(struct) -> str:
    """{'date': '2022-12-06', 'type': 'ACTUAL'} to '2022-12-06 (actual)'."""
    if not isinstance(struct, dict) or not struct.get("date"):
        return "not stated"
    kind = str(struct.get("type") or "").lower()
    return f"{struct['date']} ({kind})" if kind else str(struct["date"])


def place_words(place: str) -> list[str]:
    """A place as the words a site line could contain: 'Boston, MA' -> ['boston', 'ma']."""
    return [word for word in
            (bit.strip().lower() for bit in str(place or "").replace(",", " ").split())
            if len(word) > 1]


def clean(text) -> str:
    return " ".join(str(text or "").split())


def parse_study(raw: dict) -> dict:
    """One study record to the fields a person asks about, with the sites kept whole."""
    if not isinstance(raw, dict) or not isinstance(raw.get("protocolSection"), dict):
        raise TrialsError("a study record arrived without its protocol section")
    section = raw["protocolSection"]
    identity = section.get("identificationModule") or {}
    status = section.get("statusModule") or {}
    design = section.get("designModule") or {}
    sponsor = section.get("sponsorCollaboratorsModule") or {}
    locations = (section.get("contactsLocationsModule") or {}).get("locations") or []
    eligibility = section.get("eligibilityModule") or {}
    interventions = (section.get("armsInterventionsModule") or {}).get("interventions") or []
    conditions = (section.get("conditionsModule") or {}).get("conditions") or []
    description = section.get("descriptionModule") or {}
    nct = str(identity.get("nctId") or "").strip().upper()
    return {
        "nct": nct,
        "title": clean(identity.get("briefTitle")) or "(no title registered)",
        "official_title": clean(identity.get("officialTitle")),
        "status": str(status.get("overallStatus") or "").strip(),
        "why_stopped": clean(status.get("whyStopped")),
        "start": status.get("startDateStruct"),
        "completion": status.get("completionDateStruct"),
        "last_update": status.get("lastUpdatePostDateStruct"),
        "phases": list(design.get("phases") or []),
        "study_type": str(design.get("studyType") or "").strip(),
        "enrollment": design.get("enrollmentInfo") or {},
        "sponsor": clean((sponsor.get("leadSponsor") or {}).get("name")),
        "collaborators": [clean(item.get("name")) for item in
                          (sponsor.get("collaborators") or [])[:MAX_COLLABORATORS]],
        "conditions": [clean(item) for item in conditions],
        "interventions": [{"type": clean(item.get("type")).lower(),
                           "name": clean(item.get("name"))}
                          for item in interventions[:MAX_INTERVENTIONS]],
        "intervention_count": len(interventions),
        "locations": [{"facility": clean(item.get("facility")), "city": clean(item.get("city")),
                       "state": clean(item.get("state")), "country": clean(item.get("country"))}
                      for item in locations],
        "location_count": len(locations),
        "sex": str(eligibility.get("sex") or "").strip(),
        "minimum_age": clean(eligibility.get("minimumAge")),
        "maximum_age": clean(eligibility.get("maximumAge")),
        "summary": clean(description.get("briefSummary")),
        "has_results": bool(raw.get("hasResults")),
        "url": STUDY_PAGE.format(nct=nct) if nct else "",
    }


def site_line(site: dict) -> str:
    """One site as a person would write it, without empty commas."""
    parts = [site.get("facility"), site.get("city"), site.get("state"), site.get("country")]
    return ", ".join(part for part in parts if part)


def matching_sites(study: dict, place: str) -> list[dict]:
    """The sites of a study that mention the place asked about, in the study's own order."""
    words = place_words(place)
    if not words:
        return []
    found = []
    for site in study.get("locations") or []:
        haystack = " ".join([site.get("facility", ""), site.get("city", ""), site.get("state", ""),
                             site.get("country", "")]).lower()
        if all(word in haystack for word in words):
            found.append(site)
    return found


class TrialsData:
    """Search studies, and read one study by its NCT number."""

    def __init__(self, fetch=None, timeout: float = 30.0, user_agent: str | None = None) -> None:
        self.timeout = float(os.environ.get("TRIALS_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("TRIALS_USER_AGENT") or DEFAULT_USER_AGENT
        self.fetch = fetch or self._http

    # -- transport ---------------------------------------------------------

    def _http(self, url: str) -> str:
        req = request.Request(url, headers={"User-Agent": self.user_agent,
                                            "Accept": "application/json"})
        try:
            with request.urlopen(req, timeout=self.timeout) as reply:
                return reply.read().decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            raise TrialsError(self._say(exc)) from exc
        except error.URLError as exc:
            raise TrialsError(f"ClinicalTrials.gov is unreachable ({exc.reason})") from exc
        except TimeoutError as exc:
            raise TrialsError(f"ClinicalTrials.gov did not answer within {self.timeout:.0f}s") \
                from exc

    @staticmethod
    def _say(exc: "error.HTTPError") -> str:
        """The registry's refusal in words, including the plain-text bodies it sends."""
        detail = ""
        try:
            body = exc.read().decode("utf-8", errors="replace").strip()
            detail = body[:200]
            if detail.startswith("{"):
                found = json.loads(detail).get("message") or ""
                detail = clean(found) or detail
        except Exception:  # noqa: BLE001 - a missing body must not hide the status
            detail = ""
        if exc.code == 400:
            return f"ClinicalTrials.gov answered 400: it refused a search parameter. {detail}"
        if exc.code == 404:
            return f"ClinicalTrials.gov answered 404: no such study. {detail}"
        if exc.code == 429:
            return f"ClinicalTrials.gov answered 429: too many requests right now. {detail}"
        return f"ClinicalTrials.gov answered {exc.code}. {detail}".strip()

    # -- queries -----------------------------------------------------------

    def search_url(self, condition: str | None = None, place: str | None = None,
                   status: str | None = None, term: str | None = None, size: int = MAX_STUDIES,
                   sort: str = "LastUpdatePostDate:desc") -> str:
        """The URL for a search, so an answer can show its own question."""
        params = {"pageSize": str(max(1, min(int(size), MAX_PAGE))),
                  "countTotal": "true", "sort": sort}
        if condition:
            params["query.cond"] = condition
        if place:
            params["query.locn"] = place
        if term:
            params["query.term"] = term
        if status:
            params["filter.overallStatus"] = status
        return f"{BASE}?{parse.urlencode(params)}"

    def search(self, condition: str | None = None, place: str | None = None,
               status: str | None = None, term: str | None = None, limit: int = MAX_STUDIES) -> dict:
        """Studies for a condition and/or a place, newest update first."""
        if not any((condition, place, term)):
            raise TrialsError("give me a condition, a place or a term to search the registry for")
        url = self.search_url(condition, place, status, term, size=limit)
        payload = self.fetch(url)
        try:
            raw = json.loads(payload)
        except ValueError as exc:
            raise TrialsError(f"the registry's answer was not JSON ({exc})") from exc
        if not isinstance(raw, dict):
            raise TrialsError("the registry answered with something other than a result set")
        listed = [parse_study(item) for item in raw.get("studies") or []
                  if isinstance(item, dict)]
        studies = listed[:limit]
        # `returned` is what the caller was actually given, so an answer can never say it showed
        # more studies than it printed; anything held back is counted and reported.
        return {"condition": condition, "place": place, "term": term, "status": status,
                "url": url, "total": raw.get("totalCount"),
                "returned": len(studies), "studies": studies,
                "withheld": len(listed) - len(studies),
                "next_page": raw.get("nextPageToken")}

    def study(self, nct: str) -> dict:
        """One study by its NCT number, or a clear error naming the number."""
        wanted = str(nct or "").strip().upper()
        if not wanted.startswith("NCT") or not wanted[3:].isdigit() or len(wanted) != 11:
            raise TrialsError(f"a trial's number looks like NCT01065467, and {nct!r} is not one")
        payload = self.fetch(f"{BASE}/{wanted}")
        try:
            raw = json.loads(payload)
        except ValueError as exc:
            raise TrialsError(f"the registry's answer was not JSON ({exc})") from exc
        return parse_study(raw)
