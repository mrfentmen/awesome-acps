"""SEC EDGAR - what a public company has filed, and who filed what on a given day.

Verified live on 2026-09-23, all keyless (no API key, no account):

  https://www.sec.gov/files/company_tickers.json                  -> 10,461 tickers, cik_str/ticker/title
  https://data.sec.gov/submissions/CIK0000320193.json             -> name, tickers, sicDescription, filings.recent
  https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent
      &type=10-K&owner=include&count=10&output=atom               -> the latest filings, as Atom
  https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/
      form.20260922.idx                                           -> every filing of that day

Three things this reader must not paper over, because they change the answer:

  1. **SEC will refuse a caller with a 403, and says why only sometimes.** A plain
     `awesome-acps-company/1.0` was served every time; a token with a parenthesised link inside
     it was refused once and served the next minute, so the refusal is about the caller, not
     about the file. SEC asks that the agent send a contact address, and `SEC_USER_AGENT` is
     honoured when it is set. A 403 is reported as a 403, never as "no such company".
  2. **`filings.recent` is only recent.** EDGAR keeps roughly the latest 1,000 filings in that
     array and puts older ones in separate files, so a filing from 2019 is genuinely not in what
     this reader sees, and every answer that lists filings says how deep the list went.
  3. **The daily index is published after the close, and a missing one is not a 404.** Ask for
     today's index at midday and SEC answers **403**, exactly as it does when it refuses a
     caller - measured on 2026-09-23 against `form.20260923.idx`. So neither status is trusted:
     when the day's index cannot be fetched, the quarter's own directory listing is read, and a
     file that is not in it is reported as "not published yet" with the listing as the evidence.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import time
import xml.etree.ElementTree as ElementTree
from urllib import error, request

TICKERS = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
LATEST = ("https://www.sec.gov/cgi-bin/browse-edgar"
          "?action=getcurrent&type={form}&dateb=&owner=include&count={count}&output=atom")
DAILY_INDEX = "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{quarter}/form.{stamp}.idx"
DIRECTORY = "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{quarter}/"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"

DATASET = "SEC EDGAR (company_tickers.json, submissions, browse-edgar atom, daily index .idx)"

#: SEC serves a plain product token and refuses a parenthesised one; see the module docstring.
DEFAULT_USER_AGENT = "awesome-acps-company/1.0"

#: What the reader asks for when nobody says. SEC asks for a contact address; this is the
#: fallback, and SEC_USER_AGENT replaces it, documented in .env.example.
DEFAULT_CONTACT = "contact: see the repository README"

#: The form types a person names by name. A bare number is deliberately absent: "3", "4"
#: and "5" are also ordinary figures, so they are only read as forms after the word "form".
FORMS = ("DEF 14A", "DEFA14A", "SC 13D", "SC 13G", "13F-HR", "10-K", "10-Q", "20-F", "40-F",
         "11-K", "6-K", "8-K", "S-1", "S-3", "S-4", "424B1", "424B2", "424B3", "424B4", "424B5",
         "N-CSR", "N-PORT", "13F", "144")

#: Words a company name ends in that nobody says out loud.
SUFFIXES = ("incorporated", "corporation", "corp", "company", "co", "inc", "ltd", "limited",
            "plc", "llc", "l.l.c", "holdings", "holding", "group", "the", "sa", "nv", "ag",
            "se", "spa", "trust", "technologies", "technology")

#: A company name in a question: letters, digits, & , . ' - and spaces, no verbs.
NAME_WORDS = re.compile(r"[A-Za-z0-9][A-Za-z0-9&.,'\-]*(?:\s+[A-Za-z0-9][A-Za-z0-9&.,'\-]*)*")

#: "form 4", "a form 3", so a bare number is only read as a form when the word form is there.
FORM_WORD = re.compile(r"\bform\s+([0-9]{1,2}[A-Za-z]?)\b", re.IGNORECASE)

#: Words that are neither tickers nor company names. SEC's ticker file really does contain CIK,
#: ALL, IT, ON and ONE - and "/agents/company" once answered "what is Tesla's CIK?" with Credit
#: Suisse, because CIK is a ticker. Ordinary words are never read as a ticker or as a name.
COMMON_WORDS = frozenset({
    "a", "about", "above", "after", "all", "also", "an", "and", "annual", "any", "are", "as",
    "ask", "asked", "at", "be", "been", "before", "being", "below", "between", "but", "by",
    "call", "called", "can", "cik", "could", "count", "did", "do", "document", "documents",
    "does", "each", "edgar", "every", "file", "filed", "filing", "filings", "find", "for",
    "form", "found", "from", "get", "give", "go", "got", "has", "have", "he", "her", "his",
    "how", "i", "if", "in", "into", "is", "it", "its", "just", "k", "know", "last",
    "latest", "less", "list", "look", "made", "make", "many", "may", "me", "might", "more",
    "most", "much", "must", "my", "name", "named", "need", "new", "newest", "no", "not",
    "now", "number", "of", "on", "one", "only", "or", "other", "our", "out", "over", "own",
    "please", "prospectus", "proxy", "q", "quarterly", "read", "receive", "received", "receives",
    "recent", "recently", "registration", "report", "reported", "reports", "same", "search", "sec",
    "see", "she", "should", "show", "so", "some", "statement", "such", "submit", "submitted",
    "tell", "than", "that", "the", "their", "them", "then", "there", "these", "they", "this",
    "those", "to", "today", "tonight", "total", "two", "under", "up", "us", "want", "was", "we",
    "were", "what", "when", "where", "which", "while", "who", "whose", "why", "will", "with",
    "would", "yesterday", "you", "your",
})

#: A company name is at least this long; 's', 'a' and 'x' are not names.
MIN_NAME = 3

#: One row of a daily `.idx`: form type, company, CIK, 8-digit date, then the archive path.
ROW = re.compile(r"^(?P<form>\S.*?)\s{2,}(?P<company>.+?)\s{2,}(?P<cik>\d+)\s+"
                 r"(?P<filed>\d{8})\s+(?P<file>edgar/\S+)\s*$")


class CompanyError(RuntimeError):
    """EDGAR could not be read, or the question did not name a company EDGAR knows."""


def normalize(name: str) -> str:
    """A company name reduced to the words people actually say: 'Apple Inc.' -> 'apple'."""
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", str(name or "").lower())
    words = [word for word in cleaned.split() if word and word != "the"]
    while words and words[-1] in SUFFIXES:
        words.pop()
    return " ".join(words)


def form_from_text(text: str, default: str | None = None) -> str | None:
    """The form type named in a question, if any. '10-k' and 'form 4' both count."""
    asked = str(text or "").upper()
    pattern = re.compile(r"(?<![0-9A-Z])" + "|".join(re.escape(form) for form in FORMS)
                         + r"(?![0-9A-Z])")
    found = pattern.search(asked)
    if found:
        return found.group(0)
    numbered = FORM_WORD.search(str(text or ""))
    if numbered:
        return numbered.group(1).upper()
    return default


def quarter_of(date: datetime.date) -> int:
    return (date.month - 1) // 3 + 1


def document_url(cik: int, accession: str, document: str) -> str:
    """The link a person can click: EDGAR puts the accession without dashes in the path."""
    if not accession or not document:
        return f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
    return ARCHIVE.format(cik=int(cik), accession=str(accession).replace("-", ""),
                          document=document)


def age_days(filed: str, today: datetime.date | None = None) -> int | None:
    """How long ago a filing date was, or None when EDGAR's date does not parse."""
    try:
        when = datetime.date.fromisoformat(str(filed)[:10])
    except ValueError:
        return None
    return ((today or datetime.date.today()) - when).days


