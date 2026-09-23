"""Number formatting for alert messages, kept in one place.

An alert is read once, in a hurry, often on a phone: 41112118151111.92 is unreadable and
"41.1 trillion" is not. Thresholds and readings are formatted the same way here so a message
can never say "41.1T" and then "above your threshold of 41112118151111.92".
"""

from __future__ import annotations


def number(value, unit: str = "") -> str:
    """A number a person can read: 40112118151111.92 -> '40.1 trillion'."""
    if value is None:
        return "n/a"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "n/a"
    magnitude = abs(amount)
    if magnitude >= 1_000_000_000_000:
        return f"{amount / 1_000_000_000_000:.1f} trillion{unit}"
    if magnitude >= 1_000_000_000:
        return f"{amount / 1_000_000_000:.1f} billion{unit}"
    if magnitude >= 1_000_000:
        return f"{amount / 1_000_000:.1f} million{unit}"
    if magnitude >= 1_000:
        return f"{amount:,.0f}{unit}"
    if amount.is_integer():
        return f"{amount:.0f}{unit}"
    return f"{amount:g}{unit}"
