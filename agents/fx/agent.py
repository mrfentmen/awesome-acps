"""FX — European Central Bank reference rates inside your editor.

No ACP agent knows what a currency is worth. This one reads Frankfurter (keyless), which
serves the ECB's daily reference rates, and answers a conversion, a rate, or how a rate
moved over a range - always with the date the rate is from.

Deterministic on purpose: routing is rules, every number is the ECB's, and nothing is
guessed. It reports a plan, opens one tool call per lookup, asks permission before its
first read, streams the answer, and closes the tool call with a one-line summary.
"""

from __future__ import annotations

import re
import sys
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from data import (  # noqa: E402
    DATASET_CURRENCIES,
    DATASET_HISTORY,
    DATASET_LATEST,
    FxData,
    FxError,
)

HELP = (
    "I read the European Central Bank reference rates through Frankfurter (keyless). Ask me:\n"
    "  • what is 100 USD in EUR?\n"
    "  • what is the EUR to JPY rate?\n"
    "  • how did USD-EUR move over the last 30 days?\n"
    "  • which currencies do you know?\n"
    "These are reference rates published once per working day: nothing on weekends or "
    "holidays, and not a rate you can trade at."
)

PERMISSION_KEY = "fx-read-public-ecb-rates"

SKILLS = ("fx-rate", "fx-history", "fx-currencies", "help")

_AMOUNT_RE = re.compile(r"\b(\d[\d,]*(?:\.\d+)?)\b")
_WINDOW_RE = re.compile(r"\b(?:last|past|previous|over the last)\s+(\d{1,4})\s*(days?|weeks?|months?|years?)\b",
                        re.IGNORECASE)
_SINCE_RE = re.compile(r"\bsince\s+(\d{4}-\d{2}-\d{2})\b", re.IGNORECASE)
_NAMED_WINDOW_RE = re.compile(r"\b(this year|last year|this month|last month|this week|last week|ytd)\b",
                              re.IGNORECASE)
_WINDOW_MULTIPLIER = {"day": 1, "week": 7, "month": 30, "year": 365}
_CURRENCY_WORDS = {
    "dollar": "USD", "dollars": "USD", "buck": "USD", "bucks": "USD",
    "euro": "EUR", "euros": "EUR",
    "pound": "GBP", "pounds": "GBP", "sterling": "GBP", "quid": "GBP",
    "yen": "JPY", "yuan": "CNY", "renminbi": "CNY",
    "franc": "CHF", "francs": "CHF",
    "rupee": "INR", "rupees": "INR",
    "won": "KRW", "real": "BRL", "reais": "BRL", "peso": "MXN", "pesos": "MXN",
    "rand": "ZAR", "lira": "TRY", "krona": "SEK", "krone": "NOK", "zloty": "PLN",
    "shekel": "ILS", "dirham": "AED", "riyal": "SAR", "ringgit": "MYR", "baht": "THB",
    "rupiah": "IDR", "peseta": "EUR",
}


def currencies_in(text: str) -> list[str]:
    """Currency codes mentioned, by word or by three-letter code, in the order they appear."""
    found: list[tuple[int, str]] = []
    lowered = text.lower()
    for word, code in _CURRENCY_WORDS.items():
        match = re.search(rf"\b{re.escape(word)}\b", lowered)
        if match:
            found.append((match.start(), code))
    for match in re.finditer(r"\b[A-Z]{3}\b", text):
        found.append((match.start(), match.group(0)))
    ordered: list[str] = []
    for _, code in sorted(found):
        if code not in ordered:
            ordered.append(code)
    return ordered


def amount_in(text: str) -> float | None:
    """A quantity to convert, ignoring years and date fragments."""
    stripped = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", text)
    for match in _AMOUNT_RE.finditer(stripped):
        raw = match.group(1).replace(",", "")
        if re.fullmatch(r"(19|20)\d{2}", raw):
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if 0 < value <= 1e12:
            return value
    return None


