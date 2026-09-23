"""Company - what a public company has filed with the SEC, and who filed today.

Nobody in any editor answers "Microsoft's last 10-K" or "who filed a 10-K today?" - the filings
are public, structured and keyless, and no ACP agent reads them. This one does, and it states
the three limits EDGAR really has in every answer:

  * `filings.recent` holds roughly the latest 1,000 filings, not the whole history, so an answer
    says how far back it looked;
  * the daily index is published after the close, so a midday "today" is answered with the most
    recent day that exists and the reason why, never with a made-up empty list;
  * a 403 from SEC is about the caller's User-Agent, so it is reported as that, not as "no such
    company".

Nothing is inferred from a company name that SEC did not say: the ticker, the name and the SIC
code all come from EDGAR's own files.
"""

from __future__ import annotations

import datetime
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from data import (  # noqa: E402
    COMMON_WORDS,
    DATASET,
    FORMS,
    CompanyData,
    CompanyError,
    form_from_text,
    normalize,
    summarize_forms,
)

HELP = (
    "I read SEC EDGAR (keyless, no account). Ask me:\n"
    "  - Microsoft's last 10-K\n"
    "  - what has Apple filed recently?\n"
    "  - who filed a 10-K today?\n"
    "  - how many filings did EDGAR receive yesterday?\n"
    "  - which company is CIK 320193?\n"
    "  - what is Tesla's CIK?\n"
    "I answer for one company at a time, from EDGAR's own files: the form type, the filing date,\n"
    "the period it covers and a link to the document. The latest ~1,000 filings are in reach for\n"
    "one company; older ones need a search on SEC's site."
)

PERMISSION_KEY = "company-read-edgar"

SKILLS = ("company-filings", "company-today", "company-lookup", "company-help")

#: How many rows one answer prints, and how far back a daily index is looked for.
MAX_ROWS = 10
MAX_DAYS_BACK = 5

#: A day is named, so the question is about a day of filings rather than one company's list.
_TODAY = re.compile(r"\b(today|tonight|this morning|right now|just filed|who filed|"
                    r"filings received|received (?:today|yesterday)|receive(?:s|d)? (?:today|"
                    r"yesterday))\b", re.IGNORECASE)
#: "today" alone still means a day - but only when no company is named as well.
_DAY = re.compile(r"\b(today|tonight|yesterday|this morning)\b", re.IGNORECASE)
_LOOKUP = re.compile(r"\b(cik|ticker|ticker symbol|which company|what company|whose cik|"
                     r"what is the cik|identified by)\b", re.IGNORECASE)
_FILED = re.compile(r"\b(filed|filing|filings|file|lodge[d]?|submitted|annual report|"
                    r"quarterly report|proxy|prospectus|registration statement|10-?[kq]|8-?k|"
                    r"13f|s-1|def 14a)\b", re.IGNORECASE)

