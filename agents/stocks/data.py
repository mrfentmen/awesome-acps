"""Share prices from Yahoo Finance's public chart endpoint - no key, and no promise.

Verified live on 2026-09-23:

  https://query1.finance.yahoo.com/v1/finance/search?q=apple        -> symbols, exchDisp, type
  https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=1d   -> meta + daily bars

There is one thing this reader never hides: Yahoo publishes no supported public API, so this is
the endpoint its own web page calls. It has no key, no terms to sign and **no guarantee** - it
can change or start refusing a User-Agent without notice. Every answer says that, and the
reader raises one error type when it happens instead of quietly showing a stale number.
"""

from __future__ import annotations

import datetime
import json
import os
import time
from urllib import parse, request

SEARCH = "https://query1.finance.yahoo.com/v1/finance/search"
CHART = "https://query1.finance.yahoo.com/v8/finance/chart"

DATASET = "Yahoo Finance chart endpoint (unofficial, keyless, no guarantee)"

DEFAULT_USER_AGENT = ("Mozilla/5.0 (compatible; awesome-acps-stocks/1.0; "
                      "+https://github.com/mrfentmen/awesome-acps)")

#: Quote types worth answering a question about; anything else (futures, options) is skipped.
KINDS = {"EQUITY": "share", "ETF": "ETF", "MUTUALFUND": "fund", "INDEX": "index"}


class StockError(RuntimeError):
    """The quote could not be read."""


def when(epoch) -> str:
    """A Unix timestamp to '2026-09-22 20:00 UTC'."""
    try:
        moment = datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc)
    except (TypeError, ValueError, OSError):
        return ""
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def money(value, currency: str = "") -> str:
    """339.75 -> '339.75 USD'; big volumes come back as words."""
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
    return f"{amount:,.2f}{unit}"


class StockData:
    """Symbol lookup, one quote, and a window of closes."""

    def __init__(self, fetch=None, cache_ttl: float = 120.0, timeout: float = 25.0,
                 user_agent: str | None = None) -> None:
        self.cache_ttl = float(os.environ.get("STOCKS_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("STOCKS_HTTP_TIMEOUT", timeout))
        self.user_agent = (user_agent or os.environ.get("STOCKS_USER_AGENT")
                           or DEFAULT_USER_AGENT)
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
            raise StockError(f"request failed: {exc}") from exc

    def _json(self, url: str, params: dict | None = None, ttl: float | None = None) -> dict:
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
                raise StockError(f"the quote service returned something that is not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise StockError("the quote service returned an unexpected payload")
        self._cache[key] = (now + (ttl if ttl is not None else self.cache_ttl), payload)
        return payload

    # -- reads -------------------------------------------------------------

    def find(self, query: str) -> dict:
        """The best symbol for a company name or ticker. No match is an error."""
        text = str(query or "").strip()
        if not text:
            raise ValueError("tell me which company or ticker, for example Apple or AAPL")
        payload = self._json(SEARCH, {"q": text, "quotesCount": 10}, ttl=86400.0)
        quotes = [row for row in (payload.get("quotes") or []) if row.get("symbol")]
        wanted = [row for row in quotes if row.get("quoteType") in KINDS]
        if not wanted:
            if quotes:  # a future or an option is not a share price; say so plainly
                raise ValueError(f"{text!r} matched only "
                                 f"{quotes[0].get('quoteType')} instruments, not a listed "
                                 "share, ETF, fund or index")
            raise ValueError(f"no listed instrument called {text!r} was found")
        best = wanted[0]
        return {"symbol": str(best["symbol"]), "name": best.get("longname")
                or best.get("shortname") or best["symbol"],
                "kind": KINDS.get(best.get("quoteType"), "instrument"),
                "exchange": best.get("exchDisp") or best.get("exchange") or "",
                "sector": best.get("sector") or "", "industry": best.get("industry") or ""}

    def quote(self, query: str, rng: str = "1d") -> dict:
        """One quote: price, move, the day's range, the 52 week range and the volume."""
        found = self.find(query)
        payload = self._json(f"{CHART}/{found['symbol']}",
                             {"range": rng, "interval": "1d"}, ttl=60.0)
        chart = payload.get("chart") or {}
        if chart.get("error"):
            detail = (chart["error"] or {}).get("description") or "unknown error"
            raise StockError(f"the quote service refused {found['symbol']}: {detail}")
        results = chart.get("result") or []
        if not results:
            raise StockError(f"no quote came back for {found['symbol']}")
        meta = results[0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price is None:
            raise StockError(f"the quote for {found['symbol']} had no price in it")
        return {
            "instrument": found,
            "currency": meta.get("currency") or "",
            "price": price,
            "change_percent": meta.get("regularMarketChangePercent"),
            "previous_close": meta.get("chartPreviousClose"),
            "day_high": meta.get("regularMarketDayHigh"),
            "day_low": meta.get("regularMarketDayLow"),
            "week52_high": meta.get("fiftyTwoWeekHigh"),
            "week52_low": meta.get("fiftyTwoWeekLow"),
            "volume": meta.get("regularMarketVolume"),
            "exchange": meta.get("fullExchangeName") or found["exchange"],
            "timezone": meta.get("exchangeTimezoneName") or "",
            "at": when(meta.get("regularMarketTime")),
            "source": DATASET,
        }

    def history(self, query: str, days: int = 30) -> dict:
        """The closes over a window, summarised - the raw bars are not useful in a chat."""
        windows = {5: ("5d", "1d"), 30: ("1mo", "1d"), 90: ("3mo", "1d"), 365: ("1y", "1wk")}
        wanted = max(5, min(int(days), 365))
        nearest = min(windows, key=lambda key: abs(key - wanted))
        rng, interval = windows[nearest]
        found = self.find(query)
        payload = self._json(f"{CHART}/{found['symbol']}",
                             {"range": rng, "interval": interval}, ttl=300.0)
        results = ((payload.get("chart") or {}).get("result") or [])
        if not results:
            raise StockError(f"no history came back for {found['symbol']}")
        bars = results[0]
        stamps = bars.get("timestamp") or []
        closes = ((bars.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        pairs = [(stamp, close) for stamp, close in zip(stamps, closes)
                 if isinstance(close, (int, float))]
        if len(pairs) < 2:
            raise StockError(f"the history for {found['symbol']} came back too thin to summarise")
        first, last = pairs[0][1], pairs[-1][1]
        values = [close for _stamp, close in pairs]
        return {"instrument": found, "range": rng, "interval": interval,
                "points": len(pairs), "first": first, "last": last,
                "change_percent": ((last - first) / first * 100.0) if first else None,
                "high": max(values), "low": min(values),
                "from": when(pairs[0][0]), "to": when(pairs[-1][0]),
                "currency": (bars.get("meta") or {}).get("currency") or "",
                "source": DATASET}
