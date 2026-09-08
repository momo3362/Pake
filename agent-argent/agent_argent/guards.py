"""The hard ceilings, re-checked immediately before every single order.

This module is the last thing that runs before money moves. It is deliberately
dumb: no strategy, no network, no state mutation. It takes the intended order
and the freshly-read world, and answers "allowed" or "blocked, because".

Every check is written so that missing or unexpected data BLOCKS. A guard that
fails open is not a guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .auto_config import AutoRules


@dataclass(frozen=True)
class Intent:
    """An order the strategy wants to place, before any guard has run."""

    symbol: str
    asset: str
    side: str            # "BUY" or "SELL"
    amount_display: float  # spend, in display currency (EUR)
    reason: str          # why the strategy wants this, for the audit log


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    needs_approval: bool
    blocks: tuple[str, ...]
    amount_display: float  # possibly reduced to fit under a ceiling

    @property
    def summary(self) -> str:
        if self.allowed:
            return "autorise"
        if self.needs_approval:
            return "validation requise"
        return "bloque: " + " | ".join(self.blocks)


def _today_trades(journal: list[dict], now: datetime) -> list[dict]:
    """Orders placed in the last rolling 24h, not since midnight.

    A calendar reset would let a bad night burn the daily loss limit twice in
    fourteen hours.
    """
    cutoff = now - timedelta(hours=24)
    out = []
    for entry in journal:
        try:
            at = datetime.fromisoformat(entry["at"])
        except (KeyError, ValueError):
            continue  # an unparseable entry is not counted, but never crashes the guard
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if at >= cutoff:
            out.append(entry)
    return out


def evaluate(
    intent: Intent,
    rules: AutoRules,
    *,
    auto_exposure_by_asset: dict[str, float],
    open_positions: int,
    journal: list[dict],
    available_cash_display: float,
    now: datetime | None = None,
) -> Verdict:
    """Decide whether `intent` may be executed autonomously, and at what size."""
    now = now or datetime.now(timezone.utc)
    blocks: list[str] = []
    amount = intent.amount_display

    armed, why_not = rules.is_armed()
    if not armed:
        blocks.append(f"mode automatique desarme ({why_not})")

    if intent.symbol not in rules.whitelist:
        blocks.append(
            f"{intent.symbol} absent de la liste blanche "
            f"({', '.join(rules.whitelist) or 'vide'})"
        )

    if amount <= 0:
        blocks.append(f"montant nul ou negatif ({amount})")

    # --- circuit breakers -------------------------------------------------
    recent = _today_trades(journal, now)
    if len(recent) >= rules.max_trades_per_day:
        blocks.append(
            f"{len(recent)} ordres sur 24h, plafond {rules.max_trades_per_day}"
        )

    realised = sum(float(e.get("realised_pnl", 0.0)) for e in recent)
    if realised <= -rules.daily_loss_limit:
        blocks.append(
            f"perte de {abs(realised):.2f} sur 24h, coupe-circuit a "
            f"{rules.daily_loss_limit:.2f}: arret jusqu'a demain"
        )

    if open_positions >= rules.max_open_positions:
        blocks.append(
            f"{open_positions} positions automatiques ouvertes, plafond "
            f"{rules.max_open_positions}"
        )

    # --- ceilings, which may shrink the order rather than block it --------
    if intent.side == "BUY":
        if amount > rules.max_order:
            amount = rules.max_order

        held = auto_exposure_by_asset.get(intent.asset, 0.0)
        room_asset = rules.max_per_asset - held
        if room_asset <= 0:
            blocks.append(
                f"{intent.asset} occupe deja {held:.2f} en automatique, "
                f"plafond par crypto {rules.max_per_asset:.2f}"
            )
        else:
            amount = min(amount, room_asset)

        total_auto = sum(auto_exposure_by_asset.values())
        room_total = rules.max_total_auto - total_auto
        if room_total <= 0:
            blocks.append(
                f"exposition automatique totale {total_auto:.2f}, "
                f"plafond {rules.max_total_auto:.2f}"
            )
        else:
            amount = min(amount, room_total)

        if amount > available_cash_display:
            amount = available_cash_display
        if amount <= 0:
            blocks.append("liquidites insuffisantes")

    if blocks:
        return Verdict(False, False, tuple(blocks), 0.0)

    # Above the threshold the bot does not decide. You do.
    if intent.amount_display > rules.approval_threshold:
        return Verdict(
            allowed=False,
            needs_approval=True,
            blocks=(
                f"montant {intent.amount_display:.2f} au-dessus du seuil de "
                f"validation {rules.approval_threshold:.2f}",
            ),
            amount_display=amount,
        )

    return Verdict(True, False, (), amount)
