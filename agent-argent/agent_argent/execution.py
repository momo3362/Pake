"""Order placement. Nothing here decides what to trade or how big.

Two invariants:

  * Dry run is the default. Real orders require an explicit `live=True`, which
    the CLI only passes when you type --reel.
  * Every entry is followed by a protective OCO exit. If the exit cannot be
    placed, the entry is unwound at market immediately. An unprotected position
    is the one outcome this module treats as an emergency.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .binance import BinanceError, BinanceTradingClient
from .risk import symbol_filters


def _tick_size(definition: dict) -> float:
    for filter_ in definition.get("filters", []):
        if filter_["filterType"] == "PRICE_FILTER":
            return float(filter_["tickSize"])
    return 0.0


def round_to_tick(price: float, tick: float, *, up: bool = False) -> str:
    """Snap a price to the exchange tick grid, as a plain decimal string.

    Binance rejects scientific notation, which is what f-string formatting of
    small tick sizes produces if you are not careful.
    """
    if tick <= 0:
        return f"{price:.8f}".rstrip("0").rstrip(".")
    steps = price / tick
    steps = math.ceil(steps - 1e-9) if up else math.floor(steps + 1e-9)
    decimals = max(0, -math.floor(math.log10(tick))) if tick < 1 else 0
    return f"{steps * tick:.{decimals}f}"


def format_quantity(quantity: float, step: float) -> str:
    if step <= 0:
        return f"{quantity:.8f}".rstrip("0").rstrip(".")
    steps = math.floor(quantity / step + 1e-9)
    decimals = max(0, -math.floor(math.log10(step))) if step < 1 else 0
    return f"{steps * step:.{decimals}f}"


@dataclass
class Execution:
    ok: bool
    dry_run: bool
    symbol: str
    detail: str
    entry_price: float = 0.0
    quantity: str = "0"
    spent: float = 0.0
    take_profit: str = ""
    stop_price: str = ""
    protected: bool = False
    order_id: str = ""

    def as_journal_entry(self, reason: str) -> dict:
        return {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "symbol": self.symbol,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "spent": round(self.spent, 2),
            "take_profit": self.take_profit,
            "stop_price": self.stop_price,
            "protected": self.protected,
            "dry_run": self.dry_run,
            "order_id": self.order_id,
            "reason": reason,
            "realised_pnl": 0.0,
        }


def _filled(response: dict) -> tuple[float, float]:
    """Average fill price and base quantity from a FULL order response."""
    fills = response.get("fills") or []
    quantity = float(response.get("executedQty", 0) or 0)
    quote = float(response.get("cummulativeQuoteQty", 0) or 0)
    if quantity <= 0:
        raise BinanceError(f"Ordre non execute: {response}")
    if quote > 0:
        return quote / quantity, quantity
    if fills:
        total = sum(float(f["price"]) * float(f["qty"]) for f in fills)
        return total / quantity, quantity
    raise BinanceError(f"Reponse d'ordre sans prix exploitable: {response}")


def open_protected_position(
    client: BinanceTradingClient,
    *,
    symbol: str,
    spend_quote: float,
    take_profit_pct: float,
    stop_loss_pct: float,
    reference_price: float,
    live: bool,
) -> Execution:
    """Buy at market for `spend_quote`, then immediately protect with an OCO."""
    definition = client.exchange_info(symbol)
    lot_step, min_notional = symbol_filters(definition)
    tick = _tick_size(definition)

    if min_notional and spend_quote < min_notional:
        return Execution(
            ok=False, dry_run=not live, symbol=symbol,
            detail=(
                f"Montant {spend_quote:.2f} sous le minimum Binance "
                f"{min_notional:.2f} pour {symbol}."
            ),
        )

    if not live:
        quantity = format_quantity(spend_quote / reference_price, lot_step)
        return Execution(
            ok=True, dry_run=True, symbol=symbol,
            detail="SIMULATION — aucun ordre envoye",
            entry_price=reference_price,
            quantity=quantity,
            spent=spend_quote,
            take_profit=round_to_tick(reference_price * (1 + take_profit_pct), tick, up=True),
            stop_price=round_to_tick(reference_price * (1 - stop_loss_pct), tick),
            protected=True,
            order_id="dry-run",
        )

    client_id = f"aa{uuid.uuid4().hex[:20]}"
    entry = client.market_buy(symbol, spend_quote, client_id)
    price, quantity = _filled(entry)
    quantity_str = format_quantity(quantity, lot_step)

    take_profit = round_to_tick(price * (1 + take_profit_pct), tick, up=True)
    stop_trigger = round_to_tick(price * (1 - stop_loss_pct), tick)
    # The limit sits below the trigger so the stop still fills in a fast drop.
    stop_limit = round_to_tick(price * (1 - stop_loss_pct * 1.5), tick)

    try:
        client.protective_oco_sell(
            symbol=symbol,
            quantity=quantity_str,
            take_profit=take_profit,
            stop_trigger=stop_trigger,
            stop_limit=stop_limit,
            client_id=f"oco{uuid.uuid4().hex[:18]}",
        )
    except BinanceError as protective_error:
        # An unprotected position is worse than no position. Unwind it now.
        try:
            time.sleep(0.3)
            client.market_sell(symbol, quantity_str, f"unw{uuid.uuid4().hex[:18]}")
            unwound = "position revendue au marche immediatement"
        except BinanceError as unwind_error:
            unwound = (
                f"ECHEC DU DEBOUCLAGE: {unwind_error}. "
                f"POSITION NON PROTEGEE DE {quantity_str} {symbol} — "
                "intervention manuelle requise immediatement"
            )
        return Execution(
            ok=False, dry_run=False, symbol=symbol,
            detail=f"Stop impossible a placer ({protective_error}); {unwound}",
            entry_price=price, quantity=quantity_str, spent=spend_quote,
            protected=False, order_id=str(entry.get("orderId", "")),
        )

    return Execution(
        ok=True, dry_run=False, symbol=symbol,
        detail="Position ouverte et protegee par un OCO",
        entry_price=price,
        quantity=quantity_str,
        spent=spend_quote,
        take_profit=take_profit,
        stop_price=stop_trigger,
        protected=True,
        order_id=str(entry.get("orderId", "")),
    )
