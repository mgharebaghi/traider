"""Normalize GMX raw event prices and lifecycle fees without using net pnlUsd twice.

Reference: gmx-interface/src/components/TradeHistory/TradeHistoryRow/utils/position.ts
(getFeesBreakdown, getDecreasePayoutUsd). Contract prices have 30-tokenDecimals
decimals; basePnlUsd and impact fields already have 30 USD decimals.
"""
from decimal import Decimal


def contract_price(raw, token_decimals):
    return float(Decimal(str(raw)) / Decimal(10) ** (30 - token_decimals))


def lifecycle_totals(trades, execution_reserve):
    gross = Decimal(0)
    fees = Decimal(str(execution_reserve))
    scale = Decimal(10) ** 30
    for trade in trades:
        order_type = int(trade['orderType'])
        close = order_type in (4, 5, 6, 7)
        if close:
            gross += Decimal(trade['basePnlUsd']) / scale
        # V2.2 defers increase price impact; count net impact on decreases only.
        impact = trade.get('totalImpactUsd', 0) if 'srcChainId' in trade and close else (
            0 if 'srcChainId' in trade else trade.get('priceImpactUsd', 0))
        gross += (Decimal(impact) + Decimal(trade.get('swapImpactUsd', 0))) / scale
        # Claimable impact refunds are not yet spendable wallet funds.
        if close:
            gross -= max(Decimal(0), Decimal(trade.get('priceImpactDiffUsd', 0))) / scale
        amount = sum(Decimal(trade[k]) for k in ('positionFeeAmount', 'borrowingFeeAmount', 'fundingFeeAmount'))
        amount -= Decimal(trade.get('traderDiscountAmount', 0))
        amount += Decimal(trade.get('liquidationFeeAmount', 0))
        # Token amount * raw contract price produces 30-decimal USD directly.
        fees += amount * Decimal(trade['collateralTokenPriceMin']) / scale
        fees += Decimal(trade.get('swapFeeUsd', 0)) / scale
    return float(gross), float(fees)
