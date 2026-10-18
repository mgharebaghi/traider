"""GMX lifecycle: atomic entry+SL/TP, durable prepare-before-submit, chain reconciliation.

LIVE deliberately uses a full-size SL and full-size TP2, without software trailing
or a partial TP1. Conditional orders execute through GMX even while this bot is off.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone

from gmx_client import units, usd30
from gmx_accounting import contract_price, lifecycle_totals
from trader import Trader, Position, MODE_KEY, utcnow


class GmxTrader(Trader):
    JOURNAL = 'gmx_request'

    def __init__(self, settings, client, risk, store, analyzer, notifier):
        identity = f'gmx:{settings.gmx_chain_id}:{settings.symbol}:{settings.gmx_wallet_address.lower()}'
        previous = store.get('venue_identity')
        if previous and previous != identity:
            raw = store.get('position') or {}
            first_wallet_binding = (previous == f'gmx:{settings.gmx_chain_id}:{settings.symbol}:'
                and raw.get('mode') != 'LIVE' and not store.get(self.JOURNAL)
                and store.summary('LIVE')['trades'] == 0)
            if not first_wallet_binding:
                raise ValueError('Use a separate DATA_DIR for each GMX chain, market and wallet')
        if not previous and (store.get('position') or store.recent_trades(1)):
            raise ValueError('GMX requires a fresh DATA_DIR; keep historical exchange state separately')
        store.set('venue_identity', identity)
        super().__init__(settings, client, risk, store, analyzer, notifier)
        self._economic_warning = None

    @property
    def has_exchange_keys(self):
        return self.client.has_credentials

    def set_exchange_keys(self, *_):
        raise ValueError('کلید خصوصی فقط در تنظیمات محلی سرور؛ هرگز در تلگرام ارسال نکنید')

    def has_position(self):
        return super().has_position() or bool(self.store.get(self.JOURNAL))

    async def verify_funds_before_scan(self):
        if not await super().verify_funds_before_scan():
            return False
        balance = await self.get_balance()
        if balance * self.s.risk_per_trade_pct <= self.s.gmx_execution_budget_usd:
            key = (self.mode, balance)
            if self._economic_warning != key:
                self._economic_warning = key
                await self.notifier.notify_warning('بودجه ریسک این حساب از ذخیره اجرای GMX کمتر است؛ تحلیل سهمیه‌دار و ورود تازه انجام نمی‌شود. /guide fees را بخوانید. برای تمرین فقط موجودی مجازی را با /resetdry تغییر دهید.')
            return False
        self._economic_warning = None
        return True

    async def switch_mode(self, new_mode):
        if new_mode.upper() == 'LIVE':
            try:
                self.client.validate_live_support()
                account = await self.client.account()
                if account['positions'] or account['orders'] or self.store.get(self.JOURNAL):
                    return False, 'کیف پول باید اختصاصی و بدون پوزیشن، سفارش یا درخواست حل‌نشده باشد.', 0.0
            except Exception:
                return False, 'اتصال GMX آماده نیست؛ /guide live را بخوانید و تنظیمات کیف پول و مجوز را بررسی کنید.', 0.0
        if self.store.get(self.JOURNAL):
            return False, 'ابتدا نتیجه درخواست GMX را در /gmx و برنامه رسمی بررسی کنید.', 0.0
        return await super().switch_mode(new_mode)

    async def _journal_submit(self, quote, purpose):
        # Persist before signing/network I/O; never automatically re-submit on timeout.
        journal = dict(requestId=quote['requestId'], prepared=quote['prepared'], purpose=purpose, submittedAt=time.time())
        self.store.set(self.JOURNAL, journal)
        try:
            result = await self.client.submit(quote['prepared'])
            if result.get('requestId') != quote['requestId']:
                raise RuntimeError('GMX request ID mismatch')
            return result
        except BaseException:
            self.set_paused(True)
            raise

    async def open_from_signal(self, signal, snap):
        if self.mode == 'DRY':
            return await super().open_from_signal(signal, snap)
        async with self.lock:
            if self.has_position() or self.paused or not signal.is_trade or signal.confidence < self.s.ai_min_confidence or not self.risk.can_trade()[0]:
                return None
            self.client.validate_live_support()
            account = await self.client.account()
            if account['positions'] or account['orders']:
                self.set_paused(True)
                return None
            ticker = await self.client.fetch_ticker()
            if self.client.projected_carry_rate(signal.action) > self.s.gmx_carry_budget_pct:
                await self.notifier.notify_trade_rejected(signal, 'ذخیره funding/borrowing از هزینه زنده نگهداری کمتر است')
                return None
            if abs(ticker['last'] - signal.entry_price) / ticker['last'] > self.s.max_entry_deviation_pct:
                await self.notifier.notify_trade_rejected(signal, 'قیمت پس از تحلیل جابه‌جا شده؛ سیگنال منقضی است')
                return None
            balance = await self.get_balance()
            plan = self.risk.plan_position(balance, signal.action, signal.entry_price, signal.stop_loss,
                signal.take_profit_1, signal.take_profit_2, signal.suggested_leverage)
            if not plan.ok:
                await self.notifier.notify_trade_rejected(signal, plan.reason)
                return None
            request = dict(kind='increase', symbol=self.s.symbol, direction=signal.action.lower(), orderType='market',
                size=units(plan.notional), collateralToken='USDC',
                collateralToPay=dict(amount=units(plan.margin, 6), token='USDC'),
                slippage=self.s.slippage_pct * 10000,
                gasPaymentToken='USDC',
                tpsl=[{'type': 'stop-loss', 'triggerPrice': units(signal.stop_loss)},
                      {'type': 'take-profit', 'triggerPrice': units(signal.take_profit_2)}])
            quote = await self.client.prepare(request)
            estimates = quote.get('estimates') or {}
            if quote.get('warnings') or not estimates:
                await self.notifier.notify_trade_rejected(signal, 'GMX نتوانست ظرفیت و هزینه اجرای مطمئن را تأیید کند')
                return None
            # Reserve for entry + both conditional orders + emergency execution.
            if quote.get('paymentFeeUsd') is None:
                await self.notifier.notify_trade_rejected(signal, 'هزینه کامل Express قابل تأیید نیست')
                return None
            execution = max(4 * usd30(quote['executionFeeUsd']), 2 * usd30(quote['paymentFeeUsd']))
            impact = abs(usd30(estimates['positionPriceImpactDeltaUsd'])) + abs(usd30(estimates['swapPriceImpactDeltaUsd']))
            protocol_fee = usd30(estimates['positionFeeUsd'])
            if (execution > self.s.gmx_execution_budget_usd or protocol_fee > plan.notional * self.s.taker_fee
                or impact > plan.notional * self.s.slippage_pct
                or abs(usd30(estimates['sizeDeltaUsd']) - plan.notional) > 0.01
                or plan.margin + execution + protocol_fee > balance):
                await self.notifier.notify_trade_rejected(signal, 'هزینه یا حجم واقعی quote از بودجه محافظه‌کارانه ربات بیشتر است')
                return None
            pos = Position(id=uuid.uuid4().hex, mode='LIVE', symbol=self.s.symbol, side=signal.action, status='UNKNOWN',
                entry_price=signal.entry_price, contracts=plan.contracts, base_qty=plan.base_qty, leverage=plan.leverage,
                stop_loss=signal.stop_loss, initial_stop=signal.stop_loss, tp1=signal.take_profit_1, tp2=signal.take_profit_2,
                risk_usd=plan.expected_loss, margin=plan.margin, confidence=signal.confidence, reasoning_fa=signal.reasoning_fa,
                created_at=utcnow().isoformat(), balance_before=balance, entry_order_id=quote['requestId'], soft_stop=False)
            self.position = pos
            self._save()
            self.store.set('gmx_order_keys', [])
            await self._journal_submit(quote, 'entry')
            pos.status = 'PENDING'
            self._save()
            await self.notifier.notify_signal_opened(pos, plan, signal, ['GMX Express: خروج کامل با SL یا TP2؛ TP1 و trailing خودکار در LIVE فعال نیست.'])
            return pos

    async def _on_filled(self, pos, price, contracts, fee_override, maker):
        # Paper execution charges the reserved chain execution cost once per trade.
        if pos.mode == 'DRY':
            pos.fees_paid += self.s.gmx_execution_budget_usd
            self._add_virtual(-self.s.gmx_execution_budget_usd)
        await super()._on_filled(pos, price, contracts, fee_override, maker)

    async def _finalize(self, pos, exit_price, reason, fee_override):
        if pos.mode == 'DRY':
            carry = pos.entry_price * pos.base_qty * self.s.gmx_carry_budget_pct
            pos.fees_paid += carry
            self._add_virtual(-carry)
        await super()._finalize(pos, exit_price, reason, fee_override)

    async def _move_stop(self, pos, new_stop):
        if pos.mode == 'DRY' and pos.stage >= 1 and pos.remaining_contracts > 0:
            base = pos.remaining_contracts * self.client.contract_size
            carry = pos.entry_price * pos.base_qty * self.s.gmx_carry_budget_pct
            future_fee = self.risk.fee(pos.fill_price, base, maker=False)
            required = pos.fees_paid + carry + future_fee - pos.realized_pnl
            net_be = pos.fill_price + pos.direction * (required / base + pos.fill_price * self.s.slippage_pct + .01)
            new_stop = max(new_stop, net_be) if pos.direction == 1 else min(new_stop, net_be)
        await super()._move_stop(pos, new_stop)

    async def manage(self):
        if self.mode == 'DRY':
            return await super().manage()
        async with self.lock:
            await self._sync_live()

    async def _sync_live(self):
        journal = self.store.get(self.JOURNAL)
        entry_failed = False
        if journal:
            status = await self.client.rpc('status', requestId=journal['requestId'])
            state = status['status']
            if state in ('cancelled', 'relay_failed', 'relay_reverted'):
                entry_failed = journal['purpose'] == 'entry'
                self.set_paused(True)
                self.store.delete(self.JOURNAL)
                await self.notifier.notify_warning('درخواست GMX اجرا نشد؛ کیف پول و سفارش‌های محافظ را در برنامه رسمی بررسی کنید. ورودها متوقف شد.')
                # Do not discard position evidence without reading the account.
            elif state != 'executed':
                if time.time() - journal['submittedAt'] > 120:
                    self.set_paused(True)
                return
            else:
                keys = set(self.store.get('gmx_order_keys', [])) | set(status.get('orderKeys') or [])
                self.store.set('gmx_order_keys', sorted(keys))
        account = await self.client.account()
        positions = self.client.matching_positions(account)
        if journal and state == 'executed' and journal['purpose'] == 'panic' and positions:
            return  # API account indexing still lags the confirmed decrease; no second close.
        if journal and state == 'executed' and journal['purpose'] == 'cancel':
            if any(o['marketAddress'].lower() == self.client.market['marketTokenAddress'].lower() for o in account['orders']):
                return
            self.store.delete(self.JOURNAL)
        pos = self.position
        if not pos:
            if journal and state == 'executed' and journal['purpose'] == 'panic' and not positions:
                self.store.delete(self.JOURNAL)
            if account['positions'] or account['orders']:
                self.set_paused(True)
            return
        if positions:
            if len(positions) != 1 or positions[0]['isLong'] != (pos.side == 'LONG'):
                self.set_paused(True)
                raise RuntimeError('GMX position mismatch')
            p = positions[0]
            if journal and state == 'executed' and journal['purpose'] == 'entry':
                self.store.delete(self.JOURNAL)
            if pos.status != 'OPEN':
                fill_price = usd30(p['entryPrice'])
                with self.store.transaction():
                    self.risk.register_entry()
                    pos.status = 'OPEN'
                    pos.fill_price = fill_price
                    pos.filled_at = utcnow().isoformat()
                    self._save()
            pos.remaining_contracts = int(p['sizeInTokens']) / 10 ** self.client.index_decimals
            pos.base_qty = pos.remaining_contracts
            pos.contracts = pos.remaining_contracts
            self._save()
            # Require native full-size SL. Missing/frozen protection halts entries and alerts.
            stops = [o for o in account['orders'] if o['marketAddress'].lower() == p['marketAddress'].lower()
                and o['isLong'] == p['isLong'] and int(o['orderType']) == 6 and not o['isFrozen']
                and int(o['sizeDeltaUsd']) >= int(p['sizeInUsd'])
                and o['initialCollateralTokenAddress'].lower() == self.client.market['shortTokenAddress'].lower()]
            if not stops:
                self.set_paused(True)
                raise RuntimeError('GMX native stop not confirmed; inspect app.gmx.io and use /panic if needed')
            if self.store.get('gmx_filled_notified') != pos.id:
                await self.notifier.notify_entry_filled(pos)
                self.store.set('gmx_filled_notified', pos.id)
            if (utcnow() - datetime.fromisoformat(pos.created_at)).total_seconds() > self.s.gmx_max_holding_minutes * 60:
                await self._request_close(p)
            return
        # An indexer can lag execution. Never infer a close from a single empty position result.
        if pos.status in ('UNKNOWN', 'PENDING'):
            self.set_paused(True)
            if entry_failed and not account['orders']:
                self.position = None
                self._save()
                return
            await self._settle_gmx(pos, account)
            return
        self.set_paused(True)
        await self._settle_gmx(pos, account)

    async def _settle_gmx(self, pos, account):
        market = self.client.market['marketTokenAddress']
        history = await self.client.rpc('history', address=self.s.gmx_wallet_address, marketAddress=market,
            since=int(datetime.fromisoformat(pos.created_at).timestamp()) - 2)
        if history.get('hasMore'):
            raise RuntimeError('GMX history requires pagination; settlement held for inspection')
        keys = set(self.store.get('gmx_order_keys', []))
        trades = [t for t in history['trades'] if t['eventName'] == 'OrderExecuted' and t.get('isLong') == (pos.side == 'LONG') and t['orderKey'] in keys]
        closed = [t for t in trades if int(t['orderType']) in (4, 5, 6, 7)]
        opened = [t for t in trades if int(t['orderType']) in (2, 3, 8)]
        if not opened or not closed:
            return  # Wait for trade evidence, including after restart.
        size_open = sum(int(t['sizeDeltaUsd']) for t in opened)
        if sum(int(t['sizeDeltaUsd']) for t in closed) < size_open:
            return
        unobserved_entry = not pos.filled_at
        if unobserved_entry:
            pos.fill_price = contract_price(opened[0]['executionPrice'], self.client.index_decimals)
            pos.filled_at = datetime.fromtimestamp(int(opened[0]['timestamp']), timezone.utc).isoformat()
        # USDC collateral is mandatory; report conservative native-execution reserve separately.
        for t in trades:
            if t['initialCollateralTokenAddress'].lower() != self.client.market['shortTokenAddress'].lower():
                raise RuntimeError('Unexpected collateral in GMX trade history')
        try:
            gross, fees = lifecycle_totals(trades, self.s.gmx_execution_budget_usd)
        except (KeyError, ValueError):
            raise RuntimeError('Incomplete GMX PnL/fee evidence; settlement deferred') from None
        pos.realized_pnl, pos.fees_paid = gross, fees
        pos.remaining_contracts, pos.status = 0.0, 'CLOSED'
        last_close = max(closed, key=lambda t: int(t['timestamp']))
        pos.close_reason = {5: 'TP2', 6: 'SL'}.get(int(last_close['orderType']), 'EXTERNAL')
        pos.closed_at = datetime.fromtimestamp(int(last_close['timestamp']), timezone.utc).isoformat()
        pos.exit_price_avg = sum(contract_price(t['executionPrice'], self.client.index_decimals) * int(t['sizeDeltaUsd']) for t in closed) / sum(int(t['sizeDeltaUsd']) for t in closed)
        net, balance = pos.net_pnl, await self.get_balance()
        record = dict(asdict(pos), net_pnl=net, execution_fee_is_estimate=True, exchange='gmx', chain_id=self.s.gmx_chain_id)
        with self.store.transaction():
            if unobserved_entry:
                self.risk.register_entry()
            self.risk.register_trade_result(net, balance)
            self.store.record_trade(record)
            self.store.delete('position')
            self.store.delete(self.JOURNAL)
            self.store.delete('gmx_order_keys')
        self.position = None
        await self.notifier.notify_position_closed(pos, net, balance, self.risk.daily, self.risk.daily_target(), self.risk.daily_loss_limit())
        await self.notifier.notify_warning('هزینه اجرا با ذخیره محافظه‌کارانه برآورد شده است. ورود جدید متوقف است؛ سفارش‌های باقی‌مانده و نتیجه را در GMX بررسی کنید، سپس /resume بزنید.')

    async def panic_close(self):
        if self.mode == 'DRY':
            return await super().panic_close()
        async with self.lock:
            self.set_paused(True)
            if self.store.get(self.JOURNAL):
                return ['نتیجه درخواست قبلی هنوز قطعی نیست؛ /gmx را ببینید و در app.gmx.io وضعیت را بررسی کنید. سفارش تکراری ارسال نشد.']
            account = await self.client.account()
            positions = self.client.matching_positions(account)
            if len(positions) > 1:
                return ['چند پوزیشن وجود دارد؛ از برنامه رسمی GMX ببندید.']
            if positions:
                p = positions[0]
                await self._request_close(p)
                return ['درخواست بستن ارسال شد؛ تا تأیید executed معامله بسته‌شده محسوب نمی‌شود. حد ضرر فعلی حفظ شد.']
            orders = [o['key'] for o in account['orders'] if o['marketAddress'].lower() == self.client.market['marketTokenAddress'].lower()]
            if orders:
                quote = await self.client.rpc('prepareCancel', orderIds=orders, address=self.s.gmx_wallet_address)
                await self._journal_submit(quote, 'cancel')
                return ['درخواست لغو سفارش‌های باقی‌مانده ارسال شد؛ /gmx را بررسی کنید.']
            return ['پوزیشن و سفارش فعالی در بازار انتخاب‌شده پیدا نشد.']

    async def _request_close(self, position):
        quote = await self.client.prepare(dict(kind='decrease', symbol=self.s.symbol,
            direction='long' if position['isLong'] else 'short', orderType='market', size=position['sizeInUsd'],
            collateralToken='USDC', receiveToken='USDC', keepLeverage=True, gasPaymentToken='USDC',
            slippage=self.s.slippage_pct * 10000))
        await self._journal_submit(quote, 'panic')

    async def reconcile_on_startup(self):
        if self.mode == 'DRY':
            return await super().reconcile_on_startup()
        await self.manage()
        return 'وضعیت GMX از API بررسی شد؛ درخواست‌های حل‌نشده دوباره ارسال نمی‌شوند.'

    async def status_snapshot(self):
        info = await super().status_snapshot()
        if self.mode == 'LIVE' and self.position:
            try:
                positions = self.client.matching_positions(await self.client.account())
                if len(positions) == 1:
                    p = positions[0]
                    opening_fee_reserve = self.position.entry_price * self.position.base_qty * self.s.taker_fee
                    info['unrealized'] = usd30(p['pnlAfterAllFees']) - opening_fee_reserve - self.s.gmx_execution_budget_usd
                else:
                    info.pop('unrealized', None)
            except Exception:
                info.pop('unrealized', None)
        return info
