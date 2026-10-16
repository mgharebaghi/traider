"""
Risk management engine.

Rules enforced (defaults for a $10 account):
  * Max risk per trade = 4% of current balance ($0.40), INCLUDING fees + slippage.
  * ISOLATED margin, dynamic leverage 10x..35x, liquidation always well beyond SL.
  * Minimum R/R 1:2.
  * Daily profit target  : +10% of day-start balance (+$1.00)  -> pause until next UTC day.
  * Daily circuit breaker: 2 consecutive losses OR -8% (-$0.80) -> halt for 24 hours.
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import Settings
from exchange_client import ExchangeClient
from state_store import StateStore

log = logging.getLogger("risk")


@dataclass
class PositionPlan:
    ok: bool
    reason: str = ""
    contracts: float = 0.0
    base_qty: float = 0.0
    notional: float = 0.0
    leverage: int = 0
    margin: float = 0.0
    risk_budget: float = 0.0
    expected_loss: float = 0.0
    expected_fees: float = 0.0
    sl_pct: float = 0.0
    rr_tp1: float = 0.0
    rr_tp2: float = 0.0
    liquidation_price: float = 0.0
    risk_reduced: bool = False


@dataclass
class DailyStats:
    date: str
    start_balance: float
    realized_pnl: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    consecutive_losses: int = 0
    target_hit: bool = False
    breaker_hit: bool = False


class RiskManager:
    DAILY_KEY = "daily_stats"
    HALT_KEY = "halt"

    def __init__(self, settings: Settings, store: StateStore, client: ExchangeClient) -> None:
        self.s = settings
        self.store = store
        self.client = client

    # ================================================================ daily
    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @property
    def daily(self) -> Optional[DailyStats]:
        raw = self.store.get(self.DAILY_KEY)
        return DailyStats(**raw) if raw else None

    def _save_daily(self, d: DailyStats) -> None:
        self.store.set(self.DAILY_KEY, asdict(d))

    def needs_roll(self) -> bool:
        d = self.daily
        return d is None or d.date != self._today()

    def roll_day(self, balance: float) -> Optional[DailyStats]:
        """Starts a new UTC day if needed. Returns the PREVIOUS day stats when rolled."""
        previous = self.daily
        if previous is not None and previous.date == self._today():
            return None
        self._save_daily(DailyStats(date=self._today(), start_balance=round(balance, 6)))
        halt = self.halt_state()
        if halt and halt.get("reason") == "target":
            self.store.delete(self.HALT_KEY)
        log.info("New trading day %s, start balance %.4f", self._today(), balance)
        return previous

    def daily_target(self) -> float:
        d = self.daily
        base = d.start_balance if d else self.s.starting_balance
        if self.s.daily_profit_target_pct > 0:
            return round(base * self.s.daily_profit_target_pct, 4)
        return self.s.daily_profit_target_usd

    def daily_loss_limit(self) -> float:
        d = self.daily
        base = d.start_balance if d else self.s.starting_balance
        if self.s.daily_max_loss_pct > 0:
            return round(base * self.s.daily_max_loss_pct, 4)
        return self.s.daily_max_loss_usd

    # ================================================================= halt
    def halt_state(self) -> Optional[dict]:
        halt = self.store.get(self.HALT_KEY)
        if not halt:
            return None
        until = datetime.fromisoformat(halt["until"])
        if datetime.now(timezone.utc) >= until:
            self.store.delete(self.HALT_KEY)
            return None
        return halt

    def _halt(self, until: datetime, reason: str) -> None:
        self.store.set(self.HALT_KEY, {"until": until.isoformat(), "reason": reason})

    def clear_halt(self) -> None:
        self.store.delete(self.HALT_KEY)

    def can_trade(self) -> tuple[bool, str]:
        halt = self.halt_state()
        if halt:
            return False, f"halted ({halt['reason']}) until {halt['until']}"
        d = self.daily
        if d and d.target_hit:
            return False, "daily target reached"
        return True, ""

    # ============================================================ sizing
    def plan_position(
        self,
        balance: float,
        side: str,
        entry: float,
        stop_loss: float,
        tp1: float,
        tp2: float,
        suggested_leverage: int,
    ) -> PositionPlan:
        s = self.s
        if side not in ("LONG", "SHORT") or not all(
            math.isfinite(v) and v > 0 for v in (balance, entry, stop_loss, tp1, tp2)
        ):
            return PositionPlan(False, "جهت یا اعداد طرح معامله نامعتبر است")
        d = 1 if side == "LONG" else -1
        if balance <= 0:
            return PositionPlan(False, "موجودی حساب صفر است")
        dist = abs(entry - stop_loss)
        if dist <= 0:
            return PositionPlan(False, "فاصله ورود تا حد ضرر صفر است")
        if d * (entry - stop_loss) <= 0:
            return PositionPlan(False, "حد ضرر در سمت اشتباه است")

        risk_budget = balance * s.risk_per_trade_pct
        # Conservative: assume taker fees on both legs + slippage on the stop fill.
        fee_in = s.taker_fee
        fee_out = s.taker_fee
        loss_per_unit = dist + entry * fee_in + stop_loss * fee_out + stop_loss * s.slippage_pct
        base_qty = risk_budget / loss_per_unit
        sl_pct = dist / entry
        if not s.min_sl_pct - 1e-9 <= sl_pct <= s.max_sl_pct + 1e-9:
            return PositionPlan(False, "فاصله حد ضرر خارج از بازه مجاز است")
        if d * (tp1 - entry) <= 0 or d * (tp2 - tp1) <= 0:
            return PositionPlan(False, "ترتیب اهداف سود نامعتبر است")

        # Liquidation-safe leverage cap: liquidation distance ≈ 1/lev - MMR
        max_safe_lev = math.floor(1.0 / (sl_pct * s.liquidation_buffer + s.maintenance_margin_rate))
        lev_cap = min(s.max_leverage, max_safe_lev)
        if lev_cap < s.min_leverage:
            return PositionPlan(
                False,
                f"حد ضرر ({sl_pct * 100:.2f}%) برای حداقل لوریج {s.min_leverage}x خیلی باز است (ریسک لیکوئید شدن)",
            )

        margin_budget = balance * s.max_margin_usage
        notional = base_qty * entry
        lev_needed = math.ceil(notional / margin_budget) if margin_budget > 0 else s.max_leverage
        risk_reduced = False
        if lev_needed > lev_cap:
            notional = margin_budget * lev_cap
            base_qty = notional / entry
            lev_needed = lev_cap
            risk_reduced = True

        leverage = max(s.min_leverage, lev_needed, min(int(suggested_leverage or 0), lev_cap))
        leverage = int(min(leverage, lev_cap))

        contracts = self.client.amount_to_precision(base_qty / self.client.contract_size)
        min_amount = self.client.min_amount
        if contracts <= 0 or (min_amount and contracts < min_amount):
            min_notional = max(min_amount, 0) * self.client.contract_size * entry
            return PositionPlan(
                False,
                "حجم محاسبه‌شده از حداقل حجم مجاز صرافی کمتر است "
                f"(حداقل ارزش پوزیشن ≈ {min_notional:.2f}$). موجودی یا فاصله حد ضرر کافی نیست.",
            )

        base_qty = contracts * self.client.contract_size
        notional = base_qty * entry
        if self.client.min_cost and notional < self.client.min_cost:
            return PositionPlan(False, f"ارزش پوزیشن ({notional:.2f}$) کمتر از حداقل صرافی است")
        margin = notional / leverage
        expected_fees = notional * fee_in + base_qty * stop_loss * fee_out
        expected_loss = base_qty * loss_per_unit
        if expected_loss > risk_budget + 1e-9:
            return PositionPlan(False, "ریسک پس از گرد کردن حجم از سقف مجاز بیشتر شد")
        if margin > margin_budget + 1e-9 or margin + notional * fee_in > balance:
            return PositionPlan(False, "مارجین مورد نیاز از موجودی بیشتر است")

        rr_tp1 = d * (tp1 - entry) / dist
        rr_tp2 = d * (tp2 - entry) / dist
        if rr_tp2 < s.min_rr - 1e-6:
            return PositionPlan(False, f"نسبت ریسک به ریوارد ({rr_tp2:.2f}) کمتر از 1:{s.min_rr:g} است")

        liq = entry * (1 - d / leverage + d * s.maintenance_margin_rate)
        return PositionPlan(
            ok=True,
            contracts=contracts,
            base_qty=base_qty,
            notional=notional,
            leverage=leverage,
            margin=margin,
            risk_budget=risk_budget,
            expected_loss=expected_loss,
            expected_fees=expected_fees,
            sl_pct=sl_pct,
            rr_tp1=rr_tp1,
            rr_tp2=rr_tp2,
            liquidation_price=self.client.price_to_precision(liq),
            risk_reduced=risk_reduced,
        )

    # ============================================================== helpers
    def break_even_price(self, side: str, fill_price: float) -> float:
        d = 1 if side == "LONG" else -1
        return self.client.price_to_precision(fill_price * (1 + d * self.s.effective_breakeven_buffer))

    def fee(self, price: float, base_qty: float, maker: bool) -> float:
        rate = self.s.maker_fee if maker else self.s.taker_fee
        return abs(price * base_qty) * rate

    # ============================================================ results
    def register_trade_result(self, net_pnl: float, balance_after: float) -> list[str]:
        """Updates daily stats and returns triggered events:
        TARGET_HIT | BREAKER_CONSECUTIVE | BREAKER_DAILY_LOSS"""
        if self.needs_roll():
            self.roll_day(balance_after - net_pnl)
        d = self.daily
        assert d is not None
        d.realized_pnl = round(d.realized_pnl + net_pnl, 6)
        d.trades += 1
        if net_pnl > 0:
            d.wins += 1
            d.consecutive_losses = 0
        else:
            d.losses += 1
            d.consecutive_losses += 1

        events: list[str] = []
        now = datetime.now(timezone.utc)
        if not d.target_hit and d.realized_pnl >= self.daily_target():
            d.target_hit = True
            next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            self._halt(next_day, "target")
            events.append("TARGET_HIT")
        elif not d.breaker_hit:
            until = now + timedelta(hours=self.s.circuit_breaker_hours)
            if d.consecutive_losses >= self.s.max_consecutive_losses:
                d.breaker_hit = True
                self._halt(until, "breaker_consecutive")
                events.append("BREAKER_CONSECUTIVE")
            elif d.realized_pnl <= -self.daily_loss_limit():
                d.breaker_hit = True
                self._halt(until, "breaker_daily_loss")
                events.append("BREAKER_DAILY_LOSS")
        self._save_daily(d)
        return events
