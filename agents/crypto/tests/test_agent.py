"""Tests for the crypto agent: coin lookup, prices, the market table, routing and turns.

    python3 agents/crypto/tests/test_agent.py

No network: CoinGecko and DefiLlama payloads are injected, shaped like the live ones read on
2026-09-23 (bitcoin 86,639 USD, up 1.13% on the day, 24h low 85,107).
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

from agent import coin_from_text, route  # noqa: E402
from data import CryptoData, CryptoError, money, observed  # noqa: E402

SEARCH = {"coins": [
    {"id": "bitcoin", "name": "Bitcoin", "symbol": "BTC", "market_cap_rank": 1},
    {"id": "bitcoin-cash", "name": "Bitcoin Cash", "symbol": "BCH", "market_cap_rank": 21},
], "exchanges": [], "nfts": []}

MARKET = [{
    "id": "bitcoin", "symbol": "btc", "name": "Bitcoin",
    "current_price": 86639, "high_24h": 86889, "low_24h": 85107,
    "price_change_percentage_24h": 1.1273, "market_cap": 1740157806607,
    "market_cap_rank": 1, "last_updated": "2026-09-23T03:14:00.000Z",
}]

TABLE = [
    {"market_cap_rank": 1, "name": "Bitcoin", "symbol": "btc", "current_price": 86639,
     "price_change_percentage_24h": 1.13, "market_cap": 1740157806607},
    {"market_cap_rank": 2, "name": "Ethereum", "symbol": "eth", "current_price": 2775.77,
     "price_change_percentage_24h": 1.55, "market_cap": 335000000000},
]

PROTOCOLS = [
    {"name": "Lido", "category": "Liquid Staking", "tvl": 24000000000, "change_1d": -0.4,
     "chains": ["Ethereum", "Solana"]},
    {"name": "AAVE", "category": "Lending", "tvl": 18000000000, "change_1d": 1.2,
     "chains": ["Ethereum"]},
    {"name": "No TVL", "category": "Other", "tvl": None, "change_1d": None, "chains": []},
]


class FakeFeeds:
    def __init__(self, search=None, market=None, table=None, protocols=None, fail=None):
        self.calls: list[tuple[str, dict]] = []
        self.search = SEARCH if search is None else search
        self.market = MARKET if market is None else market
        self.table = TABLE if table is None else table
        self.protocols = PROTOCOLS if protocols is None else protocols
        self.fail = fail

    def __call__(self, url: str, params: dict):
        self.calls.append((url, dict(params)))
        if self.fail and self.fail in url:
            raise CryptoError("the market is unreachable")
        if "/search" in url:
            return json.dumps(self.search)
        if "/coins/markets" in url:
            if params.get("order") == "market_cap_desc":
                return json.dumps(self.table)
            return json.dumps(self.market)
        if "llama" in url:
            return json.dumps(self.protocols)
        raise AssertionError(f"unexpected url {url}")


def make_data(**kw):
    feed = FakeFeeds(**kw)
    return CryptoData(fetch=feed), feed


class FormatTests(unittest.TestCase):
    def test_prices_keep_a_sensible_number_of_digits(self):
        self.assertEqual(money(86639, "USD"), "86,639 USD")
        self.assertEqual(money(2775.77, "USD"), "2,775.77 USD")
        self.assertEqual(money(0.5, "USD"), "0.500000 USD")
        self.assertEqual(money(1740157806607, "USD"), "1.74 trillion USD")
        self.assertEqual(money(42000000000, "USD"), "42.00 billion USD")
        self.assertEqual(money(None), "n/a")

    def test_the_observation_time_is_utc(self):
        self.assertEqual(observed("2026-09-23T03:14:00.000Z"), "2026-09-23 03:14 UTC")
        self.assertEqual(observed(None), "")


class ReadTests(unittest.TestCase):
    def test_a_coin_name_resolves_to_the_biggest_match_not_the_first(self):
        data, _feed = make_data()
        coin = data.find("bitcoin")
        self.assertEqual(coin["id"], "bitcoin")  # rank 1 beats bitcoin-cash at rank 21

    def test_a_ticker_uses_the_known_alias_without_calling_out(self):
        data, feed = make_data()
        self.assertEqual(data.find("btc")["id"], "bitcoin")
        self.assertEqual(feed.calls, [])

    def test_an_unknown_coin_is_refused_not_guessed(self):
        data, _feed = make_data(search={"coins": []})
        with self.assertRaises(ValueError):
            data.find("zzzzzznothing")

    def test_a_price_carries_the_range_change_and_time(self):
        data, _feed = make_data()
        reading = data.price("bitcoin")
        self.assertEqual(reading["price"], 86639)
        self.assertEqual(reading["low"], 85107)
        self.assertEqual(reading["change"], 1.1273)
        self.assertEqual(reading["rank"], 1)
        self.assertEqual(reading["at"], "2026-09-23 03:14 UTC")

    def test_pricing_in_another_currency_is_passed_through(self):
        data, feed = make_data()
        reading = data.price("eth", "eur")
        self.assertEqual(reading["currency"], "EUR")
        markets = [call for call in feed.calls if "/coins/markets" in call[0]][-1]
        self.assertEqual(markets[1]["vs_currency"], "eur")

    def test_a_currency_that_does_not_exist_is_refused(self):
        def boom(url, params):
            if "/search" in url:
                return json.dumps(SEARCH)
            raise CryptoError("request failed: HTTP Error 400: Bad Request")

        data = CryptoData(fetch=boom)
        with self.assertRaises(ValueError):
            data.price("bitcoin", "xyz")

    def test_the_market_table_is_biggest_first_and_bounded(self):
        data, feed = make_data()
        reading = data.top(limit=99)
        self.assertEqual([row["symbol"] for row in reading["rows"]], ["BTC", "ETH"])
        self.assertEqual(feed.calls[-1][1]["per_page"], 25)

    def test_defi_rows_without_a_tvl_are_dropped_not_counted_as_zero(self):
        data, _feed = make_data()
        reading = data.defi(limit=10)
        self.assertEqual([row["name"] for row in reading["rows"]], ["Lido", "AAVE"])
        self.assertEqual(reading["protocols"], 2)
        self.assertEqual(reading["total"], 42000000000)

    def test_an_empty_market_or_protocol_list_is_an_error(self):
        data, _feed = make_data(table=[])
        with self.assertRaises(CryptoError):
            data.top()
        data, _feed = make_data(protocols=[])
        with self.assertRaises(CryptoError):
            data.defi()


class RouteTests(unittest.TestCase):
    def test_a_plain_price_question(self):
        skill, params = route("what is bitcoin worth right now?")
        self.assertEqual(skill, "crypto-price")
        self.assertEqual(params["coin"], "bitcoin")

    def test_a_ticker_and_a_currency(self):
        _skill, params = route("price of eth in EUR")
        self.assertEqual(params["coin"], "eth")
        self.assertEqual(params["vs"], "eur")

    def test_the_market_table(self):
        skill, params = route("top 5 coins")
        self.assertEqual(skill, "crypto-top")
        self.assertEqual(params["limit"], 5)

    def test_defi(self):
        self.assertEqual(route("top defi protocols")[0], "defi-top")
        self.assertEqual(route("how big is defi in total?")[1]["total"], True)
        self.assertEqual(route("top 3 defi protocols")[1]["limit"], 3)

    def test_no_crypto_word_is_help(self):
        self.assertEqual(route("what is the national debt?")[0], "help")
        self.assertEqual(route("")[0], "help")
        self.assertEqual(route("what can you do?")[0], "help")

    def test_helpers(self):
        self.assertEqual(coin_from_text("what is bitcoin worth?"), "bitcoin")
        self.assertEqual(coin_from_text("price of ethereum in eur"), "ethereum")
        self.assertIsNone(coin_from_text("top coins"))


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
        from agent import CryptoAgent

        agent_conn, client_conn = connected_pair()
        data, feed = make_data(**kw)
        agent = CryptoAgent(agent_conn, data)
        threading.Thread(target=agent_conn.serve, daemon=True).start()
        client = AcpClient(connection=client_conn, permission=permission)
        client.start()
        client.initialize()
        session_id = client.new_session(cwd="/tmp")
        try:
            return client.prompt(text, session_id), client, feed
        finally:
            client.stop()

    def test_a_price_answer(self):
        result, client, _ = self.turn("what is bitcoin worth right now?")
        self.assertEqual(result["stopReason"], STOP_END_TURN)
        self.assertEqual(len(client.permission_requests), 1)
        self.assertIn("Bitcoin (BTC) - 86,639 USD", result["text"])
        self.assertIn("24h: up 1.13%", result["text"])
        self.assertIn("the market reported this at 2026-09-23 03:14 UTC", result["text"])

    def test_the_market_table(self):
        result, _, _ = self.turn("top 2 coins")
        self.assertIn("1. Bitcoin (BTC)  86,639 USD", result["text"])
        self.assertIn("2. Ethereum (ETH)  2,775.77 USD", result["text"])

    def test_defi_in_total(self):
        result, _, _ = self.turn("how big is defi in total?")
        self.assertIn("DeFi total value locked across 2 protocols: 42.00 billion USD",
                      result["text"])
        self.assertIn("Lido (Liquid Staking)", result["text"])

    def test_an_unknown_coin_is_reported_with_no_price(self):
        result, _, _ = self.turn("what is zzzzznothing worth?", search={"coins": []})
        self.assertIn("no coin called", result["text"])
        self.assertNotIn("USD", result["text"])

    def test_help_asks_nothing(self):
        result, client, feed = self.turn("what can you do?")
        self.assertIn("CoinGecko", result["text"])
        self.assertEqual(client.permission_requests, [])
        self.assertEqual(feed.calls, [])

    def test_permission_denied(self):
        result, _, _ = self.turn("price of bitcoin", permission="reject")
        self.assertEqual(result["stopReason"], STOP_REFUSAL)

    def test_a_dead_market(self):
        result, _, _ = self.turn("price of bitcoin", fail="/coins/markets")
        self.assertIn("I could not read the market", result["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
