"""Quota, restart and exchange-boundary regression tests (fully offline)."""
import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ai_agent import AIAgent
from exchange_client import ExchangeClient
from state_store import StateStore
from test_bot import settings_at, FakeExchange
from risk_manager import RiskManager


class QuotaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.s = settings_at(self.path, ai_quota_block_until="")
        self.store = StateStore(self.path / "quota.db")
        self.agent = object.__new__(AIAgent)
        self.agent.s, self.agent.store = self.s, self.store
        self.agent._user_prompt = lambda *_: "market snapshot"
        self.response = SimpleNamespace(text=json.dumps(dict(
            action="WAIT", confidence=50, entry_price=100, stop_loss=99,
            take_profit_1=102, take_profit_2=103, suggested_leverage=2, reasoning_fa="صبر")))
        self.call = AsyncMock(return_value=self.response)
        self.agent._genai = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=self.call)))
        self.setup = SimpleNamespace(key="LONG:1", reasons=[])

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_manual_force_cannot_bypass_cooldown(self):
        self.assertIsNotNone(await self.agent.analyze(None, self.setup))
        self.assertIsNone(await self.agent.analyze(None, SimpleNamespace(key="manual"), force=True))
        self.call.assert_awaited_once()

    async def test_concurrent_manual_and_scanner_reserve_once(self):
        results = await asyncio.gather(*(self.agent.analyze(None, self.setup, force=True) for _ in range(8)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(self.agent.calls_today(), 1)

    async def test_daily_budget_survives_restart_and_model_change(self):
        usage = self.agent._usage()
        usage["count"] = 18
        self.store.set(self.agent.USAGE_KEY, usage)
        self.agent.set_model("gemini-3.6-flash")
        self.assertIsNone(await self.agent.analyze(None, self.setup, force=True))
        other = object.__new__(AIAgent)
        other.s, other.store = self.s, self.store
        self.assertFalse(other.can_call("different")[0])
        self.call.assert_not_awaited()

    async def test_429_blocks_until_pacific_midnight_and_persists(self):
        self.call.side_effect = RuntimeError("429 RESOURCE_EXHAUSTED")
        await self.agent.analyze(None, self.setup)
        usage = self.agent._usage()
        until = datetime.fromtimestamp(usage["blocked_until"], timezone.utc)
        from zoneinfo import ZoneInfo
        self.assertEqual(until.astimezone(ZoneInfo("America/Los_Angeles")).hour, 0)
        self.assertFalse(self.agent.can_call("new")[0])
        await self.agent.analyze(None, self.setup, force=True)
        self.assertEqual(self.call.await_count, 1)

    async def test_invalid_json_is_counted_without_immediate_retry(self):
        self.response.text = "not json"
        self.assertIsNone(await self.agent.analyze(None, self.setup))
        self.assertEqual(self.agent.calls_today(), 1)
        self.call.assert_awaited_once()

    async def test_model_error_never_silently_changes_model(self):
        self.call.side_effect = RuntimeError("404 model not found")
        await self.agent.analyze(None, self.setup)
        self.assertEqual(self.agent.active_model, self.s.gemini_model)
        self.assertFalse(self.agent.can_call("new")[0])
        self.call.assert_awaited_once()

    async def test_input_token_budget_blocks_before_network(self):
        from dataclasses import replace
        self.agent.s = replace(self.s, ai_input_tokens_per_minute=100)
        self.assertIsNone(await self.agent.analyze(None, self.setup))
        self.assertEqual(self.agent.calls_today(), 0)
        self.call.assert_not_awaited()

    async def test_prior_project_exhaustion_blocks_before_network(self):
        from dataclasses import replace
        self.agent.s = replace(self.s, ai_quota_block_until="2099-01-01T00:00:00+00:00")
        self.assertIsNone(await self.agent.analyze(None, self.setup, force=True))
        self.call.assert_not_awaited()

    async def test_utc_budget_migration_carries_count(self):
        self.store.set(self.agent.USAGE_KEY, {"date": "2000-01-01", "count": 29, "keys": []})
        self.assertEqual(self.agent.calls_today(), 29)
        self.assertFalse(self.agent.can_call("new")[0])

    async def test_budget_reset_uses_pacific_not_utc(self):
        class Clock(datetime):
            value = datetime(2026, 10, 18, 6, 59, tzinfo=timezone.utc)
            @classmethod
            def now(cls, tz=None):
                return cls.value.astimezone(tz)
        with patch("ai_agent.datetime", Clock):
            self.assertEqual(self.agent._usage()["date"], "2026-10-17")
            self.agent._register_call("used")
            Clock.value = datetime(2026, 10, 18, 7, 0, tzinfo=timezone.utc)
            self.assertEqual(self.agent.calls_today(), 0)
            self.assertEqual(self.agent._usage()["date"], "2026-10-18")


class RiskIsolationTests(unittest.TestCase):
    def test_switching_modes_keeps_limits_and_day_roll_reopens_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = settings_at(Path(tmp))
            store = StateStore(Path(tmp) / "risk.db")
            try:
                risk = RiskManager(s, store, FakeExchange())
                risk.roll_day(10)
                for _ in range(4):
                    risk.register_entry()
                self.assertFalse(risk.can_trade()[0])
                risk.activate_mode("LIVE")
                risk.roll_day(100)
                self.assertTrue(risk.can_trade()[0])
                risk.activate_mode("DRY")
                self.assertFalse(risk.can_trade()[0])
                raw = store.get(risk.DAILY_KEY)
                raw["date"] = "2000-01-01"
                store.set(risk.DAILY_KEY, raw)
                self.assertTrue(risk.needs_roll())
                risk.roll_day(10)
                self.assertTrue(risk.can_trade()[0])
            finally:
                store.close()

    def test_next_trade_risk_cannot_exceed_remaining_daily_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = settings_at(Path(tmp))
            store = StateStore(Path(tmp) / "risk.db")
            try:
                risk = RiskManager(s, store, FakeExchange())
                risk.roll_day(10)
                d = risk.daily
                d.realized_pnl = -0.28  # daily limit is $0.30
                risk._save_daily(d)
                plan = risk.plan_position(10, "LONG", 100, 99, 101.5, 102, 5)
                self.assertTrue(plan.ok)
                self.assertLessEqual(plan.expected_loss, 0.0200001)
            finally:
                store.close()


class AdapterBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_paper_selftest_settles_win_and_loss_without_production_state(self):
        from paper_check import run_paper_check
        with tempfile.TemporaryDirectory() as tmp:
            s = settings_at(Path(tmp))
            results = await run_paper_check(s)
            self.assertGreater(results[0]["net_pnl"], 0)
            self.assertLess(results[1]["net_pnl"], 0)
            self.assertEqual(list(Path(tmp).glob("*.db")), [])

    async def test_candle_error_never_falls_back_to_spot(self):
        client = object.__new__(ExchangeClient)
        call = AsyncMock(side_effect=RuntimeError("unavailable"))
        client.symbol = "BTC/USDT:USDT"
        client.data_ex = SimpleNamespace(fetch_ohlcv=call)
        with self.assertRaises(RuntimeError):
            await client.fetch_ohlcv("1m", 300)
        call.assert_awaited_once_with("BTC/USDT:USDT", timeframe="1m", limit=300)

    async def test_lbank_balance_not_reported_as_futures(self):
        client = object.__new__(ExchangeClient)
        client.trade_ex = SimpleNamespace(id="lbank", fetch_balance=AsyncMock())
        with self.assertRaisesRegex(RuntimeError, "LBank futures"):
            await client.fetch_free_usdt()
        client.trade_ex.fetch_balance.assert_not_awaited()

    async def test_empty_balance_is_not_zero(self):
        client = object.__new__(ExchangeClient)
        client.validate_live_support = lambda: None
        client.trade_ex = SimpleNamespace(id="okx", fetch_balance=AsyncMock(return_value={"info": {}}))
        with self.assertRaisesRegex(RuntimeError, "Unable to fetch"):
            await client.fetch_free_usdt()
        client.trade_ex.fetch_balance.assert_awaited_once_with({"type": "swap"})
