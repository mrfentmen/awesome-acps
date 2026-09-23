"""Tests for the papers agent: PubMed, arXiv and Crossref parsing, routing and turns.

    python3 agents/papers/tests/test_agent.py

No network: all three services are injected, shaped like the live answers verified on
2026-09-23 (PubMed for 'CRISPR sickle cell' returned 492 matches). Two behaviours matter
beyond parsing: a preprint is never presented as a peer-reviewed paper, and a service that
is down is reported rather than silently answered with an empty list.
"""

from __future__ import annotations

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

from agent import _author_line, route, topic_from_text  # noqa: E402
from data import (  # noqa: E402
    DATASET,
    PapersData,
    PapersError,
    doi_from_text,
)

ESEARCH = {"header": {"type": "esearch", "version": "0.3"},
           "esearchresult": {"count": "492", "retmax": "2", "idlist": ["33283989", "37646679"]}}

ESUMMARY = {"result": {
    "uids": ["33283989", "37646679"],
    "33283989": {"uid": "33283989", "title": "CRISPR-Cas9 Gene Editing for Sickle Cell "
                                              "Disease and beta-Thalassemia.",
                 "source": "N Engl J Med", "pubdate": "2021 Jan 21",
                 "authors": [{"name": "Frangoul H"}, {"name": "Altshuler D"}],
                 "articleids": [{"idtype": "pubmed", "value": "33283989"},
                                {"idtype": "doi", "value": "10.1056/NEJMoa2031054"}]},
    "37646679": {"uid": "37646679", "title": "CRISPR-Cas9 Editing of the HBG1 and HBG2 "
                                             "Promoters.",
                 "source": "N Engl J Med", "pubdate": "2023 Aug 31",
                 "authors": [{"name": "Sharma A"}], "articleids": [{"idtype": "pubmed",
                                                                    "value": "37646679"}]},
}}

ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <title type="html">ArXiv Query</title>
  <entry>
    <id>http://arxiv.org/abs/2409.00001v1</id>
    <updated>2026-09-20T10:00:00Z</updated>
    <published>2026-09-20T10:00:00Z</published>
    <title>Folding
    proteins with a small model</title>
    <summary>We fold things.</summary>
    <author><name>Ada Lovelace</name></author>
    <author><name>Alan Turing</name></author>
    <arxiv:primary_category term="q-bio.BM"/>
    <arxiv:doi>10.1234/example</arxiv:doi>
  </entry>
