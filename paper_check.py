"""Isolated deterministic paper-engine check; no network or production state."""
import asyncio
import math
import tempfile
from dataclasses import replace
from pathlib import Path

from ai_agent import TradeSignal
from config import load_settings
from risk_manager import RiskManager
from state_store import StateStore
from trader import Trader


class ReplayMarket:
    contract_size = 1.0
    min_amount = 0.001
    min_cost = 0.0
    price = 100.0

    def amount_to_precision(self, amount):
        return math.floor(amount * 1000) / 1000

    def price_to_precision(self, price):
        return round(price, 4)

    async def fetch_ticker(self, source="data"):
        return dict.fromkeys(("last", "bid", "ask"), self.price)


class SilentNotifier:
    def __getattr__(self, name):
        async def ignore(*args, **kwargs):
            pass
        return ignore


async def run_paper_check(settings):
    results = []
    for side, prices in (("LONG", (101.5, 102)), ("SHORT", (101.1,))):
        with tempfile.TemporaryDirectory(prefix="scalper-paper-check-") as tmp:
            s = replace(settings, data_dir=Path(tmp), dry_run=True, starting_balance=100, close_at_tp2=True)
            store = StateStore(Path(tmp) / "check.db")
            try:
                market = ReplayMarket()
                risk = RiskManager(s, store, market)
                risk.roll_day(100)
                trader = Trader(s, market, risk, store, None, SilentNotifier())
                d = 1 if side == "LONG" else -1
                signal = TradeSignal(side, 100, 100, 100 - d, 100 + 1.5*d, 100 + 2*d,
                                     s.min_leverage, "آزمایش فنی با قیمت‌های از پیش تعیین‌شده")
                pos = await trader.open_from_signal(signal, None)
                if pos is None:
                    raise RuntimeError("Paper test entry rejected; review risk settings")
                await trader.manage()
                for price in prices:
                    market.price = price
                    await trader.manage()
                if trader.has_position() or len(store.recent_trades()) != 1:
                    raise RuntimeError("Paper trade did not settle")
                if (pos.net_pnl > 0) != (side == "LONG"):
                    raise RuntimeError("Incorrect paper PnL sign")
                if not math.isclose(trader.virtual_balance, 100 + pos.net_pnl, abs_tol=1e-6):
                    raise RuntimeError("Paper wallet and ledger disagree")
                results.append(dict(side=side, net_pnl=pos.net_pnl, balance=trader.virtual_balance,
                                    fees=pos.fees_paid, reason=pos.close_reason))
            finally:
                store.close()
    return results


if __name__ == "__main__":
    for result in asyncio.run(run_paper_check(load_settings())):
        print(result)
