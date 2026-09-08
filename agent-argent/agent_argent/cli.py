"""Command-line entry point.

Subcommands:
  verifier  preflight: reachability, key validity, and that the key cannot trade
  bilan     valued portfolio, rule audit, drawdown
  signal    position sizing proposal for one symbol (advice only)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .binance import BinanceError, BinanceReadOnlyClient
from .config import ConfigError, Credentials, RiskRules, load_dotenv
from .portfolio import build_portfolio
from .report import render_balance, render_proposal
from .risk import audit, average_true_range, propose, symbol_filters
from .state import State

DEFAULT_ROOT = Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-argent",
        description="Suivi de portefeuille Binance en lecture seule et regles de money management. "
        "Ne passe jamais d'ordre.",
    )
    parser.add_argument(
        "--env", type=Path, default=DEFAULT_ROOT / ".env", help="fichier de credentials"
    )
    parser.add_argument(
        "--regles", type=Path, default=DEFAULT_ROOT / "regles.json", help="regles de risque (JSON)"
    )
    parser.add_argument(
        "--etat", type=Path, default=DEFAULT_ROOT / "state" / "equity.json", help="historique"
    )

    sub = parser.add_subparsers(dest="commande", required=True)
    sub.add_parser("verifier", help="controle la cle API et refuse une cle qui peut trader")

    balance = sub.add_parser("bilan", help="portefeuille valorise et audit des regles")
    balance.add_argument(
        "--sans-enregistrer",
        action="store_true",
        help="n'ecrit pas l'historique (utile pour un essai)",
    )

    signal = sub.add_parser("signal", help="dimensionne une position, sans l'executer")
    signal.add_argument("symbole", help="paire Binance, ex. BTCUSDT")
    signal.add_argument(
        "--intervalle", default="1d", help="intervalle des bougies pour l'ATR (defaut 1d)"
    )
    return parser


def _client(args) -> BinanceReadOnlyClient:
    load_dotenv(args.env)
    credentials = Credentials.from_env()
    return BinanceReadOnlyClient(credentials.api_key, credentials.api_secret)


def _load_context(args):
    rules = RiskRules.load(args.regles)
    client = _client(args)
    account = client.assert_read_only_key()
    prices = client.prices()
    portfolio, unpriced = build_portfolio(account.get("balances", []), prices, rules)
    return client, rules, portfolio, unpriced, prices


def command_verifier(args) -> int:
    rules = RiskRules.load(args.regles)
    client = _client(args)
    client.server_time()
    account = client.assert_read_only_key()
    print("Connexion Binance : OK")
    print("Droits de la cle  : lecture seule confirmee (trading et retrait desactives)")
    print(f"Type de compte    : {account.get('accountType', 'inconnu')}")
    print(f"Soldes non nuls   : {sum(1 for b in account.get('balances', []) if float(b['free']) + float(b['locked']) > 0)}")
    print(f"Regles chargees   : risque/idee {rules.risk_per_trade:.1%}, "
          f"position max {rules.max_position_pct:.0%}, "
          f"exposition max {rules.max_total_exposure_pct:.0%}, "
          f"repli max {rules.max_drawdown_pct:.0%}")
    return 0


def command_bilan(args) -> int:
    _, rules, portfolio, unpriced, prices = _load_context(args)
    state = State(args.etat)
    breaches = audit(portfolio, rules, state.high_water_mark)
    previous = state.previous_equity()

    display_rate = None
    if rules.display_asset != rules.valuation_asset:
        pair = prices.get(f"{rules.display_asset}{rules.valuation_asset}")
        if pair:
            display_rate = 1.0 / pair

    if not args.sans_enregistrer:
        state.record(portfolio.equity, portfolio.valuation_asset)
        state.save()

    print(
        render_balance(
            portfolio, breaches, rules, state.high_water_mark, previous, unpriced, display_rate
        )
    )
    return 1 if any(b.blocking for b in breaches) else 0


def command_signal(args) -> int:
    client, rules, portfolio, _, prices = _load_context(args)
    symbol = args.symbole.upper()

    definition = client.exchange_info(symbol)
    base_asset = definition["baseAsset"]
    lot_step, min_notional = symbol_filters(definition)

    entry = prices.get(symbol)
    if not entry:
        raise BinanceError(f"Pas de prix courant pour {symbol}.")

    klines = client.klines(symbol, args.intervalle, limit=rules.atr_period + 5)
    atr = average_true_range(klines, rules.atr_period)

    state = State(args.etat)
    breaches = audit(portfolio, rules, state.high_water_mark)
    proposal = propose(
        symbol=symbol,
        asset=base_asset,
        entry=entry,
        atr=atr,
        portfolio=portfolio,
        rules=rules,
        lot_step=lot_step,
        min_notional=min_notional,
        breaches=breaches,
    )
    print(render_proposal(proposal, rules, portfolio.valuation_asset))
    return 0 if proposal.actionable else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "verifier": command_verifier,
        "bilan": command_bilan,
        "signal": command_signal,
    }
    try:
        return handlers[args.commande](args)
    except (ConfigError, BinanceError, ValueError, RuntimeError) as exc:
        print(f"Erreur: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