def company_hint(text: str) -> str | None:
    """The slice of a question that could name a company, or None if nothing could.

    A ticker is only believed when it is written as the ticker map writes it and is not an
    ordinary uppercase word; everything else is left to the ticker file's own search.
    """
    stripped = str(text or "").strip()
    if re.search(r"\bCIK\s*0*\d{3,10}\b", stripped, re.IGNORECASE):
        return stripped
    tokens = re.findall(r"\b[A-Z]{2,5}\b", stripped)
    if any(token.lower() not in COMMON_WORDS for token in tokens):
        return stripped
    # Digits are part of a word here, so "10-K" is one token rather than a stray "K".
    words = [word.strip(".,?!'\"()")
             for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9&.,'\-]*", stripped)]
    for index, word in enumerate(words):
        lowered = word.lower()
        if lowered in COMMON_WORDS or lowered in {form.lower() for form in FORMS}:
            continue
        if len(word) < 3:
            continue
        # "microsoft's" belongs to "microsoft"; a following apostrophe-s is not a word of its own.
        return " ".join(words[index:index + 3])
    return None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    stripped = str(text or "").strip()
    if not stripped or re.search(r"\b(help|what can you do|commands?)\b", stripped, re.IGNORECASE):
        return "company-help", {}
    form = form_from_text(stripped)
    hint = company_hint(stripped)
    if _TODAY.search(stripped) or (_DAY.search(stripped) and not hint):
        params: dict = {}
        if form:
            params["form"] = form
        if hint:
            params["query"] = hint
        if re.search(r"\byesterday\b", stripped, re.IGNORECASE):
            params["day"] = 1
        return "company-today", params
    if _LOOKUP.search(stripped) and hint:
        return "company-lookup", {"query": hint}
    if hint and (_FILED.search(stripped) or form):
        params = {"query": hint}
        if form:
            params["form"] = form
        return "company-filings", params
    if hint and re.search(r"\b(report|results|earnings|revenue|10-?k|10-?q)\b", stripped,
                           re.IGNORECASE):
        return "company-filings", {"query": hint}
    return "company-help", {}


def human_date(value: str) -> str:
    """'2025-10-31' stays as written - the period EDGAR prints is the period it means."""
    return str(value or "").strip() or "-"


def render_filings(reading: dict) -> str:
    """A company's recent filings, with how deep the list went."""
    tickers = ", ".join(reading["tickers"]) or "no ticker"
    form = reading["form"]
    head = f"{reading['name']} ({tickers})"
    if reading["sic"]:
        head += f" - {reading['sic']}"
    lines = [f"{head}:"]
    if form:
        lines.append(f"  - {reading['matched']} filing(s) of form {form} among the last "
                     f"{reading['seen']} EDGAR lists for this company"
                     + (f" (back to {reading['oldest_seen']})" if reading["oldest_seen"] else ""))
    else:
        lines.append(f"  - the last {reading['seen']} EDGAR lists for this company"
                     + (f" (back to {reading['oldest_seen']})" if reading["oldest_seen"] else ""))
    if not reading["filings"]:
        lines.append(f"  - nothing of that form is in those {reading['seen']} filings")
        return "\n".join(lines)
    for row in reading["filings"]:
        detail = "  ".join(part for part in (
            f"period {row['period']}" if row["period"] else "",
            row["description"] if row["description"] != row["form"] else "") if part)
        lines.append(f"  - {human_date(row['filed'])}  {row['form']:<10}  {detail}".rstrip())
        lines.append(f"      {row['url']}")
    return "\n".join(lines)


def render_today(reading: dict, form: str | None, matching: list[dict] | None) -> str:
    """A day of EDGAR's index: the whole day's totals, then the filers that were asked about.

    The totals are always of the whole day. Counting them after a filter would turn a 3,400
    filing day into "3 filings", which is the kind of quiet lie a count can tell.
    """
    rows = reading["rows"]
    filers = {row["cik"] for row in rows if row["cik"]}
    lines = [f"EDGAR's daily index for {reading['date']}: {len(rows)} filing(s) from "
             f"{len(filers)} filer(s)"]
    if reading.get("shifted"):
        lines.append(f"  - note: {reading['shifted']}")
    counts = summarize_forms(rows)
    if counts:
        lines.append("  - forms received: " + ", ".join(f"{name} {count}" for name, count
                                                          in counts[:8]))
        if len(counts) > 8:
            lines.append(f"      ... and {len(counts) - 8} more form type(s) that day")
    else:
        lines.append("  - no filings in this index")
    if matching is not None:
        wanted = f"form {form}" if form else "the company asked about"
        lines.append(f"  - of those, {len(matching)} match {wanted}"
                     + (":" if len(matching) <= MAX_ROWS else f"; the first {MAX_ROWS}:"))
        for row in matching[:MAX_ROWS]:
            lines.append(f"      {row['company']} (CIK {row['cik']}) filed {row['form']}"
                         f" on {row['filed']}")
        if len(matching) > MAX_ROWS:
            lines.append(f"      ... and {len(matching) - MAX_ROWS} more")
    lines.append(f"  - the whole index: {reading['url']}")
    return "\n".join(lines)


class CompanyAgent(AcpAgent):
    name = "company"
    title = "Company - SEC filings, and who filed today"
    version = "1.0.0"

    def __init__(self, connection=None, data: CompanyData | None = None) -> None:
        super().__init__(connection)
        self.data = data or CompanyData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Company session opened. " + HELP.splitlines()[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read EDGAR (ticker file, submissions, daily index)", "medium"),
            ("Report the form, the dates and a link, with the limits stated", "medium"),
        ])

        if skill == "company-help":
            ctx.stream_text(HELP)
            ctx.message("Name a company (ticker, CIK or its filed name) and a form type, and I "
                        "will read EDGAR for it.")
            return STOP_END_TURN

        tool = f"call_{skill.replace('-', '_')}"
        ctx.tool_call(tool, f"Read SEC EDGAR for {skill.replace('company-', '')}",
                      kind="fetch", name=skill, raw_input=dict(params))
        if not ctx.ask_permission(tool, "Allow Company to read SEC EDGAR?", remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read SEC EDGAR first.")
            return STOP_REFUSAL
        ctx.tool_call_update(tool, status="in_progress")

        try:
            if skill == "company-lookup":
                body, summary = self._lookup(params), "ticker file read"
            elif skill == "company-filings":
                body, summary = self._filings(params)
            else:
                body, summary = self._today(params)
        except CompanyError as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read that: {exc}")
            return STOP_END_TURN

        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(summary))
        ctx.stream_text(body + "\n")
        ctx.message(f"\nSource: {DATASET}, read live. "
                    "Everything above is what EDGAR published - nothing here is estimated.")
        return STOP_END_TURN

    # -- the three answers -------------------------------------------------

    def _lookup(self, params: dict) -> str:
        row = self.data.resolve(params["query"])
        lines = [f"{row['title']}"]
        lines.append(f"  - ticker: {row['ticker'] or 'no ticker on file'}")
        lines.append(f"  - CIK: {int(row['cik'])} (padded: {int(row['cik']):010d})")
        lines.append(f"  - matched on {row['how']}")
        payload = self.data.company(row["cik"])
        if payload.get("sicDescription"):
            lines.append(f"  - SIC {payload.get('sic') or '-'}: {payload['sicDescription']}")
        if payload.get("exchanges"):
            lines.append(f"  - exchanges: {', '.join(payload['exchanges'])}")
        return "\n".join(lines)

    def _filings(self, params: dict) -> tuple[str, str]:
        row = self.data.resolve(params["query"])
        reading = self.data.filings(row["cik"], form=params.get("form"), limit=MAX_ROWS)
        if not reading["form"] and not reading["matched"]:
            raise CompanyError(f"{row['title']} has no filings in EDGAR's recent list")
        return render_filings(reading), f"{reading['matched']} filing(s) read"

    def _today(self, params: dict) -> tuple[str, str]:
        """One day of EDGAR's index, with the day that actually exists named out loud."""
        form = params.get("form")
        days_back = int(params.get("day") or 0)
        reading: dict | None = None
        for step in range(days_back, days_back + MAX_DAYS_BACK):
            found = self.data.daily(datetime.date.today() - datetime.timedelta(days=step))
            if not found["posted"]:
                continue
            if step > days_back:
                asked = "today" if not days_back else f"{days_back} day(s) ago"
                found["shifted"] = (f"EDGAR has not published an index for {asked} - SEC posts the "
                                    f"daily index after the close - so this is the most recent "
                                    f"day that exists")
            reading = found
            break
        if reading is None:
            raise CompanyError("SEC has not published a daily index for the last "
                               f"{MAX_DAYS_BACK} days, so I have no day to count filings for")

        matching: list[dict] | None = None
        scope: list[str] = []
        if params.get("query"):
            row = self.data.resolve(params["query"])
            wanted = normalize(row["title"])
            name = re.sub(r"\s+", " ", row["title"].lower())
            matching = [item for item in reading["rows"]
                        if wanted and (wanted in normalize(item["company"])
                                       or name in item["company"].lower())]
            scope.append(row["title"])
        if form:
            matching = [item for item in (reading["rows"] if matching is None else matching)
                        if item["form"] == form]
            scope.append(f"a {form}")

        if scope and not matching:
            what = " and ".join(scope)
            if params.get("query"):
                # A latest-filings feed would answer about the form, not about this company, so
                # it is not offered here: the honest answer is that this company filed nothing.
                return (f"No {what} appears in EDGAR's index for {reading['date']} "
                        f"({reading['url']}).", f"0 for {what}")
            # No such filing that day, but EDGAR's own latest-filings feed still answers "what
            # is the most recent 10-K" - shown as that, never as "filed today".
            latest = self.data.latest(form=form, count=MAX_ROWS)
            lines = [f"No {what} was filed on {reading['date']} according to EDGAR's daily index "
                     f"({reading['url']}).",
                     f"The most recent {form} filings EDGAR lists right now:"]
            for entry in latest["entries"][:MAX_ROWS]:
                lines.append(f"  - {entry['filed']}  {entry['title']}")
                lines.append(f"      {entry['url']}")
            lines.append(f"  - that feed: {latest['feed']} (as of {latest['updated']})")
            return "\n".join(lines), f"0 on {reading['date']}, {len(latest['entries'])} recent read"

        body = render_today(reading, form, matching)
        return body, f"{len(reading['rows'])} filing(s) on {reading['date']}"

    def on_cancel(self, session) -> None:
        return None  # a handful of reads, one answer


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        CompanyAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
