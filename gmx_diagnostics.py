"""Read public GMX data and optionally build an UNSIGNED dummy-wallet quote."""
import argparse
import asyncio
from dataclasses import replace

from config import load_settings
from gmx_client import GmxClient, units, usd30
from indicators import MarketAnalyzer
from chart_image import render_chart


async def check(quote=False, peers=False):
    s = replace(load_settings(), dry_run=True, gmx_live_enabled=False, gmx_private_key='',
                gmx_wallet_address='0x0000000000000000000000000000000000000001')
    client = GmxClient(s)
    try:
        if peers:
            for row in await client.rpc('probe', symbol=s.symbol):
                print(f'{row["host"]}: {"OK" if row["ok"] else "FAILED"} | {row["elapsedMs"]}ms | HTTP={row.get("statusCode")} | {row.get("error", "")}')
            return
        await client.initialize()
        snap = await MarketAnalyzer(client, s).snapshot()
        chart = render_chart(snap)
        print(f'GMX data and chart: OK | PNG={len(chart)} bytes | minPosition=${client.min_cost:g} | minCollateral=${client.min_collateral:g}')
        if quote:
            result = await client.prepare(dict(kind='increase', symbol=s.symbol, direction='long', orderType='market',
                size=units(100), collateralToken='USDC', collateralToPay=dict(amount=units(25, 6), token='USDC'),
                slippage=30, gasPaymentToken='USDC', tpsl=[{'type': 'stop-loss', 'triggerPrice': units(snap.price*.99)},
                                                       {'type': 'take-profit', 'triggerPrice': units(snap.price*1.03)}]))
            print(f'Unsigned quote: OK | execution fee estimate=${usd30(result["executionFeeUsd"]):.4f}')
            print(f'Total unsigned Express payment estimate=${usd30(result["paymentFeeUsd"]):.4f}')
        print('No signature, order submission, Telegram message or Gemini generation performed.')
    finally:
        await client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quote', action='store_true')
    parser.add_argument('--peers', action='store_true', help='Probe each public GMX host separately without wallet access')
    args = parser.parse_args()
    asyncio.run(check(args.quote, args.peers))
