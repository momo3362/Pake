"""Turn raw Binance balances into a valued portfolio."""

from __future__ import annotations

from dataclasses import dataclass

from .config import RiskRules


@dataclass(frozen=True)
class Position:
    asset: str
    quantity: float
    price: float          # in the valuation asset
    value: float          # quantity * price
    is_stable: bool

    @property
    def symbol(self) -> str | None:
        """The Binance pair used to value this asset, or None for the quote itself."""
        return None if self.price == 1.0 and self.is_stable else self.asset


@dataclass(frozen=True)
class Portfolio:
    positions: tuple[Position, ...]
    valuation_asset: str
    equity: float          # total value, stable + exposed
    exposure: float        # value of non-stable positions only
    cash: float            # value of stable positions
    ignored_dust: tuple[str, ...]

    @property
    def exposure_pct(self) -> float:
        return self.exposure / self.equity if self.equity else 0.0

    def weight(self, asset: str) -> float:
        if not self.equity:
            return 0.0
        for position in self.positions:
            if position.asset == asset:
                return position.value / self.equity
        return 0.0


def _resolve_price(asset: str, prices: dict[str, float], quote: str) -> float | None:
    """Price of `asset` expressed in `quote`, via a direct or inverted pair."""
    if asset == quote:
        return 1.0
    direct = prices.get(f"{asset}{quote}")
    if direct:
        return direct
    inverse = prices.get(f"{quote}{asset}")
    if inverse:
        return 1.0 / inverse
    return None


def build_portfolio(
    balances: list[dict], prices: dict[str, float], rules: RiskRules
) -> tuple[Portfolio, tuple[str, ...]]:
    """Value every non-zero balance. Returns the portfolio and unpriceable assets."""
    quote = rules.valuation_asset
    positions: list[Position] = []
    dust: list[str] = []
    unpriced: list[str] = []

    for balance in balances:
        asset = balance["asset"]
        quantity = float(balance.get("free", 0)) + float(balance.get("locked", 0))
        if quantity <= 0:
            continue

        price = _resolve_price(asset, prices, quote)
        if price is None:
            unpriced.append(asset)
            continue

        value = quantity * price
        if value < rules.dust_threshold:
            dust.append(asset)
            continue

        positions.append(
            Position(
                asset=asset,
                quantity=quantity,
                price=price,
                value=value,
                is_stable=asset in rules.stable_assets,
            )
        )

    positions.sort(key=lambda p: p.value, reverse=True)
    cash = sum(p.value for p in positions if p.is_stable)
    exposure = sum(p.value for p in positions if not p.is_stable)

    portfolio = Portfolio(
        positions=tuple(positions),
        valuation_asset=quote,
        equity=cash + exposure,
        exposure=exposure,
        cash=cash,
        ignored_dust=tuple(sorted(dust)),
    )
    return portfolio, tuple(sorted(unpriced))