def history_window(text: str) -> tuple[str, str] | None:
    """A (start, end) date pair for a history question, or None if it is not one."""
    since = _SINCE_RE.search(text)
    end = date.today().isoformat()
    if since:
        return since.group(1), end
    window = _WINDOW_RE.search(text)
    if window:
        days = int(window.group(1)) * _WINDOW_MULTIPLIER[window.group(2).lower().rstrip("s")]
        return (date.today() - timedelta(days=days)).isoformat(), end
    named = _NAMED_WINDOW_RE.search(text)
    if named:
        phrase = named.group(1).lower()
        if phrase in ("this year", "ytd"):
            return date(date.today().year, 1, 1).isoformat(), end
        if phrase == "last year":
            return date(date.today().year - 1, 1, 1).isoformat(), date(date.today().year - 1, 12, 31).isoformat()
        if phrase == "this month":
            return date(date.today().year, date.today().month, 1).isoformat(), end
        if phrase == "last month":
            first = date(date.today().year, date.today().month, 1) - timedelta(days=1)
            return date(first.year, first.month, 1).isoformat(), first.isoformat()
        if phrase == "this week":
            return (date.today() - timedelta(days=date.today().weekday())).isoformat(), end
        return (date.today() - timedelta(days=7)).isoformat(), end
    return None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    if not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE):
        return "help", {}

    params: dict = {}
    if re.search(r"\b(which|what) (?:currencies|currency codes?)\b|\bcurrencies do you\b", text, re.IGNORECASE):
        return "fx-currencies", params

    codes = currencies_in(text)
    window = history_window(text)
    if window or re.search(r"\b(moved?|move|change[ds]?|trend|history|over time|chart)\b", text, re.IGNORECASE):
        start, end = window or ((date.today() - timedelta(days=30)).isoformat(), date.today().isoformat())
        params["start"] = start
        params["end"] = end
        if codes:
            params["base"] = codes[0]
            # One code means "that base against everything"; two or more narrows the symbols.
            params["symbols"] = codes[1:3] if len(codes) > 1 else []
        return "fx-history", params

    if not codes:
        return "fx-rate", params
    params["base"] = codes[0]
    if len(codes) > 1:
        params["quote"] = codes[1]
        params["symbols"] = codes[1:6]
    amount = amount_in(text)
    if amount is not None and len(codes) > 1:
        params["amount"] = amount
    return "fx-rate", params


