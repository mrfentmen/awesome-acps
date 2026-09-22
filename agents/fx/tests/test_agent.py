"""Tests for the fx agent: rate reader, history, routing, permissions.

    python3 agents/fx/tests/test_agent.py
"""

from __future__ import annotations

import queue
import sys
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for path in (str(REPO_ROOT), str(AGENT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from acp_kit import AcpClient, Connection, STOP_END_TURN, STOP_REFUSAL  # noqa: E402

from agent import FxAgent, amount_in, currencies_in, history_window, route  # noqa: E402
from data import FxData, FxError  # noqa: E402

LATEST_PAYLOAD = {"amount": 1.0, "base": "USD", "date": "2026-09-22",
                  "rates": {"EUR": 0.87237, "GBP": 0.74832, "JPY": 157.18}}

HISTORY_PAYLOAD = {
    "amount": 1.0, "base": "USD", "start_date": "2026-09-01", "end_date": "2026-09-04",
    "rates": {
        "2026-09-01": {"EUR": 0.86281},
        "2026-09-02": {"EUR": 0.86371},
        "2026-09-03": {"EUR": 0.86540},
        "2026-09-04": {"EUR": 0.87012},
    },
}

CURRENCIES_PAYLOAD = {"AUD": "Australian Dollar", "BRL": "Brazilian Real", "CAD": "Canadian Dollar",
                      "CHF": "Swiss Franc", "EUR": "Euro", "GBP": "Pound Sterling", "JPY": "Japanese Yen",
                      "USD": "United States Dollar"}


class FakeData(FxData):
    """Same interface as the real reader, no network."""

    def __init__(self, raise_error: bool = False):
        self.raise_error = raise_error
        self.calls: list[dict] = []

    def latest(self, base: str = "EUR", symbols=None) -> dict:
        if self.raise_error:
            raise FxError("Frankfurter is offline")
        wanted = FxData.check_symbols(symbols)
        self.calls.append({"kind": "latest", "base": base, "symbols": wanted})
        rates = dict(LATEST_PAYLOAD["rates"])
        rates.pop(FxData.check_currency(base), None)
        if wanted:
            rates = {code: rates[code] for code in wanted if code in rates}
        return {"dataset": "api.frankfurter.dev/v1/latest", "date": LATEST_PAYLOAD["date"],
                "base": FxData.check_currency(base), "rates": rates}

    def convert(self, amount: float, base: str, quote: str) -> dict:
        if self.raise_error:
            raise FxError("Frankfurter is offline")
        self.calls.append({"kind": "convert", "amount": amount, "base": base, "quote": quote})
        from_base = FxData.check_currency(base)
        to_quote = FxData.check_currency(quote)
        if from_base == to_quote:
            return {"dataset": "api.frankfurter.dev/v1/latest", "date": None, "base": from_base,
                    "quote": to_quote, "rate": 1.0, "amount": float(amount), "converted": float(amount)}
        rate = LATEST_PAYLOAD["rates"][to_quote]
        return {"dataset": "api.frankfurter.dev/v1/latest", "date": LATEST_PAYLOAD["date"],
                "base": from_base, "quote": to_quote, "rate": rate, "amount": float(amount),
                "converted": round(float(amount) * rate, 6)}

    def history(self, start: str, end: str, base: str = "EUR", symbols=None) -> dict:
        if self.raise_error:
            raise FxError("Frankfurter is offline")
        wanted = FxData.check_symbols(symbols)
        self.calls.append({"kind": "history", "start": start, "end": end, "base": base, "symbols": wanted})
        FxData.check_range(start, end)
        days = sorted(HISTORY_PAYLOAD["rates"])
        codes = wanted or ["EUR"]
        series = {code: [{"date": day, "rate": HISTORY_PAYLOAD["rates"][day][code]} for day in days]
                  for code in codes}
        stats = {}
        for code, points in series.items():
            values = [point["rate"] for point in points]
            change = round(values[-1] - values[0], 6)
            stats[code] = {"first": points[0], "last": points[-1], "change": change,
                           "change_percent": round(change / values[0] * 100, 4),
                           "min": min(values), "max": max(values)}
        return {"dataset": f"api.frankfurter.dev/v1/{start}..{end}", "base": FxData.check_currency(base),
                "start_date": start, "end_date": end, "working_days": len(days),
                "series": series, "stats": stats}

    def currencies(self) -> dict:
        if self.raise_error:
            raise FxError("Frankfurter is offline")
        self.calls.append({"kind": "currencies"})
        return {"dataset": "api.frankfurter.dev/v1/currencies",
                "currencies": {code: CURRENCIES_PAYLOAD[code] for code in sorted(CURRENCIES_PAYLOAD)}}


class QueueReader:
    """One side's inbox: an iterator of lines, plus push to add one."""

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


class ReaderTests(unittest.TestCase):
    def test_currency_codes_are_validated(self):
        self.assertEqual(FxData.check_currency("usd"), "USD")
        with self.assertRaises(ValueError):
            FxData.check_currency("dollars")
        with self.assertRaises(ValueError):
            FxData.check_currency("USDX")

    def test_symbols_amounts_and_dates_are_validated(self):
        self.assertEqual(FxData.check_symbols("eur, gbp"), ["EUR", "GBP"])
        self.assertIsNone(FxData.check_symbols(None))
        with self.assertRaises(ValueError):
            FxData.check_symbols("euro")
        self.assertEqual(FxData.check_amount("100"), 100.0)
        with self.assertRaises(ValueError):
            FxData.check_amount(0)
        self.assertEqual(FxData.check_date("2026-09-22").isoformat(), "2026-09-22")
        with self.assertRaises(ValueError):
            FxData.check_date("22/09/2026")
        with self.assertRaises(ValueError):
            FxData.check_date("1990-01-01")
        with self.assertRaises(ValueError):
            FxData.check_date((date.today() + timedelta(days=30)).isoformat())

    def test_ranges_are_validated(self):
        first, last = FxData.check_range("2026-09-01", "2026-09-04")
        self.assertEqual((first.isoformat(), last.isoformat()), ("2026-09-01", "2026-09-04"))
        with self.assertRaises(ValueError):
            FxData.check_range("2026-09-04", "2026-09-01")

    def test_latest_maps_the_payload(self):
        data = FxData(fetch=lambda url, params: LATEST_PAYLOAD)
        latest = data.latest("USD")
        self.assertEqual(latest["date"], "2026-09-22")
        self.assertEqual(latest["base"], "USD")
        self.assertEqual(latest["rates"], {"EUR": 0.87237, "GBP": 0.74832, "JPY": 157.18})

    def test_latest_asks_the_api_for_the_requested_symbols(self):
        seen: dict = {}

        def fetch(url, params):
            seen.update(params)
            return LATEST_PAYLOAD

        FxData(fetch=fetch).latest("usd", symbols=["eur", "gbp"])
        self.assertEqual(seen["base"], "USD")
        self.assertEqual(seen["symbols"], "EUR,GBP")

    def test_latest_without_rates_is_an_error(self):
        data = FxData(fetch=lambda url, params: {"base": "USD"})
        with self.assertRaises(FxError):
            data.latest("USD")

    def test_convert_multiplies_and_rounds(self):
        data = FxData(fetch=lambda url, params: LATEST_PAYLOAD)
        conversion = data.convert(100, "USD", "EUR")
        self.assertEqual(conversion["rate"], 0.87237)
        self.assertEqual(conversion["converted"], 87.237)
        same = data.convert(50, "EUR", "EUR")
        self.assertEqual(same["rate"], 1.0)
        self.assertEqual(same["converted"], 50)

    def test_convert_refuses_a_missing_quote(self):
        data = FxData(fetch=lambda url, params: LATEST_PAYLOAD)
        with self.assertRaises(FxError):
            data.convert(100, "USD", "CHF")

    def test_history_computes_the_stats(self):
        data = FxData(fetch=lambda url, params: HISTORY_PAYLOAD)
        history = data.history("2026-09-01", "2026-09-04", base="USD", symbols=["EUR"])
        self.assertEqual(history["working_days"], 4)
        stats = history["stats"]["EUR"]
        self.assertEqual(stats["first"]["rate"], 0.86281)
        self.assertEqual(stats["last"]["rate"], 0.87012)
        self.assertAlmostEqual(stats["change"], 0.00731, places=6)
        self.assertAlmostEqual(stats["change_percent"], 0.8472, places=4)
        self.assertEqual(stats["min"], 0.86281)
        self.assertEqual(stats["max"], 0.87012)
        self.assertEqual(len(history["series"]["EUR"]), 4)

    def test_a_range_with_no_rows_is_an_error(self):
        data = FxData(fetch=lambda url, params: {"rates": {}, "base": "USD"})
        with self.assertRaises(FxError):
            data.history("2026-09-01", "2026-09-04", base="USD", symbols=["EUR"])

    def test_currencies_are_sorted(self):
        data = FxData(fetch=lambda url, params: CURRENCIES_PAYLOAD)
        listed = data.currencies()
        self.assertEqual(list(listed["currencies"])[:3], ["AUD", "BRL", "CAD"])


class RouteTests(unittest.TestCase):
    def test_a_conversion(self):
        skill, params = route("what is 100 USD in EUR?")
        self.assertEqual(skill, "fx-rate")
        self.assertEqual(params["base"], "USD")
        self.assertEqual(params["quote"], "EUR")
        self.assertEqual(params["amount"], 100.0)

    def test_currency_words_work(self):
        skill, params = route("how much is 50 euros in yen?")
        self.assertEqual(skill, "fx-rate")
        self.assertEqual(params["base"], "EUR")
        self.assertEqual(params["quote"], "JPY")
        self.assertEqual(params["amount"], 50.0)

    def test_a_rate_without_an_amount(self):
        skill, params = route("what is the GBP to JPY rate?")
        self.assertEqual(skill, "fx-rate")
        self.assertEqual(params["base"], "GBP")
        self.assertEqual(params["quote"], "JPY")
        self.assertNotIn("amount", params)

    def test_a_history_question(self):
        skill, params = route("how did USD-EUR move over the last 30 days?")
        self.assertEqual(skill, "fx-history")
        self.assertEqual(params["base"], "USD")
        self.assertEqual(params["symbols"], ["EUR"])
        self.assertEqual(params["start"], (date.today() - timedelta(days=30)).isoformat())
        self.assertEqual(params["end"], date.today().isoformat())

    def test_shorthand_dates(self):
        skill, params = route("EUR history since 2026-01-01?")
        self.assertEqual(skill, "fx-history")
        self.assertEqual(params["start"], "2026-01-01")
        self.assertEqual(params["base"], "EUR")
        self.assertEqual(params["symbols"], [])
        self.assertEqual(history_window("the move this year")[0], f"{date.today().year}-01-01")

    def test_currencies_list(self):
        self.assertEqual(route("which currencies do you know?")[0], "fx-currencies")

    def test_no_currency_still_routes_to_the_rate_so_it_can_ask(self):
        skill, params = route("what is a dollar worth?")
        self.assertEqual(skill, "fx-rate")
        self.assertEqual(params.get("base"), "USD")

    def test_helpers(self):
        self.assertEqual(currencies_in("what is 100 usd in eur").__class__, list)
        self.assertEqual(currencies_in("100 USD to EUR"), ["USD", "EUR"])
        self.assertEqual(currencies_in("50 euros in yen"), ["EUR", "JPY"])
        self.assertEqual(amount_in("convert 1,500 GBP"), 1500.0)
        self.assertIsNone(amount_in("no numbers here"))

    def test_help(self):
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")


class TurnTests(unittest.TestCase):
    def turn(self, text: str, data: FakeData | None = None, permission: str = "allow-once"):
        agent_conn, client_conn = connected_pair()
        agent = FxAgent(agent_conn, data or FakeData())
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client
        finally:
            client.stop()

    def test_a_conversion_states_the_rate_and_the_date(self):
        result, client = self.turn("what is 100 USD in EUR?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertIn("100 USD = 87.237 EUR", result["text"])
        self.assertIn("ECB reference rate USD/EUR = 0.87237 on 2026-09-22", result["text"])
        self.assertIn("not what a bank will give you", result["text"])
        tools = [update for update in result["updates"] if update.get("sessionUpdate") == "tool_call"]
        self.assertEqual(tools[0]["name"], "fx-rate")
        self.assertEqual(len(client.permission_requests), 1)

    def test_streaming_is_chunked_under_one_message_id(self):
        result, _ = self.turn("what is 100 USD in EUR?")
        chunks = [update for update in result["updates"] if update.get("sessionUpdate") == "agent_message_chunk"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len({chunk["messageId"] for chunk in chunks}), 1)

    def test_a_rate_lists_the_quotes(self):
        result, _ = self.turn("what is the USD rate today?")
        self.assertIn("1 USD in ECB reference rates for 2026-09-22", result["text"])
        self.assertIn("EUR: 0.87237", result["text"])

    def test_history_reports_the_change(self):
        result, _ = self.turn("how did USD-EUR move over the last 30 days?")
        self.assertIn("4 working day(s) with a rate", result["text"])
        self.assertIn("up +0.00731 (+0.8472%)", result["text"])

    def test_currencies_are_listed(self):
        result, _ = self.turn("which currencies do you know?")
        self.assertIn("8 currencies in the ECB reference series", result["text"])
        self.assertIn("JPY (Japanese Yen)", result["text"])

    def test_no_currency_asks_for_one(self):
        result, _ = self.turn("what is it worth today?")
        self.assertIn("I need a currency", result["text"])

    def test_help_does_not_ask_permission(self):
        result, client = self.turn("what can you do?")
        self.assertIn("European Central Bank", result["text"])
        self.assertEqual(client.permission_requests, [])

    def test_permission_denied(self):
        result, _ = self.turn("100 USD to EUR?", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)
        self.assertIn("need permission", result["text"])

    def test_a_feed_failure_is_reported(self):
        result, _ = self.turn("100 USD to EUR?", data=FakeData(raise_error=True))
        self.assertIn("could not read the reference rates", result["text"])
        statuses = [update.get("status") for update in result["updates"]
                    if update.get("sessionUpdate") == "tool_call_update"]
        self.assertIn("failed", statuses)


if __name__ == "__main__":
    unittest.main()
