"""
Async orchestrator.

Runs concurrently:
  1. scanner_loop  -> local indicators every SCAN_INTERVAL_SEC, Gemini only on valid setups
  2. monitor_loop  -> entry fills, stop/TP1/break-even/trailing management every MONITOR_INTERVAL_SEC
  3. heartbeat     -> writes DATA_DIR/heartbeat for the Docker HEALTHCHECK
  4. Telegram bot  -> polling (started inside the same event loop)
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import Awaitable, Callable

from ai_agent import AIAgent
from config import Settings, load_settings
from exchange_client import ExchangeClient
from indicators import MarketAnalyzer
from instance_lock import instance_lock
from risk_manager import RiskManager
from state_store import StateStore
from telegram_bot import TelegramBot, esc
from trader import Trader

log = logging.getLogger("main")


def setup_logging(settings: Settings) -> None:
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)-10s | %(message)s")
    root = logging.getLogger()
    root.setLevel(settings.log_level)
    root.handlers.clear()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    file_handler = RotatingFileHandler(
        settings.data_dir / "bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    for noisy in ("httpx", "telegram", "apscheduler", "google_genai", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class Orchestrator:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.store = StateStore(settings.data_dir / "state.db")
        self.client = ExchangeClient(settings)
        self.analyzer = MarketAnalyzer(self.client, settings)
        self.ai = AIAgent(settings, self.store, self.client)
        self.risk = RiskManager(settings, self.store, self.client)
        self.telegram = TelegramBot(settings, self.store)
        self.trader = Trader(settings, self.client, self.risk, self.store, self.analyzer, self.telegram)
        self.telegram.attach(self.trader, self.risk, self.ai)
        self.stop_event = asyncio.Event()
        self._last_error_notify: dict[str, float] = {}

    # ------------------------------------------------------------- helpers
    async def _notify_error(self, where: str, exc: Exception) -> None:
        """Throttled error notification (max once per 10 minutes per loop)."""
        now = time.time()
        if now - self._last_error_notify.get(where, 0.0) < 600:
            return
        self._last_error_notify[where] = now
        await self.telegram.send(f"⚠️ <b>خطا در {esc(where)}</b>\n<code>{esc(str(exc)[:500])}</code>\nربات به کار ادامه می‌دهد.")

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _loop(self, name: str, interval: float, body: Callable[[], Awaitable[None]]) -> None:
        log.info("%s loop started (every %.1fs)", name, interval)
        while not self.stop_event.is_set():
            try:
                await body()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("%s loop error", name)
                await self._notify_error(name, exc)
            await self._sleep(interval)

    # ---------------------------------------------------------------- loops
    async def _check_day_roll(self) -> None:
        if not self.risk.needs_roll():
            return
        balance = await self.trader.get_balance()
        previous = self.risk.roll_day(balance)
        if previous is not None:
            await self.telegram.notify_new_day(previous, balance)

    async def scan_once(self) -> None:
        await self._check_day_roll()
        if self.trader.paused or self.trader.has_position():
            return
        allowed, reason = self.risk.can_trade()
        if not allowed:
            log.debug("Trading blocked: %s", reason)
            return
        if not await self.trader.verify_funds_before_scan():
            return
        snap = await self.analyzer.snapshot()
        setup = self.analyzer.detect_setup(snap)
        log.debug(
            "Scan price=%.1f trend15=%s rsi1=%.1f div=%s setup=%s",
            snap.price, snap.trend15, snap.rsi1, snap.divergence_1m, setup.direction if setup else None,
        )
        if setup is None:
            return
        can_call, why = self.ai.can_call(setup.key)
        if not can_call:
            log.info("Setup %s found but Gemini skipped: %s", setup.key, why)
            return
        log.info("Setup %s -> consulting Gemini (%s)", setup.key, "; ".join(setup.reasons))
        signal_ = await self.ai.analyze(snap, setup)
        if signal_ is None:
            return
        if not signal_.is_trade or signal_.confidence < self.s.ai_min_confidence:
            await self.telegram.notify_ai_wait(signal_, setup.direction)
            return
        # Re-check conditions: state may have changed during the AI call
        if self.trader.paused or self.trader.has_position() or not self.risk.can_trade()[0]:
            return
        await self.trader.open_from_signal(signal_, snap)

    async def heartbeat_once(self) -> None:
        (self.s.data_dir / "heartbeat").write_text(str(int(time.time())), encoding="utf-8")

    # ------------------------------------------------------------- lifecycle
    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except (NotImplementedError, RuntimeError, ValueError):
                # Windows: fall back to KeyboardInterrupt handling in run()
                pass

    async def run(self) -> None:
        try:
            await self._run()
        finally:
            # Also release resources when initialization fails halfway through.
            await self.telegram.stop()
            try:
                await self.ai.close()
            finally:
                try:
                    await self.client.close()
                finally:
                    self.store.close()

    async def _run(self) -> None:
        await self.client.initialize()
        await self.telegram.start()
        restored = None
        try:
            restored = await self.trader.reconcile_on_startup()
        except Exception as exc:  # noqa: BLE001
            log.exception("Startup reconciliation failed")
            self.trader.set_paused(True)
            restored = f"خطا در بازیابی پوزیشن: {exc}"
        balance = None
        try:
            balance = await self.trader.get_balance()
            if self.risk.needs_roll():
                self.risk.roll_day(balance)
        except Exception as exc:  # noqa: BLE001
            log.warning("Initial balance fetch failed: %s", exc)
        await self.telegram.notify_startup(self.trader.mode, balance, self.client.trade_ex.id, restored)
        await self.trader.verify_funds_before_scan()

        self._install_signal_handlers()
        tasks = [
            asyncio.create_task(self._loop("scanner", self.s.scan_interval_sec, self.scan_once), name="scanner"),
            asyncio.create_task(self._loop("monitor", self.s.monitor_interval_sec, self.trader.manage), name="monitor"),
            asyncio.create_task(self._loop("heartbeat", 30, self.heartbeat_once), name="heartbeat"),
        ]
        try:
            await self.stop_event.wait()
        finally:
            log.info("Shutting down...")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await self.telegram.send("🔌 ربات خاموش شد. پوزیشن‌های باز (در صورت وجود) در حافظه ذخیره شده‌اند.")
            except Exception:  # noqa: BLE001
                pass
            log.info("Shutdown complete")


async def amain() -> None:
    settings = load_settings()
    setup_logging(settings)
    log.info(
        "Starting scalper | exchange=%s data=%s symbol=%s dry_run_default=%s model=%s",
        settings.exchange_id,
        settings.market_data_exchange_id or settings.exchange_id,
        settings.symbol,
        settings.dry_run,
        settings.gemini_model,
    )
    with instance_lock(settings.data_dir):
        orchestrator = Orchestrator(settings)
        try:
            await orchestrator.run()
        except asyncio.CancelledError:
            orchestrator.stop_event.set()


def main() -> None:
    if sys.platform.startswith("win"):
        # aiohttp/aiodns (used by ccxt) require the selector loop on Windows
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("Stopped by user")


if __name__ == "__main__":
    main()
