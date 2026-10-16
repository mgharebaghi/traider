"""Read-only LBank futures diagnostics. No API key, Telegram or Gemini needed."""
from __future__ import annotations

import asyncio
import os
import sys

import ccxt.async_support as ccxt
from config import _str  # Loads the project .env without requiring credentials.


async def diagnose() -> int:
    config = {"enableRateLimit": True, "timeout": 20000, "options": {"defaultType": "swap"}}
    proxy = _str("EXCHANGE_PROXY_URL")
    if proxy:
        config["socksProxy" if proxy.lower().startswith("socks") else "httpsProxy"] = proxy
    exchange = ccxt.lbank(config)  # Deliberately no apiKey or secret.
    symbol = _str("SYMBOL", "BTC/USDT:USDT")
    try:
        await exchange.load_markets()
        market = exchange.market(symbol)
        if not market.get("swap") or not market.get("linear"):
            raise ValueError("A linear futures symbol is required")
        print(f"LBank futures market: OK | {symbol}")
        print(f"Contract size: {market.get('contractSize')}")
        ticker = await exchange.fetch_ticker(symbol)
        print(f"LBank futures ticker: OK | last={ticker.get('last')}")
        try:
            candles = await exchange.fetch_ohlcv(symbol, "1m", limit=100)
            print(f"CCXT candle response: {len(candles)} rows (not yet validated for futures use)")
        except Exception as exc:
            print(f"CCXT futures candles: unavailable ({type(exc).__name__})")
        print("Futures order/account adapter: NOT IMPLEMENTED")
        print("No account credentials used and no orders submitted.")
        print("See README_LBANK_FA.md for the missing API specification.")
        return 2  # Public quotes alone do not mean the trading bot is ready.
    except Exception as exc:
        print(f"Public LBank check failed ({type(exc).__name__}); check network/proxy/symbol.", file=sys.stderr)
        return 1
    finally:
        await exchange.close()


def main() -> None:
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(diagnose()))


if __name__ == "__main__":
    main()
