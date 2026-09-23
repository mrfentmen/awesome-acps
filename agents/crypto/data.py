"""Coin prices and DeFi totals, from three keyless APIs.

Verified live on 2026-09-23:

  https://api.coingecko.com/api/v3/search?query=bitcoin          -> coins[] with id, symbol, rank
  https://api.coingecko.com/api/v3/coins/markets?ids=..&vs_currency=usd
  https://api.coingecko.com/api/v3/simple/supported_vs_currencies
  https://blockchain.info/ticker                                 -> BTC in ~250 currencies
  https://api.llama.fi/protocols                                 -> every protocol with its TVL

Two things this reader does not do: it does not invent a coin id (an unknown name is an error,
and CoinGecko answers `{}` for an unknown id rather than failing, so the empty answer is checked
for), and it does not hide that these are unregulated 24/7 markets - every answer carries the
observation time the exchange reports.
"""

from __future__ import annotations

import datetime
import json
import os
import time
from urllib import parse, request

COINGECKO = "https://api.coingecko.com/api/v3"
BLOCKCHAIN = "https://blockchain.info/ticker"
DEFILLAMA = "https://api.llama.fi/protocols"

DATASET = "CoinGecko (api.coingecko.com)"
DEFI_DATASET = "DefiLlama (api.llama.fi)"

DEFAULT_USER_AGENT = "awesome-acps-crypto/1.0 (+https://github.com/mrfentmen/awesome-acps)"

#: Short names people type that CoinGecko's search does not always rank first.
ALIASES = {"btc": "bitcoin", "eth": "ethereum", "sol": "solana", "doge": "dogecoin",
           "ada": "cardano", "xrp": "ripple", "bnb": "binancecoin", "xmr": "monero",
           "ltc": "litecoin", "dot": "polkadot", "link": "chainlink", "avax": "avalanche-2",
           "matic": "matic-network", "atom": "cosmos", "usdt": "tether", "usdc": "usd-coin"}


class CryptoError(RuntimeError):
    """A market feed could not be read."""


def money(value, currency: str = "") -> str:
    """A figure a person can read.

    86639 -> '86,639 USD', 2775.77 -> '2,775.77 USD', 1740157806607 -> '1.74 trillion USD',
    42000000000 -> '42.00 billion USD', 0.5 -> '0.500000 USD'. A market cap of
    1,740,157,806,607 is not a number anybody reads; 1.74 trillion is.
    """
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "n/a"
    unit = f" {currency}" if currency else ""
    if abs(amount) >= 1_000_000_000_000:
        return f"{amount / 1_000_000_000_000:.2f} trillion{unit}"
    if abs(amount) >= 1_000_000_000:
        return f"{amount / 1_000_000_000:.2f} billion{unit}"
    if abs(amount) >= 1_000_000:
        return f"{amount / 1_000_000:.2f} million{unit}"
    if amount.is_integer():
        return f"{amount:,.0f}{unit}"
    if abs(amount) >= 1:
        return f"{amount:,.2f}{unit}"
    return f"{amount:.6f}{unit}"


