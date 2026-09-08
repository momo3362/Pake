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

from .approvals import ApprovalQueue, render_notification
from .auto_config import AutoRules
from .binance import BinanceError, BinanceReadOnlyClient, BinanceTradingClient
from .config import ConfigError, Credentials, RiskRules, load_dotenv
from .portfolio import build_portfolio
from .report import render_balance, render_proposal
from .risk import audit, average_true_range, propose, symbol_filters
from .runner import run_pass
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

    auto = sub.add_parser(
        "auto", help="une passe automatique: signaux, garde-fous, execution ou validation"
    )
    auto.add_argument(
        "--reel",
        action="store_true",
        help="envoie de vrais ordres. Sans ce drapeau, tout est simule.",
    )
    auto.add_argument(
        "--auto-regles",
        type=Path,
        default=DEFAULT_ROOT / "autonomous.json",
        help="plafonds du mode automatique",
    )
    auto.add_argument(
        "--notifier",
        default=None,
        help="commande shell recevant la notification sur stdin (ex. 'mail -s ... vous@ex.fr')",
    )

    pending = sub.add_parser("attente", help="liste les demandes de validation en cours")
    pending.add_argument(
        "--auto-regles", type=Path, default=DEFAULT_ROOT / "autonomous.json"
    )

    for name, helptext in (("valider", "autorise une demande"), ("refuser", "rejette une demande")):
        decision = sub.add_parser(name, help=helptext)
        decision.add_argument("identifiant", help="id court affiche par 'attente'")
        decision.add_argument("--note", default=None, help="commentaire libre")
        decision.add_argument(
            "--auto-regles", type=Path, default=DEFAULT_ROOT / "autonomous.json"
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


def command_auto(args) -> int:
    load_dotenv(args.env)
    credentials = Credentials.from_env()
    rules = RiskRules.load(args.regles)
    auto = AutoRules.load(args.auto_regles)
    client = BinanceTradingClient(credentials.api_key, credentials.api_secret)

    report = run_pass(
        client,
        rules,
        auto,
        state_dir=args.etat.parent,
        live=args.reel,
        notify_command=args.notifier,
    )
    print(report.render())
    return 0


def command_attente(args) -> int:
    auto = AutoRules.load(args.auto_regles)
    rules = RiskRules.load(args.regles)
    queue = ApprovalQueue(args.etat.parent / "validations.json", auto.approval_ttl_minutes)
    queue.expire_stale()
    queue.save()

    pending = queue.pending()
    if not pending:
        print("Aucune demande en attente.")
        return 0
    print(f"{len(pending)} demande(s) en attente:\n")
    for request in pending:
        print(render_notification(request, rules.display_asset))
        print()
    return 0


def command_decision(args, approve: bool) -> int:
    auto = AutoRules.load(args.auto_regles)
    queue = ApprovalQueue(args.etat.parent / "validations.json", auto.approval_ttl_minutes)
    queue.expire_stale()
    try:
        request = queue.decide(args.identifiant, approve, args.note)
    except (KeyError, ValueError) as exc:
        print(f"Erreur: {exc}", file=sys.stderr)
        return 2
    queue.save()
    verb = "validee" if approve else "refusee"
    print(f"Demande {request.id} ({request.symbol}) {verb}.")
    if approve:
        print(
            "Elle sera executee a la prochaine passe 'auto', et seulement si le "
            f"marche n'a pas bouge de plus de 1% et si moins de "
            f"{auto.approval_ttl_minutes} min se sont ecoulees."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "verifier": command_verifier,
        "bilan": command_bilan,
        "signal": command_signal,
        "auto": command_auto,
        "attente": command_attente,
        "valider": lambda a: command_decision(a, approve=True),
        "refuser": lambda a: command_decision(a, approve=False),
    }
    try:
        return handlers[args.commande](args)
    except (ConfigError, BinanceError, ValueError, RuntimeError) as exc:
        print(f"Erreur: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
