"""Offline regression tests. Never submit orders or call external services."""
import asyncio
import math
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ai_agent import AIAgent, GeminiSignal, TradeSignal
from config import _bool, _int, load_settings
from exchange_client import ExchangeClient, _build_exchange
from indicators import MarketAnalyzer, add_indicators, ohlcv_to_df
from risk_manager import RiskManager
from state_store import StateStore
from trader import Trader


def settings_at(path, **changes):
    env = {"TELEGRAM_BOT_TOKEN": "123456:test-token", "TELEGRAM_ADMIN_CHAT_ID": "123456",
           "GEMINI_API_KEY": "test-key", "DATA_DIR": str(path)}
    with patch.dict(os.environ, env, clear=True):
        return replace(load_settings(), **changes)


class FakeExchange:
    contract_size = 1.0
    min_amount = 0.001
    min_cost = 0.0

    def __init__(self):
        self.price = 100.0
        self.ticker = {"last": 100.0, "bid": 100.0, "ask": 100.0}
        self.fetch_position_contracts = AsyncMock(return_value=0.0)
        self.create_stop_market = AsyncMock(return_value="stop1")
        self.create_market = AsyncMock(return_value={"id": "exit1"})
        self.cancel_order = AsyncMock()
        self.cancel_all = AsyncMock()
        self.fetch_order = AsyncMock()
        self.fetch_free_usdt = AsyncMock(return_value=100.0)
        self.validate_live_support = lambda: None

    def amount_to_precision(self, amount):
        return math.floor(amount * 1000) / 1000

    def price_to_precision(self, price):
        return round(price, 4)

    async def fetch_ticker(self, source="data"):
        return self.ticker


