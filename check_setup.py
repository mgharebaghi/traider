"""Validate installation/configuration; optional read-only connectivity checks."""
from __future__ import annotations

import argparse
import asyncio
import sys

from config import load_settings
from exchange_client import ExchangeClient
from indicators import MarketAnalyzer


def check_config():
    settings = load_settings()
    print("Configuration: OK")
    print(f"Mode default: {'DRY' if settings.dry_run else 'LIVE'} | Exchange: {settings.exchange_id}")
    return settings


async def check_network(settings) -> None:
    from google import genai
    from google.genai import types
    from telegram import Bot
    from telegram.request import HTTPXRequest

    exchange = ExchangeClient(settings)
    ai = genai.Client(api_key=settings.gemini_api_key, http_options=types.HttpOptions(timeout=30000))
    try:
        await exchange.initialize()
        snapshot = await MarketAnalyzer(exchange, settings).snapshot()
        print(f"Market data: OK | last={snapshot.price}")
        async with Bot(settings.telegram_token, request=HTTPXRequest(proxy=settings.telegram_proxy or None)) as bot:
            me = await bot.get_me()
            print(f"Telegram: OK | @{me.username}")
        model = await ai.aio.models.get(model=settings.gemini_model)
        print(f"Gemini model access: OK | {model.name}")
        print("No orders placed and no AI generation requested.")
    finally:
        await exchange.close()
        await ai.aio.aclose()
        ai.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", action="store_true", help="Check public market data, bot identity and model access")
    args = parser.parse_args()
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        settings = check_config()
        if args.network:
            asyncio.run(check_network(settings))
        else:
            print("Offline check passed. Use --network for read-only API checks.")
    except Exception as exc:
        # Do not echo API exceptions: some include request URLs with tokens.
        if isinstance(exc, ValueError) and str(exc).startswith("Invalid configuration"):
            print(str(exc), file=sys.stderr)
        else:
            print(f"Check failed ({type(exc).__name__}). Check configuration, API access and connectivity.", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
