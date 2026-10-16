"""
Order execution + trade management.

Lifecycle of a position:
    PENDING  -> limit entry order resting (timeout -> cancelled)
    OPEN     -> stage 0: initial stop-loss, waiting for TP1
                stage 1: TP1 hit -> 50% closed, SL moved to break-even, trailing active
                stage 2: TP2 hit -> trailing tightened (or full close if CLOSE_AT_TP2)
    CLOSED   -> PnL booked, daily stats updated, Telegram report sent

The same engine powers both modes:
  * DRY  : live market data, simulated fills (maker/taker fees + slippage), virtual balance.
  * LIVE : real ISOLATED orders via ccxt + exchange-native reduce-only stop orders
           (bot-monitored "soft stop" fallback when the exchange can't place them).
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

import pandas as pd

from ai_agent import TradeSignal
from config import Settings
from exchange_client import ExchangeClient
from indicators import MarketAnalyzer, MarketSnapshot
from risk_manager import DailyStats, PositionPlan, RiskManager
from state_store import StateStore

log = logging.getLogger("trader")

MODE_KEY = "mode"
POSITION_KEY = "position"
VBAL_KEY = "virtual_balance"
PAUSED_KEY = "paused"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Position:
    id: str
    mode: str  # DRY | LIVE
    symbol: str
    side: str  # LONG | SHORT
    status: str  # PENDING | OPEN | CLOSED | CANCELLED
    entry_price: float
    contracts: float
    base_qty: float
    leverage: int
    stop_loss: float
    initial_stop: float
    tp1: float
    tp2: float
    risk_usd: float
    margin: float
    confidence: int
    reasoning_fa: str
    created_at: str
    balance_before: float
    fill_price: float = 0.0
    filled_at: str = ""
    remaining_contracts: float = 0.0
    stage: int = 0
    best_price: float = 0.0
    break_even: float = 0.0
    fees_paid: float = 0.0
    realized_pnl: float = 0.0
    partial_contracts: float = 0.0
    partial_exit_price: float = 0.0
    entry_order_id: str = ""
    sl_order_id: str = ""
    soft_stop: bool = True
    close_reason: str = ""
    closed_at: str = ""
    exit_price_avg: float = 0.0
    exit_order_id: str = ""
    exit_kind: str = ""  # PARTIAL | FULL; retained until exchange confirms fill
    exit_price_hint: float = 0.0

    @property
    def direction(self) -> int:
        return 1 if self.side == "LONG" else -1

    @property
    def entry_side(self) -> str:
        return "buy" if self.side == "LONG" else "sell"

    @property
    def close_side(self) -> str:
        return "sell" if self.side == "LONG" else "buy"

    @property
    def net_pnl(self) -> float:
        return self.realized_pnl - self.fees_paid

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Position":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in names})


class Notifier(Protocol):
    async def notify_signal_opened(
        self, pos: Position, plan: PositionPlan, signal: TradeSignal, warnings: list[str]
    ) -> None: ...

    async def notify_entry_filled(self, pos: Position) -> None: ...

    async def notify_entry_cancelled(self, pos: Position, reason: str) -> None: ...

    async def notify_trade_rejected(self, signal: TradeSignal, reason: str) -> None: ...

    async def notify_risk_free(
        self, pos: Position, part_contracts: float, part_price: float, part_net: float
    ) -> None: ...

    async def notify_tp2_reached(self, pos: Position) -> None: ...

    async def notify_position_closed(
        self,
        pos: Position,
        net: float,
        balance_after: float,
        daily: Optional[DailyStats],
        target: float,
        loss_limit: float,
    ) -> None: ...

    async def notify_daily_halt(self, event: str, daily: Optional[DailyStats], until: str) -> None: ...

    async def notify_insufficient_balance(
        self, balance: float, min_required: float, mode: str, spot_balance: Optional[float] = None
    ) -> None: ...

    async def notify_warning(self, text: str) -> None: ...


class Trader:
    def __init__(
        self,
        settings: Settings,
        client: ExchangeClient,
        risk: RiskManager,
        store: StateStore,
        analyzer: MarketAnalyzer,
        notifier: Notifier,
    ) -> None:
        self.s = settings
        self.client = client
        self.risk = risk
        self.store = store
        self.analyzer = analyzer
        self.notifier = notifier
        self.lock = asyncio.Lock()

        saved_key = store.get("exchange_api_key")
        saved_sec = store.get("exchange_api_secret")
        if saved_key and saved_sec and not settings.has_exchange_keys and hasattr(client, "set_credentials"):
            client.set_credentials(str(saved_key), str(saved_sec))

        if store.get(MODE_KEY) is None:
            store.set(MODE_KEY, "DRY" if settings.dry_run else "LIVE")
        if store.get(VBAL_KEY) is None:
            store.set(VBAL_KEY, settings.starting_balance)
        if store.get(PAUSED_KEY) is None:
            store.set(PAUSED_KEY, False)

        raw = store.get(POSITION_KEY)
        self.position: Optional[Position] = Position.from_dict(raw) if raw else None
        if settings.dry_run:
            if self.position is not None and self.position.mode == "LIVE":
                raise ValueError("Saved LIVE position exists. Reconcile it with DRY_RUN=false before using simulation.")
            store.set(MODE_KEY, "DRY")
        elif self.mode == "LIVE" and not self.has_exchange_keys:
            raise ValueError("Saved LIVE mode requires exchange credentials")

        self._trail_df: Optional[pd.DataFrame] = None
        self._trail_ts = 0.0
        self._last_sync = 0.0
        self.last_price = 0.0
        self._soft_warned_for: str = ""
        self._low_balance_warned = False

    # =============================================================== state
    @property
    def has_exchange_keys(self) -> bool:
        return bool(self.s.has_exchange_keys or getattr(self.client, "has_credentials", False))

    def set_exchange_keys(self, api_key: str, api_secret: str) -> None:
        api_key = api_key.strip().strip('"').strip("'")
        api_secret = api_secret.strip().strip('"').strip("'")
        self.store.set("exchange_api_key", api_key)
        self.store.set("exchange_api_secret", api_secret)
        if hasattr(self.client, "set_credentials"):
            self.client.set_credentials(api_key, api_secret)

    @property
    def mode(self) -> str:
        return str(self.store.get(MODE_KEY, "DRY"))

    @property
    def paused(self) -> bool:
        return bool(self.store.get(PAUSED_KEY, False))

    def set_paused(self, value: bool) -> None:
        self.store.set(PAUSED_KEY, bool(value))

    @property
    def virtual_balance(self) -> float:
        return float(self.store.get(VBAL_KEY, self.s.starting_balance))

    def _add_virtual(self, delta: float) -> None:
        self.store.set(VBAL_KEY, round(self.virtual_balance + delta, 8))

    def reset_virtual_balance(self, amount: Optional[float] = None) -> float:
        value = float(amount if amount is not None else self.s.starting_balance)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Virtual balance must be finite and positive")
        self.store.set(VBAL_KEY, value)
        self._low_balance_warned = False
        return value

    def _save(self) -> None:
        if self.position is None:
            self.store.delete(POSITION_KEY)
        else:
            self.store.set(POSITION_KEY, asdict(self.position))

    def has_position(self) -> bool:
        return self.position is not None

    async def get_balance(self) -> float:
        if self.mode == "LIVE":
            return await self.client.fetch_free_usdt()
        return self.virtual_balance

    async def verify_funds_before_scan(self) -> bool:
        """
        Checks whether account balance is sufficient before scanning/calling Gemini.
        Notifies the user in Persian once when balance drops below MIN_BALANCE_TO_TRADE
        (and checks Spot balance in LIVE mode so the user knows if funds are in Spot).
        """
        try:
            balance = await self.get_balance()
        except Exception as exc:  # noqa: BLE001
            log.warning("Balance check failed: %s", exc)
            return False
        if balance < self.s.min_balance_to_trade:
            if not self._low_balance_warned:
                self._low_balance_warned = True
                spot = await self.client.fetch_spot_usdt() if self.mode == "LIVE" else None
                await self.notifier.notify_insufficient_balance(
                    balance, self.s.min_balance_to_trade, self.mode, spot
                )
            return False
        self._low_balance_warned = False
        return True

    async def switch_mode(self, new_mode: str) -> tuple[bool, str, float]:
        """Switches DRY <-> LIVE directly from Telegram without requiring .env edits."""
        new_mode = new_mode.upper()
        if new_mode not in ("DRY", "LIVE"):
            return False, "حالت نامعتبر است", 0.0
        async with self.lock:
            if self.position is not None:
                return False, "تا زمانی که پوزیشن یا سفارش باز وجود دارد، تغییر حالت ممکن نیست.", 0.0
            if new_mode == self.mode:
                return True, "", await self.get_balance()
            spot_note = ""
            if new_mode == "LIVE":
                if not self.has_exchange_keys:
                    return (
                        False,
                        "کلیدهای API صرافی هنوز ثبت نشده‌اند. در تلگرام دستور:\n/setkeys API_KEY SECRET_KEY\nرا بفرستید!",
                        0.0,
                    )
                try:
                    balance = await self.client.fetch_free_usdt()
                except Exception as exc:  # noqa: BLE001
                    return False, f"اتصال به حساب صرافی ناموفق بود: {exc}", 0.0
                if balance < self.s.min_balance_to_trade:
                    spot = await self.client.fetch_spot_usdt()
                    if spot and spot > 0:
                        spot_note = (
                            f"💡 در کیف پول اسپات (Spot) شما {spot:.2f}$ تتر وجود دارد؛ "
                            "آن را به کیف پول Futures منتقل کنید."
                        )
            else:
                balance = self.virtual_balance
            self.store.set(MODE_KEY, new_mode)
            self.store.delete(RiskManager.DAILY_KEY)
            self.risk.roll_day(balance)
            self._low_balance_warned = False
            return True, spot_note, balance

    # ============================================================ opening
    async def open_from_signal(self, signal: TradeSignal, snap: MarketSnapshot) -> Optional[Position]:
        async with self.lock:
            if self.position is not None or self.paused or not self.risk.can_trade()[0] or not signal.is_trade:
                return None
            if signal.confidence < self.s.ai_min_confidence:
                return None
            ticker = await self._ticker()
            if abs(signal.entry_price - ticker["last"]) / ticker["last"] > self.s.max_entry_deviation_pct:
                await self.notifier.notify_trade_rejected(signal, "قیمت بازار پس از تحلیل تغییر کرده؛ سیگنال منقضی شد")
                return None
            if self.mode == "LIVE" and await self.client.fetch_position_contracts() > 0:
                self.set_paused(True)
                await self.notifier.notify_trade_rejected(signal, "پوزیشن مدیریت‌نشده در صرافی وجود دارد؛ اسکن متوقف شد")
                return None
            try:
                balance = await self.get_balance()
            except Exception as exc:  # noqa: BLE001
                await self.notifier.notify_trade_rejected(signal, f"دریافت موجودی ناموفق بود: {exc}")
                return None
            if balance < self.s.min_balance_to_trade:
                spot = await self.client.fetch_spot_usdt() if self.mode == "LIVE" else None
                await self.notifier.notify_insufficient_balance(
                    balance, self.s.min_balance_to_trade, self.mode, spot
                )
                return None

            plan = self.risk.plan_position(
                balance,
                signal.action,
                signal.entry_price,
                signal.stop_loss,
                signal.take_profit_1,
                signal.take_profit_2,
                signal.suggested_leverage,
            )
            if not plan.ok:
                await self.notifier.notify_trade_rejected(signal, plan.reason)
                return None

            pos = Position(
                id=uuid.uuid4().hex[:10],
                mode=self.mode,
                symbol=self.s.symbol,
                side=signal.action,
                status="PENDING",
                entry_price=signal.entry_price,
                contracts=plan.contracts,
                base_qty=plan.base_qty,
                leverage=plan.leverage,
                stop_loss=signal.stop_loss,
                initial_stop=signal.stop_loss,
                tp1=signal.take_profit_1,
                tp2=signal.take_profit_2,
                risk_usd=plan.expected_loss,
                margin=plan.margin,
                confidence=signal.confidence,
                reasoning_fa=signal.reasoning_fa,
                created_at=utcnow().isoformat(),
                balance_before=balance,
            )
            warnings: list[str] = []
            if pos.mode == "LIVE":
                submitting = False
                try:
                    warnings = await self.client.set_isolated_and_leverage(plan.leverage)
                    # If the connection dies after acceptance, keep an explicit
                    # unknown state instead of silently placing a second entry.
                    pos.status = "UNKNOWN"
                    self.position = pos
                    self._save()
                    submitting = True
                    order = await self.client.create_limit(
                        pos.entry_side, plan.contracts, signal.entry_price, self.s.entry_post_only
                    )
                    pos.entry_order_id = str(order["id"])
                    pos.status = "PENDING"
                except Exception as exc:  # noqa: BLE001
                    log.exception("Live entry failed")
                    if submitting:
                        self.set_paused(True)
                    await self.notifier.notify_trade_rejected(signal, f"خطای صرافی هنگام ثبت سفارش: {exc}")
                    return None
            else:
                pos.entry_order_id = f"SIM-{pos.id}"

            self.position = pos
            self._save()
            log.info(
                "Entry order placed %s %s %.6f @ %.2f lev=%dx", pos.mode, pos.side, pos.contracts, pos.entry_price,
                pos.leverage,
            )
            await self.notifier.notify_signal_opened(pos, plan, signal, warnings)
            return pos

    # ========================================================= monitoring
    async def manage(self) -> None:
        async with self.lock:
            pos = self.position
            if pos is None:
                return
            if pos.status == "UNKNOWN":
                self.set_paused(True)
                return
            if pos.status == "PENDING":
                await self._check_entry(pos)
            elif pos.status == "OPEN":
                if pos.exit_order_id:
                    await self._settle_exit(pos)
                else:
                    await self._manage_open(pos)

    async def _ticker(self, pos: Optional[Position] = None) -> dict[str, float]:
        mode = pos.mode if pos else self.mode
        ticker = await self.client.fetch_ticker("trade" if mode == "LIVE" else "data")
        self.last_price = ticker["last"]
        return ticker

    # ---------------------------------------------------------- entry fill
    async def _check_entry(self, pos: Position) -> None:
        age_min = (utcnow() - datetime.fromisoformat(pos.created_at)).total_seconds() / 60.0
        timed_out = age_min >= self.s.entry_timeout_min
        d = pos.direction

        if pos.mode == "DRY":
            t = await self._ticker(pos)
            if d == 1:
                touched = t["ask"] <= pos.entry_price or t["last"] <= pos.entry_price
            else:
                touched = t["bid"] >= pos.entry_price or t["last"] >= pos.entry_price
            if touched:
                await self._on_filled(pos, pos.entry_price, pos.contracts, None, maker=True)
            elif timed_out:
                await self._cancel_pending(pos, f"عدم پر شدن سفارش لیمیت پس از {self.s.entry_timeout_min:g} دقیقه")
            return

        order = await self.client.fetch_order(pos.entry_order_id)
        status = order.get("status")
        filled = float(order.get("filled") or 0.0)
        avg = ExchangeClient.order_avg_price(order, pos.entry_price)
        if status == "closed" and filled > 0:
            await self._on_filled(pos, avg, filled, ExchangeClient.order_fee(order), maker=self.s.entry_post_only)
            return
        if status in ("canceled", "cancelled", "rejected", "expired"):
            if filled > 0:
                await self._on_filled(pos, avg, filled, ExchangeClient.order_fee(order), maker=True)
            else:
                await self._cancel_pending(pos, "سفارش توسط صرافی لغو/رد شد")
            return
        # Protect partially filled entries immediately rather than leaving them
        # exposed until the entire entry timeout expires.
        if filled > 0:
            timed_out = True
        if timed_out:
            await self.client.cancel_order(pos.entry_order_id)
            order = await self.client.fetch_order(pos.entry_order_id)
            if order.get("status") not in ("closed", "canceled", "cancelled", "rejected", "expired"):
                raise RuntimeError("Entry cancellation not yet confirmed; retaining pending state")
            filled = float(order.get("filled") or 0.0)
            avg = ExchangeClient.order_avg_price(order, pos.entry_price)
            if filled > 0:
                await self._on_filled(pos, avg, filled, ExchangeClient.order_fee(order), maker=True)
            else:
                await self._cancel_pending(pos, f"عدم پر شدن سفارش لیمیت پس از {self.s.entry_timeout_min:g} دقیقه")

    async def _cancel_pending(self, pos: Position, reason: str, notify: bool = True) -> None:
        pos.status = "CANCELLED"
        pos.closed_at = utcnow().isoformat()
        self.position = None
        self._save()
        log.info("Entry cancelled: %s", reason)
        if notify:
            await self.notifier.notify_entry_cancelled(pos, reason)

    async def _on_filled(
        self, pos: Position, price: float, contracts: float, fee_override: Optional[float], maker: bool
    ) -> None:
        pos.status = "OPEN"
        pos.fill_price = float(price)
        pos.contracts = float(contracts)
        pos.base_qty = pos.contracts * self.client.contract_size
        pos.remaining_contracts = pos.contracts
        pos.best_price = pos.fill_price
        pos.filled_at = utcnow().isoformat()
        pos.break_even = self.risk.break_even_price(pos.side, pos.fill_price)
        fee = fee_override if fee_override is not None else self.risk.fee(pos.fill_price, pos.base_qty, maker)
        pos.fees_paid += fee
        self._save()  # Persist the fill before any network request or notification.
        if pos.mode == "DRY":
            self._add_virtual(-fee)
            pos.soft_stop = True
        else:
            sl_id = await self.client.create_stop_market(pos.close_side, pos.contracts, pos.stop_loss)
            if sl_id:
                pos.sl_order_id = sl_id
                pos.soft_stop = False
            else:
                pos.soft_stop = True
                await self.notifier.notify_warning(
                    "ثبت حد ضرر روی صرافی ممکن نشد؛ ربات حد ضرر را به‌صورت نرم‌افزاری (Soft Stop) "
                    "مانیتور می‌کند. ربات را خاموش نکنید."
                )
        self._save()
        log.info("Entry filled %s %.6f @ %.2f", pos.side, pos.contracts, pos.fill_price)
        await self.notifier.notify_entry_filled(pos)

    # ----------------------------------------------------- open management
    def _stop_reason(self, pos: Position) -> str:
        if pos.stage == 0:
            return "SL"
        if pos.fill_price and abs(pos.stop_loss - pos.break_even) / pos.fill_price < 0.0003:
            return "BREAKEVEN"
        return "TRAILING"

    async def _manage_open(self, pos: Position) -> None:
        t = await self._ticker(pos)
        price = t["last"]
        d = pos.direction
        if pos.close_reason:
            await self._close_all(pos, pos.close_reason, price)
            return

        # ---- LIVE: detect closure by exchange stop / manual action
        if pos.mode == "LIVE" and time.time() - self._last_sync >= self.s.live_sync_sec:
            self._last_sync = time.time()
            contracts = await self.client.fetch_position_contracts()
            if contracts <= 0:
                exit_price = price
                reason = self._stop_reason(pos) if pos.sl_order_id else "EXTERNAL"
                fee: Optional[float] = None
                if pos.sl_order_id:
                    try:
                        o = await self.client.fetch_order(pos.sl_order_id)
                        if o.get("status") == "closed":
                            exit_price = ExchangeClient.order_avg_price(o, pos.stop_loss)
                            fee = ExchangeClient.order_fee(o)
                        else:
                            reason = "EXTERNAL"
                    except Exception as exc:  # noqa: BLE001
                        log.warning("Could not fetch stop order: %s", exc)
                await self.client.cancel_all()
                await self._finalize(pos, exit_price, reason, fee)
                return
            if contracts < pos.remaining_contracts - 1e-12:
                pos.remaining_contracts = contracts

        # ---- best price tracking
        pos.best_price = max(pos.best_price, price) if d == 1 else min(pos.best_price, price)

        # ---- stop-loss / break-even / trailing hit
        stop_hit = price <= pos.stop_loss if d == 1 else price >= pos.stop_loss
        if stop_hit:
            beyond = d * (pos.stop_loss - price) / price
            if pos.mode == "DRY" or pos.soft_stop or beyond > 0.0015:
                exit_hint = min(pos.stop_loss, price) if d == 1 else max(pos.stop_loss, price)
                await self._close_all(pos, self._stop_reason(pos), exit_hint)
                return

        # ---- stage 0 -> TP1
        if pos.stage == 0:
            if d * (price - pos.tp1) >= 0:
                await self._take_partial(pos, price)
            self._save()
            return

        # ---- stage 1 -> TP2
        if pos.stage == 1 and d * (price - pos.tp2) >= 0:
            if self.s.close_at_tp2:
                await self._close_all(pos, "TP2", price)
                return
            pos.stage = 2
            self._save()
            await self.notifier.notify_tp2_reached(pos)

        await self._update_trailing(pos, price)
        self._save()

    async def _take_partial(self, pos: Position, price: float) -> None:
        d = pos.direction
        cs = self.client.contract_size
        part = self.client.amount_to_precision(pos.remaining_contracts * self.s.tp1_close_fraction)
        remaining_after = self.client.amount_to_precision(pos.remaining_contracts - part)
        min_amt = self.client.min_amount
        can_split = part > 0 and remaining_after > 0 and (not min_amt or (part >= min_amt and remaining_after >= min_amt))

        part_net = 0.0
        exit_price = price
        if can_split:
            fee: Optional[float] = None
            try:
                if pos.mode == "LIVE":
                    order = await self.client.create_market(pos.close_side, part, reduce_only=True)
                    pos.exit_order_id = str(order["id"])
                    pos.exit_kind = "PARTIAL"
                    pos.exit_price_hint = price
                    self._save()
                    await self._settle_exit(pos)
                    return
                else:
                    exit_price = price * (1 - d * self.s.slippage_pct)
            except Exception as exc:  # noqa: BLE001
                if pos.exit_order_id:
                    raise  # Keep an accepted exit pending until it can be reconciled.
                log.exception("Partial close failed")
                await self.notifier.notify_warning(f"بستن ۵۰٪ پوزیشن ناموفق بود ({exc}). فقط حد ضرر به نقطه ورود منتقل می‌شود.")
                can_split = False
            if can_split:
                base = part * cs
                gross = (exit_price - pos.fill_price) * d * base
                fee = fee if fee is not None else self.risk.fee(exit_price, base, maker=False)
                pos.realized_pnl += gross
                pos.fees_paid += fee
                pos.remaining_contracts = remaining_after
                pos.partial_contracts = part
                pos.partial_exit_price = exit_price
                part_net = gross - fee
                if pos.mode == "DRY":
                    self._add_virtual(gross - fee)

        be = pos.break_even or self.risk.break_even_price(pos.side, pos.fill_price)
        be = min(be, price * (1 - 0.0003)) if d == 1 else max(be, price * (1 + 0.0003))
        pos.stage = 1
        await self._move_stop(pos, be)
        self._save()
        log.info("TP1 reached: partial=%s @ %.2f, SL -> %.2f", part if can_split else 0, exit_price, pos.stop_loss)
        await self.notifier.notify_risk_free(pos, part if can_split else 0.0, exit_price, part_net)

    async def _move_stop(self, pos: Position, new_stop: float) -> None:
        new_stop = self.client.price_to_precision(new_stop)
        if pos.direction * (new_stop - pos.stop_loss) < 0:
            return
        if pos.mode == "LIVE":
            new_id = await self.client.create_stop_market(pos.close_side, pos.remaining_contracts, new_stop)
            if new_id:
                old_id = pos.sl_order_id
                pos.sl_order_id = new_id
                pos.soft_stop = False
                pos.stop_loss = new_stop
                self._save()
                if old_id:
                    await self.client.cancel_order(old_id)
            else:
                pos.soft_stop = True
                if self._soft_warned_for != pos.id:
                    self._soft_warned_for = pos.id
                    await self.notifier.notify_warning(
                        "جابجایی حد ضرر روی صرافی ناموفق بود؛ حد ضرر جدید به‌صورت نرم‌افزاری مانیتور می‌شود "
                        "(حد ضرر قبلی روی صرافی به‌عنوان محافظ باقی است)."
                    )
        pos.stop_loss = new_stop

    async def _update_trailing(self, pos: Position, price: float) -> None:
        now = time.time()
        if self._trail_df is None or now - self._trail_ts >= self.s.trail_refresh_sec:
            try:
                self._trail_df = await self.analyzer.fetch_entry_frame()
                self._trail_ts = now
            except Exception as exc:  # noqa: BLE001
                log.warning("Trailing data refresh failed: %s", exc)
                if self._trail_df is None:
                    return
        df = self._trail_df
        assert df is not None
        d = pos.direction
        atr = float(df["atr"].iloc[-1])
        ema = float(df["ema_fast"].iloc[-1])
        mult = self.s.trailing_tight_atr_mult if pos.stage >= 2 else self.s.trailing_atr_mult
        atr_stop = pos.best_price - d * mult * atr
        ema_stop = ema - d * self.s.trailing_ema_buffer_atr * atr

        method = self.s.trailing_method
        if method == "atr":
            candidate = atr_stop
        elif method == "ema":
            candidate = ema_stop
        else:
            tighter = max(atr_stop, ema_stop) if d == 1 else min(atr_stop, ema_stop)
            looser = min(atr_stop, ema_stop) if d == 1 else max(atr_stop, ema_stop)
            candidate = tighter if pos.stage >= 2 else looser

        be = pos.break_even
        candidate = max(candidate, be) if d == 1 else min(candidate, be)
        limit = price * (1 - d * 0.0005)
        candidate = min(candidate, limit) if d == 1 else max(candidate, limit)

        improvement = d * (candidate - pos.stop_loss)
        if improvement <= 0 or improvement / price < self.s.trail_min_step_pct:
            return
        old = pos.stop_loss
        await self._move_stop(pos, candidate)
        log.info("Trailing stop %s: %.2f -> %.2f", pos.side, old, pos.stop_loss)

    # ============================================================ closing
    async def _close_all(self, pos: Position, reason: str, price_hint: float) -> None:
        d = pos.direction
        exit_price = price_hint
        fee: Optional[float] = None
        if pos.mode == "LIVE":
            if pos.exit_order_id:
                await self._settle_exit(pos)
                return
            try:
                live_contracts = await self.client.fetch_position_contracts()
            except Exception as exc:  # noqa: BLE001
                log.warning("fetch_position_contracts failed: %s", exc)
                live_contracts = pos.remaining_contracts
            qty = min(pos.remaining_contracts, live_contracts)
            if qty > 0:
                pos.close_reason = reason
                self._save()
                order = await self.client.create_market(pos.close_side, qty, reduce_only=True)
                pos.exit_order_id = str(order["id"])
                pos.exit_kind = "FULL"
                pos.exit_price_hint = price_hint
                self._save()
                await self._settle_exit(pos)
                return
            await self.client.cancel_all()
        else:
            exit_price = price_hint * (1 - d * self.s.slippage_pct)
        await self._finalize(pos, exit_price, reason, fee)

    async def _settle_exit(self, pos: Position) -> None:
        """Never book a market order as filled solely because it was accepted."""
        order = await self.client.fetch_order(pos.exit_order_id)
        if order.get("status") not in ("closed", "canceled", "cancelled", "expired", "rejected"):
            return
        filled = min(float(order.get("filled") or 0), pos.remaining_contracts)
        if filled <= 0:
            pos.exit_order_id = ""
            self._save()
            return
        price = ExchangeClient.order_avg_price(order, pos.exit_price_hint)
        fee = ExchangeClient.order_fee(order)
        if filled >= pos.remaining_contracts - 1e-12:
            await self.client.cancel_all()
            await self._finalize(pos, price, pos.close_reason or "TP1", fee)
            return
        base = filled * self.client.contract_size
        gross = (price - pos.fill_price) * pos.direction * base
        fee = fee if fee is not None else self.risk.fee(price, base, maker=False)
        old_partial = pos.partial_contracts
        pos.partial_contracts += filled
        pos.partial_exit_price = (pos.partial_exit_price * old_partial + price * filled) / pos.partial_contracts
        pos.remaining_contracts -= filled
        pos.realized_pnl += gross
        pos.fees_paid += fee
        kind = pos.exit_kind
        pos.exit_order_id = ""
        if kind == "PARTIAL":
            pos.stage = 1
        self._save()
        if kind == "PARTIAL":
            await self._move_stop(pos, pos.break_even)
            self._save()
            await self.notifier.notify_risk_free(pos, filled, price, gross - fee)

    async def _finalize(self, pos: Position, exit_price: float, reason: str, fee_override: Optional[float]) -> None:
        d = pos.direction
        cs = self.client.contract_size
        base = pos.remaining_contracts * cs
        gross = (exit_price - pos.fill_price) * d * base
        fee = fee_override if fee_override is not None else self.risk.fee(exit_price, base, maker=False)
        pos.realized_pnl += gross
        pos.fees_paid += fee

        partial_base = pos.partial_contracts * cs
        total_base = partial_base + base
        pos.exit_price_avg = (
            (pos.partial_exit_price * partial_base + exit_price * base) / total_base if total_base > 0 else exit_price
        )
        pos.remaining_contracts = 0.0
        pos.status = "CLOSED"
        pos.close_reason = reason
        pos.closed_at = utcnow().isoformat()
        net = pos.net_pnl

        if pos.mode == "DRY":
            self._add_virtual(gross - fee)
            balance_after = self.virtual_balance
        else:
            try:
                balance_after = await self.client.fetch_free_usdt()
            except Exception as exc:  # noqa: BLE001
                log.warning("Balance fetch after close failed: %s", exc)
                balance_after = pos.balance_before + net

        events = self.risk.register_trade_result(net, balance_after)
        record = asdict(pos)
        record["net_pnl"] = net
        self.store.record_trade(record)

        self.position = None
        self._save()
        self._trail_df = None
        log.info("Position closed %s reason=%s net=%.4f balance=%.4f", pos.side, reason, net, balance_after)

        await self.notifier.notify_position_closed(
            pos, net, balance_after, self.risk.daily, self.risk.daily_target(), self.risk.daily_loss_limit()
        )
        for event in events:
            halt = self.risk.halt_state()
            await self.notifier.notify_daily_halt(event, self.risk.daily, halt["until"] if halt else "")

    # ============================================================== panic
    async def panic_close(self) -> list[str]:
        """Emergency: cancel all orders, flatten all positions, pause scanning."""
        self.set_paused(True)
        messages: list[str] = ["⏸ اسکن بازار متوقف شد."]
        async with self.lock:
            pos = self.position
            live = self.mode == "LIVE" or (pos is not None and pos.mode == "LIVE")
            if live:
                await self.client.cancel_all()
                messages.append("🧹 تمام سفارش‌های باز (ورود و حد ضرر) لغو شد.")
                if pos is not None and pos.status == "OPEN":
                    t = await self._ticker(pos)
                    await self._close_all(pos, "PANIC", t["last"])
                    messages.append("✅ پوزیشن ثبت‌شده ربات با قیمت بازار بسته شد.")
                elif pos is not None and pos.status == "PENDING":
                    await self.client.cancel_order(pos.entry_order_id)
                    order = await self.client.fetch_order(pos.entry_order_id)
                    if order.get("status") not in ("closed", "canceled", "cancelled", "expired", "rejected"):
                        raise RuntimeError("Pending entry cancellation is unconfirmed")
                    filled = float(order.get("filled") or 0)
                    if filled:
                        await self._on_filled(pos, ExchangeClient.order_avg_price(order, pos.entry_price),
                                              filled, ExchangeClient.order_fee(order), maker=self.s.entry_post_only)
                        t = await self._ticker(pos)
                        await self._close_all(pos, "PANIC", t["last"])
                    else:
                        await self._cancel_pending(pos, "بستن اضطراری (/panic)", notify=False)
                    messages.append("✅ سفارش ورود بررسی و لغو شد؛ بخش پرشده برای بستن ارسال شد.")
                if self.position is not None and self.position.exit_order_id:
                    messages.append("⏳ تأیید اجرای سفارش خروج در حال پیگیری است؛ حساب صرافی را بررسی کنید.")
                    return messages
                closed = await self.client.close_all_positions()
                if closed:
                    messages.append(f"✅ {closed} پوزیشن باقی‌مانده روی صرافی بسته شد.")
                if pos is not None and pos.status == "UNKNOWN":
                    if await self.client.fetch_position_contracts() > 0:
                        raise RuntimeError("Unknown entry still has an exchange position")
                    open_orders = await self.client.trade_ex.fetch_open_orders(self.s.symbol)
                    if open_orders:
                        raise RuntimeError("Unknown entry still has open exchange orders")
                    await self._cancel_pending(pos, "بازیابی سفارش نامشخص با /panic", notify=False)
            else:
                if pos is not None and pos.status == "OPEN":
                    t = await self._ticker(pos)
                    await self._close_all(pos, "PANIC", t["last"])
                    messages.append("✅ پوزیشن شبیه‌سازی‌شده با قیمت بازار بسته شد.")
                elif pos is not None and pos.status == "PENDING":
                    await self._cancel_pending(pos, "بستن اضطراری (/panic)", notify=False)
                    messages.append("✅ سفارش ورود شبیه‌سازی‌شده لغو شد.")
                else:
                    messages.append("ℹ️ پوزیشن یا سفارش بازی وجود نداشت.")
        return messages

    # ========================================================== recovery
    async def reconcile_on_startup(self) -> Optional[str]:
        async with self.lock:
            pos = self.position
            if pos is None:
                if self.mode == "LIVE" and await self.client.fetch_position_contracts() > 0:
                    self.set_paused(True)
                    return "پوزیشن ناشناس در حساب وجود دارد؛ اسکن متوقف شد. حساب را دستی بررسی کنید."
                return None
            if pos.status == "UNKNOWN":
                self.set_paused(True)
                return "نتیجه ثبت سفارش قبلی نامشخص است؛ حساب را بررسی کنید و از /panic برای پاک‌سازی استفاده کنید."
            if pos.mode == "LIVE" and pos.status == "OPEN":
                contracts = await self.client.fetch_position_contracts()
                if contracts <= 0:
                    t = await self._ticker(pos)
                    await self._finalize(pos, t["last"], "RESTART_SYNC", None)
                    return "پوزیشن ذخیره‌شده هنگام خاموش بودن ربات بسته شده بود و گزارش آن ثبت شد."
                pos.remaining_contracts = min(pos.remaining_contracts, contracts)
                self._save()
            return f"پوزیشن {pos.side} ({pos.status}) از حافظه بازیابی شد و مدیریت آن ادامه دارد."

    # ============================================================= status
    async def status_snapshot(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "mode": self.mode,
            "paused": self.paused,
            "min_balance": self.s.min_balance_to_trade,
            "virtual_balance": self.virtual_balance,
            "starting_balance": self.s.starting_balance,
        }
        try:
            bal = await self.get_balance()
            info["balance"] = bal
        except Exception as exc:  # noqa: BLE001
            info["balance"] = None
            info["balance_error"] = str(exc)

        if self.mode == "LIVE" or self.has_exchange_keys:
            try:
                real_bal = info["balance"] if self.mode == "LIVE" and info.get("balance") is not None else await self.client.fetch_free_usdt()
                info["real_futures_balance"] = real_bal
                info["spot_balance"] = await self.client.fetch_spot_usdt()
            except Exception as exc:  # noqa: BLE001
                info["real_futures_balance"] = None
                info["real_balance_error"] = str(exc)

        pos = self.position
        price = None
        try:
            price = (await self._ticker(pos))["last"]
        except Exception as exc:  # noqa: BLE001
            log.warning("status ticker failed: %s", exc)
        info["price"] = price
        if pos is not None:
            info["position"] = pos
            if pos.status == "OPEN" and price:
                base = pos.remaining_contracts * self.client.contract_size
                unrealized = (price - pos.fill_price) * pos.direction * base
                exit_fee = self.risk.fee(price, base, maker=False)
                info["unrealized"] = unrealized + pos.realized_pnl - pos.fees_paid - exit_fee
        return info
