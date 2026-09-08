"""Money-management rules: what the portfolio violates, and how big a trade may be.

Nothing here talks to Binance and nothing here executes. It takes numbers in and
returns verdicts and sizes out, which is what makes it testable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import RiskRules
from .portfolio import Portfolio


def _num(value: float, decimals: int = 0) -> str:
    return f"{value:,.{decimals}f}".replace(",", "\u202f")


@dataclass(frozen=True)
class Breach:
    rule: str
    detail: str
    severity: str  # "alerte" blocks new risk, "attention" is informational

    @property
    def blocking(self) -> bool:
        return self.severity == "alerte"


def average_true_range(klines: list[list], period: int) -> float:
    """Wilder's ATR over Binance kline rows [open_time, o, h, l, c, ...].

    Uses a simple mean of true ranges, which is the common approximation and is
    stable enough for sizing decisions a human reviews before acting.
    """
    if len(klines) < period + 1:
        raise ValueError(
            f"ATR({period}) exige au moins {period + 1} bougies, {len(klines)} recues."
        )
    true_ranges = []
    for previous, current in zip(klines[-(period + 1) : -1], klines[-period:]):
        high = float(current[2])
        low = float(current[3])
        previous_close = float(previous[4])
        true_ranges.append(
            max(high - low, abs(high - previous_close), abs(low - previous_close))
        )
    return sum(true_ranges) / len(true_ranges)


def round_to_step(quantity: float, step: float) -> float:
    """Round down to the exchange lot size. Rounding up could exceed the risk budget."""
    if step <= 0:
        return quantity
    steps = math.floor(quantity / step + 1e-9)
    decimals = max(0, -math.floor(math.log10(step))) if step < 1 else 0
    return round(steps * step, decimals + 2)


def symbol_filters(exchange_symbol: dict) -> tuple[float, float]:
    """Extract (lot step size, minimum notional) from a Binance symbol definition."""
    step = 0.0
    min_notional = 0.0
    for filter_ in exchange_symbol.get("filters", []):
        if filter_["filterType"] == "LOT_SIZE":
            step = float(filter_["stepSize"])
        elif filter_["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
            min_notional = float(filter_.get("minNotional", 0))
    return step, min_notional


def audit(portfolio: Portfolio, rules: RiskRules, high_water_mark: float) -> list[Breach]:
    """Every rule the current portfolio breaks, worst first."""
    breaches: list[Breach] = []

    if high_water_mark > 0:
        drawdown = max(0.0, (high_water_mark - portfolio.equity) / high_water_mark)
        if drawdown >= rules.max_drawdown_pct:
            breaches.append(
                Breach(
                    rule="drawdown",
                    detail=(
                        f"Repli de {drawdown:.1%} depuis le plus haut "
                        f"({_num(high_water_mark)} -> {_num(portfolio.equity)} "
                        f"{portfolio.valuation_asset}), limite {rules.max_drawdown_pct:.0%}. "
                        "Aucune nouvelle prise de risque proposee."
                    ),
                    severity="alerte",
                )
            )
        elif drawdown >= rules.max_drawdown_pct * 0.75:
            breaches.append(
                Breach(
                    rule="drawdown",
                    detail=(
                        f"Repli de {drawdown:.1%}, soit 75% de la limite "
                        f"({rules.max_drawdown_pct:.0%})."
                    ),
                    severity="attention",
                )
            )

    if portfolio.exposure_pct > rules.max_total_exposure_pct:
        breaches.append(
            Breach(
                rule="exposition_totale",
                detail=(
                    f"Exposition {portfolio.exposure_pct:.1%} au-dessus de la limite "
                    f"{rules.max_total_exposure_pct:.0%}. "
                    f"Exces: {_num(portfolio.exposure - rules.max_total_exposure_pct * portfolio.equity)} "
                    f"{portfolio.valuation_asset} a alleger."
                ),
                severity="alerte",
            )
        )

    for position in portfolio.positions:
        if position.is_stable:
            continue
        weight = position.value / portfolio.equity if portfolio.equity else 0.0
        if weight > rules.max_position_pct:
            excess = position.value - rules.max_position_pct * portfolio.equity
            breaches.append(
                Breach(
                    rule="concentration",
                    detail=(
                        f"{position.asset} pese {weight:.1%} du portefeuille, limite "
                        f"{rules.max_position_pct:.0%}. Exces: {_num(excess)} "
                        f"{portfolio.valuation_asset}."
                    ),
                    severity="alerte",
                )
            )

    breaches.sort(key=lambda b: 0 if b.blocking else 1)
    return breaches


@dataclass(frozen=True)
class Proposal:
    """A sizing proposal. It is advice; the human places the order."""

    symbol: str
    asset: str
    entry: float
    stop: float
    quantity: float
    notional: float
    risk_amount: float
    atr: float
    rejected: str | None = None

    @property
    def actionable(self) -> bool:
        return self.rejected is None and self.quantity > 0


def propose(
    *,
    symbol: str,
    asset: str,
    entry: float,
    atr: float,
    portfolio: Portfolio,
    rules: RiskRules,
    lot_step: float,
    min_notional: float,
    breaches: list[Breach],
) -> Proposal:
    """Size a position so that hitting the stop costs exactly risk_per_trade of equity."""
    blocking = [b for b in breaches if b.blocking]
    stop = entry - rules.atr_multiple * atr
    empty = Proposal(
        symbol=symbol, asset=asset, entry=entry, stop=stop,
        quantity=0.0, notional=0.0, risk_amount=0.0, atr=atr,
    )

    if blocking:
        return Proposal(
            **{**empty.__dict__, "rejected": (
                "Regles de risque en alerte, aucune nouvelle position: "
                + "; ".join(b.rule for b in blocking)
            )}
        )
    if stop <= 0:
        return Proposal(**{**empty.__dict__, "rejected": (
            f"Stop calcule negatif ({stop:.6f}): l'ATR ({atr:.6f}) est trop large "
            f"face au prix ({entry:.6f}). Reduisez atr_multiple."
        )})

    risk_budget = portfolio.equity * rules.risk_per_trade
    per_unit_risk = entry - stop
    raw_quantity = risk_budget / per_unit_risk

    # Never let the sized position break the per-asset concentration ceiling.
    current_value = portfolio.weight(asset) * portfolio.equity
    room = rules.max_position_pct * portfolio.equity - current_value
    if room <= 0:
        return Proposal(**{**empty.__dict__, "rejected": (
            f"{asset} occupe deja {portfolio.weight(asset):.1%} du portefeuille "
            f"(limite {rules.max_position_pct:.0%}): aucune place pour renforcer."
        )})
    raw_quantity = min(raw_quantity, room / entry)

    # And never let it break the total exposure ceiling.
    total_room = rules.max_total_exposure_pct * portfolio.equity - portfolio.exposure
    if total_room <= 0:
        return Proposal(**{**empty.__dict__, "rejected": (
            f"Exposition totale deja a {portfolio.exposure_pct:.1%} "
            f"(limite {rules.max_total_exposure_pct:.0%})."
        )})
    raw_quantity = min(raw_quantity, total_room / entry)

    # And never spend more cash than is actually available.
    if portfolio.cash <= 0:
        return Proposal(**{**empty.__dict__, "rejected": "Aucune liquidite disponible."})
    raw_quantity = min(raw_quantity, portfolio.cash / entry)

    quantity = round_to_step(raw_quantity, lot_step)
    notional = quantity * entry

    if quantity <= 0:
        return Proposal(**{**empty.__dict__, "rejected": (
            f"Quantite nulle apres arrondi au pas de lot ({lot_step})."
        )})
    if min_notional and notional < min_notional:
        return Proposal(**{**empty.__dict__, "rejected": (
            f"Notionnel {notional:.2f} sous le minimum Binance {min_notional:.2f} "
            f"pour {symbol}. Trade trop petit pour ce budget de risque."
        )})

    return Proposal(
        symbol=symbol,
        asset=asset,
        entry=entry,
        stop=stop,
        quantity=quantity,
        notional=notional,
        risk_amount=quantity * per_unit_risk,
        atr=atr,
    )
