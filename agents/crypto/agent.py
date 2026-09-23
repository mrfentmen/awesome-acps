"""Crypto - what a coin costs, and how big DeFi is.

The repo already answers "what is 100 USD in EUR?" (`fx`) and "what is the national debt?"
(`ledger`). Neither touches crypto, which is a different market with different rules - open 24
hours, unregulated, and moving while the rest of the world sleeps - so this agent says the
observation time and the 24 hour range with every price instead of printing a bare number.

Coin lookup goes through CoinGecko's own search and refuses to guess. "zzzzzznothing" is an
error, not the nearest coin.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp_kit import STOP_END_TURN, STOP_REFUSAL, AcpAgent, SessionContext, prompt_text  # noqa: E402

from data import DATASET, DEFI_DATASET, CryptoData, CryptoError, money  # noqa: E402

HELP = (
    "I read CoinGecko and DefiLlama (both keyless). Ask me:\n"
    "  - what is bitcoin worth right now?\n"
    "  - price of ethereum in EUR\n"
    "  - how much is doge?\n"
    "  - top 5 coins\n"
    "  - top defi protocols\n"
    "  - how big is defi in total?\n"
    "Every price comes with the 24 hour range, the 24 hour change and the time the market "
    "reported it. These markets never close, so the time matters."
)

PERMISSION_KEY = "crypto-read-markets"

SKILLS = ("crypto-price", "crypto-top", "defi-top", "help")

_COIN_RE = re.compile(r"\b(?:price|worth|value|cost)\s+(?:of\s+)?(?:a\s+)?(?:one\s+)?"
                      r"([a-z][a-z0-9 .\-]{1,24}?)(?=\s+(?:in|right now|now|today|usd|eur|"
                      r"gbp|jpy)\b|[?.!]|$)", re.IGNORECASE)
_COIN_ALT = re.compile(r"\b(?:how much is|what is)\s+([a-z][a-z0-9 .\-]{1,24}?)(?=\s+(?:worth|in|"
                       r"right now|now|today)\b|[?.!]|$)", re.IGNORECASE)
_VS_RE = re.compile(r"\b(?:in|versus|vs\.?|against)\s+([a-z]{3})\b", re.IGNORECASE)
_DEFI_WORDS = re.compile(r"\b(defi|decentralized finance|protocols?|tvl|total value locked)\b",
                         re.IGNORECASE)
_TOP_RE = re.compile(r"\b(?:top|biggest|largest|best|leading|ranking)\b", re.IGNORECASE)
_TOTAL_RE = re.compile(r"\b(total|altogether|all of it|how big|overall)\b", re.IGNORECASE)
_LIMIT_RE = re.compile(r"\b(?:top|first|biggest|largest)\s+(\d{1,2})\b|\b(\d{1,2})\s+(?:top|"
                       r"biggest|largest|coins?|protocols?)\b", re.IGNORECASE)
#: An explicit "price of X" / "what is X worth" counts as a crypto question: the agent is
#: invoked on purpose, and an unknown name gets the honest "no coin called that" instead of a
#: silent fallback to the help text.
_CRYPTO_WORDS = re.compile(r"\b(bitcoin|btc|ethereum|eth|crypto|coins?|tokens?|defi|tvl|doge|"
                           r"solana|sol|cardano|ada|xrp|ripple|bnb|binance|usdt|usdc|polkadot|"
                           r"chainlink|avax|monero|currency|protocol|price|worth|value)\b",
                           re.IGNORECASE)
_COIN_STOPWORDS = {"the", "a", "an", "it", "one", "that", "this", "right", "now",
                   "price", "worth", "value", "crypto", "coin", "today"}


def coin_from_text(text: str) -> str | None:
    """The coin asked about, or None."""
    work = str(text or "").strip()
    for pattern in (_COIN_RE, _COIN_ALT):
        match = pattern.search(work)
        if not match:
            continue
        phrase = match.group(1).strip(" .,?!").lower()
        words = [word for word in phrase.split() if word not in _COIN_STOPWORDS]
        if words:
            return " ".join(words)[:24]
    match = re.search(r"\b(bitcoin|btc|ethereum|eth|doge|dogecoin|solana|sol|cardano|ada|xrp|"
                      r"ripple|bnb|usdt|usdc|polkadot|chainlink|avax|monero|monero)\b",
                      work, re.IGNORECASE)
    return match.group(1).lower() if match else None


def route(text: str) -> tuple[str, dict]:
    """Deterministic intent routing: (skill, params). Pure function, easy to test."""
    stripped = str(text or "").strip()
    if not stripped or re.search(r"\b(help|what can you do|commands?)\b", stripped, re.IGNORECASE):
        return "help", {}
    if not _CRYPTO_WORDS.search(stripped):
        return "help", {}
    limit = _LIMIT_RE.search(stripped)
    count = None
    if limit:
        count = int(limit.group(1) or limit.group(2))

    if _DEFI_WORDS.search(stripped) and not coin_from_text(stripped):
        params = {}
        if count:
            params["limit"] = count
        elif _TOTAL_RE.search(stripped):
            params["total"] = True
        return "defi-top", params
    if _TOP_RE.search(stripped) and not coin_from_text(stripped):
        return "crypto-top", {"limit": count or 10}
    coin = coin_from_text(stripped)
    if coin:
        params = {"coin": coin}
        vs = _VS_RE.search(stripped)
        if vs:
            params["vs"] = vs.group(1).lower()
        elif re.search(r"\beur(o|os)?\b", stripped, re.IGNORECASE):
            params["vs"] = "eur"
        elif re.search(r"\bgbp\b|\bpounds?\b", stripped, re.IGNORECASE):
            params["vs"] = "gbp"
        return "crypto-price", params
    if _TOP_RE.search(stripped):
        return "crypto-top", {"limit": count or 10}
    return "help", {}


def render_price(reading: dict) -> str:
    """One coin, its range, its change and when the market said so."""
    coin = reading["coin"]
    lines = [f"{coin['name']} ({coin['symbol']}) - {money(reading['price'], reading['currency'])}"]
    if reading["change"] is not None:
        direction = "up" if reading["change"] >= 0 else "down"
        lines.append(f"  24h: {direction} {abs(float(reading['change'])):.2f}%"
                     f"  range {money(reading['low'], reading['currency']).strip()}"
                     f" - {money(reading['high'], reading['currency']).strip()}")
    if reading["market_cap"]:
        lines.append(f"  market cap {money(reading['market_cap'], reading['currency'])}"
                     + (f", rank {reading['rank']}" if reading["rank"] else ""))
    if reading["at"]:
        lines.append(f"  the market reported this at {reading['at']}")
    return "\n".join(lines)


class CryptoAgent(AcpAgent):
    name = "crypto"
    title = "Crypto - coin prices and DeFi totals, keyless"
    version = "1.0.0"

    def __init__(self, connection=None, data: CryptoData | None = None) -> None:
        super().__init__(connection)
        self.data = data or CryptoData()

    # -- ACP ---------------------------------------------------------------

    def new_session(self, session) -> dict:
        session.remember("agent", "Crypto session opened. " + HELP.splitlines()[0])
        return {}

    def prompt(self, ctx: SessionContext, prompt: list[dict]) -> str:
        text = prompt_text(prompt)
        skill, params = route(text)
        ctx.plan([
            (f"Route the request ({skill})", "high"),
            ("Read the live market", "medium"),
            ("Report the number with its range, change and observation time", "medium"),
        ])

        if skill == "help":
            ctx.stream_text(HELP)
            ctx.message("I do not guess a coin: an unknown name is an error, and every price "
                        "carries the time the market reported it.")
            return STOP_END_TURN

        tool = f"call_{skill.replace('-', '_')}"
        ctx.tool_call(tool, f"Read {DATASET if skill != 'defi-top' else DEFI_DATASET}",
                      kind="fetch", name=skill, raw_input=dict(params))
        if not ctx.ask_permission(tool, "Allow Crypto to read the public market feeds?",
                                  remember_key=PERMISSION_KEY):
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content("permission denied"))
            ctx.message("I need permission to read the market feeds first.")
            return STOP_REFUSAL
        ctx.tool_call_update(tool, status="in_progress")

        try:
            if skill == "crypto-price":
                reading = self.data.price(params["coin"], params.get("vs") or "usd")
                body = render_price(reading)
                summary = f"{reading['coin']['symbol']} at {money(reading['price'], reading['currency'])}"
            elif skill == "crypto-top":
                reading = self.data.top(params.get("limit") or 10)
                body = "\n".join(
                    f"  {row['rank']:>2}. {row['name']} ({row['symbol']})"
                    f"  {money(row['price'], 'USD')}"
                    + (f"  {float(row['change']):+.2f}% 24h" if row["change"] is not None else "")
                    for row in reading["rows"])
                summary = f"{len(reading['rows'])} coins read"
            else:
                reading = self.data.defi(params.get("limit") or 10)
                body = "\n".join(
                    f"  {index + 1:>2}. {row['name']} ({row['category']})"
                    f"  TVL {money(row['tvl'], 'USD')}"
                    + (f"  {float(row['change']):+.2f}% 24h" if row["change"] is not None else "")
                    for index, row in enumerate(reading["rows"]))
                if params.get("total"):
                    body = (f"DeFi total value locked across {reading['protocols']} protocols: "
                            f"{money(reading['total'], 'USD')}\n" + body)
                summary = f"{reading['protocols']} protocols, TVL {money(reading['total'], 'USD')}"
        except (CryptoError, ValueError) as exc:
            ctx.tool_call_update(tool, status="failed", content=ctx.text_content(str(exc)))
            ctx.message(f"I could not read the market: {exc}")
            return STOP_END_TURN

        ctx.tool_call_update(tool, status="completed", content=ctx.text_content(summary))
        ctx.stream_text(body + "\n")
        ctx.message(f"\nSource: {DATASET if skill != 'defi-top' else DEFI_DATASET}, read live. "
                    "Crypto trades 24/7 and this is a market price, not advice.")
        return STOP_END_TURN

    def on_cancel(self, session) -> None:
        return None  # one read, one answer


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level="INFO", stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        CryptoAgent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
