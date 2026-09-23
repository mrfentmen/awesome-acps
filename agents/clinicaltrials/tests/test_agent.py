"""Tests for the clinicaltrials agent: the record, the sites, the routing and the turns.

    python3 agents/clinicaltrials/tests/test_agent.py

No network: the registry's answer is injected, shaped like the live payload read on 2026-09-23
(NCT01065467, Panobinostat in metastatic melanoma). Two traps are pinned on purpose:

  * a place filter matches a **study** that lists a site there, not the site, so a study returned
    for Boston can hold sites in Arizona - the answer names the sites that match and the total;
  * the API answers a bad NCT number with a **404 and a plain-text body**, not JSON.
"""

from __future__ import annotations

import io
import json
import queue
import sys
import threading
import unittest
import urllib.error
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for path in (str(REPO_ROOT), str(AGENT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from acp_kit import AcpClient, Connection, STOP_END_TURN, STOP_REFUSAL  # noqa: E402

from agent import (  # noqa: E402
    HELP,
    condition_from_text,
    nct_from_text,
    place_from_text,
    render_study,
    route,
    status_from_text,
)
from data import (  # noqa: E402
    MAX_STUDIES,
    STATUSES,
    TrialsData,
    TrialsError,
    date_text,
    enrollment_text,
    matching_sites,
    parse_study,
    phase_text,
    place_words,
    site_line,
    status_text,
)

STUDY = {
    "hasResults": False,
    "protocolSection": {
        "identificationModule": {
            "nctId": "NCT01065467",
            "briefTitle": "Panobinostat (LBH589) in Patients With Metastatic Melanoma",
            "officialTitle": "A Pilot/Phase I Study of Panobinostat (LBH589) in Patients With "
                            "Metastatic Melanoma",
        },
        "statusModule": {
            "overallStatus": "COMPLETED",
            "startDateStruct": {"date": "2010-02", "type": "ACTUAL"},
            "completionDateStruct": {"date": "2017-03-13", "type": "ACTUAL"},
            "lastUpdatePostDateStruct": {"date": "2017-03-28", "type": "ACTUAL"},
        },
        "sponsorCollaboratorsModule": {
            "leadSponsor": {"name": "Dana-Farber Cancer Institute"},
            "collaborators": [{"name": "Brigham and Women's Hospital"}, {"name": "Novartis"}],
        },
        "descriptionModule": {"briefSummary": "The purpose of this research study  is to "
                                             "determine the safety of LBH589."},
        "conditionsModule": {"conditions": ["Melanoma", "Malignant Melanoma"]},
        "designModule": {"phases": ["PHASE1"], "studyType": "INTERVENTIONAL",
                         "enrollmentInfo": {"count": 16, "type": "ACTUAL"}},
        "armsInterventionsModule": {"interventions": [
            {"type": "DRUG", "name": "LBH589"}, {"type": "DRUG", "name": "Panobinostat"},
            {"type": "PROCEDURE", "name": "Biopsy"}, {"type": "DRUG", "name": "Placebo"},
            {"type": "OTHER", "name": "Questionnaire"}]},
        "eligibilityModule": {"sex": "ALL", "minimumAge": "18 Years"},
        "contactsLocationsModule": {"locations": [
            {"facility": "Dana-Farber Cancer Institute", "city": "Boston", "state": "Massachusetts",
             "country": "United States"},
            {"facility": "Mayo Clinic Hospital in Arizona", "city": "Phoenix", "state": "Arizona",
             "country": "United States"},
            {"facility": "University of Arkansas", "city": "Little Rock", "state": "Arkansas",
             "country": "United States"},
        ]},
    },
}


def page(studies, total=None, token=None) -> str:
    return json.dumps({"totalCount": len(studies) if total is None else total,
                       "studies": studies, "nextPageToken": token})


class FakeRegistry:
    def __init__(self, body=None, study_body=None, error=None) -> None:
        self.calls: list[str] = []
        self.body = body
        self.study_body = study_body
        self.error = error

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        if self.error:
            # The transport's contract: an HTTPError becomes a TrialsError carrying the same
            # words the real transport would build, so _say is exercised rather than faked.
            raise TrialsError(TrialsData._say(self.error))
        if "/studies/" in url:
            return self.study_body if self.study_body is not None else json.dumps(STUDY)
        if self.body is not None:
            return self.body
        return page([STUDY], total=396)


class TextTests(unittest.TestCase):
    def test_phases_read_as_phases(self):
        self.assertEqual(phase_text(["PHASE1"]), "phase 1")
        self.assertEqual(phase_text(["PHASE2", "PHASE3"]), "phase 2, phase 3")
        self.assertIn("not applicable", phase_text(["NA"]))
        self.assertEqual(phase_text([]), "phase not stated")

    def test_status_reads_as_words(self):
        self.assertEqual(status_text("RECRUITING"), "recruiting")
        self.assertEqual(status_text("ACTIVE_NOT_RECRUITING"), "active not recruiting")
        self.assertEqual(status_text(""), "status not stated")

    def test_enrollment_and_dates_keep_their_kind(self):
        self.assertEqual(enrollment_text({"count": 150, "type": "ESTIMATED"}), "150 (estimated)")
        self.assertEqual(enrollment_text({}), "not stated")
        self.assertEqual(date_text({"date": "2022-12-06", "type": "ACTUAL"}),
                         "2022-12-06 (actual)")
        self.assertEqual(date_text({}), "not stated")

    def test_a_place_becomes_the_words_a_site_line_could_hold(self):
        self.assertEqual(place_words("Boston, MA"), ["boston", "ma"])
        self.assertEqual(place_words("the UK"), ["the", "uk"])

    def test_a_site_line_drops_the_empty_parts(self):
        self.assertEqual(site_line({"facility": "X", "city": "", "state": "AZ",
                                    "country": "United States"}), "X, AZ, United States")


class RecordTests(unittest.TestCase):
    def test_a_study_parses_into_the_fields_a_person_asks_about(self):
        study = parse_study(STUDY)
        self.assertEqual(study["nct"], "NCT01065467")
        self.assertEqual(study["status"], "COMPLETED")
        self.assertEqual(study["phases"], ["PHASE1"])
        self.assertEqual(study["study_type"], "INTERVENTIONAL")
        self.assertEqual(study["sponsor"], "Dana-Farber Cancer Institute")
        self.assertEqual(len(study["collaborators"]), 2)
        self.assertEqual(study["conditions"], ["Melanoma", "Malignant Melanoma"])
        self.assertEqual(study["location_count"], 3)
        self.assertEqual(study["location_count"], len(study["locations"]))
        self.assertEqual(study["url"], "https://clinicaltrials.gov/study/NCT01065467")
        self.assertEqual(study["summary"], "The purpose of this research study is to determine the "
                                           "safety of LBH589.")

    def test_interventions_are_capped_and_the_rest_counted(self):
        study = parse_study(STUDY)
        self.assertEqual(len(study["interventions"]), 4)
        self.assertEqual(study["intervention_count"], 5)

    def test_a_record_without_a_protocol_is_an_error(self):
        with self.assertRaises(TrialsError):
            parse_study({"hasResults": False})

    def test_matching_sites_finds_the_place_and_nothing_else(self):
        study = parse_study(STUDY)
        boston = matching_sites(study, "Boston")
        self.assertEqual([site["city"] for site in boston], ["Boston"])
        self.assertEqual([site["city"] for site in matching_sites(study, "Massachusetts")],
                         ["Boston"])
        self.assertEqual([site["city"] for site in matching_sites(study, "Arizona")], ["Phoenix"])
        self.assertEqual(matching_sites(study, "Vermont"), [])
        self.assertEqual(matching_sites(study, ""), [])


class SearchTests(unittest.TestCase):
    def test_a_search_url_carries_its_own_question(self):
        url = TrialsData(fetch=FakeRegistry()).search_url("melanoma", "Boston", "RECRUITING")
        self.assertIn("query.cond=melanoma", url)
        self.assertIn("query.locn=Boston", url)
        self.assertIn("filter.overallStatus=RECRUITING", url)
        self.assertIn("countTotal=true", url)
        self.assertIn("pageSize=10", url)

    def test_the_page_size_is_capped(self):
        url = TrialsData(fetch=FakeRegistry()).search_url("x", size=99999)
        self.assertIn("pageSize=200", url)

    def test_a_search_reports_the_total_and_the_studies(self):
        feed = FakeRegistry()
        reading = TrialsData(fetch=feed).search(condition="melanoma", place="Boston")
        self.assertEqual(reading["total"], 396)
        self.assertEqual(reading["returned"], 1)
        self.assertEqual(reading["studies"][0]["nct"], "NCT01065467")
        self.assertIn("query.cond=melanoma", feed.calls[0])

    def test_a_search_with_no_subject_is_an_error(self):
        with self.assertRaises(TrialsError) as caught:
            TrialsData(fetch=FakeRegistry()).search()
        self.assertIn("give me a condition", str(caught.exception))

    def test_no_matches_is_an_answer_not_an_error(self):
        reading = TrialsData(fetch=FakeRegistry(body=page([], total=0))).search(condition="zzz")
        self.assertEqual(reading["total"], 0)
        self.assertEqual(reading["studies"], [])

    def test_a_result_set_that_is_not_a_result_set_is_an_error(self):
        with self.assertRaises(TrialsError) as caught:
            TrialsData(fetch=FakeRegistry(body=json.dumps([1, 2]))).search(condition="x")
        self.assertIn("other than a result set", str(caught.exception))

    def test_a_next_page_token_is_reported(self):
        reading = TrialsData(fetch=FakeRegistry(body=page([STUDY], token="abc"))).search(
            condition="melanoma")
        self.assertEqual(reading["next_page"], "abc")


class StudyTests(unittest.TestCase):
    def test_one_study_by_its_number(self):
        feed = FakeRegistry()
        study = TrialsData(fetch=feed).study("nct01065467")
        self.assertEqual(study["nct"], "NCT01065467")
        self.assertTrue(feed.calls[0].endswith("/NCT01065467"))

    def test_a_number_that_is_not_a_number_is_refused_with_the_shape_needed(self):
        with self.assertRaises(TrialsError) as caught:
            TrialsData(fetch=FakeRegistry()).study("12345")
        self.assertIn("NCT01065467", str(caught.exception))

    def test_a_missing_study_reports_the_registry_404_and_its_words(self):
        error = urllib.error.HTTPError("https://clinicaltrials.gov/api/v2/studies/NCT99999999",
                                      404, "Not Found", {},
                                      io.BytesIO(b"NCT number NCT99999999 not found"))
        with self.assertRaises(TrialsError) as caught:
            TrialsData(fetch=FakeRegistry(error=error)).study("NCT99999999")
        self.assertIn("404", str(caught.exception))
        self.assertIn("NCT99999999 not found", str(caught.exception))

    def test_a_refused_parameter_is_reported_with_the_api_message(self):
        error = urllib.error.HTTPError("https://clinicaltrials.gov/api/v2/studies", 400, "Bad", {},
                                       io.BytesIO(b"Value provided in parameter `pageSize` cannot "
                                                   b"be converted to 32-bit integer"))
        said = TrialsData._say(error)
        self.assertIn("400", said)
        self.assertIn("pageSize", said)

    def test_a_rate_limit_is_reported_as_one(self):
        error = urllib.error.HTTPError("https://clinicaltrials.gov/api/v2/studies", 429, "Slow", {},
                                       io.BytesIO(b"rate limited"))
        said = TrialsData._say(error)
        self.assertIn("429", said)
        self.assertIn("too many requests", said.lower())


class RouteTests(unittest.TestCase):
    def test_a_condition_and_a_place(self):
        self.assertEqual(route("trials for melanoma in Boston"),
                         ("trials-search", {"condition": "melanoma", "place": "Boston"}))
        self.assertEqual(route("studies about long COVID in the UK"),
                         ("trials-search", {"condition": "long COVID", "place": "the UK"}))

    def test_a_status_is_read_as_the_apis_own_token(self):
        skill, params = route("recruiting trials for ALS in California")
        self.assertEqual(skill, "trials-search")
        self.assertEqual(params["status"], "RECRUITING")
        self.assertEqual(params["condition"], "ALS")
        self.assertEqual(status_from_text("completed studies"), "COMPLETED")
        self.assertEqual(status_from_text("active not recruiting"), "ACTIVE_NOT_RECRUITING")
        self.assertIn("RECRUITING", STATUSES.values())

    def test_a_condition_with_no_place(self):
        self.assertEqual(route("trials for sickle cell disease"),
                         ("trials-search", {"condition": "sickle cell disease"}))

    def test_a_number_is_a_study_lookup(self):
        self.assertEqual(route("what is trial NCT01065467?"),
                         ("trials-study", {"nct": "NCT01065467"}))
        self.assertEqual(route("NCT01065467"), ("trials-study", {"nct": "NCT01065467"}))
        self.assertEqual(nct_from_text("trial nct 01065467"), "NCT01065467")

    def test_a_condition_phrase_is_tidied(self):
        self.assertEqual(condition_from_text("trials for melanoma in Boston right now"), "melanoma")
        self.assertEqual(condition_from_text("trials for me"), None)
        self.assertEqual(place_from_text("trials for ALS near me"), None)
        self.assertEqual(place_from_text("trials for ALS in California right now"), "California")

    def test_nothing_to_look_up_gets_help(self):
        self.assertEqual(route("")[0], "trials-help")
        self.assertEqual(route("hello there")[0], "trials-help")
        self.assertEqual(route("what can you do?")[0], "trials-help")
        self.assertIn("not medical advice", HELP)


class RenderTests(unittest.TestCase):
    def study(self):
        return parse_study(STUDY)

    def test_a_study_leads_with_its_number_and_status(self):
        text = "\n".join(render_study(self.study(), "Boston"))
        self.assertIn("NCT01065467  Panobinostat (LBH589)", text)
        self.assertIn("status completed, interventional, phase 1", text)
        self.assertIn("sponsor Dana-Farber Cancer Institute", text)
        self.assertIn("sites matching Boston: 1 of 3", text)
        self.assertIn("Dana-Farber Cancer Institute, Boston, Massachusetts, United States", text)
        self.assertIn("record last updated 2017-03-28", text)
        self.assertIn("https://clinicaltrials.gov/study/NCT01065467", text)

    def test_a_place_that_matches_no_site_says_exactly_that(self):
        text = "\n".join(render_study(self.study(), "Vermont"))
        self.assertIn("none of its 3 listed site(s) mention Vermont", text)
        self.assertIn("matched this study on another field", text)

    def test_no_place_prints_the_site_count_instead(self):
        text = "\n".join(render_study(self.study(), None))
        self.assertIn("sites: 3 listed", text)
        self.assertIn("first is Dana-Farber", text)

    def test_more_sites_than_shown_are_counted(self):
        study = self.study()
        study["locations"] = [dict(site, city="Boston") for site in study["locations"]] * 3
        study["location_count"] = len(study["locations"])
        text = "\n".join(render_study(study, "Boston"))
        self.assertIn("sites matching Boston: 9 of 9", text)
        self.assertIn("and 5 more matching site(s)", text)

    def test_eligibility_is_only_shown_when_it_narrows_something(self):
        self.assertIn("ages 18 Years to any", "\n".join(render_study(self.study(), None)))
        self.assertNotIn("sex all", "\n".join(render_study(self.study(), None)).lower())


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
        from agent import TrialsAgent

        agent_conn, client_conn = connected_pair()
        agent = TrialsAgent(agent_conn, TrialsData(fetch=feed or FakeRegistry()))
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_a_search_turn(self):
        result, client = self.turn("trials for melanoma in Boston")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertEqual(len(client.permission_requests), 1)
        self.assertIn("396 study(ies) match", result["text"])
        self.assertIn("NCT01065467", result["text"])
        self.assertIn("sites matching Boston: 1 of 3", result["text"])
        self.assertIn("none of this is medical advice", result["text"])
        self.assertIn("a status is the sponsor's own word", result["text"])

    def test_a_search_that_matches_nothing_says_so(self):
        result, _ = self.turn("trials for zzzz nowhere",
                              feed=FakeRegistry(body=page([], total=0)))
        self.assertIn("no study in the registry matches", result["text"])

    def test_a_study_turn(self):
        result, _ = self.turn("what is trial NCT01065467?")
        self.assertIn("NCT01065467  Panobinostat", result["text"])
        self.assertIn("official title:", result["text"])
        self.assertIn("summary:", result["text"])

    def test_a_study_that_does_not_exist_is_reported(self):
        error = urllib.error.HTTPError("https://clinicaltrials.gov/api/v2/studies/NCT99999999",
                                       404, "Not Found", {},
                                       io.BytesIO(b"NCT number NCT99999999 not found"))
        result, _ = self.turn("what is trial NCT99999999?", feed=FakeRegistry(error=error))
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("I could not read that", result["text"])
        self.assertIn("404", result["text"])
        self.assertIn("NCT99999999 not found", result["text"])

    def test_permission_denied_stops_with_a_refusal(self):
        result, _ = self.turn("trials for melanoma in Boston", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("I need permission", result["text"])

    def test_help_reads_nothing_and_asks_nothing(self):
        result, client = self.turn("what can you do?")
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(client.tool_calls(), [])
        self.assertIn("I read ClinicalTrials.gov", result["text"])
        self.assertIn("not medical advice", result["text"])

    def test_the_tool_call_completes_with_the_count(self):
        _, client = self.turn("trials for melanoma in Boston")
        done = [update for update in client.updates
                if update.get("sessionUpdate") == "tool_call_update"
                and update.get("status") == "completed"]
        self.assertEqual(len(done), 1)
        self.assertIn("of 396 studies", done[0]["content"][0]["content"]["text"])

    def test_a_long_list_stays_inside_the_cap(self):
        body = page([STUDY] * 40, total=40)
        result, _ = self.turn("trials for melanoma", feed=FakeRegistry(body=body))
        self.assertIn(f"showing {MAX_STUDIES} of them", result["text"])
        self.assertEqual(result["text"].count("https://clinicaltrials.gov/study/"), MAX_STUDIES)
        self.assertIn("30 more row(s) than one answer shows", result["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