def signal(side="LONG"):
    d = 1 if side == "LONG" else -1
    return TradeSignal(side, 90, 100, 100 - d, 100 + 1.5*d, 100 + 2*d, 10, "تست")


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.s = settings_at(self.path, risk_per_trade_pct=0.04, min_leverage=10, max_leverage=35, max_margin_usage=0.9, daily_profit_target_pct=0.1, daily_max_loss_pct=0.08)
        self.store = StateStore(self.path / "state.db")
        self.client = FakeExchange()
        self.risk = RiskManager(self.s, self.store, self.client)
        self.risk.roll_day(10)
        self.notify = SimpleNamespace(**{name: AsyncMock() for name in (
            "notify_trade_rejected", "notify_signal_opened", "notify_entry_filled", "notify_entry_cancelled",
            "notify_warning", "notify_risk_free", "notify_position_closed", "notify_daily_halt", "notify_tp2_reached")})
        self.analyzer = SimpleNamespace(fetch_entry_frame=AsyncMock(side_effect=RuntimeError("No frame")))
        self.trader = Trader(self.s, self.client, self.risk, self.store, self.analyzer, self.notify)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def open_position(self, side="LONG"):
        p = await self.trader.open_from_signal(signal(side), None)
        self.assertIsNotNone(p)
        await self.trader.manage()
        self.assertEqual(p.status, "OPEN")
        return p

    async def test_dry_long_and_short_lifecycle(self):
        for side in ("LONG", "SHORT"):
            self.trader.reset_virtual_balance(10)
            self.store.delete(self.risk.DAILY_KEY)
            self.risk.clear_halt()
            self.risk.roll_day(10)
            self.client.ticker = dict(last=100., bid=100., ask=100.)
            pos = await self.open_position(side)
            self.client.ticker = dict.fromkeys(("last", "bid", "ask"), pos.tp1)
            await self.trader.manage()
            self.assertEqual(pos.stage, 1)
            self.assertGreater(pos.partial_contracts, 0)
            self.client.ticker = dict.fromkeys(("last", "bid", "ask"), pos.stop_loss)
            await self.trader.manage()
            self.assertIsNone(self.trader.position)
            self.assertAlmostEqual(self.trader.virtual_balance, 10 + pos.net_pnl, places=6)
        self.assertEqual(len(self.store.recent_trades()), 2)
        self.client.create_market.assert_not_awaited()

    async def test_stop_loss_and_circuit_breaker(self):
        for _ in range(2):
            self.client.ticker = dict.fromkeys(("last", "bid", "ask"), 100.)
            pos = await self.open_position()
            self.client.ticker = dict.fromkeys(("last", "bid", "ask"), 98.9)
            await self.trader.manage()
            self.assertEqual(pos.close_reason, "SL")
        self.assertFalse(self.risk.can_trade()[0])
        self.assertEqual(self.risk.daily.consecutive_losses, 2)

    async def test_target_pauses_trading(self):
        events = self.risk.register_trade_result(1.1, 11.1)
        self.assertIn("TARGET_HIT", events)
        self.assertFalse(self.risk.can_trade()[0])

    async def test_reselecting_mode_does_not_reset_daily_limits(self):
        self.risk.register_trade_result(1.1, 11.1)
        await self.trader.switch_mode("DRY")
        self.assertTrue(self.risk.daily.target_hit)
        self.assertFalse(self.risk.can_trade()[0])

    async def test_saved_live_mode_cannot_override_dry(self):
        self.store.set("mode", "LIVE")
        t = Trader(self.s, self.client, self.risk, self.store, self.analyzer, self.notify)
        self.assertEqual(t.mode, "DRY")
        self.assertFalse((await t.switch_mode("LIVE"))[0])

    async def test_position_survives_restart(self):
        pos = await self.open_position()
        t = Trader(self.s, self.client, self.risk, self.store, self.analyzer, self.notify)
        self.assertEqual(t.position.id, pos.id)
        self.assertEqual(t.position.remaining_contracts, pos.contracts)

    async def test_live_position_cannot_be_loaded_in_dry(self):
        pos = await self.open_position()
        pos.mode = "LIVE"
        self.trader._save()
        with self.assertRaises(ValueError):
            Trader(self.s, self.client, self.risk, self.store, self.analyzer, self.notify)

    async def test_invalid_virtual_balances(self):
        for value in (float("nan"), float("inf"), -1, 0):
            with self.assertRaises(ValueError):
                self.trader.reset_virtual_balance(value)

    async def test_pause_and_stale_signal_prevent_open(self):
        self.trader.set_paused(True)
        self.assertIsNone(await self.trader.open_from_signal(signal(), None))
        self.trader.set_paused(False)
        self.client.ticker["last"] = 105
        self.assertIsNone(await self.trader.open_from_signal(signal(), None))

    async def test_pending_exit_is_not_assumed_filled(self):
        pos = await self.open_position()
        pos.mode = "LIVE"
        self.client.fetch_position_contracts.return_value = pos.remaining_contracts
        self.client.fetch_order.return_value = {"status": "open", "filled": 0}
        await self.trader._close_all(pos, "PANIC", 100)
        self.assertEqual(pos.exit_order_id, "exit1")
        self.assertEqual(pos.status, "OPEN")
        await self.trader.manage()
        self.assertEqual(self.client.create_market.await_count, 1)
        self.client.fetch_order.return_value = {"status": "closed", "filled": pos.remaining_contracts, "average": 100}
        await self.trader.manage()
        self.assertIsNone(self.trader.position)

    async def test_partial_entry_is_cancelled_then_protected(self):
        pos = await self.trader.open_from_signal(signal(), None)
        pos.mode = "LIVE"
        self.client.fetch_order.side_effect = [
            {"status": "open", "filled": 0.1, "average": 100},
            {"status": "canceled", "filled": 0.1, "average": 100},
        ]
        await self.trader.manage()
        self.client.cancel_order.assert_awaited_once()
        self.assertEqual(pos.status, "OPEN")
        self.assertEqual(pos.contracts, 0.1)
        self.client.create_stop_market.assert_awaited_once()

    async def test_unconfirmed_cancel_retains_pending_position(self):
        pos = await self.trader.open_from_signal(signal(), None)
        pos.mode = "LIVE"
        pos.created_at = "2000-01-01T00:00:00+00:00"
        self.client.fetch_order.return_value = {"status": "open", "filled": 0}
        with self.assertRaises(RuntimeError):
            await self.trader.manage()
        self.assertIs(self.trader.position, pos)
        self.assertEqual(pos.status, "PENDING")

    async def test_soft_stop_can_be_upgraded_to_native(self):
        pos = await self.open_position()
        pos.mode = "LIVE"
        pos.sl_order_id = ""
        await self.trader._move_stop(pos, 100)
        self.assertEqual(pos.sl_order_id, "stop1")
        self.assertFalse(pos.soft_stop)
        await self.trader._move_stop(pos, 99)
        self.assertEqual(pos.stop_loss, 100)

    async def test_live_partial_exit_books_actual_filled_quantity(self):
        pos = await self.open_position()
        pos.mode = "LIVE"
        before = pos.remaining_contracts
        self.client.fetch_order.return_value = {"status": "closed", "filled": 0.1, "average": 101.5}
        await self.trader._take_partial(pos, 101.5)
        self.assertAlmostEqual(pos.remaining_contracts, before - 0.1)
        self.assertEqual(pos.partial_contracts, 0.1)
        self.assertEqual(pos.stage, 1)

    async def test_partly_filled_full_exit_is_retried_for_remainder(self):
        pos = await self.open_position()
        pos.mode = "LIVE"
        self.client.fetch_position_contracts.return_value = pos.remaining_contracts
        self.client.fetch_order.return_value = {"status": "canceled", "filled": 0.1, "average": 100}
        before = pos.remaining_contracts
        await self.trader._close_all(pos, "PANIC", 100)
        self.assertAlmostEqual(pos.remaining_contracts, before - 0.1)
        self.assertEqual(pos.close_reason, "PANIC")
        self.assertIs(self.trader.position, pos)

    async def test_ambiguous_entry_blocks_further_entries(self):
        self.store.set("mode", "LIVE")
        self.client.set_isolated_and_leverage = AsyncMock(return_value=[])
        self.client.create_limit = AsyncMock(side_effect=TimeoutError("acceptance unknown"))
        self.assertIsNone(await self.trader.open_from_signal(signal(), None))
        self.assertEqual(self.trader.position.status, "UNKNOWN")
        self.assertTrue(self.trader.paused)
        self.assertIsNone(await self.trader.open_from_signal(signal(), None))
        self.client.create_limit.assert_awaited_once()

    async def test_risk_rejects_nonfinite_and_bad_targets(self):
        for values in ((10, "LONG", float("nan"), 99, 101.5, 102, 10),
                       (10, "LONG", 100, 99, 98, 102, 10),
                       (10, "LONG", 100, 90, 115, 120, 10)):
            self.assertFalse(self.risk.plan_position(*values).ok)
        plan = self.risk.plan_position(10, "LONG", 100, 99, 101.5, 102, 10)
        self.assertTrue(plan.ok)
        self.assertLessEqual(plan.expected_loss, plan.risk_budget)

    async def test_short_history_fails_cleanly(self):
        self.client.fetch_ohlcv = AsyncMock(return_value=[])
        analyzer = MarketAnalyzer(self.client, self.s)
        with self.assertRaisesRegex(RuntimeError, "Not enough raw candles"):
            await analyzer.fetch_frame("1m")


