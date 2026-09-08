"""Tests for autonomous mode: the ceilings, the arming gates, the approval queue.

These target the code that decides whether real money moves. Every test that
asserts a ceiling holds is worth more than any test of the strategy.
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_argent.approvals import APPROVED, PENDING, ApprovalQueue
from agent_argent.auto_config import AutoRules
from agent_argent.binance import BinanceTradingClient
from agent_argent.config import ConfigError
from agent_argent.execution import format_quantity, round_to_tick
from agent_argent.guards import Intent, evaluate
from agent_argent.strategy import relative_strength_index, simple_moving_average

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def armed_rules(**overrides):
    base = dict(armed=True, whitelist=("BTCUSDT", "ETHUSDT"))
    base.update(overrides)
    rules = AutoRules(**base)
    rules.validate()
    return rules


def buy(amount=300.0, symbol="BTCUSDT", asset="BTC"):
    return Intent(symbol=symbol, asset=asset, side="BUY", amount_display=amount, reason="test")


def verdict(intent, rules, *, exposure=None, positions=0, journal=None, cash=100000.0):
    return evaluate(
        intent, rules,
        auto_exposure_by_asset=exposure or {},
        open_positions=positions,
        journal=journal or [],
        available_cash_display=cash,
        now=NOW,
    )


class ArmingGates(unittest.TestCase):
    def setUp(self):
        self.env = "AGENT_ARGENT_ARME"
        self._saved = __import__("os").environ.pop(self.env, None)

    def tearDown(self):
        import os

        os.environ.pop(self.env, None)
        if self._saved is not None:
            os.environ[self.env] = self._saved

    def test_config_alone_does_not_arm(self):
        rules = armed_rules()
        armed, why = rules.is_armed()
        self.assertFalse(armed)
        self.assertIn("variable d'environnement", why)

    def test_env_var_alone_does_not_arm(self):
        import os

        os.environ[self.env] = "1"
        armed, why = AutoRules(armed=False).is_armed()
        self.assertFalse(armed)
        self.assertIn("armed", why)

    def test_both_gates_arm(self):
        import os

        os.environ[self.env] = "1"
        self.assertTrue(armed_rules().is_armed()[0])

    def test_disarmed_blocks_every_order(self):
        self.assertFalse(verdict(buy(), armed_rules()).allowed)


class ArmedGuards(unittest.TestCase):
    """All of these run with both arming gates satisfied."""

    def setUp(self):
        import os

        self._saved = os.environ.get("AGENT_ARGENT_ARME")
        os.environ["AGENT_ARGENT_ARME"] = "1"

    def tearDown(self):
        import os

        os.environ.pop("AGENT_ARGENT_ARME", None)
        if self._saved is not None:
            os.environ["AGENT_ARGENT_ARME"] = self._saved

    def test_small_order_on_whitelist_is_allowed(self):
        result = verdict(buy(200.0), armed_rules())
        self.assertTrue(result.allowed)
        self.assertAlmostEqual(result.amount_display, 200.0)

    def test_symbol_off_whitelist_is_blocked(self):
        result = verdict(buy(200.0, "DOGEUSDT", "DOGE"), armed_rules())
        self.assertFalse(result.allowed)
        self.assertTrue(any("liste blanche" in b for b in result.blocks))

    def test_order_is_capped_at_max_order(self):
        rules = armed_rules(max_order=300.0, approval_threshold=10000.0)
        result = verdict(buy(5000.0), rules)
        self.assertTrue(result.allowed)
        self.assertAlmostEqual(result.amount_display, 300.0)

    def test_per_asset_ceiling_shrinks_the_order(self):
        rules = armed_rules(max_order=300.0, max_per_asset=300.0)
        result = verdict(buy(300.0), rules, exposure={"BTC": 250.0})
        self.assertTrue(result.allowed)
        self.assertAlmostEqual(result.amount_display, 50.0)

    def test_per_asset_ceiling_reached_blocks(self):
        rules = armed_rules(max_per_asset=300.0)
        result = verdict(buy(300.0), rules, exposure={"BTC": 300.0})
        self.assertFalse(result.allowed)
        self.assertTrue(any("plafond par crypto" in b for b in result.blocks))

    def test_total_autonomous_ceiling_blocks(self):
        rules = armed_rules(max_total_auto=600.0)
        result = verdict(buy(300.0), rules, exposure={"ETH": 300.0, "SOL": 300.0})
        self.assertFalse(result.allowed)
        self.assertTrue(any("automatique totale" in b for b in result.blocks))

    def test_daily_loss_limit_halts_trading(self):
        journal = [
            {"at": (NOW - timedelta(hours=2)).isoformat(), "realised_pnl": -60.0},
            {"at": (NOW - timedelta(hours=1)).isoformat(), "realised_pnl": -45.0},
        ]
        result = verdict(buy(100.0), armed_rules(daily_loss_limit=100.0), journal=journal)
        self.assertFalse(result.allowed)
        self.assertTrue(any("coupe-circuit" in b for b in result.blocks))

    def test_losses_older_than_24h_do_not_count(self):
        journal = [{"at": (NOW - timedelta(hours=30)).isoformat(), "realised_pnl": -500.0}]
        self.assertTrue(verdict(buy(100.0), armed_rules(), journal=journal).allowed)

    def test_trade_count_limit(self):
        journal = [
            {"at": (NOW - timedelta(minutes=i)).isoformat(), "realised_pnl": 0.0}
            for i in range(20)
        ]
        result = verdict(buy(100.0), armed_rules(max_trades_per_day=20), journal=journal)
        self.assertFalse(result.allowed)
        self.assertTrue(any("ordres sur 24h" in b for b in result.blocks))

    def test_open_position_limit(self):
        result = verdict(buy(100.0), armed_rules(max_open_positions=3), positions=3)
        self.assertFalse(result.allowed)

    def test_unparseable_journal_entry_does_not_crash_the_guard(self):
        journal = [{"at": "pas-une-date", "realised_pnl": -9999.0}, {"realised_pnl": -1.0}]
        self.assertTrue(verdict(buy(100.0), armed_rules(), journal=journal).allowed)

    def test_insufficient_cash_blocks(self):
        result = verdict(buy(300.0), armed_rules(), cash=0.0)
        self.assertFalse(result.allowed)

    def test_cash_shrinks_the_order(self):
        result = verdict(buy(300.0), armed_rules(), cash=120.0)
        self.assertTrue(result.allowed)
        self.assertAlmostEqual(result.amount_display, 120.0)


class ApprovalThreshold(unittest.TestCase):
    def setUp(self):
        import os

        os.environ["AGENT_ARGENT_ARME"] = "1"

    def tearDown(self):
        import os

        os.environ.pop("AGENT_ARGENT_ARME", None)

    def test_above_threshold_requires_approval_not_execution(self):
        rules = armed_rules(
            approval_threshold=300.0,
            max_order=1000.0,
            max_per_asset=1000.0,
            max_total_auto=2000.0,
        )
        result = verdict(buy(500.0), rules)
        self.assertFalse(result.allowed)
        self.assertTrue(result.needs_approval)

    def test_at_threshold_executes_autonomously(self):
        rules = armed_rules(approval_threshold=300.0, max_order=300.0)
        result = verdict(buy(300.0), rules)
        self.assertTrue(result.allowed)
        self.assertFalse(result.needs_approval)


class Ceilings(unittest.TestCase):
    def test_per_asset_below_per_order_is_rejected(self):
        with self.assertRaises(ConfigError):
            AutoRules(max_order=300.0, max_per_asset=100.0).validate()

    def test_take_profit_below_stop_is_rejected(self):
        with self.assertRaises(ConfigError):
            AutoRules(take_profit_pct=0.005, stop_loss_pct=0.01).validate()

    def test_armed_with_empty_whitelist_is_rejected(self):
        with self.assertRaises(ConfigError):
            AutoRules(armed=True, whitelist=()).validate()

    def test_zero_daily_loss_limit_is_rejected(self):
        with self.assertRaises(ConfigError):
            AutoRules(daily_loss_limit=0.0).validate()


class Queue(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "validations.json"

    def tearDown(self):
        self.dir.cleanup()

    def make(self, ttl=30):
        queue = ApprovalQueue(self.path, ttl)
        request = queue.submit(
            symbol="BTCUSDT", asset="BTC", side="BUY",
            amount=500.0, price=60000.0, reason="test",
        )
        return queue, request

    def test_submit_then_approve_round_trip(self):
        queue, request = self.make()
        self.assertEqual(request.status, PENDING)
        queue.decide(request.id, True)
        queue.save()
        self.assertEqual(ApprovalQueue(self.path, 30).approved()[0].id, request.id)

    def test_refusal_is_persisted(self):
        queue, request = self.make()
        queue.decide(request.id, False, note="non merci")
        queue.save()
        self.assertEqual(ApprovalQueue(self.path, 30).pending(), [])

    def test_cannot_decide_twice(self):
        queue, request = self.make()
        queue.decide(request.id, True)
        with self.assertRaises(ValueError):
            queue.decide(request.id, False)

    def test_unknown_id_raises(self):
        queue, _ = self.make()
        with self.assertRaises(KeyError):
            queue.decide("deadbeef", True)

    def test_stale_request_expires_unexecuted(self):
        queue, request = self.make(ttl=30)
        request.at = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(
            timespec="seconds"
        )
        expired = queue.expire_stale()
        self.assertEqual([r.id for r in expired], [request.id])
        self.assertEqual(queue.pending(), [])

    def test_approved_but_stale_also_expires(self):
        queue, request = self.make(ttl=30)
        queue.decide(request.id, True)
        request.at = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(
            timespec="seconds"
        )
        queue.expire_stale()
        self.assertEqual(queue.approved(), [])

    def test_corrupt_queue_raises_instead_of_losing_requests(self):
        self.path.write_text("{casse", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            ApprovalQueue(self.path, 30)


class KeyRightsChecks(unittest.TestCase):
    """A trading-enabled key must pass the checks that monitoring commands run.

    Regression: bilan/signal/verifier used assert_read_only_key, which rejects
    the very key autonomous mode requires. The whole documented setup sequence
    failed on step one.
    """

    class FakeClient(BinanceTradingClient):
        def __init__(self, account):
            super().__init__("k", "s")
            self._account = account

        def account(self):
            return self._account

    TRADING_KEY = {"canTrade": True, "canWithdraw": False, "balances": []}
    WITHDRAW_KEY = {"canTrade": True, "canWithdraw": True, "balances": []}
    MONITOR_KEY = {"canTrade": False, "canWithdraw": False, "balances": []}

    def test_trading_key_passes_the_no_withdrawal_floor(self):
        client = self.FakeClient(self.TRADING_KEY)
        self.assertEqual(client.assert_no_withdrawal(), self.TRADING_KEY)

    def test_monitor_key_also_passes_the_floor(self):
        client = self.FakeClient(self.MONITOR_KEY)
        self.assertEqual(client.assert_no_withdrawal(), self.MONITOR_KEY)

    def test_withdrawal_key_is_refused_by_the_floor(self):
        from agent_argent.binance import BinanceError

        with self.assertRaises(BinanceError):
            self.FakeClient(self.WITHDRAW_KEY).assert_no_withdrawal()

    def test_withdrawal_key_is_refused_for_trading(self):
        from agent_argent.binance import BinanceError

        with self.assertRaises(BinanceError):
            self.FakeClient(self.WITHDRAW_KEY).assert_trading_key()

    def test_autonomous_mode_requires_the_trading_right(self):
        from agent_argent.binance import BinanceError

        with self.assertRaises(BinanceError):
            self.FakeClient(self.MONITOR_KEY).assert_trading_key()

    def test_strict_read_only_check_still_rejects_a_trading_key(self):
        from agent_argent.binance import BinanceError

        with self.assertRaises(BinanceError):
            self.FakeClient(self.TRADING_KEY).assert_read_only_key()


class CredentialDiagnostics(unittest.TestCase):
    """The commonest setup failure deserves a message that names the actual fix."""

    def setUp(self):
        import os

        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self._saved = {
            name: os.environ.pop(name, None)
            for name in ("BINANCE_API_KEY", "BINANCE_API_SECRET")
        }

    def tearDown(self):
        import os

        self.dir.cleanup()
        for name, value in self._saved.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value

    def test_missing_file_tells_you_to_create_it(self):
        from agent_argent.config import Credentials

        with self.assertRaises(ConfigError) as caught:
            Credentials.from_env(self.root / ".env")
        self.assertIn("Aucun fichier", str(caught.exception))
        self.assertIn("cp", str(caught.exception))

    def test_unfilled_copy_is_diagnosed_differently(self):
        from agent_argent.config import Credentials, load_dotenv

        env = self.root / ".env"
        env.write_text("BINANCE_API_KEY=\nBINANCE_API_SECRET=\n", encoding="utf-8")
        load_dotenv(env)
        with self.assertRaises(ConfigError) as caught:
            Credentials.from_env(env)
        message = str(caught.exception)
        self.assertIn("existe mais", message)
        self.assertIn("sans le remplir", message)

    def test_partially_filled_names_the_missing_one(self):
        from agent_argent.config import Credentials, load_dotenv

        env = self.root / ".env"
        env.write_text("BINANCE_API_KEY=abc\nBINANCE_API_SECRET=\n", encoding="utf-8")
        load_dotenv(env)
        with self.assertRaises(ConfigError) as caught:
            Credentials.from_env(env)
        self.assertIn("BINANCE_API_SECRET", str(caught.exception))
        self.assertNotIn("BINANCE_API_KEY n", str(caught.exception))

    def test_filled_file_loads(self):
        from agent_argent.config import Credentials, load_dotenv

        env = self.root / ".env"
        env.write_text("BINANCE_API_KEY=abc\nBINANCE_API_SECRET=def\n", encoding="utf-8")
        load_dotenv(env)
        credentials = Credentials.from_env(env)
        self.assertEqual(credentials.api_key, "abc")
        self.assertEqual(credentials.api_secret, "def")

    def test_quoted_values_are_unwrapped(self):
        from agent_argent.config import Credentials, load_dotenv

        env = self.root / ".env"
        env.write_text('BINANCE_API_KEY="abc"\nBINANCE_API_SECRET=\'def\'\n', encoding="utf-8")
        load_dotenv(env)
        self.assertEqual(Credentials.from_env(env).api_key, "abc")


class PriceFormatting(unittest.TestCase):
    def test_tick_rounding_never_uses_scientific_notation(self):
        self.assertNotIn("e", round_to_tick(0.00001234, 0.00000001).lower())

    def test_tick_rounding_down_and_up(self):
        self.assertEqual(round_to_tick(100.567, 0.01), "100.56")
        self.assertEqual(round_to_tick(100.561, 0.01, up=True), "100.57")

    def test_quantity_rounds_down_to_lot_step(self):
        self.assertEqual(format_quantity(1.23456789, 0.001), "1.234")


class StrategyMaths(unittest.TestCase):
    def test_sma(self):
        self.assertAlmostEqual(simple_moving_average([1, 2, 3, 4, 5], 5), 3.0)

    def test_rsi_all_gains_is_hundred(self):
        self.assertAlmostEqual(relative_strength_index(list(range(1, 20)), 14), 100.0)

    def test_rsi_flat_series_does_not_divide_by_zero(self):
        self.assertAlmostEqual(relative_strength_index([5.0] * 20, 14), 50.0)

    def test_rsi_needs_enough_data(self):
        with self.assertRaises(ValueError):
            relative_strength_index([1.0, 2.0], 14)


if __name__ == "__main__":
    unittest.main(verbosity=2)
