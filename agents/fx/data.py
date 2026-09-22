"""Read-only exchange rate reader for the fx agent.

Self-contained on purpose: the Frankfurter transport lives here with one small cache.

  https://api.frankfurter.dev/v1/latest?base=USD&symbols=EUR,GBP  latest reference rates
  https://api.frankfurter.dev/v1/2026-09-01..2026-09-05?base=USD   a date range
  https://api.frankfurter.dev/v1/currencies                        the supported currency list

Keyless, verified live on 2026-09-22 (USD to EUR 0.87237, GBP 0.74832, JPY 157.18; the
1st-to-5th September range returned four working days).

Three real properties of this data are stated in every answer instead of hidden:
  * these are European Central Bank reference rates, published once per working day;
  * there are no rows for weekends or TARGET holidays, so a "yesterday" question can
    legitimately come back with a date three days old;
  * a reference rate is not a rate you can trade at.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from urllib import parse, request

BASE_URL = "https://api.frankfurter.dev/v1"
DATASET_LATEST = "api.frankfurter.dev/v1/latest"
DATASET_HISTORY = "api.frankfurter.dev/v1"
DATASET_CURRENCIES = "api.frankfurter.dev/v1/currencies"

DEFAULT_USER_AGENT = "awesome-acps-fx/1.0 (+https://github.com/mrfentmen/awesome-acps)"

_CURRENCY_RE = re.compile(r"\b([A-Z]{3})\b")
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


class FxError(RuntimeError):
    """A Frankfurter response could not be read."""


class FxData:
    """Frankfurter (ECB reference rates), read over HTTP with caching and validation."""

    def __init__(self, fetch=None, cache_ttl: float = 3600.0, timeout: float = 20.0,
                 user_agent: str | None = None) -> None:
        # Rates change once per working day; an hour of caching costs nothing and saves calls.
        self.cache_ttl = float(os.environ.get("FX_CACHE_TTL", cache_ttl))
        self.timeout = float(os.environ.get("FX_HTTP_TIMEOUT", timeout))
        self.user_agent = user_agent or os.environ.get("FX_USER_AGENT") or DEFAULT_USER_AGENT
        self._fetch = fetch or self._http_get
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    # -- transport ---------------------------------------------------------

    def _http_get(self, url: str, params: dict):
        query = parse.urlencode(params) if params else ""
        full = f"{url}?{query}" if query else url
        req = request.Request(full, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # urllib raises many types; callers see one error type
            raise FxError(f"exchange rate request failed: {exc}") from exc

    def _get(self, url: str, params: dict, ttl: float | None = None):
        key = f"{url}:{json.dumps({k: str(v) for k, v in sorted(params.items())})}"
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        payload = self._fetch(url, params)
        with self._lock:
            self._cache[key] = (now + (ttl if ttl is not None else self.cache_ttl), payload)
        return payload

    # -- validation --------------------------------------------------------

    @staticmethod
    def check_currency(value: str) -> str:
        code = str(value).strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", code):
            raise ValueError(f"{value!r} is not a currency code - a code is three letters, like USD")
        return code

    @staticmethod
    def check_symbols(value) -> list[str] | None:
        if value in (None, "", []):
            return None
        if isinstance(value, str):
            parts = [part for part in re.split(r"[,\s]+", value) if part]
        else:
            parts = list(value)
        codes = [FxData.check_currency(part) for part in parts]
        if not codes:
            return None
        if len(codes) > 20:
            raise ValueError("ask for at most 20 currencies at once")
        return codes

    @staticmethod
    def check_amount(value) -> float:
        try:
            amount = float(value)
        except (TypeError, ValueError):
            raise ValueError("an amount must be a number") from None
        if not 0 < amount <= 1e12:
            raise ValueError("an amount must be greater than 0 and at most 1 trillion")
        return amount

    @staticmethod
    def check_date(value) -> date:
        text = str(value).strip()
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError(f"{value!r} is not a date - dates are YYYY-MM-DD") from None
        if parsed > date.today() + timedelta(days=1):
            raise ValueError(f"{text} is in the future and has no reference rate yet")
        if parsed < date(1999, 1, 4):
            raise ValueError("the euro reference series starts on 1999-01-04")
        return parsed

    @staticmethod
    def check_range(start: str, end: str) -> tuple[date, date]:
        first, last = FxData.check_date(start), FxData.check_date(end)
        if first > last:
            raise ValueError("the start date must not be after the end date")
        if (last - first).days > 3660:
            raise ValueError("keep the range under about ten years")
        return first, last

    @staticmethod
    def window_start(days: int) -> str:
        """YYYY-MM-DD `days` before today, for 'last N days' questions."""
        try:
            window = int(days)
        except (TypeError, ValueError):
            raise ValueError("a window must be a whole number of days") from None
        if not 1 <= window <= 3650:
            raise ValueError("a window must be between 1 and 3650 days")
        return (date.today() - timedelta(days=window)).isoformat()

    @staticmethod
    def date_from_text(text: str) -> str | None:
        match = _DATE_RE.search(str(text))
        return match.group(1) if match else None

    # -- readers -----------------------------------------------------------

    def currencies(self) -> dict:
        payload = self._get(f"{BASE_URL}/currencies", {}, ttl=min(self.cache_ttl * 24, 86400))
        if not isinstance(payload, dict) or not payload:
            raise FxError("Frankfurter returned an unexpected currency list")
        return {"dataset": DATASET_CURRENCIES,
                "currencies": {code: name for code, name in sorted(payload.items())}}

    def latest(self, base: str = "EUR", symbols=None) -> dict:
        wanted_base = self.check_currency(base)
        wanted = self.check_symbols(symbols)
        params = {"base": wanted_base}
        if wanted:
            params["symbols"] = ",".join(wanted)
        payload = self._get(f"{BASE_URL}/latest", params, ttl=min(self.cache_ttl, 3600))
        rates = payload.get("rates") if isinstance(payload, dict) else None
        if not isinstance(rates, dict) or not rates:
            raise FxError(f"Frankfurter returned no rates for base {wanted_base}")
        return {
            "dataset": DATASET_LATEST,
            "date": payload.get("date"),
            "base": payload.get("base") or wanted_base,
            "rates": {code: rates[code] for code in sorted(rates)},
        }

    def convert(self, amount: float, base: str, quote: str) -> dict:
        quantity = self.check_amount(amount)
        wanted_base = self.check_currency(base)
        wanted_quote = self.check_currency(quote)
        if wanted_base == wanted_quote:
            return {"dataset": DATASET_LATEST, "date": None, "base": wanted_base, "quote": wanted_quote,
                    "rate": 1.0, "amount": quantity, "converted": quantity,
                    "note": "same currency, so the rate is exactly 1"}
        payload = self._get(f"{BASE_URL}/latest", {"base": wanted_base, "symbols": wanted_quote},
                            ttl=min(self.cache_ttl, 3600))
        rates = payload.get("rates") if isinstance(payload, dict) else None
        if not isinstance(rates, dict) or wanted_quote not in rates:
            raise FxError(f"Frankfurter has no rate for {wanted_base} to {wanted_quote}")
        rate = float(rates[wanted_quote])
        return {
            "dataset": DATASET_LATEST,
            "date": payload.get("date"),
            "base": wanted_base,
            "quote": wanted_quote,
            "rate": rate,
            "amount": quantity,
            "converted": round(quantity * rate, 6),
        }

    def history(self, start: str, end: str, base: str = "EUR", symbols=None) -> dict:
        first, last = self.check_range(start, end)
        wanted_base = self.check_currency(base)
        wanted = self.check_symbols(symbols)
        params = {"base": wanted_base}
        if wanted:
            params["symbols"] = ",".join(wanted)
        url = f"{BASE_URL}/{first.isoformat()}..{last.isoformat()}"
        payload = self._get(url, params, ttl=min(self.cache_ttl * 6, 21600))
        rates = payload.get("rates") if isinstance(payload, dict) else None
        if not isinstance(rates, dict) or not rates:
            raise FxError(
                f"Frankfurter has no reference rates between {first.isoformat()} and {last.isoformat()} "
                f"for base {wanted_base} - the range may fall entirely on weekends and holidays")
        days = sorted(rates)
        codes = sorted({code for day in rates.values() for code in day})
        series = {code: [{"date": day, "rate": rates[day][code]} for day in days if code in rates[day]]
                  for code in codes}
        stats = {}
        for code, points in series.items():
            values = [point["rate"] for point in points]
            change = round(values[-1] - values[0], 6)
            stats[code] = {
                "first": points[0],
                "last": points[-1],
                "change": change,
                "change_percent": round(change / values[0] * 100, 4) if values[0] else None,
                "min": min(values),
                "max": max(values),
            }
        return {
            "dataset": f"{DATASET_HISTORY}/{first.isoformat()}..{last.isoformat()}",
            "base": wanted_base,
            "start_date": payload.get("start_date") or first.isoformat(),
            "end_date": payload.get("end_date") or last.isoformat(),
            "working_days": len(days),
            "series": series,
            "stats": stats,
        }