class FxAgent(AcpAgent):
    name = "fx"
    title = "FX — ECB reference rates"
    version = "1.0.0"

    def __init__(self, connection=None, data: FxData | None = None) -> None:
        super().__init__(connection)
        self.data = data or FxData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "FX session opened. " + HELP.split("\n", 1)[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read the ECB reference rates", "medium"),
            ("Report the rate with the date it is from", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("One source: the ECB reference rates via Frankfurter. Published around "
                        "16:00 CET on working days, so the newest date you will see is often "
                        "yesterday's, and weekends have no rate at all.")
            return STOP_END_TURN

        tool = f"call_{skill}"
        ctx.tool_call(tool, f"Read ECB reference rates for {skill}", kind="fetch", name=skill,
                      raw_input={"skill": skill, **params})
        if not ctx.ask_permission(tool, "Allow FX to read the public ECB reference rates?",
                                 remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the public reference rates before I can answer.")
            return STOP_REFUSAL

        ctx.tool_call_update(tool, status="in_progress")
        try:
            answer, artifact = self._run_skill(skill, params)
        except (FxError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the reference rates: {exc}")
            return STOP_END_TURN
        ctx.check_cancelled()
        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(artifact["summary"]))
        ctx.stream_text(answer)
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        # Every skill is a single HTTP read; nothing long-running to interrupt.
        return None

    # -- skills ------------------------------------------------------------

    def _run_skill(self, skill: str, params: dict) -> tuple[str, dict]:
        if skill == "fx-history":
            return self._history(params)
        if skill == "fx-currencies":
            return self._currencies(params)
        if skill == "fx-rate":
            return self._rate(params)
        return HELP, {"summary": "help", "dataset": None}

    def _rate(self, params: dict) -> tuple[str, dict]:
        if not params.get("base"):
            return (
                "I need a currency - a three-letter code like USD, EUR, GBP, JPY, or a word like "
                "dollars or yen. For example 'what is 100 USD in EUR?' or 'the GBP to JPY rate'.",
                {"summary": "no currency given", "dataset": DATASET_LATEST, "known": False},
            )
        base = params["base"]
        quote = params.get("quote")
        amount = params.get("amount")
        if quote and amount is not None:
            conversion = self.data.convert(amount, base, quote)
            artifact = {
                "summary": f"{DATASET_LATEST}: {conversion['amount']:g} {base} = "
                           f"{conversion['converted']:g} {quote} at {conversion['rate']} "
                           f"({conversion['date']})",
                "dataset": DATASET_LATEST,
                "date": conversion["date"],
                "base": base,
                "quote": quote,
                "rate": conversion["rate"],
                "amount": conversion["amount"],
                "converted": conversion["converted"],
            }
            lines = [
                f"{conversion['amount']:g} {base} = {conversion['converted']:g} {quote}",
                f"  • ECB reference rate {base}/{quote} = {conversion['rate']} on {conversion['date']}",
                f"  • The same conversion at 10,000 {base} would be "
                f"{round(conversion['rate'] * 10000, 2):g} {quote}",
            ]
            lines.append(f"\nSource: {DATASET_LATEST} (ECB reference rates via Frankfurter, read live). "
                         "A reference rate is a bookkeeping rate, not what a bank will give you - "
                         "spreads and fees are on top.")
            return "\n".join(lines), artifact

        latest = self.data.latest(base, symbols=params.get("symbols"))
        artifact = {
            "summary": f"{DATASET_LATEST}: 1 {base} on {latest['date']} = "
                       + ", ".join(f"{code} {rate}" for code, rate in list(latest["rates"].items())[:5]),
            "dataset": DATASET_LATEST,
            "date": latest["date"],
            "base": latest["base"],
            "rates": latest["rates"],
        }
        lines = [f"1 {base} in ECB reference rates for {latest['date']}:"]
        for code, rate in list(latest["rates"].items())[:12]:
            lines.append(f"  • {code}: {rate}")
        if len(latest["rates"]) > 12:
            lines.append(f"  • ... {len(latest['rates']) - 12} more in the artifact")
        lines.append(f"\nSource: {DATASET_LATEST} (ECB reference rates via Frankfurter, read live). "
                     f"The date is the ECB's publication date - weekends and holidays have none, so "
                     f"{latest['date']} is often the last working day.")
        return "\n".join(lines), artifact

    def _history(self, params: dict) -> tuple[str, dict]:
        if not params.get("base"):
            return (
                "I need a base currency for the history - for example 'how did USD-EUR move over the "
                "last 30 days?'. Give me the two codes or a word like dollars.",
                {"summary": "no currency given", "dataset": DATASET_HISTORY, "known": False},
            )
        base = params["base"]
        symbols = params.get("symbols") or None
        history = self.data.history(params["start"], params["end"], base=base, symbols=symbols)
        artifact = {
            "summary": f"{history['dataset']} ({base}): {history['working_days']} working day(s), "
                       + "; ".join(f"{code} {stats['first']['rate']} to {stats['last']['rate']}"
                                   for code, stats in list(history["stats"].items())[:3]),
            "dataset": history["dataset"],
            "base": history["base"],
            "start_date": history["start_date"],
            "end_date": history["end_date"],
            "working_days": history["working_days"],
            "stats": history["stats"],
            "series": history["series"],
        }
        lines = [f"{base} against the ECB basket, {history['start_date']} to {history['end_date']} "
                 f"({history['working_days']} working day(s) with a rate):"]
        for code, stats in list(history["stats"].items())[:5]:
            direction = "up" if stats["change"] > 0 else "down" if stats["change"] < 0 else "flat"
            lines.append(f"  • {code}: {stats['first']['rate']} on {stats['first']['date']} to "
                         f"{stats['last']['rate']} on {stats['last']['date']} - {direction} "
                         f"{stats['change']:+g} ({stats['change_percent']:+g}%)")
            lines.append(f"      range {stats['min']} to {stats['max']}")
        lines.append(f"\nSource: {history['dataset']} (ECB reference rates via Frankfurter, read "
                     "live). Only working days have rows, so a short range can hold three points "
                     "- and a reference rate is not a trading rate.")
        return "\n".join(lines), artifact

    def _currencies(self, params: dict) -> tuple[str, dict]:
        listed = self.data.currencies()
        artifact = {
            "summary": f"{DATASET_CURRENCIES}: {len(listed['currencies'])} currency code(s)",
            "dataset": DATASET_CURRENCIES,
            "currencies": listed["currencies"],
        }
        lines = [f"{len(listed['currencies'])} currencies in the ECB reference series:"]
        lines.append("  " + ", ".join(f"{code} ({name})" for code, name in listed["currencies"].items()))
        lines.append(f"\nSource: {DATASET_CURRENCIES} (read live). The ECB publishes one reference "
                     "rate per currency per working day - anything not on this list has no reference "
                     "rate and I will not invent one.")
        return "\n".join(lines), artifact


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        FxAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