class ConfigAndAdapterTests(unittest.TestCase):
    def test_sample_configuration_is_valid(self):
        from dotenv import dotenv_values
        sample = {k: v for k, v in dotenv_values(Path(__file__).resolve().parents[1] / ".env.example").items()
                  if v is not None}
        with tempfile.TemporaryDirectory() as tmp:
            sample.update(TELEGRAM_BOT_TOKEN="123456:token", TELEGRAM_ADMIN_CHAT_ID="1", GEMINI_API_KEY="x", DATA_DIR=tmp)
            with patch.dict(os.environ, sample, clear=True):
                self.assertEqual(load_settings().exchange_id, "lbank")

    def test_instance_lock_prevents_second_process(self):
        from instance_lock import instance_lock
        with tempfile.TemporaryDirectory() as tmp:
            with instance_lock(Path(tmp)):
                with self.assertRaises(RuntimeError):
                    with instance_lock(Path(tmp)):
                        pass
            with instance_lock(Path(tmp)):
                pass
    def test_strict_env_parsing(self):
        with patch.dict(os.environ, {"DRY_RUN": "treu", "EMA_FAST": "9.5"}):
            with self.assertRaises(ValueError):
                _bool("DRY_RUN", True)
            with self.assertRaises(ValueError):
                _int("EMA_FAST", 9)

    def test_invalid_config_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, value in (("MONITOR_INTERVAL_SEC", "0"), ("STARTING_BALANCE", "nan"),
                                ("MAX_MARGIN_USAGE", "2"), ("OHLCV_LIMIT", "5")):
                env = {"TELEGRAM_BOT_TOKEN": "123456:token", "TELEGRAM_ADMIN_CHAT_ID": "1",
                       "GEMINI_API_KEY": "x", "DATA_DIR": tmp, name: value}
                with patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError):
                    load_settings()

    def test_lbank_live_blocked(self):
        client = object.__new__(ExchangeClient)
        client.trade_ex = SimpleNamespace(id="lbank")
        with self.assertRaisesRegex(RuntimeError, "LBank futures"):
            client.validate_live_support()

    def test_sandbox_failure_is_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = settings_at(Path(tmp), use_testnet=True, exchange_id="lbank")
            fake = SimpleNamespace(set_sandbox_mode=lambda _: (_ for _ in ()).throw(RuntimeError("no sandbox")))
            with patch("exchange_client.ccxt.lbank", return_value=fake), self.assertRaises(RuntimeError):
                _build_exchange("lbank", s, False)


class AITests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_attempts_respect_daily_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = settings_at(Path(tmp), ai_max_calls_per_day=2)
            store = StateStore(Path(tmp) / "s.db")
            agent = object.__new__(AIAgent)
            agent.s, agent.store = s, store
            call = AsyncMock(side_effect=RuntimeError("offline"))
            agent._genai = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=call)))
            agent._user_prompt = lambda snap, setup: "test"
            setup = SimpleNamespace(key="LONG:123")
            try:
                with patch("ai_agent.asyncio.sleep", new_callable=AsyncMock):
                    self.assertIsNone(await agent.analyze(None, setup))
                self.assertEqual(call.await_count, 1)
                self.assertEqual(agent.calls_today(), 1)
                self.assertFalse(agent.can_call("other")[0])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