def summarize_forms(rows: list[dict]) -> list[tuple[str, int]]:
    """Daily-index rows to (form, count), most common first, ties broken by form name."""
    counts: dict[str, int] = {}
    for row in rows:
        form = str(row.get("form") or "?").strip() or "?"
        counts[form] = counts.get(form, 0) + 1
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))


class CompanyData:
    """The ticker map, one company's filings, the latest-filings feed and the daily index."""

    def __init__(self, fetch=None, cache_ttl: float = 86400.0, timeout: float = 25.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("SEC_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("SEC_HTTP_TIMEOUT", timeout))
        contact = os.environ.get("SEC_USER_AGENT")
        self.user_agent = user_agent or contact or f"{DEFAULT_USER_AGENT} ({DEFAULT_CONTACT})"
        self.fetch = fetch or self._http
        self._tickers: list[dict] | None = None
        self._tickers_at = 0.0
        #: cik -> {"at": epoch, "payload": submissions json}
        self._company: dict[int, dict] = {}

    # -- transport ---------------------------------------------------------

    def _http(self, url: str, headers: dict | None = None) -> str:
        """One GET. Raises CompanyError carrying SEC's own status, never a silent empty page."""
        merged = {"User-Agent": self.user_agent, "Accept-Encoding": "gzip"}
        merged.update(headers or {})
        req = request.Request(url, headers=merged)
        try:
            with request.urlopen(req, timeout=self.timeout) as reply:
                raw = reply.read()
                if reply.headers.get("Content-Encoding") == "gzip":
                    import gzip

                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            raise CompanyError(self._say(exc.code, url)) from exc
        except error.URLError as exc:
            raise CompanyError(f"EDGAR is unreachable ({exc.reason}) for {url}") from exc
        except TimeoutError as exc:
            raise CompanyError(f"EDGAR did not answer within {self.timeout:.0f}s") from exc

    @staticmethod
    def _say(code: int, url: str) -> str:
        """SEC's status in words, including the one that is about the caller, not the company."""
        if code == 403:
            return (f"SEC answered 403 for {url}: it refused this request. SEC asks every caller "
                    f"to send a product name and a contact address - set SEC_USER_AGENT and try "
                    f"again")
        if code == 404:
            return f"SEC answered 404 for {url}: that document does not exist"
        if code == 429:
            return f"SEC answered 429 for {url}: too many requests, wait and try again"
        return f"SEC answered {code} for {url}"

    # -- the ticker map ----------------------------------------------------

    def tickers(self) -> list[dict]:
        """Every ticker SEC knows: [{'cik': int, 'ticker': str, 'title': str}, ...], cached."""
        fresh = time.monotonic() - self._tickers_at < self.cache_ttl
        if self._tickers is not None and fresh:
            return self._tickers
        payload = json.loads(self.fetch(TICKERS))
        if not isinstance(payload, dict):
            raise CompanyError("SEC's ticker file was not a JSON object")
        rows = []
        for entry in payload.values():
            if not isinstance(entry, dict):
                continue
            try:
                cik = int(entry["cik_str"])
            except (KeyError, TypeError, ValueError):
                continue
            rows.append({"cik": cik, "ticker": str(entry.get("ticker") or "").upper(),
                         "title": str(entry.get("title") or "")})
        if not rows:
            raise CompanyError("SEC's ticker file held no readable rows")
        self._tickers, self._tickers_at = rows, time.monotonic()
        return rows

    def resolve(self, text: str) -> dict:
        """The company a question names: by ticker, by CIK, or by name. Raises when unclear."""
        words = str(text or "")
        digits = re.search(r"\bCIK\s*0*(\d{3,10})\b", words, re.IGNORECASE)
        rows = self.tickers()
        by_ticker = {row["ticker"]: row for row in rows if row["ticker"]}
        by_name = {normalize(row["title"]): row for row in rows}
        wanted = normalize(words)

        if digits:
            cik = int(digits.group(1))
            match = next((row for row in rows if row["cik"] == cik), None)
            return {"cik": cik, "ticker": (match or {}).get("ticker", ""),
                    "title": (match or {}).get("title", f"CIK {cik}"), "how": "the CIK in the question"}

        # A ticker is only a ticker when it is written the way the map writes it, and is not an
        # ordinary word that happens to be somebody's ticker.
        for token in re.findall(r"\b[A-Z]{1,5}\b", words):
            if token in by_ticker and token.lower() not in COMMON_WORDS:
                row = by_ticker[token]
                return {"cik": row["cik"], "ticker": row["ticker"], "title": row["title"],
                        "how": f"the ticker {row['ticker']}"}

        # The whole name first ('apple' -> Apple Inc.), then the most specific phrase inside it.
        if wanted and wanted in by_name:
            row = by_name[wanted]
            return {"cik": row["cik"], "ticker": row["ticker"], "title": row["title"],
                    "how": f"the name {row['title']}"}

        for phrase in self._phrases(wanted):
            # One ordinary word in a question is not a company: 'how many filings' must not
            # become a filer because some title happens to contain the word "many".
            if phrase in COMMON_WORDS or len(phrase) < MIN_NAME:
                continue
            matches = [row for row in rows if normalize(row["title"]) == phrase]
            if matches:
                row = matches[0]
                return {"cik": row["cik"], "ticker": row["ticker"], "title": row["title"],
                        "how": f"the name {row['title']}"}
            contained = [row for row in rows if phrase in normalize(row["title"])]
            if contained:
                row = min(contained, key=lambda item: len(normalize(item["title"])))
                return {"cik": row["cik"], "ticker": row["ticker"], "title": row["title"],
                        "how": f"the name {row['title']}"}
        raise CompanyError(
            f"I could not find a company called {str(text or '').strip()!r} in SEC's ticker file. "
            f"Name it as a ticker (AAPL), a CIK (CIK 320193) or its filed name (Apple Inc.)"
        )

    @staticmethod
    def _phrases(text: str, longest: int = 4) -> list[str]:
        """The word runs in a question, longest first, so 'bank of america' beats 'bank'."""
        words = text.split()
        phrases = []
        for size in range(min(longest, len(words)), 0, -1):
            for start in range(len(words) - size + 1):
                phrases.append(" ".join(words[start:start + size]))
        return phrases

    # -- one company -------------------------------------------------------

    def company(self, cik: int) -> dict:
        """A company's EDGAR record, with the recent filings already turned into rows."""
        cached = self._company.get(int(cik))
        if cached and time.monotonic() - cached["at"] < self.cache_ttl:
            return cached["payload"]
        payload = json.loads(self.fetch(SUBMISSIONS.format(cik=f"{int(cik):010d}")))
        payload["filings"]["recent"] = self._rows(int(cik), payload.get("filings") or {})
        self._company[int(cik)] = {"at": time.monotonic(), "payload": payload}
        return payload

    @staticmethod
    def _rows(cik: int, filings: dict) -> list[dict]:
        """EDGAR's parallel arrays as one list of filings, newest first as EDGAR sent them."""
        recent = filings.get("recent") or {}
        forms = recent.get("form") or []
        rows = []
        for index, form in enumerate(forms):
            def at(key):
                values = recent.get(key) or []
                return values[index] if index < len(values) else ""

            accession = str(at("accessionNumber") or "")
            document = str(at("primaryDocument") or "")
            rows.append({
                "form": str(form or ""),
                "filed": str(at("filingDate") or ""),
                "period": str(at("reportDate") or ""),
                "description": str(at("primaryDocDescription") or ""),
                "accession": accession,
                "document": document,
                "size": at("size"),
                "url": document_url(cik, accession, document),
            })
        return rows

    def filings(self, cik: int, form: str | None = None, limit: int = 10,
                since: str | None = None) -> dict:
        """Recent filings for one company, newest first, optionally of one form type.

        The depth of the list is reported because `filings.recent` is not the whole history.
        """
        payload = self.company(cik)
        rows = payload["filings"]["recent"]
        wanted = [row for row in rows if not form or row["form"] == form]
        if since:
            wanted = [row for row in wanted if row["filed"] >= since]
        return {"name": payload.get("name") or f"CIK {int(cik)}",
                "tickers": payload.get("tickers") or [],
                "sic": payload.get("sicDescription") or "",
                "form": form,
                "matched": len(wanted),
                "seen": len(rows),
                "oldest_seen": rows[-1]["filed"] if rows else "",
                "filings": wanted[:limit]}

    # -- what EDGAR is receiving right now ---------------------------------

    def latest(self, form: str | None = None, count: int = 10) -> dict:
        """The most recent filings EDGAR lists for a form type, from the Atom feed."""
        if not form:
            raise CompanyError("EDGAR's latest-filings feed needs a form type (10-K, 8-K, ...)")
        url = LATEST.format(form=request.quote(str(form)), count=max(1, min(int(count), 100)))
        xml = self.fetch(url)
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError as exc:
            raise CompanyError(f"EDGAR's latest-filings feed was not XML ({exc})") from exc
        namespace = {"atom": "http://www.w3.org/2005/Atom"}
        updated = ""
        feed_title = ""
        for child in root:
            tag = child.tag.split("}")[-1]
            if tag == "updated":
                updated = child.text or ""
            elif tag == "title":
                feed_title = child.text or ""
        rows = []
        for entry in root.findall("atom:entry", namespace):
            title = entry.findtext("atom:title", default="", namespaces=namespace)
            link = entry.find("atom:link", namespace)
            summary = entry.findtext("atom:summary", default="", namespaces=namespace)
            rows.append({
                "title": " ".join(title.split()),
                "filed": (entry.findtext("atom:updated", default="", namespaces=namespace) or "")[:10],
                "url": link.get("href") if link is not None else "",
                "detail": " ".join(re.sub(r"<[^>]+>", " ", summary).split()),
            })
        return {"form": form, "feed": feed_title, "updated": updated, "entries": rows}

    def listed(self, date: datetime.date) -> bool:
        """Whether the quarter's directory listing names that day's index - the ground truth.

        SEC serves a 403 both for a file it has not published and for a caller it will not
        serve, so the status alone cannot answer "does today's index exist yet". The listing
        can, and it is only read when the index itself could not be fetched.
        """
        url = DIRECTORY.format(year=date.year, quarter=quarter_of(date))
        page = self.fetch(url)
        return f"form.{date.strftime('%Y%m%d')}.idx" in page

    def daily(self, date: datetime.date | None = None) -> dict:
        """Every filing of one day, from the daily index. `posted` says whether it exists yet."""
        when = date or datetime.date.today()
        url = DAILY_INDEX.format(year=when.year, quarter=quarter_of(when), stamp=when.strftime("%Y%m%d"))
        try:
            text = self.fetch(url)
        except CompanyError as exc:
            listing = DIRECTORY.format(year=when.year, quarter=quarter_of(when))
            if self.listed(when):
                raise CompanyError(f"{exc}; SEC's own listing does name this index, so the "
                                   f"refusal is about the caller") from exc
            # At midday this is the normal answer, not a failure: SEC posts the index after the
            # close, and does not publish it until then. Callers get a fact, not an exception.
            return {"date": when.isoformat(), "posted": False, "url": url, "listing": listing,
                    "rows": []}
        rows = parse_index(text)
        if not rows:
            raise CompanyError(f"the daily index for {when.isoformat()} parsed into no rows; its "
                               f"columns may have changed")
        return {"date": when.isoformat(), "posted": True, "url": url, "rows": rows}


def parse_index(text: str) -> list[dict]:
    """A daily `.idx` file into rows.

    The rows are fixed-width, but the column headers are printed on two lines whose indentation
    does not line up with the data below it - reading the offsets from the header puts company
    names and CIKs in the wrong columns. So each row is read by shape instead, anchored on the
    8-digit filing date and the `edgar/` path that every row ends with, both of which the format
    guarantees. The dash line under the header marks where the rows begin.
    """
    lines = text.splitlines()
    start = 0
    for index, line in enumerate(lines):
        if line.startswith("-----"):
            start = index + 1
            break
    rows = []
    for line in lines[start:]:
        match = ROW.search(line)
        if not match:
            continue
        cells = match.groupdict()
        if not cells["cik"] or not cells["file"].startswith("edgar/"):
            continue
        rows.append({"form": cells["form"].strip(), "company": cells["company"].strip(),
                     "cik": cells["cik"], "filed": cells["filed"],
                     "url": "https://www.sec.gov/Archives/" + cells["file"].strip()})
    return rows