</feed>
"""

CROSSREF_SEARCH = {"message": {"items": [
    {"DOI": "10.1038/nature12373", "title": ["Nanometre-scale thermometry in a living cell"],
     "container-title": ["Nature"], "publisher": "Springer Science and Business Media LLC",
     "issued": {"date-parts": [[2013, 7, 31]]}, "type": "journal-article",
     "author": [{"given": "G.", "family": "Kucsko"}], "is-referenced-by-count": 1821,
     "URL": "https://doi.org/10.1038/nature12373"},
]}}

CROSSREF_ONE = {"message": CROSSREF_SEARCH["message"]["items"][0]}


class FakeServices:
    """Serves the three service shapes and records every (url, params) pair."""

    def __init__(self, fail: str | None = None, arxiv_xml: str | None = None,
                 empty: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail
        self.empty = empty
        self.arxiv_xml = ARXIV_XML if arxiv_xml is None else arxiv_xml

    def __call__(self, url: str, params: dict):
        self.calls.append((url, dict(params)))
        if self.fail and self.fail in url:
            raise PapersError("service is unreachable")
        if "esearch" in url:
            return json.dumps({"esearchresult": {"count": "0", "idlist": []}} if self.empty else ESEARCH)
        if "esummary" in url:
            return json.dumps(ESUMMARY)
        if "arxiv" in url:
            return self.arxiv_xml
        if url.rstrip("/").endswith("works"):
            return json.dumps(CROSSREF_SEARCH)
        if "crossref" in url:
            return json.dumps(CROSSREF_ONE)
        raise AssertionError(f"unexpected url {url}")


def make_data(fail: str | None = None, **kw) -> tuple[PapersData, FakeServices]:
    feed = FakeServices(fail=fail, **kw)
    return PapersData(fetch=feed), feed


class PubmedTests(unittest.TestCase):
    def test_a_search_returns_records_with_ids(self):
        data, feed = make_data()
        papers = data.pubmed("CRISPR sickle cell", limit=2)
        self.assertEqual(len(papers), 2)
        self.assertEqual(papers[0]["pmid"], "33283989")
        self.assertEqual(papers[0]["doi"], "10.1056/NEJMoa2031054")
        self.assertEqual(papers[0]["journal"], "N Engl J Med")
        self.assertEqual(papers[0]["date"], "2021 Jan 21")
        self.assertEqual(papers[0]["authors"], ["Frangoul H", "Altshuler D"])
        self.assertIsNone(papers[1]["doi"])
        self.assertEqual(len(feed.calls), 2)

    def test_pubmed_is_told_who_is_calling(self):
        data, feed = make_data()
        data.pubmed("anything", limit=1)
        for _url, params in feed.calls:
            self.assertIn("tool", params)
            self.assertIn("email", params)

    def test_recent_asks_for_date_order(self):
        data, feed = make_data()
        data.pubmed("anything", limit=1, sort="date")
        self.assertEqual(feed.calls[0][1]["sort"], "pub_date")

    def test_no_matches_is_an_empty_list_not_an_error(self):
        data, _ = make_data()
        data._fetch = lambda url, params: json.dumps({"esearchresult": {"idlist": []}})
        self.assertEqual(data.pubmed("nothing at all", limit=3), [])

    def test_an_empty_query_is_refused(self):
        data, _ = make_data()
        with self.assertRaises(ValueError):
            data.pubmed("   ")


class ArxivTests(unittest.TestCase):
    def test_entries_are_parsed_and_wrapped_titles_are_collapsed(self):
        data, _ = make_data()
        papers = data.arxiv("protein folding", limit=2)
        self.assertEqual(len(papers), 1)
        paper = papers[0]
        self.assertEqual(paper["title"], "Folding proteins with a small model")
        self.assertEqual(paper["arxiv_id"], "2409.00001v1")
        self.assertEqual(paper["date"], "2026-09-20")
        self.assertEqual(paper["category"], "q-bio.BM")
        self.assertEqual(paper["authors"], ["Ada Lovelace", "Alan Turing"])
        self.assertEqual(paper["doi"], "10.1234/example")

    def test_arxiv_is_asked_for_newest_first(self):
        data, feed = make_data()
        data.arxiv("anything", limit=2)
        self.assertEqual(feed.calls[0][1]["sortBy"], "submittedDate")
        self.assertEqual(feed.calls[0][1]["search_query"], "all:anything")

    def test_broken_xml_is_one_error_type(self):
        data, _ = make_data(arxiv_xml="<feed><entry>")
        with self.assertRaises(PapersError):
            data.arxiv("anything")


class CrossrefTests(unittest.TestCase):
    def test_a_doi_lookup_returns_the_work(self):
        data, _ = make_data()
        record = data.doi("10.1038/nature12373")
        self.assertEqual(record["title"], "Nanometre-scale thermometry in a living cell")
        self.assertEqual(record["journal"], "Nature")
        self.assertEqual(record["date"], "2013-7-31")
        self.assertEqual(record["citations"], 1821)
        self.assertEqual(record["authors"], ["G. Kucsko"])

    def test_a_doi_is_found_inside_a_sentence(self):
        self.assertEqual(doi_from_text("what is DOI 10.1038/nature12373? please"),
                         "10.1038/nature12373")
        self.assertIsNone(doi_from_text("no doi here"))

    def test_a_missing_doi_is_none_and_a_dead_service_is_an_error(self):
        data, _ = make_data()
        data._fetch = lambda url, params: json.dumps({"message": "not found"})
        self.assertIsNone(data.doi("10.9999/nope"))
        dead, _ = make_data(fail="crossref")
        with self.assertRaises(PapersError):
            dead.crossref("anything")

    def test_a_search_returns_crossref_items(self):
        data, _ = make_data()
        works = data.crossref("thermometry")
        self.assertEqual(works[0]["doi"], "10.1038/nature12373")


class RouteTests(unittest.TestCase):
    def test_a_plain_search_has_no_source_so_pubmed_is_the_default(self):
        skill, params = route("papers about 17th century poetry")
        self.assertEqual(skill, "papers-search")
        self.assertEqual(params["topic"], "17th century poetry")
        self.assertNotIn("source", params)

    def test_a_biomedical_word_picks_pubmed(self):
        skill, params = route("papers about CRISPR sickle cell")
        self.assertEqual(skill, "papers-search")
        self.assertEqual(params["topic"], "CRISPR sickle cell")
        self.assertEqual(params["source"], "pubmed")

    def test_arxiv_is_chosen_explicitly(self):
        self.assertEqual(route("arxiv papers on protein folding")[1]["source"], "arxiv")
        self.assertEqual(route("preprints about protein folding")[1]["source"], "arxiv")

    def test_a_recent_question_sorts_by_date(self):
        self.assertEqual(route("recent papers about protein folding")[0], "papers-recent")

    def test_a_doi_question_is_a_doi_lookup(self):
        self.assertEqual(route("what is DOI 10.1038/nature12373?"),
                         ("papers-doi", {"doi": "10.1038/nature12373"}))

    def test_a_count_is_read(self):
        self.assertEqual(route("top 3 papers about gut microbiome")[1]["limit"], 3)

    def test_topic_extraction(self):
        self.assertEqual(topic_from_text("papers about gut microbiome"), "gut microbiome")
        self.assertIsNone(topic_from_text("papers"))

    def test_help(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")

    def test_author_line_shortens_long_lists(self):
        self.assertEqual(_author_line(["A", "B"]), "A, B")
        self.assertEqual(_author_line(["A", "B", "C", "D"]), "A, B, C, and 1 more")
        self.assertEqual(_author_line([]), "authors not listed")


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
    def turn(self, text: str, permission: str = "allow-once", **kw):
        from agent import PapersAgent

        agent_conn, client_conn = connected_pair()
        data, feed = make_data(**kw)
        agent = PapersAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client, feed
        finally:
            client.stop()

    def test_a_pubmed_answer_lists_identifiers(self):
        result, client, _ = self.turn("papers about CRISPR sickle cell")  # biomedical -> PubMed
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("2 PubMed papers for 'CRISPR sickle cell', best match first:", result["text"])
        self.assertIn("PMID 33283989 doi:10.1056/NEJMoa2031054", result["text"])
        self.assertIn("https://pubmed.ncbi.nlm.nih.gov/33283989/", result["text"])
        self.assertIn("usually - not always - peer reviewed", result["text"])
        self.assertIn(DATASET, result["text"])
        self.assertEqual(len(client.permission_requests), 1)

    def test_an_arxiv_answer_says_it_is_a_preprint(self):
        result, _, _ = self.turn("arxiv papers on protein folding")
        self.assertIn("1 arXiv preprints for 'protein folding', newest first:", result["text"])
        self.assertIn("arXiv:2409.00001v1", result["text"])
        self.assertIn("have not necessarily been peer reviewed", result["text"])

    def test_a_doi_answer_reports_the_work(self):
        result, _, _ = self.turn("what is DOI 10.1038/nature12373?")
        self.assertIn("Nanometre-scale thermometry in a living cell", result["text"])
        self.assertIn("cited by: 1821 works (Crossref's own count)", result["text"])
        self.assertIn("floor, not the whole picture", result["text"])

    def test_no_results_says_so_without_padding(self):
        result, _, _ = self.turn("papers about xyzzy plugh", empty=True)
        self.assertIn("returned nothing for", result["text"])
        self.assertIn("not that nothing exists", result["text"])

    def test_help_does_not_ask_permission(self):
        result, client, _ = self.turn("what can you do?")
        self.assertIn("PubMed", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _, _ = self.turn("papers about CRISPR", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_a_dead_service_is_reported(self):
        result, _, _ = self.turn("papers about CRISPR", fail="esearch")
        self.assertIn("could not read the literature service", result["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
