"""Signal generation for short round trips.

HONEST STATEMENT OF WHAT THIS IS. This is a mean-reversion filter, not an edge.
It buys a whitelisted pair when price has dropped meaningfully below its own
recent average while the longer trend is still up, and it does nothing the rest
of the time. It is transparent and auditable, which is the point: you can read
every condition and change it.

It has NOT been backtested against your data, because there is none yet. Until
`journal.json` holds a few hundred round trips, the parameters below are my
guesses, not measurements. Run it with --reel absent (simulation) first and
compare the journal against reality before arming anything.

The strategy answers "is now a reasonable moment", never "how much". Sizing is
guards.py's job, and it is the guards that hold the 300 EUR ceilings.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Signal:
    symbol: str
    asset: str
    price: float
    reason: str
    strength: float  # 0..1, used only to rank candidates when several fire


def simple_moving_average(closes: list[float], period: int) -> float:
    if len(closes) < period:
        raise ValueError(f"SMA({period}) exige {period} cloture(s), {len(closes)} recue(s).")
    return sum(closes[-period:]) / period


def closes_from_klines(klines: list[list]) -> list[float]:
    return [float(row[4]) for row in klines]


def relative_strength_index(closes: list[float], period: int = 14) -> float:
    """Wilder's RSI. Returns 50.0 for a perfectly flat series rather than dividing by zero."""
    if len(closes) < period + 1:
        raise ValueError(f"RSI({period}) exige {period + 1} clotures, {len(closes)} recues.")
    gains = 0.0
    losses = 0.0
    for previous, current in zip(closes[-(period + 1) : -1], closes[-period:]):
        change = current - previous
        if change >= 0:
            gains += change
        else:
            losses -= change
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    rs = (gains / period) / (losses / period)
    return 100.0 - (100.0 / (1.0 + rs))


def evaluate(
    symbol: str,
    asset: str,
    klines: list[list],
    *,
    fast_period: int = 20,
    slow_period: int = 50,
    rsi_period: int = 14,
    oversold: float = 32.0,
    min_dip_pct: float = 0.012,
) -> Signal | None:
    """Return a buy signal, or None when no condition is met.

    Conditions, all of which must hold:
      1. The longer trend is up   — fast SMA above slow SMA.
      2. Price has dipped         — at least `min_dip_pct` below the fast SMA.
      3. The dip looks exhausted  — RSI below `oversold`.
      4. The dip is not a collapse — price still above the slow SMA.

    Condition 4 is what keeps this from buying every step of a crash.
    """
    closes = closes_from_klines(klines)
    if len(closes) < slow_period + 1:
        return None

    price = closes[-1]
    fast = simple_moving_average(closes, fast_period)
    slow = simple_moving_average(closes, slow_period)
    rsi = relative_strength_index(closes, rsi_period)

    if fast <= slow:
        return None
    dip = (fast - price) / fast
    if dip < min_dip_pct:
        return None
    if rsi >= oversold:
        return None
    if price <= slow:
        return None

    # Strength blends how deep the dip is and how oversold it reads, purely to
    # rank candidates when the whitelist produces several at once.
    strength = min(1.0, (dip / (min_dip_pct * 3)) * 0.5 + ((oversold - rsi) / oversold) * 0.5)
    return Signal(
        symbol=symbol,
        asset=asset,
        price=price,
        reason=(
            f"repli de {dip:.2%} sous SMA{fast_period}, RSI({rsi_period})={rsi:.1f} "
            f"< {oversold}, tendance haussiere (SMA{fast_period} > SMA{slow_period}), "
            f"prix au-dessus de SMA{slow_period}"
        ),
        strength=round(strength, 3),
    )
