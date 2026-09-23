"""Tests for the company agent: the ticker map, EDGAR's filings, the index and the routing.

    python3 agents/company/tests/test_agent.py

No network: every EDGAR document is injected. The fixtures are shaped like the live payloads
read on 2026-09-23, and three of them pin traps that were measured live rather than imagined:

  1. SEC's ticker file really does carry a company whose ticker is `CIK`, so "what is Tesla's
     CIK?" once resolved to Credit Suisse. A common word is never read as a ticker.
  2. The daily `.idx` header prints its column names on two lines whose indentation does not
     line up with the rows, so reading offsets from the header mis-splits every row.
  3. A daily index that does not exist yet answers **403**, the same status as "I will not serve
     this caller", so the quarter's directory listing is what decides which one it was.
"""

from __future__ import annotations

import datetime
import json
import queue
import sys
import threading
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for path in (str(REPO_ROOT), str(AGENT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from acp_kit import AcpClient, Connection, STOP_END_TURN, STOP_REFUSAL  # noqa: E402

from agent import (  # noqa: E402
    HELP,
    MAX_ROWS,
    company_hint,
    human_date,
    render_filings,
    render_today,
    route,
)
from data import (  # noqa: E402
    COMMON_WORDS,
    DIRECTORY,
    CompanyData,
    CompanyError,
    age_days,
    document_url,
    form_from_text,
    normalize,
    parse_index,
    quarter_of,
    summarize_forms,
)

TICKERS_JSON = json.dumps({
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
    "2": {"cik_str": 1318605, "ticker": "TSLA", "title": "Tesla, Inc."},
    # Real, and the reason COMMON_WORDS exists.
    "3": {"cik_str": 810766, "ticker": "CIK",
          "title": "CREDIT SUISSE ASSET MANAGEMENT INCOME FUND, INC."},
    "4": {"cik_str": 1114859, "ticker": "MANY", "title": "MANY HOLDINGS LTD"},
    "5": {"cik_str": 1385818, "ticker": "AYTU", "title": "Aytu Biopharma, Inc."},
})

MS_SUBMISSIONS = json.dumps({
    "cik": "0000789019",
    "name": "MICROSOFT CORP",
    "tickers": ["MSFT"],
    "exchanges": ["Nasdaq"],
    "sic": "7372",
    "sicDescription": "Services-Prepackaged Software",
    "filings": {"recent": {
        "form": ["10-K", "8-K"],
        "filingDate": ["2026-07-29", "2026-08-01"],
        "reportDate": ["2026-06-30", "2026-07-28"],
        "accessionNumber": ["0001193125-26-323660", "0001193125-26-330000"],
        "primaryDocument": ["msft-20260630.htm", "msft-8k.htm"],
        "primaryDocDescription": ["10-K", "8-K"],
        "size": [900, 100],
    }},
})

SUBMISSIONS = json.dumps({
    "cik": "0000320193",
    "name": "Apple Inc.",
    "tickers": ["AAPL"],
    "exchanges": ["Nasdaq"],
    "sic": "3571",
    "sicDescription": "Electronic Computers",
    "filings": {"recent": {
        "form": ["10-K", "8-K", "4"],
        "filingDate": ["2025-10-31", "2025-11-01", "2025-11-02"],
        "reportDate": ["2025-09-27", "2025-10-30", "2025-11-01"],
        "accessionNumber": ["0000320193-25-000079", "0000320193-25-000080",
                            "0000320193-25-000081"],
        "primaryDocument": ["aapl-20250927.htm", "aapl-8k.htm", "xslF345X06/form4.xml"],
        "primaryDocDescription": ["10-K", "8-K", ""],
        "size": [1000, 200, 30],
    }},
})

INDEX = ("Description:           Daily Index of EDGAR Dissemination Feed by Form Type\r\n"
         "Last Data Received:    Sep 22, 2026\r\n"
         "Comments:              webmaster@sec.gov\r\n"
         "\r\n"
         "Form Type   Company Name                                                  CIK\r\n"
         "      Date Filed  File Name\r\n"
         "----------------------------------------------------------------------\r\n"
         "10-K             Aytu Biopharma, Inc.                                    1385818"
         "     20260922    edgar/data/1385818/0001437749-26-030908.txt\r\n"
         "8-K              A.K.A. BRANDS HOLDING CORP.                             1001253"
         "     20260922    edgar/data/1001253/0001001253-26-000021.txt\r\n"
         "10-K             Readvantage Corp.                                       2057381"
         "     20260922    edgar/data/2057381/0002057381-26-000021.txt\r\n"
         "not a row at all\r\n")

ATOM = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Latest Filings - Sat, 19 Sep 2026 11:36:54 EDT</title>
<updated>2026-09-19T11:36:54-04:00</updated>
<entry>
  <title>10-K - Neuphoria Therapeutics Inc. (0001191070) (Filer)</title>
  <link href="https://www.sec.gov/Archives/edgar/data/1191070/x-index.htm"/>
  <updated>2026-09-18T16:10:46-04:00</updated>
  <summary>&lt;b&gt;Filed:&lt;/b&gt; 2026-09-18 &lt;b&gt;AccNo:&lt;/b&gt; 0001193125-26-395536</summary>
</entry>
<entry>
  <title>10-K - Nutanix, Inc. (0001618732) (Filer)</title>
  <link href="https://www.sec.gov/Archives/edgar/data/1618732/y-index.htm"/>
  <updated>2026-09-17T20:42:44-04:00</updated>
  <summary>&lt;b&gt;Filed:&lt;/b&gt; 2026-09-18 &lt;b&gt;AccNo:&lt;/b&gt; 0001193125-26-394793</summary>
</entry>
</feed>
"""

YESTERDAY = datetime.date.today() - datetime.timedelta(days=1)
YESTERDAY_STAMP = YESTERDAY.strftime("%Y%m%d")
LISTING = (f"<a href=\"form.{YESTERDAY_STAMP}.idx\">form.{YESTERDAY_STAMP}.idx</a>\n"
           f"<a href=\"form.20260921.idx\">form.20260921.idx</a>\n")


class FakeEdgar:
    """The four EDGAR documents, served by url. Records what was asked for."""

    def __init__(self, index=INDEX, submissions=SUBMISSIONS, atom=ATOM, listing=LISTING,
                 fail_today=True, refuse=None) -> None:
        self.calls: list[str] = []
        self.index = index
        self.submissions = submissions
        self.atom = atom
        self.listing = listing
        self.fail_today = fail_today
        self.refuse = refuse

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        if self.refuse and self.refuse in url:
            raise CompanyError(f"SEC answered 403 for {url}: it refused this request")
        if "company_tickers.json" in url:
            return TICKERS_JSON
        if "/submissions/" in url:
            # A fake that answers with another company's record would let a real mix-up pass.
            if "0000789019" in url:
                return MS_SUBMISSIONS
            return self.submissions
        if "browse-edgar" in url:
            return self.atom
        if url.endswith(".idx"):
            stamp = url.rsplit("form.", 1)[-1].replace(".idx", "")
            if stamp == datetime.date.today().strftime("%Y%m%d") and self.fail_today:
                raise CompanyError(f"SEC answered 403 for {url}: it refused this request")
            return self.index
        if url.endswith("/QTR3/") or url.endswith("/QTR4/"):
            return self.listing
        raise AssertionError(f"unexpected url: {url}")


class NameTests(unittest.TestCase):
    def test_suffixes_are_words_people_do_not_say(self):
        self.assertEqual(normalize("Apple Inc."), "apple")
        self.assertEqual(normalize("MICROSOFT CORP"), "microsoft")
        self.assertEqual(normalize("Tesla, Inc."), "tesla")
        self.assertEqual(normalize("Howard Hughes Holdings Inc."), "howard hughes")
        self.assertEqual(normalize("The Coca-Cola Company"), "coca cola")

    def test_a_form_is_found_by_name_and_by_the_word_form(self):
        self.assertEqual(form_from_text("Microsoft's last 10-K"), "10-K")
        self.assertEqual(form_from_text("what 8-k did they file"), "8-K")
        self.assertEqual(form_from_text("the DEF 14A"), "DEF 14A")
        self.assertEqual(form_from_text("a form 4 filing"), "4")
        self.assertEqual(form_from_text("prospectus?"), None)
        self.assertEqual(form_from_text("nothing here", default="10-Q"), "10-Q")

    def test_a_bare_number_is_only_a_form_after_the_word_form(self):
        self.assertIsNone(form_from_text("did they file a 4?"))
        self.assertEqual(form_from_text("a form 3 today"), "3")
        self.assertEqual(form_from_text("form 4"), "4")

    def test_a_cik_only_reads_when_it_is_one(self):
        self.assertIsNone(form_from_text("filings from 1999"))

    def test_quarter_and_document_url(self):
        self.assertEqual(quarter_of(datetime.date(2026, 9, 23)), 3)
        self.assertEqual(quarter_of(datetime.date(2026, 1, 1)), 1)
        self.assertEqual(quarter_of(datetime.date(2026, 12, 31)), 4)
        self.assertEqual(document_url(320193, "0000320193-25-000079", "aapl-20250927.htm"),
                         "https://www.sec.gov/Archives/edgar/data/320193/"
                         "000032019325000079/aapl-20250927.htm")
        self.assertIn("browse-edgar", document_url(320193, "", ""))

    def test_age_days_is_none_for_a_date_edgar_did_not_write(self):
        self.assertEqual(age_days("2026-09-22", datetime.date(2026, 9, 23)), 1)
        self.assertIsNone(age_days("", datetime.date(2026, 9, 23)))

    def test_form_totals_are_sorted_by_count_then_by_name(self):
        rows = [{"form": "8-K"}, {"form": "10-K"}, {"form": "8-K"}, {"form": ""}]
        self.assertEqual(summarize_forms(rows), [("8-K", 2), ("10-K", 1), ("?", 1)])


class IndexTests(unittest.TestCase):
    def test_rows_split_by_shape_not_by_the_header(self):
        rows = parse_index(INDEX)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], {"form": "10-K", "company": "Aytu Biopharma, Inc.",
                                   "cik": "1385818", "filed": "20260922",
                                   "url": "https://www.sec.gov/Archives/edgar/data/1385818/"
                                          "0001437749-26-030908.txt"})
        self.assertEqual(rows[1]["company"], "A.K.A. BRANDS HOLDING CORP.")
        self.assertEqual(rows[1]["cik"], "1001253")

    def test_a_line_that_is_not_a_row_is_dropped_not_guessed(self):
        self.assertEqual([row["form"] for row in parse_index(INDEX)], ["10-K", "8-K", "10-K"])

    def test_a_file_with_no_rows_is_empty_not_wrong(self):
        self.assertEqual(parse_index("no index here\n"), [])

    def test_the_eight_digit_date_is_what_anchors_a_row(self):
        rows = parse_index(INDEX)
        self.assertTrue(all(row["filed"].isdigit() and len(row["filed"]) == 8 for row in rows))


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.data = CompanyData(fetch=FakeEdgar())

    def test_a_name_resolves(self):
        row = self.data.resolve("what has Apple filed recently?")
        self.assertEqual((row["title"], row["ticker"], row["cik"]), ("Apple Inc.", "AAPL", 320193))
        self.assertIn("the name", row["how"])

    def test_a_ticker_resolves(self):
        row = self.data.resolve("AAPL filings")
        self.assertEqual(row["cik"], 320193)
        self.assertIn("the ticker", row["how"])

    def test_a_cik_resolves_without_the_ticker_file(self):
        row = self.data.resolve("which company is CIK 320193?")
        self.assertEqual(row["title"], "Apple Inc.")
        self.assertEqual(row["how"], "the CIK in the question")

    def test_a_common_word_is_never_read_as_a_ticker(self):
        # SEC really lists a company whose ticker is CIK; the question is about Tesla.
        row = self.data.resolve("what is Tesla's CIK?")
        self.assertEqual(row["title"], "Tesla, Inc.")

    def test_an_ordinary_word_is_never_read_as_a_company(self):
        self.assertIn("many", COMMON_WORDS)
        with self.assertRaises(CompanyError) as caught:
            self.data.resolve("how many filings did EDGAR receive yesterday?")
        self.assertIn("could not find a company", str(caught.exception))

    def test_an_unknown_company_says_what_it_needs(self):
        with self.assertRaises(CompanyError) as caught:
            self.data.resolve("zzzzqqq filings")
        self.assertIn("Name it as a ticker", str(caught.exception))

    def test_the_ticker_file_is_fetched_once(self):
        feed = FakeEdgar()
        data = CompanyData(fetch=feed)
        data.resolve("Apple")
        data.resolve("Microsoft")
        self.assertEqual(len([call for call in feed.calls if "company_tickers" in call]), 1)


class CompanyFilingsTests(unittest.TestCase):
    def setUp(self):
        self.feed = FakeEdgar()
        self.data = CompanyData(fetch=self.feed)

    def test_filings_of_one_form_with_the_depth_of_the_list(self):
        reading = self.data.filings(320193, form="10-K", limit=5)
        self.assertEqual(reading["name"], "Apple Inc.")
        self.assertEqual(reading["tickers"], ["AAPL"])
        self.assertEqual(reading["sic"], "Electronic Computers")
        self.assertEqual(reading["matched"], 1)
        self.assertEqual(reading["seen"], 3)
        self.assertEqual(reading["oldest_seen"], "2025-11-02")
        self.assertEqual(reading["filings"][0]["form"], "10-K")
        self.assertEqual(reading["filings"][0]["period"], "2025-09-27")
        self.assertEqual(reading["filings"][0]["url"],
                         "https://www.sec.gov/Archives/edgar/data/320193/"
                         "000032019325000079/aapl-20250927.htm")

    def test_every_filing_when_no_form_is_asked(self):
        reading = self.data.filings(320193, limit=2)
        self.assertEqual(reading["matched"], 3)
        self.assertEqual([row["form"] for row in reading["filings"]], ["10-K", "8-K"])

    def test_latest_filings_feed(self):
        reading = self.data.latest(form="10-K", count=2)
        self.assertEqual(reading["form"], "10-K")
        self.assertEqual(len(reading["entries"]), 2)
        self.assertEqual(reading["entries"][0]["filed"], "2026-09-18")
        self.assertIn("Neuphoria", reading["entries"][0]["title"])
        self.assertIn("Filed: 2026-09-18", reading["entries"][0]["detail"])

    def test_latest_needs_a_form_because_the_feed_does(self):
        with self.assertRaises(CompanyError):
            self.data.latest(form=None)

    def test_a_403_is_reported_as_a_403_not_as_an_empty_answer(self):
        data = CompanyData(fetch=FakeEdgar(refuse="company_tickers"))
        with self.assertRaises(CompanyError) as caught:
            data.resolve("Apple")
        self.assertIn("403", str(caught.exception))

    def test_a_403_from_sec_says_what_to_do_about_it(self):
        # The wording that reaches a person, tested on the transport itself rather than a fake.
        said = CompanyData._say(403, "https://www.sec.gov/files/company_tickers.json")
        self.assertIn("403", said)
        self.assertIn("SEC_USER_AGENT", said)
        self.assertIn("429", CompanyData._say(429, "https://data.sec.gov/x"))
        self.assertIn("404", CompanyData._say(404, "https://data.sec.gov/x"))


class DailyTests(unittest.TestCase):
    def test_a_published_day(self):
        data = CompanyData(fetch=FakeEdgar())
        reading = data.daily(YESTERDAY)
        self.assertTrue(reading["posted"])
        self.assertEqual(len(reading["rows"]), 3)
        self.assertIn(YESTERDAY_STAMP, reading["url"])

    def test_todays_index_that_does_not_exist_is_not_an_error(self):
        feed = FakeEdgar()
        data = CompanyData(fetch=feed)
        reading = data.daily()
        self.assertFalse(reading["posted"])
        self.assertEqual(reading["rows"], [])
        self.assertTrue(any(DIRECTORY.split("{")[0] in call for call in feed.calls))
        self.assertIn("listing", reading)

    def test_a_refused_caller_is_not_mistaken_for_a_missing_index(self):
        # The listing names the file, so the 403 is about the caller and must be raised.
        feed = FakeEdgar(index=INDEX)
        feed.listing = f"form.{datetime.date.today().strftime('%Y%m%d')}.idx"
        data = CompanyData(fetch=feed)
        with self.assertRaises(CompanyError) as caught:
            data.daily()
        self.assertIn("refusal is about the caller", str(caught.exception))

    def test_an_index_that_parses_to_nothing_is_an_error_not_an_empty_day(self):
        data = CompanyData(fetch=FakeEdgar(index="nothing here\n"))
        with self.assertRaises(CompanyError) as caught:
            data.daily(YESTERDAY)
        self.assertIn("columns may have changed", str(caught.exception))


class RouteTests(unittest.TestCase):
    def test_a_company_and_a_form_is_a_filings_question(self):
        self.assertEqual(route("Microsoft's last 10-K"),
                         ("company-filings", {"query": "Microsoft's last 10-K", "form": "10-K"}))
        self.assertEqual(route("what has Apple filed recently?")[0], "company-filings")

    def test_a_day_of_filings_is_not_one_companys_list(self):
        skill, params = route("who filed a 10-K yesterday?")
        self.assertEqual(skill, "company-today")
        self.assertEqual(params["form"], "10-K")
        self.assertEqual(params["day"], 1)
        self.assertNotIn("query", params)

    def test_a_count_of_the_days_filings_is_about_a_day(self):
        skill, params = route("how many filings did EDGAR receive yesterday?")
        self.assertEqual(skill, "company-today")
        self.assertEqual(params, {"day": 1})

    def test_a_lookup_is_a_lookup(self):
        self.assertEqual(route("which company is CIK 320193?")[0], "company-lookup")
        self.assertEqual(route("what is Tesla's CIK?")[0], "company-lookup")

    def test_a_day_with_a_company_named_filters_the_day(self):
        skill, params = route("did Apple file anything today?")
        self.assertEqual(skill, "company-today")
        self.assertIn("query", params)
        self.assertNotIn("day", params)

    def test_nothing_to_look_up_gets_help(self):
        self.assertEqual(route("")[0], "company-help")
        self.assertEqual(route("hello there")[0], "company-help")
        self.assertEqual(route("what can you do?")[0], "company-help")

    def test_a_company_hint_is_the_slice_that_could_name_one(self):
        self.assertEqual(company_hint("Apple filings"), "Apple filings")
        self.assertIsNone(company_hint("who filed today?"))
        self.assertIsNone(company_hint("how many filings did EDGAR receive yesterday?"))
        self.assertIsNone(company_hint("what can you do?"))


class RenderTests(unittest.TestCase):
    def test_filings_lead_with_the_form_and_the_depth(self):
        data = CompanyData(fetch=FakeEdgar())
        body = render_filings(data.filings(320193, form="10-K"))
        self.assertIn("Apple Inc. (AAPL) - Electronic Computers", body)
        self.assertIn("1 filing(s) of form 10-K among the last 3 EDGAR lists", body)
        self.assertIn("back to 2025-11-02", body)
        self.assertIn("2025-10-31  10-K        period 2025-09-27", body)

    def test_a_form_with_nothing_in_the_window_says_so(self):
        data = CompanyData(fetch=FakeEdgar())
        body = render_filings(data.filings(320193, form="S-1"))
        self.assertIn("nothing of that form is in those 3 filings", body)

    def test_a_days_totals_are_the_whole_day_not_the_filtered_rows(self):
        data = CompanyData(fetch=FakeEdgar())
        reading = data.daily(YESTERDAY)
        matching = [row for row in reading["rows"] if row["form"] == "10-K"]
        body = render_today(reading, "10-K", matching)
        self.assertIn("daily index for " + YESTERDAY.isoformat() + ": 3 filing(s) from 3 filer(s)",
                      body)
        self.assertIn("of those, 2 match form 10-K", body)
        self.assertIn("Aytu Biopharma, Inc. (CIK 1385818) filed 10-K on 20260922", body)
        self.assertIn("forms received: 10-K 2, 8-K 1", body)

    def test_a_days_answer_without_a_filter_has_no_matching_section(self):
        data = CompanyData(fetch=FakeEdgar())
        body = render_today(data.daily(YESTERDAY), None, None)
        self.assertNotIn("of those", body)
        self.assertIn("the whole index: https://www.sec.gov/Archives/edgar/daily-index/", body)

    def test_the_shift_note_is_printed_when_it_exists(self):
        data = CompanyData(fetch=FakeEdgar())
        reading = data.daily(YESTERDAY)
        reading["shifted"] = "EDGAR has not published an index for today"
        body = render_today(reading, None, None)
        self.assertIn("  - note: EDGAR has not published an index for today", body)

    def test_human_date_keeps_what_edgar_wrote(self):
        self.assertEqual(human_date("2025-10-31"), "2025-10-31")
        self.assertEqual(human_date(""), "-")

    def test_the_help_text_states_the_limits(self):
        self.assertIn("latest ~1,000 filings", HELP)
        self.assertIn("Microsoft's last 10-K", HELP)


class QueueReader:
    def __init__(self):
        self._items: queue.Queue = queue.Queue()

    def push(self, text):
        self._items.put(text)

    def close(self):
        self._items.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        item = self._items.get()
        if item is None:
            raise StopIteration
        return item


class WiredWriter:
    def __init__(self, reader):
        self.reader = reader

    def write(self, text):
        self.reader.push(text)

    def flush(self):
        pass

    def close(self):
        self.reader.close()


def connected_pair():
    to_agent, to_client = QueueReader(), QueueReader()
    return (
        Connection(to_agent, WiredWriter(to_client), name="agent"),
        Connection(to_client, WiredWriter(to_agent), name="client"),
    )


class TurnTests(unittest.TestCase):
    def turn(self, text, permission="allow-once", feed=None):
        from agent import CompanyAgent

        agent_conn, client_conn = connected_pair()
        agent = CompanyAgent(agent_conn, CompanyData(fetch=feed or FakeEdgar()))
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_a_company_filings_turn(self):
        result, client = self.turn("Microsoft's last 10-K")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertEqual(len(client.permission_requests), 1)
        self.assertIn("MICROSOFT CORP (MSFT)", result["text"])
        self.assertIn("nothing here is estimated", result["text"])

    def test_a_lookup_turn_reads_the_company_record_for_its_industry(self):
        result, _ = self.turn("which company is CIK 320193?")
        self.assertIn("Apple Inc.", result["text"])
        self.assertIn("SIC 3571: Electronic Computers", result["text"])
        self.assertIn("exchanges: Nasdaq", result["text"])

    def test_a_day_turn_says_which_day_it_used_and_why(self):
        result, _ = self.turn("who filed a 10-K yesterday?")
        self.assertIn(f"daily index for {YESTERDAY.isoformat()}", result["text"])
        self.assertIn("of those, 2 match form 10-K", result["text"])
        self.assertIn("Aytu Biopharma", result["text"])
        self.assertNotIn("note: EDGAR has not published", result["text"])

    def test_today_falls_back_to_the_most_recent_day_that_exists(self):
        result, _ = self.turn("who filed today?")
        self.assertIn(f"daily index for {YESTERDAY.isoformat()}", result["text"])
        self.assertIn("SEC posts the daily index after the close", result["text"])

    def test_a_day_with_no_such_form_offers_the_latest_feed_instead(self):
        result, _ = self.turn("who filed an S-1 yesterday?")
        self.assertIn("No a S-1 was filed on " + YESTERDAY.isoformat(), result["text"])
        self.assertIn("The most recent S-1 filings EDGAR lists right now", result["text"])
        self.assertIn("Neuphoria", result["text"])

    def test_a_company_that_filed_nothing_that_day_is_not_offered_a_form_feed(self):
        result, _ = self.turn("did Apple file a 10-K today?")
        self.assertIn(f"No Apple Inc. and a 10-K appears in EDGAR's index for "
                      f"{YESTERDAY.isoformat()}", result["text"])
        self.assertNotIn("most recent", result["text"])

    def test_permission_denied_stops_with_a_refusal(self):
        result, client = self.turn("Microsoft's last 10-K", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("I need permission", result["text"])

    def test_an_unreadable_edgar_is_reported_not_guessed(self):
        result, _ = self.turn("what is Apple's CIK?",
                              feed=FakeEdgar(refuse="company_tickers"))
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("I could not read that", result["text"])
        self.assertIn("403", result["text"])

    def test_help_asks_for_no_permission(self):
        result, client = self.turn("what can you do?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertEqual(client.permission_requests, [])
        self.assertIn("I read SEC EDGAR", result["text"])

    def test_the_tool_call_names_the_skill_and_completes(self):
        result, client = self.turn("what has Apple filed recently?")
        titles = [call["title"] for call in client.tool_calls()]
        self.assertEqual(titles, ["Read SEC EDGAR for filings"])
        statuses = [update.get("status") for update in client.updates
                    if update.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("completed", statuses)
        self.assertNotIn("failed", statuses)


if __name__ == "__main__":
    unittest.main(verbosity=2)
