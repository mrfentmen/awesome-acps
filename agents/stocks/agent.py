"""Stocks - what a share costs, and how it moved.

`fx` answers exchange rates and `ledger` answers government debt; nothing here answers "what is
Apple trading at?" or "how has Tesla moved this month?". This agent does, from Yahoo's public
chart endpoint.

It is the only agent in this repo reading a source with no published API behind it: Yahoo calls
that endpoint from its own web page, and nobody promises it will keep answering. Every answer
says so in one line, and a refusal is reported as a refusal - never as a stale price.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from data import DATASET, StockData, StockError, money  # noqa: E402

HELP = (
    "I read Yahoo Finance's public chart endpoint (keyless). Ask me:\n"
    "  - what is Apple trading at?\n"
    "  - price of MSFT\n"
    "  - how has Tesla moved this month?\n"
    "  - what is NVDA's 52 week range?\n"
    "  - what stock is symbol BRK-B?\n"
    "One honest note that comes with every answer: that endpoint is the one Yahoo's own page "
    "calls, it is not a supported API, and it can change without notice."
)

PERMISSION_KEY = "stocks-read-yahoo-chart"

SKILLS = ("stock-quote", "stock-history", "help")

_COMPANY_IN = re.compile(r"\b(?:of|for|at)\s+([A-Za-z][A-Za-z0-9 .&'\-]{1,30}?)(?=\s+(?:right now|"
                         r"now|today|this (?:month|week|year)|trading|worth)\b|[?.!]|$)",
                         re.IGNORECASE)
_WHAT_IS = re.compile(r"\bwhat(?:'s| is)\s+([A-Za-z][A-Za-z0-9 .&'\-]{1,30}?)(?=\s+(?:trading|"
                      r"worth|at|doing|right now|now|today)\b|[?.!]|$)", re.IGNORECASE)
_SHARE_WORDS = re.compile(r"\b(share|stock|equity|ticker|symbol|market|trading|price|quote|"
                          r"nasdaq|nyse|etf|fund|index|52 week|52-week)\b", re.IGNORECASE)
_HISTORY = re.compile(r"\b(moved?|moving|performance|history|historical|chart|trend|up|down|"
                      r"gain|loss)\b", re.IGNORECASE)
#: Words that ask for the price now. "what is AAPL doing right now?" names no market word at all,
#: so without these it fell through to help and answered with its own instructions.
_QUOTE_WORDS = re.compile(r"\b(doing|trading|worth|value|cost|going for|at|now|right now|today|"
                          r"quote|last)\b", re.IGNORECASE)
#: Label words that ride along in a phrase but are not part of a name: "NVDA's 52 week range" is
#: NVDA. "at" stays out of this set: "trading at" is a question, not a name.
_NOISE = {"52", "week", "range", "price", "stock", "shares", "share", "quote", "value",
          "today", "now", "right", "doing", "trading", "worth", "cost", "the", "of", "for",
          "a", "an"}
_DAYS = re.compile(r"\b(?:this|last|past|the last)\s+(week|month|quarter|year)\b", re.IGNORECASE)
_DAYS_N = re.compile(r"\b(?:last|past|for)\s+(\d{1,3})\s+days?\b", re.IGNORECASE)
_TICKER = re.compile(r"\b([A-Z]{1,5}(?:[.\-][A-Z])?)\b")
_NOT_TICKERS = {"I", "A", "AN", "THE", "IS", "AT", "IT", "AND", "OR", "USD", "EUR", "GBP",
                "OK", "HI", "US", "UK", "EU", "AI", "ETF", "CEO", "GDP", "HAS", "HOW", "NOW",
                "WHAT", "WHY", "WHO", "PRICE"}


def tidy_instrument(phrase: str) -> str:
    """Drop the label words and the possessive from a captured phrase."""
    words = [word for word in re.split(r"[\s]+", str(phrase or "").replace("'s", " ")) if word]
    kept = [word for word in words if word.lower().strip(".,?!") not in _NOISE]
    return " ".join(kept).strip(" .,?!")[:30]


def instrument_from_text(text: str) -> str | None:
    """The company or ticker asked about, or None."""
    work = str(text or "").strip()
    for pattern in (_COMPANY_IN, _WHAT_IS):
        match = pattern.search(work)
        if not match:
            continue
        phrase = match.group(1).strip(" .,?!")
        if len(phrase.split()) > 4:
            continue
        tidied = tidy_instrument(phrase)
        if tidied:
            return tidied
    tickers = [token for token in _TICKER.findall(work) if token not in _NOT_TICKERS]
    if tickers:
        return tickers[0]
    lowered = re.search(r"\b(apple|tesla|microsoft|nvidia|amazon|google|alphabet|meta|netflix|"
                        r"intel|amd|boeing|ford|disney|walmart|costco|oracle|salesforce)\b",
                        work, re.IGNORECASE)
    return lowered.group(1) if lowered else None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    stripped = str(text or "").strip()
    if not stripped or re.search(r"\b(help|what can you do|commands?)\b", stripped, re.IGNORECASE):
        return "help", {}
    instrument = instrument_from_text(stripped)
    # "how has Tesla moved this month?" names no market word at all: the instrument plus a
    # move word is enough to know what is being asked.
    if not (_SHARE_WORDS.search(stripped) or _HISTORY.search(stripped)
            or (instrument and _QUOTE_WORDS.search(stripped))):
        return "help", {}
    if not instrument:
        return "help", {}
    params: dict = {"instrument": instrument}
    if _HISTORY.search(stripped):
        window = _DAYS.search(stripped)
        days = {"week": 5, "month": 30, "quarter": 90, "year": 365}.get(
            window.group(1).lower() if window else "", 30)
        exact = _DAYS_N.search(stripped)
        if exact:
            days = int(exact.group(1))
        params["days"] = days
        return "stock-history", params
    return "stock-quote", params


def render_quote(reading: dict) -> str:
    """One quote, with the ranges that make a bare price mean something."""
    found = reading["instrument"]
    currency = reading["currency"]
    lines = [f"{found['name']} ({found['symbol']}, {found['kind']} on {reading['exchange']}): "
             f"{money(reading['price'], currency)}"]
    if reading["change_percent"] is not None:
        direction = "up" if float(reading["change_percent"]) >= 0 else "down"
        lines.append(f"  {direction} {abs(float(reading['change_percent'])):.2f}% on the day"
                     + (f", previous close {money(reading['previous_close'], currency)}"
                        if reading["previous_close"] else ""))
    if reading["day_low"] and reading["day_high"]:
        lines.append(f"  day range {money(reading['day_low'], currency)} - "
                     f"{money(reading['day_high'], currency)}")
    if reading["week52_low"] and reading["week52_high"]:
        lines.append(f"  52 week range {money(reading['week52_low'], currency)} - "
                     f"{money(reading['week52_high'], currency)}")
    if reading["volume"]:
        lines.append(f"  volume {money(reading['volume'])}")
    if found["sector"]:
        lines.append(f"  {found['sector']} / {found['industry']}")
    if reading["at"]:
        lines.append(f"  quoted at {reading['at']} ({reading['timezone']})")
    return "\n".join(lines)


def render_history(reading: dict) -> str:
    """How it moved over the window, from the closes the service reported."""
    found = reading["instrument"]
    currency = reading["currency"]
    change = reading["change_percent"]
    direction = "up" if (change or 0) >= 0 else "down"
    lines = [f"{found['name']} ({found['symbol']}) over the last {reading['range']} "
             f"({reading['points']} prices):"]
    lines.append(f"  {money(reading['first'], currency)} -> {money(reading['last'], currency)}"
                 + (f"  {direction} {abs(float(change)):.2f}%" if change is not None else ""))
    lines.append(f"  high {money(reading['high'], currency)}, low {money(reading['low'], currency)}")
    lines.append(f"  {reading['from']} to {reading['to']}")
    return "\n".join(lines)


class StockAgent(AcpAgent):
    name = "stocks"
    title = "Stocks - share quotes and moves from Yahoo's public endpoint"
    version = "1.0.0"

    def __init__(self, connection=None, data: StockData | None = None) -> None:
        super().__init__(connection)
        self.data = data or StockData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Stocks session opened. " + HELP.splitlines()[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Find the symbol, then read the quote", "medium"),
            ("Report the price with its ranges and the time it was quoted", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("A price with no range and no time is not information. I always bring "
                        "both, plus the note about the endpoint behind them.")
            return STOP_END_TURN

        tool = f"call_{skill.replace('-', '_')}"
        ctx.tool_call(tool, f"Read {params['instrument']}", kind="fetch", name=skill,
                      raw_input=dict(params))
        if not ctx.ask_permission(tool, "Allow Stocks to read the public quote endpoint?",
                                  remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the quote endpoint first.")
            return STOP_REFUSAL
        ctx.tool_call_update(tool, status="in_progress")

        try:
            if skill == "stock-history":
                reading = self.data.history(params["instrument"], params.get("days") or 30)
                body = render_history(reading)
                summary = f"{reading['points']} prices for {reading['instrument']['symbol']}"
            else:
                reading = self.data.quote(params["instrument"])
                body = render_quote(reading)
                summary = (f"{reading['instrument']['symbol']} at "
                           f"{money(reading['price'], reading['currency'])}")
        except (StockError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read that quote: {exc}")
            return STOP_END_TURN

        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(summary))
        ctx.stream_text(body + "\n")
        ctx.message(f"\nSource: {DATASET}, read live. That endpoint is what Yahoo's own page "
                    "calls: it needs no key, it carries no guarantee, and this is a quote rather "
                    "than advice.")
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        return None  # one read, one answer


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        StockAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
