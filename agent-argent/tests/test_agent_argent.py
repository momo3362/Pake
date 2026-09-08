"""Tests for the parts that decide how much money is at stake."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_argent.binance import (
    BinanceReadOnlyClient,
    BinanceTradingClient,
    ReadOnlyViolation,
)
from agent_argent.config import ConfigError, RiskRules
from agent_argent.portfolio import build_portfolio
from agent_argent.risk import audit, average_true_range, propose, round_to_step, symbol_filters
from agent_argent.state import State


def make_portfolio(btc=0.04, usdt=10000.0, rules=None):
    rules = rules or RiskRules()
    balances = [
        {"asset": "BTC", "free": str(btc), "locked": "0"},
        {"asset": "USDT", "free": str(usdt), "locked": "0"},
    ]
    prices = {"BTCUSDT": 60000.0}
    return build_portfolio(balances, prices, rules)[0]


class ReadOnlyGuarantee(unittest.TestCase):
    def test_order_endpoints_are_refused(self):
        client = BinanceReadOnlyClient("k", "s")
        for path in ("/api/v3/order", "/api/v3/openOrders", "/sapi/v1/capital/withdraw/apply"):
            with self.assertRaises(ReadOnlyViolation):
                client._get(path)

    def test_no_allowlisted_endpoint_can_move_funds_off_binance(self):
        from agent_argent.binance import (
            FORBIDDEN_FRAGMENTS,
            READ_ONLY_ENDPOINTS,
            TRADING_ENDPOINTS,
        )

        for endpoint in READ_ONLY_ENDPOINTS | TRADING_ENDPOINTS:
            for fragment in FORBIDDEN_FRAGMENTS:
                self.assertNotIn(fragment, endpoint.lower())

    def test_read_only_client_refuses_to_post_orders(self):
        client = BinanceReadOnlyClient("k", "s")
        for path in ("/api/v3/order", "/api/v3/order/oco"):
            with self.assertRaises(ReadOnlyViolation):
                client._request("POST", path)


class FundEgressIsImpossible(unittest.TestCase):
    """The one property that must hold even if the strategy is completely wrong."""

    def test_trading_client_cannot_reach_withdrawal_or_transfer(self):
        client = BinanceTradingClient("k", "s")
        for path in (
            "/sapi/v1/capital/withdraw/apply",
            "/sapi/v1/asset/transfer",
            "/sapi/v1/margin/loan",
            "/sapi/v1/lending/daily/redeem",
            "/sapi/v1/sub-account/transfer/subToMaster",
        ):
            with self.assertRaises(ReadOnlyViolation):
                client._request("POST", path)

    def test_trading_client_may_reach_order_endpoints(self):
        client = BinanceTradingClient("k", "s")
        # _check_path is the boundary; reaching it without raising is the assertion.
        client._check_path("/api/v3/order", "POST")
        client._check_path("/api/v3/order/oco", "POST")
        client._check_path("/api/v3/account", "GET")

    def test_unknown_endpoint_is_refused_even_when_harmless(self):
        with self.assertRaises(ReadOnlyViolation):
            BinanceTradingClient("k", "s")._check_path("/api/v3/myAllocations", "GET")


class Valuation(unittest.TestCase):
    def test_equity_and_exposure(self):
        portfolio = make_portfolio(btc=0.1, usdt=10000.0)
        self.assertAlmostEqual(portfolio.equity, 16000.0)
        self.assertAlmostEqual(portfolio.exposure, 6000.0)
        self.assertAlmostEqual(portfolio.cash, 10000.0)
        self.assertAlmostEqual(portfolio.exposure_pct, 0.375)
        self.assertAlmostEqual(portfolio.weight("BTC"), 0.375)

    def test_locked_balance_counts(self):
        balances = [{"asset": "ETH", "free": "1", "locked": "2"}]
        portfolio, _ = build_portfolio(balances, {"ETHUSDT": 3000.0}, RiskRules())
        self.assertAlmostEqual(portfolio.equity, 9000.0)

    def test_dust_and_unpriced_are_separated(self):
        balances = [
            {"asset": "SHIB", "free": "100", "locked": "0"},
            {"asset": "NOPAIR", "free": "5", "locked": "0"},
        ]
        portfolio, unpriced = build_portfolio(balances, {"SHIBUSDT": 0.00001}, RiskRules())
        self.assertEqual(portfolio.ignored_dust, ("SHIB",))
        self.assertEqual(unpriced, ("NOPAIR",))

    def test_inverted_pair_is_used_when_direct_is_missing(self):
        balances = [{"asset": "EUR", "free": "1000", "locked": "0"}]
        portfolio, unpriced = build_portfolio(balances, {"EURUSDT": 1.1}, RiskRules())
        self.assertEqual(unpriced, ())
        self.assertAlmostEqual(portfolio.equity, 1100.0)


class Audit(unittest.TestCase):
    def test_clean_portfolio_has_no_breach(self):
        portfolio = make_portfolio()  # BTC 2400 of 12400 = 19.4%, under every cap
        self.assertEqual(audit(portfolio, RiskRules(), portfolio.equity), [])

    def test_concentration_breach(self):
        portfolio = make_portfolio(btc=1.0, usdt=1000.0)  # BTC ~98%
        breaches = audit(portfolio, RiskRules(), 0.0)
        self.assertTrue(any(b.rule == "concentration" and b.blocking for b in breaches))

    def test_drawdown_blocks_at_threshold(self):
        portfolio = make_portfolio()  # equity 12400
        breaches = audit(portfolio, RiskRules(max_drawdown_pct=0.20), high_water_mark=15500.0)
        self.assertEqual([b.rule for b in breaches], ["drawdown"])
        self.assertTrue(breaches[0].blocking)

    def test_drawdown_warns_before_threshold(self):
        # 20% below the mark, against a 25% limit: past 75% of it, so a warning.
        portfolio = make_portfolio()  # equity 12400
        breaches = audit(portfolio, RiskRules(max_drawdown_pct=0.25), high_water_mark=15500.0)
        drawdown = [b for b in breaches if b.rule == "drawdown"]
        self.assertEqual(len(drawdown), 1)
        self.assertFalse(drawdown[0].blocking)

    def test_gain_above_high_water_mark_is_not_a_drawdown(self):
        portfolio = make_portfolio()
        self.assertEqual(audit(portfolio, RiskRules(), portfolio.equity / 2), [])


class Sizing(unittest.TestCase):
    def setUp(self):
        self.rules = RiskRules()
        self.portfolio = make_portfolio(btc=0.0, usdt=100000.0)

    def size(self, **overrides):
        kwargs = dict(
            symbol="BTCUSDT", asset="BTC", entry=60000.0, atr=1500.0,
            portfolio=self.portfolio, rules=self.rules,
            lot_step=0.00001, min_notional=10.0, breaches=[],
        )
        kwargs.update(overrides)
        return propose(**kwargs)

    def test_risk_equals_configured_fraction_of_equity(self):
        proposal = self.size()
        # stop = 60000 - 2*1500 = 57000; risk budget 1% of 100000 = 1000
        self.assertAlmostEqual(proposal.stop, 57000.0)
        self.assertAlmostEqual(proposal.risk_amount, 1000.0, delta=1.0)
        self.assertAlmostEqual(proposal.quantity, 1000.0 / 3000.0, places=4)

    def test_blocking_breach_yields_no_proposal(self):
        from agent_argent.risk import Breach

        proposal = self.size(breaches=[Breach("drawdown", "repli", "alerte")])
        self.assertFalse(proposal.actionable)
        self.assertIn("drawdown", proposal.rejected)

    def test_non_blocking_breach_still_allows_a_proposal(self):
        from agent_argent.risk import Breach

        self.assertTrue(self.size(breaches=[Breach("drawdown", "x", "attention")]).actionable)

    def test_concentration_ceiling_caps_the_size(self):
        # 20% of 100000 = 20000 max in BTC; a 1% risk budget with a tiny stop
        # would otherwise ask for far more.
        proposal = self.size(atr=1.0)
        self.assertLessEqual(proposal.notional, 20000.0 + 1.0)

    def test_existing_position_at_ceiling_is_refused(self):
        portfolio = make_portfolio(btc=1.0, usdt=100000.0)  # BTC 60000 of 160000 = 37.5%
        proposal = self.size(portfolio=portfolio)
        self.assertFalse(proposal.actionable)
        self.assertIn("aucune place", proposal.rejected)

    def test_cannot_spend_more_than_available_cash(self):
        portfolio = make_portfolio(btc=0.0, usdt=500.0)
        proposal = self.size(portfolio=portfolio, atr=1.0)
        self.assertLessEqual(proposal.notional, 500.0)

    def test_below_min_notional_is_refused(self):
        proposal = self.size(min_notional=50000.0)
        self.assertFalse(proposal.actionable)
        self.assertIn("minimum", proposal.rejected)

    def test_wide_atr_producing_negative_stop_is_refused(self):
        proposal = self.size(entry=100.0, atr=80.0)
        self.assertFalse(proposal.actionable)
        self.assertIn("negatif", proposal.rejected)

    def test_no_cash_is_refused(self):
        portfolio = make_portfolio(btc=0.5, usdt=0.0)
        proposal = self.size(portfolio=portfolio)
        self.assertFalse(proposal.actionable)


class Helpers(unittest.TestCase):
    def test_round_to_step_always_rounds_down(self):
        self.assertAlmostEqual(round_to_step(0.123456789, 0.00001), 0.12345)
        self.assertAlmostEqual(round_to_step(7.9, 1.0), 7.0)
        self.assertAlmostEqual(round_to_step(0.5, 0.0), 0.5)

    def test_atr_of_constant_range_candles(self):
        klines = [[0, "10", "11", "9", "10"] for _ in range(20)]
        self.assertAlmostEqual(average_true_range(klines, 14), 2.0)

    def test_atr_needs_enough_candles(self):
        with self.assertRaises(ValueError):
            average_true_range([[0, "1", "1", "1", "1"]] * 5, 14)

    def test_symbol_filters_extraction(self):
        definition = {
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.00001"},
                {"filterType": "NOTIONAL", "minNotional": "10.0"},
            ]
        }
        self.assertEqual(symbol_filters(definition), (0.00001, 10.0))


class Rules(unittest.TestCase):
    def test_absurd_risk_is_rejected(self):
        with self.assertRaises(ConfigError):
            RiskRules(risk_per_trade=0.5).validate()

    def test_position_cap_above_total_cap_is_rejected(self):
        with self.assertRaises(ConfigError):
            RiskRules(max_position_pct=0.9, max_total_exposure_pct=0.6).validate()


class StatePersistence(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "equity.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_high_water_mark_only_rises(self):
        state = State(self.path)
        state.record(1000.0, "USDT")
        state.record(1500.0, "USDT")
        state.record(900.0, "USDT")
        state.save()
        self.assertAlmostEqual(State(self.path).high_water_mark, 1500.0)

    def test_corrupt_state_raises_instead_of_resetting(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            State(self.path)

    def test_previous_equity(self):
        state = State(self.path)
        state.record(100.0, "USDT")
        state.record(200.0, "USDT")
        self.assertAlmostEqual(state.previous_equity(), 100.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
