"""Tests for the stocks agent: symbol lookup, quotes, history, routing and turns.

    python3 agents/stocks/tests/test_agent.py

No network: the search and chart payloads are injected, shaped like the live ones read on
2026-09-23 (AAPL 339.75, up 0.23%, 52 week range 243.42 - 345.34).
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

from agent import instrument_from_text, render_history, render_quote, route  # noqa: E402
from data import StockData, StockError, money, when  # noqa: E402

SEARCH = {"quotes": [
    {"symbol": "AAPL", "longname": "Apple Inc.", "shortname": "Apple Inc.",
     "quoteType": "EQUITY", "exchDisp": "NASDAQ", "exchange": "NMS",
     "sector": "Technology", "industry": "Consumer Electronics"},
    {"symbol": "SAAPL=F", "shortname": "Apple Inc Stock Futures", "quoteType": "FUTURE"},
]}

QUOTE = {"chart": {"result": [{"meta": {
    "currency": "USD", "symbol": "AAPL", "regularMarketPrice": 339.75,
    "regularMarketChangePercent": 0.227, "chartPreviousClose": 331.34,
    "regularMarketDayHigh": 345.34, "regularMarketDayLow": 338.75,
    "fiftyTwoWeekHigh": 345.34, "fiftyTwoWeekLow": 243.42,
    "regularMarketVolume": 40599377, "fullExchangeName": "NasdaqGS",
    "exchangeTimezoneName": "America/New_York", "regularMarketTime": 1790107201,
}}]}}

HISTORY = {"chart": {"result": [{
    "meta": {"currency": "USD", "symbol": "AAPL"},
    "timestamp": [1790000000, 1790050000, 1790100000],
    "indicators": {"quote": [{"close": [300.0, 320.0, 330.0]}]},
}]}}


class FakeFeeds:
    def __init__(self, search=None, quote=None, history=None, fail=None):
        self.calls: list[tuple[str, dict]] = []
        self.search = SEARCH if search is None else search
        self.quote = QUOTE if quote is None else quote
        self.history = HISTORY if history is None else history
        self.fail = fail

    def __call__(self, url: str, params: dict):
        self.calls.append((url, dict(params)))
        if self.fail and self.fail in url:
            raise StockError("the quote endpoint refused the request")
        if "/finance/search" in url:
            return json.dumps(self.search)
        if "/chart/" in url:
            return json.dumps(self.history if params.get("interval") == "1wk"
                              or ("range" in params and params["range"] != "1d")
                              else self.quote)
        raise AssertionError(f"unexpected url {url}")


def make_data(**kw):
    feed = FakeFeeds(**kw)
    return StockData(fetch=feed), feed


class FormatTests(unittest.TestCase):
    def test_money_and_times(self):
        self.assertEqual(money(339.75, "USD"), "339.75 USD")
        self.assertEqual(money(40599377), "40.60 million")
        self.assertEqual(money(1740157806607, "USD"), "1.74 trillion USD")
        self.assertEqual(money(None), "n/a")
        self.assertEqual(when(1790107201), "2026-09-22 20:00 UTC")
        self.assertEqual(when(None), "")


class ReadTests(unittest.TestCase):
    def test_a_company_name_becomes_the_share_not_the_future(self):
        data, _feed = make_data()
        found = data.find("Apple")
        self.assertEqual(found["symbol"], "AAPL")
        self.assertEqual(found["kind"], "share")
        self.assertEqual(found["exchange"], "NASDAQ")
        self.assertEqual(found["sector"], "Technology")

    def test_only_futures_is_refused_with_the_reason(self):
        data, _feed = make_data(search={"quotes": [
            {"symbol": "SAAPL=F", "quoteType": "FUTURE", "shortname": "Apple futures"}]})
        with self.assertRaises(ValueError) as ctx:
            data.find("apple")
        self.assertIn("not a listed share", str(ctx.exception))

    def test_nothing_found_is_an_error(self):
        data, _feed = make_data(search={"quotes": []})
        with self.assertRaises(ValueError):
            data.find("zzzzzznothing")
        with self.assertRaises(ValueError):
            data.find("")

    def test_a_quote_carries_the_ranges_and_the_time(self):
        data, _feed = make_data()
        reading = data.quote("AAPL")
        self.assertEqual(reading["price"], 339.75)
        self.assertEqual(reading["day_low"], 338.75)
        self.assertEqual(reading["week52_high"], 345.34)
        self.assertEqual(reading["at"], "2026-09-22 20:00 UTC")
        self.assertEqual(reading["exchange"], "NasdaqGS")
        text = render_quote(reading)
        self.assertIn("Apple Inc. (AAPL, share on NasdaqGS): 339.75 USD", text)
        self.assertIn("up 0.23% on the day, previous close 331.34 USD", text)
        self.assertIn("52 week range 243.42 USD - 345.34 USD", text)
        self.assertIn("Technology / Consumer Electronics", text)

    def test_a_refusal_from_the_service_is_an_error_not_a_blank_price(self):
        data, _feed = make_data(quote={"chart": {"error": {"description": "Not Found"}}})
        with self.assertRaises(StockError):
            data.quote("AAPL")

    def test_history_summarises_the_window(self):
        data, _feed = make_data()
        reading = data.history("AAPL", days=30)
        self.assertEqual(reading["points"], 3)
        self.assertEqual(reading["first"], 300.0)
        self.assertEqual(reading["last"], 330.0)
        self.assertAlmostEqual(reading["change_percent"], 10.0)
        self.assertEqual(reading["high"], 330.0)
        text = render_history(reading)
        self.assertIn("over the last 1mo", text)
        self.assertIn("300.00 USD -> 330.00 USD  up 10.00%", text)

    def test_a_thin_history_is_an_error(self):
        data, _feed = make_data(history={"chart": {"result": [
            {"meta": {}, "timestamp": [1], "indicators": {"quote": [{"close": [5.0]}]}}]}})
        with self.assertRaises(StockError):
            data.history("AAPL")

    def test_a_dead_endpoint(self):
        data, _feed = make_data(fail="/chart/")
        with self.assertRaises(StockError):
            data.quote("AAPL")


class RouteTests(unittest.TestCase):
    def test_a_company_question(self):
        skill, params = route("what is Apple trading at?")
        self.assertEqual(skill, "stock-quote")
        self.assertEqual(params["instrument"], "Apple")

    def test_a_ticker(self):
        self.assertEqual(route("price of MSFT")[1]["instrument"], "MSFT")

    def test_a_move_is_history(self):
        skill, params = route("how has Tesla moved this month?")
        self.assertEqual(skill, "stock-history")
        self.assertEqual(params["days"], 30)
        self.assertEqual(route("how has Tesla moved this week?")[1]["days"], 5)
        self.assertEqual(route("how has Tesla moved over the last 200 days?")[1]["days"], 200)

    def test_the_52_week_range_is_a_quote(self):
        self.assertEqual(route("what is NVDA's 52 week range?")[0], "stock-quote")

    def test_no_market_word_is_help(self):
        self.assertEqual(route("what is the national debt?")[0], "help")
        self.assertEqual(route("book a flight")[0], "help")
        self.assertEqual(route("")[0], "help")

    def test_helpers(self):
        self.assertEqual(instrument_from_text("price of MSFT"), "MSFT")
        self.assertEqual(instrument_from_text("what is Apple trading at?"), "Apple")
        self.assertIsNone(instrument_from_text("price of"))


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
        from agent import StockAgent

        agent_conn, client_conn = connected_pair()
        data, feed = make_data(**kw)
        agent = StockAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client, feed
        finally:
            client.stop()

    def test_a_quote_answer_says_the_endpoint_is_unofficial(self):
        result, client, _ = self.turn("what is Apple trading at?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertEqual(len(client.permission_requests), 1)
        self.assertIn("339.75 USD", result["text"])
        self.assertIn("carries no guarantee", result["text"])

    def test_a_move_answer(self):
        result, _, _ = self.turn("how has AAPL moved this month?")
        self.assertIn("over the last 1mo", result["text"])
        self.assertIn("up 10.00%", result["text"])

    def test_an_unknown_symbol_is_reported(self):
        result, _, _ = self.turn("price of zzzzznothing", search={"quotes": []})
        self.assertIn("no listed instrument", result["text"])

    def test_help_asks_nothing(self):
        result, client, feed = self.turn("what can you do?")
        self.assertIn("Yahoo Finance", result["text"])
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(feed.calls, [])

    def test_permission_denied(self):
        result, _, _ = self.turn("price of AAPL", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)

    def test_a_dead_endpoint_is_reported(self):
        result, _, _ = self.turn("price of AAPL", fail="/chart/")
        self.assertIn("I could not read that quote", result["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
