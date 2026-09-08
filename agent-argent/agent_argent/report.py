"""Human-readable report. Plain text so it survives mail, SMS and the gateway."""

from __future__ import annotations

from datetime import datetime, timezone

from .config import RiskRules
from .portfolio import Portfolio
from .risk import Breach, Proposal


def _num(value: float, decimals: int = 2) -> str:
    """Thousands separated by a non-breaking-safe space, as French usage expects."""
    return f"{value:,.{decimals}f}".replace(",", "\u202f")


def _money(value: float, asset: str) -> str:
    return f"{_num(value)} {asset}"


def render_balance(
    portfolio: Portfolio,
    breaches: list[Breach],
    rules: RiskRules,
    high_water_mark: float,
    previous_equity: float | None,
    unpriced: tuple[str, ...],
    display_rate: float | None = None,
) -> str:
    quote = portfolio.valuation_asset
    lines = [
        "BILAN — agent-argent (lecture seule)",
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "",
        f"Capital total   : {_money(portfolio.equity, quote)}",
    ]
    if display_rate:
        lines.append(
            f"                  soit {_money(portfolio.equity * display_rate, rules.display_asset)}"
        )
    lines += [
        f"  dont liquide  : {_money(portfolio.cash, quote)} ({portfolio.cash / portfolio.equity:.1%})"
        if portfolio.equity
        else "  dont liquide  : 0",
        f"  dont expose   : {_money(portfolio.exposure, quote)} ({portfolio.exposure_pct:.1%}"
        f", limite {rules.max_total_exposure_pct:.0%})",
    ]

    if previous_equity:
        delta = portfolio.equity - previous_equity
        sign = "+" if delta >= 0 else ""
        lines.append(
            f"Variation       : {sign}{_money(delta, quote)} "
            f"({sign}{delta / previous_equity:.2%}) depuis le dernier bilan"
        )
    if high_water_mark > 0 and portfolio.equity < high_water_mark:
        drawdown = (high_water_mark - portfolio.equity) / high_water_mark
        lines.append(
            f"Repli           : -{drawdown:.2%} sous le plus haut "
            f"({_money(high_water_mark, quote)})"
        )

    lines += ["", "POSITIONS"]
    if not portfolio.positions:
        lines.append("  (aucune position au-dessus du seuil de poussiere)")
    for position in portfolio.positions:
        weight = position.value / portfolio.equity if portfolio.equity else 0.0
        flag = "" if position.is_stable or weight <= rules.max_position_pct else "  <-- concentre"
        lines.append(
            f"  {position.asset:<8} {_num(position.quantity, 8):>18}"
            + f"  {_money(position.value, quote):>20}  {weight:>6.1%}{flag}"
        )

    lines += ["", "REGLES DE RISQUE"]
    if not breaches:
        lines.append("  Toutes les regles sont respectees.")
    for breach in breaches:
        marker = "[ALERTE]  " if breach.blocking else "[attention] "
        lines.append(f"  {marker}{breach.rule}: {breach.detail}")

    if unpriced:
        lines += [
            "",
            f"Non valorises (aucune paire vers {quote}) : {', '.join(unpriced)}",
        ]
    if portfolio.ignored_dust:
        lines.append(
            f"Poussiere ignoree (< {rules.dust_threshold} {quote}) : "
            + ", ".join(portfolio.ignored_dust)
        )

    lines += [
        "",
        "Cet agent ne passe aucun ordre. Toute execution reste manuelle.",
    ]
    return "\n".join(lines)


def render_proposal(proposal: Proposal, rules: RiskRules, quote: str) -> str:
    lines = [
        f"PROPOSITION — {proposal.symbol} (a valider et executer manuellement)",
        "",
        f"  Prix de reference : {_num(proposal.entry, 6)}",
        f"  ATR({rules.atr_period})           : {_num(proposal.atr, 6)}",
        f"  Stop suggere      : {_num(proposal.stop, 6)}"
        f"  (-{rules.atr_multiple}x ATR, soit "
        f"-{(proposal.entry - proposal.stop) / proposal.entry:.2%})",
    ]
    if proposal.rejected:
        lines += ["", f"AUCUNE PROPOSITION : {proposal.rejected}"]
        return "\n".join(lines)

    lines += [
        f"  Quantite          : {_num(proposal.quantity, 8)} {proposal.asset}",
        f"  Montant engage    : {_money(proposal.notional, quote)}",
        f"  Perte si stop     : {_money(proposal.risk_amount, quote)}"
        f"  ({rules.risk_per_trade:.1%} du capital)",
        "",
        "Ordre a saisir vous-meme sur Binance :",
        f"  1. Achat de {_num(proposal.quantity, 8)} {proposal.asset} "
        f"(au marche, ou limite a {_num(proposal.entry, 6)})",
        f"  2. Stop-loss immediat a {_num(proposal.stop, 6)} sur la totalite",
        "",
        "Sans l'etape 2, le dimensionnement ci-dessus ne veut rien dire.",
    ]
    return "\n".join(lines)