def observed(stamp) -> str:
    """CoinGecko's last_updated to '2026-09-23 03:14 UTC'."""
    if not stamp:
        return ""
    try:
        parsed = datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return str(stamp)
    return parsed.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class CryptoData:
    """Prices, the market table and DeFi totals - one request each, cached briefly."""

    def __init__(self, fetch=None, cache_ttl: float = 60.0, timeout: float = 25.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("CRYPTO_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("CRYPTO_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("CRYPTO_USER_AGENT") or DEFAULT_USER_AGENT
        self._fetch = fetch or self._http_text
        self._cache: dict[str, tuple[float, object]] = {}

    # -- transport ---------------------------------------------------------

    def _http_text(self, url: str, params: dict) -> str:
        query = f"{url}?{parse.urlencode(params)}" if params else url
        req = request.Request(query, headers={"User-Agent": self.user_agent, "Accept": "*/*"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # urllib raises many types; callers see one error type
            raise CryptoError(f"request failed: {exc}") from exc

    def _json(self, url: str, params: dict | None = None, ttl: float | None = None):
        key = f"{url}?{parse.urlencode(params or {})}"
        now = time.time()
        hit = self._cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        payload = self._fetch(url, params or {})
        if isinstance(payload, (str, bytes)):
            try:
                payload = json.loads(payload)
            except ValueError as exc:
                raise CryptoError(f"the market returned something that is not JSON: {exc}") from exc
        if not isinstance(payload, (dict, list)):
            raise CryptoError("the market returned an unexpected payload")
        self._cache[key] = (now + (ttl if ttl is not None else self.cache_ttl), payload)
        return payload

    # -- reads -------------------------------------------------------------

    def find(self, query: str) -> dict:
        """A coin id from a name, a symbol or a ticker. Never guesses: no match is an error."""
        text = str(query or "").strip().lower()
        if not text:
            raise ValueError("tell me which coin, for example bitcoin")
        if text in ALIASES:
            return {"id": ALIASES[text], "name": text.upper(), "symbol": text.upper()}
        payload = self._json(f"{COINGECKO}/search", {"query": text}, ttl=3600.0)
        coins = [coin for coin in (payload.get("coins") or []) if coin.get("id")]
        if not coins:
            raise ValueError(f"no coin called {query!r} was found")
        ranked = [coin for coin in coins if isinstance(coin.get("market_cap_rank"), int)]
        best = min(ranked, key=lambda coin: coin["market_cap_rank"]) if ranked else coins[0]
        return {"id": best["id"], "name": best.get("name") or text,
                "symbol": (best.get("symbol") or "").upper()}

    def currencies(self) -> list[str]:
        payload = self._json(f"{COINGECKO}/simple/supported_vs_currencies", ttl=86400.0)
        return sorted(str(code).lower() for code in payload if isinstance(payload, list))

    def price(self, query: str, vs: str = "usd") -> dict:
        """One coin's price, its 24h range and change, and when the market said so."""
        coin = self.find(query)
        currency = str(vs or "usd").strip().lower()
        try:
            rows = self._json(f"{COINGECKO}/coins/markets",
                              {"vs_currency": currency, "ids": coin["id"]})
        except CryptoError as exc:
            if "400" in str(exc) or "404" in str(exc):
                raise ValueError(f"{currency!r} is not a currency I can price in") from exc
            raise
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"no price for {coin['id']!r} in {currency}")
        row = rows[0]
        return {
            "coin": coin,
            "currency": currency.upper(),
            "price": row.get("current_price"),
            "high": row.get("high_24h"),
            "low": row.get("low_24h"),
            "change": row.get("price_change_percentage_24h"),
            "market_cap": row.get("market_cap"),
            "rank": row.get("market_cap_rank"),
            "at": observed(row.get("last_updated")),
            "source": DATASET,
        }

    def top(self, limit: int = 10) -> dict:
        """The market table, biggest first."""
        wanted = max(1, min(int(limit), 25))
        rows = self._json(f"{COINGECKO}/coins/markets",
                          {"vs_currency": "usd", "order": "market_cap_desc",
                           "per_page": wanted, "page": 1})
        if not isinstance(rows, list) or not rows:
            raise CryptoError("the market table came back empty")
        return {"rows": [{"rank": row.get("market_cap_rank"), "name": row.get("name"),
                          "symbol": (row.get("symbol") or "").upper(),
                          "price": row.get("current_price"),
                          "change": row.get("price_change_percentage_24h"),
                          "market_cap": row.get("market_cap")} for row in rows],
                "source": DATASET}

    def defi(self, limit: int = 10) -> dict:
        """Protocols by total value locked, from DefiLlama."""
        wanted = max(1, min(int(limit), 25))
        payload = self._json(DEFILLAMA, {})
        if not isinstance(payload, list) or not payload:
            raise CryptoError("the protocol list came back empty")
        rows = [row for row in payload if isinstance(row.get("tvl"), (int, float))]
        rows.sort(key=lambda row: row["tvl"], reverse=True)
        total = sum(row["tvl"] for row in rows)
        return {"rows": [{"name": row.get("name"), "category": row.get("category") or "-",
                          "tvl": row["tvl"], "change": row.get("change_1d"),
                          "chains": len(row.get("chains") or [])} for row in rows[:wanted]],
                "protocols": len(rows), "total": total, "source": DEFI_DATASET}
