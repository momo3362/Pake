"""One autonomous pass. Cron calls this; it is not a long-running daemon.

A pass, in order:
  1. Read the world fresh: balances, prices, open orders.
  2. Expire stale approval requests.
  3. Execute requests you approved since the last pass.
  4. Look for new signals on whitelisted pairs.
  5. For each signal, ask the guards. Small enough -> execute. Too big -> queue
     it for your approval and notify you.

Deliberately stateless between passes apart from three JSON files, so a crash
mid-pass loses at most one candidate trade and never corrupts a ceiling.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import strategy
from .approvals import APPROVED, DONE, ApprovalQueue, notify, render_notification
from .auto_config import AutoRules
from .binance import BinanceError, BinanceTradingClient
from .config import RiskRules
from .execution import open_protected_position
from .guards import Intent, evaluate
from .portfolio import build_portfolio


@dataclass
class PassReport:
    lines: list[str]
    executed: int = 0
    queued: int = 0
    blocked: int = 0

    def say(self, message: str) -> None:
        self.lines.append(message)

    def render(self) -> str:
        head = (
            f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}] "
            f"executes={self.executed} en_attente={self.queued} bloques={self.blocked}"
        )
        return "\n".join([head, *self.lines])


def load_journal(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(
            f"Journal des trades illisible ({path}): {exc}. "
            "Les coupe-circuits en dependent, l'agent refuse de tourner a l'aveugle."
        ) from exc


def save_journal(path: Path, journal: list[dict], keep: int = 2000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(journal[-keep:], indent=2), encoding="utf-8")
    temporary.replace(path)


def _display_rate(prices: dict[str, float], rules: RiskRules) -> float:
    """How many display units one valuation unit is worth (USDT -> EUR)."""
    if rules.display_asset == rules.valuation_asset:
        return 1.0
    pair = prices.get(f"{rules.display_asset}{rules.valuation_asset}")
    if not pair:
        raise BinanceError(
            f"Pas de taux {rules.display_asset}/{rules.valuation_asset}: "
            "impossible de convertir les plafonds, arret par securite."
        )
    return 1.0 / pair


def run_pass(
    client: BinanceTradingClient,
    rules: RiskRules,
    auto: AutoRules,
    *,
    state_dir: Path,
    live: bool,
    notify_command: str | None = None,
) -> PassReport:
    report = PassReport(lines=[])

    armed, why_not = auto.is_armed()
    if not armed:
        report.say(f"Mode automatique desarme: {why_not}. Aucun ordre ne sera place.")
    if not live:
        report.say("SIMULATION (--reel absent): aucun ordre reel ne sera envoye.")

    account = client.assert_trading_key()
    prices = client.prices()
    portfolio, _ = build_portfolio(account.get("balances", []), prices, rules)
    rate = _display_rate(prices, rules)

    journal_path = state_dir / "journal.json"
    journal = load_journal(journal_path)
    queue = ApprovalQueue(state_dir / "validations.json", auto.approval_ttl_minutes)

    for expired in queue.expire_stale():
        report.say(f"  demande {expired.id} ({expired.symbol}) expiree, non executee")

    # Autonomous exposure, in display currency, per asset.
    auto_exposure = {
        position.asset: position.value * rate
        for position in portfolio.positions
        if not position.is_stable
    }
    cash_display = portfolio.cash * rate

    # --- 1. execute what you approved ------------------------------------
    for request in queue.approved():
        price_now = prices.get(request.symbol)
        if not price_now:
            report.say(f"  {request.id}: plus de prix pour {request.symbol}, ignoree")
            continue
        drift = abs(price_now - request.reference_price) / request.reference_price
        if drift > 0.01:
            request.status = "expiree"
            request.note = f"marche deplace de {drift:.2%} depuis la demande"
            report.say(f"  {request.id}: {request.note}, non executee")
            continue

        execution = open_protected_position(
            client,
            symbol=request.symbol,
            spend_quote=request.amount_display / rate,
            take_profit_pct=auto.take_profit_pct,
            stop_loss_pct=auto.stop_loss_pct,
            reference_price=price_now,
            live=live and armed,
        )
        report.say(f"  {request.id} validee -> {execution.detail}")
        if execution.ok:
            request.status = DONE
            journal.append(execution.as_journal_entry(f"validee par vous: {request.reason}"))
            report.executed += 1

    # --- 2. look for new signals -----------------------------------------
    open_positions = sum(1 for value in auto_exposure.values() if value > 1.0)

    for symbol in auto.whitelist:
        try:
            definition = client.exchange_info(symbol)
            klines = client.klines(symbol, "15m", limit=120)
        except BinanceError as exc:
            report.say(f"  {symbol}: donnees indisponibles ({exc})")
            continue

        signal = strategy.evaluate(symbol, definition["baseAsset"], klines)
        if signal is None:
            continue

        intent = Intent(
            symbol=symbol,
            asset=signal.asset,
            side="BUY",
            amount_display=auto.max_order,
            reason=signal.reason,
        )
        verdict = evaluate(
            intent,
            auto,
            auto_exposure_by_asset=auto_exposure,
            open_positions=open_positions,
            journal=journal,
            available_cash_display=cash_display,
        )

        if verdict.needs_approval:
            request = queue.submit(
                symbol=symbol, asset=signal.asset, side="BUY",
                amount=verdict.amount_display, price=signal.price, reason=signal.reason,
            )
            notify(
                render_notification(request, rules.display_asset),
                notify_command,
                state_dir / "notifications.log",
            )
            report.say(f"  {symbol}: validation requise, demande {request.id}")
            report.queued += 1
            continue

        if not verdict.allowed:
            report.say(f"  {symbol}: {verdict.summary}")
            report.blocked += 1
            continue

        execution = open_protected_position(
            client,
            symbol=symbol,
            spend_quote=verdict.amount_display / rate,
            take_profit_pct=auto.take_profit_pct,
            stop_loss_pct=auto.stop_loss_pct,
            reference_price=signal.price,
            live=live and armed,
        )
        report.say(
            f"  {symbol}: {verdict.amount_display:.2f} {rules.display_asset} "
            f"-> {execution.detail}"
        )
        if execution.ok:
            journal.append(execution.as_journal_entry(signal.reason))
            auto_exposure[signal.asset] = auto_exposure.get(signal.asset, 0.0) + verdict.amount_display
            cash_display -= verdict.amount_display
            open_positions += 1
            report.executed += 1
        else:
            report.blocked += 1

    queue.prune()
    queue.save()
    save_journal(journal_path, journal)

    if report.executed == 0 and report.queued == 0 and report.blocked == 0:
        report.say("  aucun signal sur les paires surveillees")
    return report
