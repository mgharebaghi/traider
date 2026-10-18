"""GMX V2 API/SDK adapter. USD sizes use Decimal and 30 on-chain decimals."""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from types import SimpleNamespace


def units(value: float, decimals: int = 30) -> str:
    return str(int(Decimal(str(value)) * Decimal(10) ** decimals))


def usd30(value) -> float:
    return float(Decimal(str(value)) / Decimal(10) ** 30)


class GmxClient:
    contract_size = 1.0
    min_amount = 0.0

    def __init__(self, settings):
        self.s = settings
        self.symbol = settings.symbol
        self.trade_ex = self.data_ex = SimpleNamespace(id=f"gmx:{settings.gmx_chain_id}")
        self.market = {}
        self._processes = set()
        self.market_values = {}

    @property
    def has_credentials(self):
        return bool(self.s.gmx_wallet_address and self.s.gmx_private_key)

    async def rpc(self, op, **kwargs):
        env = dict(os.environ)
        env.pop('GMX_PRIVATE_KEY', None)
        env['GMX_LIVE_ENABLED'] = 'true' if self.s.gmx_live_enabled else 'false'
        if op == 'submit':
            self.validate_live_support()
            env['GMX_PRIVATE_KEY'] = self.s.gmx_private_key
        proc = await asyncio.create_subprocess_exec(
            'node', str(Path(__file__).with_name('gmx_bridge.mjs')),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env,
        )
        self._processes.add(proc)
        try:
            payload = json.dumps(dict(op=op, chainId=self.s.gmx_chain_id, **kwargs)).encode()
            out, _ = await asyncio.wait_for(proc.communicate(payload), timeout=55)
            response = json.loads(out)
            if proc.returncode or not response.get('ok'):
                raise RuntimeError(f"GMX {op} failed (HTTP={response.get('statusCode')}, code={response.get('code')}, fields={response.get('fields')}, detail={response.get('detail', '')}); check wallet, allowance, gas and API access")
            return response['result']
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            self._processes.discard(proc)

    async def initialize(self):
        markets = await self.rpc('markets')
        self.market = next((m for m in markets if m['symbol'] == self.symbol and not m.get('isSpotOnly')), {})
        if not self.market:
            raise ValueError('GMX market not found; use an exact symbol from npm run check:gmx')
        tokens = await self.rpc('tokens')
        index = next(t for t in tokens if t['address'].lower() == self.market['indexTokenAddress'].lower())
        self.index_decimals = int(index['decimals'])
        collateral = next(t for t in tokens if t['address'].lower() == self.market['shortTokenAddress'].lower())
        if collateral['symbol'] != 'USDC' or int(collateral['decimals']) != 6:
            raise ValueError('Only native USDC-collateral markets are supported')
        if not self.s.dry_run:
            self.validate_live_support()

    @property
    def min_cost(self):
        return usd30(self.market.get('minPositionSizeUsd', '0'))

    @property
    def min_collateral(self):
        return usd30(self.market.get('minCollateralUsd', '0'))

    def validate_live_support(self):
        if not self.s.gmx_live_enabled or not self.has_credentials:
            raise RuntimeError('GMX LIVE requires local wallet settings and GMX_LIVE_ENABLED=true; see GUIDE_GMX_FA.md')

    def set_credentials(self, *_):
        raise ValueError('GMX uses a local wallet signer; never send a private key to Telegram')

    def amount_to_precision(self, amount):
        return float(Decimal(str(max(0, amount))).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN))

    def price_to_precision(self, price):
        return float(Decimal(str(price)).quantize(Decimal('0.01'), rounding=ROUND_DOWN))

    async def fetch_ohlcv(self, timeframe, limit):
        candles = await self.rpc('candles', symbol=self.symbol, timeframe=timeframe, limit=limit)
        rows = [[int(c['timestamp']), *[float(c[k]) for k in ('open', 'high', 'low', 'close')], 0.0] for c in candles]
        rows.sort(key=lambda c: c[0])
        seconds = {'1m': 60, '5m': 300, '15m': 900, '1h': 3600}.get(timeframe)
        if not seconds:
            raise ValueError('GMX timeframe must be 1m, 5m, 15m or 1h')
        if not rows or time.time() - rows[-1][0] / 1000 > seconds * 2 + self.s.gmx_max_price_age_sec:
            raise RuntimeError('GMX candles are stale')
        for row in rows:
            if not all(math.isfinite(v) and v > 0 for v in row[:5]) or row[2] < max(row[1], row[4]) or row[3] > min(row[1], row[4]):
                raise RuntimeError('Invalid GMX candle')
        return rows

    async def fetch_ticker(self, source='data'):
        data = await self.rpc('ticker', symbol=self.symbol)
        ticker = next(t for t in data['tickers'] if t['symbol'] == self.symbol)
        values = next(v for v in data['values'] if v['marketTokenAddress'].lower() == self.market['marketTokenAddress'].lower())
        self.market_values = values
        updated = values.get('updatedAt')
        if not updated or not -10 <= time.time() - float(updated) / 1000 <= self.s.gmx_max_price_age_sec:
            raise RuntimeError('GMX market values are stale or lack a timestamp')
        bid, ask, price = [usd30(ticker[k]) for k in ('minPrice', 'maxPrice', 'markPrice')]
        if not all(math.isfinite(v) and v > 0 for v in (bid, ask, price)) or bid > ask:
            raise RuntimeError('Invalid GMX oracle prices')
        # GMX min/max oracle prices are not an order book bid/ask.
        return dict(last=price, bid=bid, ask=ask)

    def projected_carry_rate(self, side):
        v = self.market_values
        borrowing = usd30(v['borrowingFactorPerSecondForLongs' if side == 'LONG' else 'borrowingFactorPerSecondForShorts'])
        funding = abs(usd30(v['fundingFactorPerSecond']))
        return (max(0, borrowing) + funding) * self.s.gmx_max_holding_minutes * 60

    async def account(self):
        if not self.s.gmx_wallet_address:
            raise RuntimeError('Set GMX_WALLET_ADDRESS for read-only wallet inspection')
        return await self.rpc('account', address=self.s.gmx_wallet_address)

    async def fetch_free_usdt(self):
        """Historical interface name; GMX returns native USDC wallet balance."""
        account = await self.account()
        balances = [b for b in account['balances'] if b['symbol'] == 'USDC']
        if len(balances) != 1:
            raise RuntimeError('Native USDC balance missing or ambiguous')
        token = balances[0]
        return float(Decimal(token['balance']) / Decimal(10) ** token['decimals'])

    async def fetch_spot_usdt(self):
        return None

    def matching_positions(self, account):
        return [p for p in account['positions'] if p['marketAddress'].lower() == self.market['marketTokenAddress'].lower() and int(p['sizeInUsd']) > 0]

    async def fetch_position_contracts(self):
        return sum(usd30(p['sizeInUsd']) for p in self.matching_positions(await self.account()))

    async def prepare(self, request):
        if request.get('gasPaymentToken') == 'USDC':
            request = dict(request, gasPaymentToken=self.market['shortTokenAddress'])
        return await self.rpc('prepare', request={
            **request, 'mode': 'express', 'from': self.s.gmx_wallet_address,
        })

    async def submit(self, prepared):
        return await self.rpc('submit', prepared=prepared, address=self.s.gmx_wallet_address)

    async def close(self):
        for proc in list(self._processes):
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
