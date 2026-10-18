"""GMX protocol boundaries and failure recovery. No network or wallet signing."""
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from test_bot import settings_at
from ai_agent import TradeSignal
from ai_agent import AIAgent
from gmx_client import GmxClient, units, usd30
from gmx_trader import GmxTrader
from gmx_guide import LESSONS
from gmx_accounting import contract_price, lifecycle_totals
from paper_check import SilentNotifier, run_paper_check
from risk_manager import RiskManager
from state_store import StateStore


class GmxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = settings_at(Path(self.temp.name), exchange_id='gmx', symbol='BTC/USD [BTC-USDC]',
            entry_post_only=False, maker_fee=.0007, taker_fee=.0007, starting_balance=1000,
            dry_run=False, gmx_live_enabled=True, gmx_wallet_address='0x'+'1'*40, gmx_private_key='0x'+'2'*64)
        self.store = StateStore(Path(self.temp.name) / 'test.db')
        self.addCleanup(self.store.close)
        self.client = GmxClient(self.s)
        self.client.market = dict(marketTokenAddress='market', shortTokenAddress='usdc',
            minPositionSizeUsd=units(1), minCollateralUsd=units(1))
        self.client.index_decimals = 8
        self.client.fetch_ticker = AsyncMock(return_value=dict(last=100, bid=100, ask=100))
        self.client.projected_carry_rate = lambda _: 0.0001
        self.account = dict(balances=[dict(symbol='USDC', balance='1000000000', decimals=6)], positions=[], orders=[])
        self.client.account = AsyncMock(return_value=self.account)
        self.client.submit = AsyncMock(return_value={'requestId': 'req1', 'status': 'relay_accepted'})
        self.client.rpc = AsyncMock()
        self.risk = RiskManager(self.s, self.store, self.client)
        self.trader = GmxTrader(self.s, self.client, self.risk, self.store, None, SilentNotifier())
        self.risk.roll_day(1000)
        self.signal = TradeSignal('LONG', 90, 100, 99, 103, 108, 2, 'test')
        plan = self.risk.plan_position(1000, 'LONG', 100, 99, 103, 108, 2)
        self.assertTrue(plan.ok, plan.reason)
        self.client.prepare = AsyncMock(return_value=dict(prepared='{}', requestId='req1', warnings=[],
            executionFeeUsd=units(.01), paymentFeeUsd=units(.02), estimates=dict(
            positionPriceImpactDeltaUsd='0', swapPriceImpactDeltaUsd='0', positionFeeUsd=units(.01), sizeDeltaUsd=units(plan.notional))))

    async def test_entry_contains_native_full_stop_and_target_and_bps(self):
        pos = await self.trader.open_from_signal(self.signal, None)
        self.assertEqual(pos.status, 'PENDING')
        request = self.client.prepare.call_args.args[0]
        self.assertEqual(request['slippage'], self.s.slippage_pct * 10000)
        self.assertEqual([o['type'] for o in request['tpsl']], ['stop-loss', 'take-profit'])
        self.assertTrue(all('size' not in o for o in request['tpsl']))
        self.assertEqual(self.store.get('gmx_request')['requestId'], 'req1')

    async def test_timeout_persists_request_and_never_duplicates(self):
        self.client.submit.side_effect = TimeoutError('unknown acceptance')
        with self.assertRaises(TimeoutError):
            await self.trader.open_from_signal(self.signal, None)
        self.assertTrue(self.trader.paused)
        self.assertEqual(self.store.get('gmx_request')['requestId'], 'req1')
        self.trader.set_paused(False)
        self.assertIsNone(await self.trader.open_from_signal(self.signal, None))
        self.client.submit.assert_awaited_once()

    async def test_created_is_not_filled(self):
        await self.trader.open_from_signal(self.signal, None)
        self.client.rpc.return_value = dict(status='created')
        await self.trader.manage()
        self.assertEqual(self.trader.position.status, 'PENDING')
        self.assertIsNotNone(self.store.get('gmx_request'))
        self.assertEqual(self.risk.daily.entries, 0)

    async def test_quote_fee_above_budget_is_rejected(self):
        self.client.prepare.return_value['paymentFeeUsd'] = units(10)
        self.assertIsNone(await self.trader.open_from_signal(self.signal, None))
        self.client.submit.assert_not_awaited()

    async def test_unknown_relay_fee_rejects_entry(self):
        self.client.prepare.return_value['paymentFeeUsd'] = None
        self.assertIsNone(await self.trader.open_from_signal(self.signal, None))
        self.client.submit.assert_not_awaited()

    async def test_unmanaged_orders_prevent_entry(self):
        self.account['orders'] = [{'key': 'orphan'}]
        await self.trader.open_from_signal(self.signal, None)
        self.assertTrue(self.trader.paused)
        self.client.submit.assert_not_awaited()

    async def test_panic_preserves_unknown_request(self):
        await self.trader.open_from_signal(self.signal, None)
        await self.trader.panic_close()
        self.assertEqual(self.client.submit.await_count, 1)
        self.assertIsNotNone(self.store.get('gmx_request'))

    async def test_rejected_entry_with_empty_account_reconciles(self):
        await self.trader.open_from_signal(self.signal, None)
        self.client.rpc.return_value = dict(status='relay_reverted')
        await self.trader.manage()
        self.assertIsNone(self.trader.position)
        self.assertIsNone(self.store.get('gmx_request'))
        self.assertTrue(self.trader.paused)

    async def test_restart_finds_executed_request_and_requires_native_stop(self):
        await self.trader.open_from_signal(self.signal, None)
        self.client.rpc.return_value = dict(status='executed')
        self.account['positions'] = [dict(marketAddress='market', isLong=True, sizeInUsd=units(100), sizeInTokens='100000000', entryPrice=units(100))]
        restored = GmxTrader(self.s, self.client, self.risk, self.store, None, SilentNotifier())
        with self.assertRaisesRegex(RuntimeError, 'native stop'):
            await restored.manage()
        self.assertTrue(restored.paused)
        self.assertEqual(restored.position.status, 'OPEN')

    async def test_economic_gate_rejects_tiny_capital_and_gross_only_rr(self):
        self.assertFalse(self.risk.plan_position(10, 'LONG', 100, 99, 103, 106, 2).ok)
        self.assertFalse(self.risk.plan_position(1000, 'LONG', 100, 99, 101.5, 102, 2).ok)

    async def test_tiny_balance_blocks_before_ai_scan(self):
        self.client.fetch_free_usdt = AsyncMock(return_value=10)
        self.assertFalse(await self.trader.verify_funds_before_scan())

    async def test_gemini_request_includes_chart_and_exact_model(self):
        agent = object.__new__(AIAgent)
        agent.s, agent.store, agent.client = self.s, self.store, self.client
        agent._user_prompt = lambda *_: 'GMX candles and indicators'
        agent._normalise = lambda parsed, *_: parsed
        body = dict(action='WAIT', confidence=50, entry_price=100, stop_loss=99, take_profit_1=103,
            take_profit_2=106, suggested_leverage=2, reasoning_fa='test')
        call = AsyncMock(return_value=SimpleNamespace(text=json.dumps(body)))
        agent._genai = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=call)))
        with patch('chart_image.render_chart', return_value=b'chart-png'):
            await agent.analyze(SimpleNamespace(), SimpleNamespace(key='LONG:1'))
        self.assertEqual(call.await_args.kwargs['model'], 'gemini-3.8-flash')
        parts = call.await_args.kwargs['contents']
        self.assertEqual(parts[1].inline_data.mime_type, 'image/png')
        self.assertEqual(parts[1].inline_data.data, b'chart-png')
        self.assertEqual(agent.calls_today(), 1)

    async def test_gmx_paper_wallet_matches_ledger_with_execution_and_carry(self):
        results = await run_paper_check(replace(self.s, dry_run=True))
        self.assertGreater(results[0]['net_pnl'], 0)
        self.assertLess(results[1]['net_pnl'], 0)
        self.assertGreater(results[0]['fees'], self.s.gmx_execution_budget_usd)

    async def test_stale_market_values_are_rejected(self):
        client = GmxClient(self.s)
        client.market = self.client.market
        client.rpc = AsyncMock(return_value=dict(tickers=[dict(symbol=self.s.symbol, minPrice=units(100), maxPrice=units(100), markPrice=units(100))],
            values=[dict(marketTokenAddress='market', updatedAt=(time.time()-500)*1000)]))
        with self.assertRaisesRegex(RuntimeError, 'stale'):
            await client.fetch_ticker()

    async def test_candles_have_no_invented_volume(self):
        client = GmxClient(self.s)
        client.rpc = AsyncMock(return_value=[dict(timestamp=int(time.time()*1000), open='100', high='101', low='99', close='100')])
        candles = await client.fetch_ohlcv('1m', 1)
        self.assertEqual(candles[0][-1], 0)

    async def test_native_usdc_missing_is_not_zero(self):
        self.account['balances'] = [dict(symbol='USDC.e', balance='10', decimals=6)]
        with self.assertRaisesRegex(RuntimeError, 'USDC balance'):
            await self.client.fetch_free_usdt()

    async def test_venue_identity_cannot_change_under_existing_state(self):
        with self.assertRaisesRegex(ValueError, 'separate DATA_DIR'):
            GmxTrader(replace(self.s, gmx_chain_id=43114), self.client, self.risk, self.store, None, SilentNotifier())

    async def test_first_public_wallet_can_be_added_after_paper_setup(self):
        self.store.set('venue_identity', f'gmx:{self.s.gmx_chain_id}:{self.s.symbol}:')
        GmxTrader(self.s, self.client, self.risk, self.store, None, SilentNotifier())
        self.assertTrue(self.store.get('venue_identity').endswith(self.s.gmx_wallet_address))

    async def test_private_key_cannot_be_saved_from_telegram(self):
        with self.assertRaises(ValueError):
            self.trader.set_exchange_keys('key', 'secret')
        self.assertIsNone(self.store.get('exchange_api_secret'))

    async def test_settlement_transaction_rolls_back(self):
        self.store.set('counter', 1)
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.store.set('counter', 2)
                raise RuntimeError('crash')
        self.assertEqual(self.store.get('counter'), 1)

    async def test_close_while_offline_recovers_fill_and_settles_once(self):
        pos = await self.trader.open_from_signal(self.signal, None)
        size = self.client.prepare.return_value['estimates']['sizeDeltaUsd']
        row = dict(eventName='OrderExecuted', isLong=True, sizeDeltaUsd=size, marketAddress='market',
            initialCollateralTokenAddress='usdc', srcChainId='0', positionFeeAmount='100000', borrowingFeeAmount='0',
            fundingFeeAmount='0', collateralTokenPriceMin='1000000000000000000000000',
            executionPrice=str(int(units(100)) // 10**8), timestamp=int(time.time()))
        opened = dict(row, orderKey='entry', orderType=2)
        closed = dict(row, orderKey='target', orderType=5, basePnlUsd=units(10), totalImpactUsd=units(-.1))
        self.store.set('gmx_order_keys', ['entry', 'target'])
        self.store.delete('gmx_request')
        self.client.rpc.return_value = dict(trades=[opened, closed], hasMore=False)
        await self.trader.manage()
        self.assertIsNone(self.trader.position)
        self.assertTrue(self.trader.paused)
        record = self.store.recent_trades()[0]
        self.assertEqual(record['entry_price'], 100)
        self.assertEqual(record['exit_price'], 100)
        self.assertAlmostEqual(record['net_pnl'], 9.9 - .2 - self.s.gmx_execution_budget_usd)
        self.assertEqual(self.risk.daily.entries, 1)
        self.assertEqual(self.risk.daily.trades, 1)
        self.client.account.return_value['orders'] = []
        await self.trader.manage()
        self.assertEqual(self.risk.daily.trades, 1)

    async def test_executed_close_with_stale_account_never_closes_again(self):
        await self.trader.open_from_signal(self.signal, None)
        journal = self.store.get('gmx_request')
        journal['purpose'] = 'panic'
        self.store.set('gmx_request', journal)
        self.client.rpc.return_value = dict(status='executed', orderKeys=['close'])
        self.account['positions'] = [dict(marketAddress='market', isLong=True, sizeInUsd=units(100))]
        await self.trader.manage()
        self.assertEqual(self.client.submit.await_count, 1)
        self.assertIsNotNone(self.store.get('gmx_request'))

    async def test_lessons_fit_telegram_limit(self):
        self.assertTrue({'deposit', 'withdraw', 'network', 'fees', 'orders', 'live'} <= LESSONS.keys())
        self.assertTrue(all(len(body) < 3000 for _, body in LESSONS.values()))


class UnitTests(unittest.TestCase):
    def test_usd_units_are_exact(self):
        self.assertEqual(units(0.1), '100000000000000000000000000000')
        self.assertEqual(units(0.000001, 6), '1')
        self.assertEqual(usd30(units(123.45)), 123.45)

    def test_contract_price_differs_from_usd30(self):
        self.assertAlmostEqual(contract_price('816164119196902350961573057', 8), 81616.41191969024)

    def test_base_pnl_and_fees_are_counted_once(self):
        opened = dict(orderType=2, srcChainId='0', positionFeeAmount='100000', borrowingFeeAmount='0', fundingFeeAmount='0',
            collateralTokenPriceMin='1000000000000000000000000', priceImpactUsd=units(-10))
        closed = dict(opened, orderType=4, basePnlUsd=units(5), pnlUsd=units(4.8), totalImpactUsd=units(-.1),
            positionFeeAmount='100000', borrowingFeeAmount='50000', fundingFeeAmount='20000',
            traderDiscountAmount='10000', priceImpactDiffUsd=units(.02))
        gross, fees = lifecycle_totals([opened, closed], .5)
        self.assertAlmostEqual(gross, 4.88)
        self.assertAlmostEqual(fees, .76)
        self.assertAlmostEqual(gross-fees, 4.12)

    def test_collateral_depeg_is_not_assumed_one_dollar(self):
        row = dict(orderType=2, srcChainId='0', positionFeeAmount='1000000', borrowingFeeAmount='0', fundingFeeAmount='0',
            collateralTokenPriceMin='980000000000000000000000')
        self.assertAlmostEqual(lifecycle_totals([row], 0)[1], .98)
