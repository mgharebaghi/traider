"""
Gemini decision engine (google-genai SDK).

* Called ONLY when `MarketAnalyzer.detect_setup` finds a valid pre-condition.
* Hard budget: AI_MAX_CALLS_PER_DAY + AI_MIN_INTERVAL_SEC + one call per
  (direction, 15m candle) setup key.
* Forces a strict JSON schema and then validates/normalises every number so
  the risk engine never receives an inconsistent plan.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Optional
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types
from pydantic import BaseModel

from config import Settings
from exchange_client import ExchangeClient
from indicators import MarketSnapshot, Setup
from state_store import StateStore

log = logging.getLogger("ai_agent")


class Action(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    WAIT = "WAIT"


class GeminiSignal(BaseModel):
    """Response schema passed to Gemini (kept constraint-free for schema compatibility)."""

    action: Action
    confidence: int
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    suggested_leverage: int
    reasoning_fa: str


@dataclass
class TradeSignal:
    action: str
    confidence: int
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    suggested_leverage: int
    reasoning_fa: str
    setup_reasons: list[str] = field(default_factory=list)
    adjustments: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_trade(self) -> bool:
        return self.action in ("LONG", "SHORT")


SYSTEM_INSTRUCTION = """You are a cautious perpetual-futures analyst and risk manager.
You receive a JSON market snapshot (15m trend structure + 1m entry data, EMA9/EMA26, RSI14, ATR14,
support/resistance zones) and a technical pre-condition detected by a local algorithm.

Your job: decide whether to open a scalp NOW in the suggested direction, or WAIT.

Hard rules:
1. Only trade WITH the 15m trend. If the pre-condition looks weak, late, or price is in the middle of
   nowhere, answer WAIT. Capital preservation beats activity.
2. entry_price: a realistic reference entry close to the current oracle min/max,
   max 0.15% away from the current price.
3. stop_loss: behind the nearest 1m/15m structure (swing low/high, S/R zone or EMA26) with an ATR buffer.
   Distance between entry and stop must be between {min_sl:.2f}% and {max_sl:.2f}% of price.
4. take_profit_1 ≈ {tp1_rr:.1f}R (R = |entry - stop_loss|). take_profit_2 >= {min_rr:.1f}R and ideally at the
   next opposing S/R zone. For LONG: stop_loss < entry < tp1 < tp2. For SHORT: stop_loss > entry > tp1 > tp2.
5. suggested_leverage: integer between {min_lev} and {max_lev}. Do not raise leverage to recover losses.
6. Round-trip fees are about {fees:.3f}% of notional — the move to TP1 must comfortably exceed fees.
7. confidence: 0-100. Use >= {min_conf} only for genuinely high-probability setups.
8. reasoning_fa: a precise, professional explanation IN PERSIAN (Farsi) of why you enter or wait,
   mentioning trend, pullback, RSI/momentum, the chosen S/R levels and invalidation. 3-6 sentences.
9. If action is WAIT, still fill numeric fields with your best hypothetical levels (or current price).
For GMX, min/max are oracle bounds, not an order book; there is no maker/post-only advantage.
When a chart image is supplied, inspect candle structure, trend, RSI divergence, EMA,
support/resistance and volatility together with the numerical data. Volume is unavailable;
do not invent volume, order flow, news, funding observations or a guaranteed profitable day.
Fixed execution and carrying reserves are additional to percentage fees. Reject marginal scalps.
For GMX the configured MIN_RR must hold NET of costs for the weighted TP1/TP2 exit,
not merely for the gross TP2 distance. A distant plausible structural target or WAIT
is preferable to manufacturing levels to pass the risk filter.
TP1/TP2 are exit recommendations from this analysis. Native SL/TP execution does not require another AI call.
Return ONLY the JSON object matching the schema."""


class AIAgent:
    USAGE_KEY = "ai_usage"
    MODEL_KEY = "gemini_model"

    def __init__(self, settings: Settings, store: StateStore, client: ExchangeClient) -> None:
        self._call_lock = asyncio.Lock()
        self.s = settings
        self.store = store
        self.client = client
        self._genai = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(timeout=30000, retry_options=types.HttpRetryOptions(attempts=1)),
        )
        if not self.store.get(self.MODEL_KEY):
            self.store.set(self.MODEL_KEY, settings.gemini_model)

    @property
    def active_model(self) -> str:
        return str(self.store.get(self.MODEL_KEY) or self.s.gemini_model)

    def set_model(self, model_name: str) -> str:
        model_name = model_name.strip()
        self.store.set(self.MODEL_KEY, model_name)
        usage = self._usage()
        if usage.get("failure_kind") == "access":
            usage.update(blocked_until=0, last_error="", failure_kind="")
            self.store.set(self.USAGE_KEY, usage)
        return model_name

    async def close(self) -> None:
        await self._genai.aio.aclose()
        self._genai.close()

    # ------------------------------------------------------------ budget
    def _usage(self) -> dict[str, Any]:
        today = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
        usage = self.store.get(self.USAGE_KEY) or {}
        if usage and usage.get("budget_timezone") != "America/Los_Angeles":
            # Carry the old UTC count forward once rather than granting a fresh
            # budget on deployment inside the same Google quota day.
            usage.update(date=today, budget_timezone="America/Los_Angeles")
            self.store.set(self.USAGE_KEY, usage)
        if usage.get("date") != today:
            usage = {**usage, "date": today, "count": 0, "keys": [], "budget_timezone": "America/Los_Angeles"}
            self.store.set(self.USAGE_KEY, usage)
        return usage

    def calls_today(self) -> int:
        return int(self._usage().get("count", 0))

    def can_call(self, setup_key: str) -> tuple[bool, str]:
        usage = self._usage()
        if self.s.ai_quota_block_until and datetime.now(timezone.utc) < datetime.fromisoformat(self.s.ai_quota_block_until):
            return False, f"سهمیه قبلاً تمام شده؛ درخواست تازه بعد از {self.s.ai_quota_block_until} مجاز است."
        if time.time() < float(usage.get("blocked_until", 0)):
            return False, "مکث محافظتی جمنای فعال است؛ بررسی وضعیت را بزنید و تا زمان اعلام‌شده صبر کنید."
        if usage["count"] >= self.s.ai_max_calls_per_day:
            return False, "بودجه روزانه جمنای تمام شده؛ تمدید در نیمه‌شب Pacific است."
        elapsed = time.time() - float(usage.get("last_call_ts", 0.0))
        interval = max(60 / self.s.ai_requests_per_minute, self.s.ai_min_interval_sec)
        if elapsed < interval:
            return False, f"برای حفظ سهمیه جمنای {math.ceil(interval - elapsed)} ثانیه صبر کنید."
        if setup_key in usage.get("keys", []):
            return False, "این موقعیت قبلاً بررسی شده؛ منتظر موقعیت تازه هستیم."
        return True, ""

    @property
    def status_reason(self) -> str:
        if self.s.ai_quota_block_until:
            until_config = datetime.fromisoformat(self.s.ai_quota_block_until)
            if datetime.now(timezone.utc) < until_config:
                local = until_config.astimezone(ZoneInfo(self.s.display_timezone))
                return f"سهمیه قبلاً مصرف شده؛ تحلیل جدید پس از {local:%Y-%m-%d %H:%M} ({self.s.display_timezone}) شروع می‌شود."
        usage = self._usage()
        reason = str(usage.get("last_error", ""))
        until = float(usage.get("blocked_until", 0))
        if until > time.time():
            return f"{reason} | پایان مکث (Pacific): {datetime.fromtimestamp(until, ZoneInfo('America/Los_Angeles')).isoformat(timespec='minutes')}"
        return reason

    def _register_call(self, setup_key: str) -> None:
        usage = self._usage()
        usage["count"] = int(usage["count"]) + 1
        usage["last_call_ts"] = time.time()
        keys = list(usage.get("keys", []))
        keys.append(setup_key)
        usage["keys"] = keys
        self.store.set(self.USAGE_KEY, usage)

    # ------------------------------------------------------------ prompt
    def _system_instruction(self) -> str:
        return SYSTEM_INSTRUCTION.format(
            min_sl=self.s.min_sl_pct * 100,
            max_sl=self.s.max_sl_pct * 100,
            tp1_rr=self.s.tp1_rr,
            min_rr=self.s.min_rr,
            min_lev=self.s.min_leverage,
            max_lev=self.s.max_leverage,
            fees=(self.s.maker_fee + self.s.taker_fee) * 100,
            min_conf=self.s.ai_min_confidence,
        )

    def _user_prompt(self, snap: MarketSnapshot, setup: Setup) -> str:
        payload = {
            "symbol": self.s.symbol,
            "exchange": self.s.exchange_id,
            "cost_reserves": {"execution_usd": self.s.gmx_execution_budget_usd,
                              "funding_borrowing_fraction": self.s.gmx_carry_budget_pct},
            "suggested_direction": setup.direction,
            "pre_condition_reasons": setup.reasons,
            "market": snap.to_prompt_dict(),
        }
        return (
            "Analyse this live market snapshot and decide. "
            "Respond strictly with the JSON schema.\n\n" + json.dumps(payload, ensure_ascii=False)
        )

    # ----------------------------------------------------------- analyze
    async def analyze(self, snap: MarketSnapshot, setup: Setup, force: bool = False) -> Optional[TradeSignal]:
        # Manual requests share exactly the same persistent budget as the scanner.
        # Keep the lock through the HTTP response to prevent overlapping reservations.
        if not hasattr(self, "_call_lock"):
            self._call_lock = asyncio.Lock()
        async with self._call_lock:
            if not self.can_call(setup.key)[0]:
                return None
            prompt = self._user_prompt(snap, setup)
            system = self._system_instruction()
            contents = prompt
            if self.s.exchange_id == 'gmx':
                from chart_image import render_chart
                png = render_chart(snap)
                contents = [types.Part.from_text(text=prompt), types.Part.from_bytes(data=png, mime_type='image/png')]
            # UTF-8 bytes provide a conservative upper bound for text input tokens,
            # without a second countTokens API request. Reserve protocol overhead too.
            tokens = len((system + prompt).encode("utf-8")) + (4096 if self.s.exchange_id == 'gmx' else 2048)
            usage = self._usage()
            reservations = [r for r in usage.get("reservations", []) if r[0] > time.time() - 60]
            if tokens + sum(r[1] for r in reservations) > self.s.ai_input_tokens_per_minute:
                usage["last_error"] = "ورودی تحلیل از بودجه توکن دقیقه بیشتر است؛ تنظیم TPM را با AI Studio تطبیق دهید."
                self.store.set(self.USAGE_KEY, usage)
                return None
            reservations.append([time.time(), tokens])
            usage["reservations"] = reservations
            self.store.set(self.USAGE_KEY, usage)
            self._register_call(setup.key)
            try:
                thinking = (types.ThinkingConfig(thinking_level="low")
                            if self.active_model.startswith(("gemini-3", "gemini-4")) else None)
                response = await self._genai.aio.models.generate_content(
                    model=self.active_model, contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system, response_mime_type="application/json",
                        response_schema=GeminiSignal, temperature=self.s.ai_temperature,
                        max_output_tokens=self.s.ai_max_output_tokens,
                        thinking_config=thinking,
                    ),
                )
                parsed = GeminiSignal.model_validate_json((response.text or "").strip())
                result = self._normalise(parsed, snap, setup)
                usage = self._usage()
                usage["last_error"] = ""
                self.store.set(self.USAGE_KEY, usage)
                return result
            except Exception as exc:
                log.warning("Gemini analysis failed (%s): %s", self.active_model, exc)
                error = str(exc).lower()
                now = datetime.now(ZoneInfo("America/Los_Angeles"))
                usage = self._usage()
                if any(word in error for word in ("429", "resource_exhausted", "quota")):
                    usage["failure_kind"] = "quota"
                    # Unknown project quota: fail closed until Google's daily reset.
                    until = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                    usage["blocked_until"] = until.timestamp()
                    message = "سهمیه پروژه جمنای پاسخ ۴۲۹ داد؛ تحلیل جدید تا تمدید روزانه متوقف شد. سقف‌های AI Studio را بررسی کنید."
                elif any(word in error for word in ("404", "not found", "invalid model", "403", "401")):
                    usage["failure_kind"] = "access"
                    usage["blocked_until"] = time.time() + 86400
                    message = "مدل یا دسترسی API معتبر نیست؛ نام مدل و مجوز پروژه را در AI Studio بررسی کنید."
                else:
                    usage["failure_kind"] = "transient"
                    usage["blocked_until"] = time.time() + max(60, self.s.ai_min_interval_sec)
                    message = "تحلیل معتبر دریافت نشد؛ ممکن است پاسخ ناقص یا اتصال دچار اختلال باشد. تلاش بعدی پس از مکث محافظتی انجام می‌شود."
                usage["last_error"] = message
                self.store.set(self.USAGE_KEY, usage)
                return None

    # ------------------------------------------------------- validation
    def _normalise(self, g: GeminiSignal, snap: MarketSnapshot, setup: Setup) -> TradeSignal:
        s = self.s
        adjustments: list[str] = []
        action = g.action.value
        confidence = max(0, min(100, int(g.confidence)))
        signal = TradeSignal(
            action=action,
            confidence=confidence,
            entry_price=float(g.entry_price),
            stop_loss=float(g.stop_loss),
            take_profit_1=float(g.take_profit_1),
            take_profit_2=float(g.take_profit_2),
            suggested_leverage=int(g.suggested_leverage),
            reasoning_fa=g.reasoning_fa.strip(),
            setup_reasons=list(setup.reasons),
            adjustments=adjustments,
            raw=g.model_dump(mode="json"),
        )
        if action == "WAIT":
            return signal

        if not all(math.isfinite(v) for v in (
            signal.entry_price, signal.stop_loss, signal.take_profit_1, signal.take_profit_2,
        )):
            raise ValueError("Gemini returned non-finite prices")

        if action != setup.direction:
            adjustments.append("جهت جمینای خلاف روند ۱۵ دقیقه بود؛ معامله لغو شد")
            signal.action = "WAIT"
            return signal

        d = 1 if action == "LONG" else -1
        price = snap.price

        # --- entry: close to market & on the maker side of the book
        entry = signal.entry_price
        if entry <= 0 or abs(entry - price) / price > s.max_entry_deviation_pct:
            entry = snap.bid if d == 1 else snap.ask
            adjustments.append("قیمت ورود به نزدیک‌ترین قیمت بازار اصلاح شد")
        if d == 1 and entry > snap.ask:
            entry = snap.bid
            adjustments.append("قیمت ورود لانگ به Bid منتقل شد (سفارش Maker)")
        if d == -1 and entry < snap.bid:
            entry = snap.ask
            adjustments.append("قیمت ورود شورت به Ask منتقل شد (سفارش Maker)")

        # --- stop loss side & distance
        sl = signal.stop_loss
        if (d == 1 and sl >= entry) or (d == -1 and sl <= entry) or sl <= 0:
            adjustments.append("حد ضرر در سمت اشتباه بود؛ معامله لغو شد")
            signal.action = "WAIT"
            return signal
        sl_pct = abs(entry - sl) / entry
        if sl_pct < s.min_sl_pct:
            sl = entry - d * entry * s.min_sl_pct
            adjustments.append(f"حد ضرر برای حداقل فاصله {s.min_sl_pct * 100:.2f}% بازتر شد")
        elif sl_pct > s.max_sl_pct:
            adjustments.append(f"فاصله حد ضرر ({sl_pct * 100:.2f}%) بیش از حد مجاز بود؛ معامله لغو شد")
            signal.action = "WAIT"
            return signal

        risk = abs(entry - sl)

        # --- take profits
        tp2 = signal.take_profit_2
        if d * (tp2 - entry) < s.min_rr * risk:
            tp2 = entry + d * s.min_rr * risk
            adjustments.append(f"حد سود دوم برای حداقل R/R 1:{s.min_rr:g} تنظیم شد")
        tp1 = signal.take_profit_1
        tp1_r = d * (tp1 - entry) / risk
        if tp1_r < 1.0 or d * (tp2 - tp1) <= 0:
            tp1 = entry + d * s.tp1_rr * risk
            adjustments.append(f"حد سود اول روی R/R 1:{s.tp1_rr:g} تنظیم شد")

        pp = self.client.price_to_precision
        signal.entry_price = pp(entry)
        signal.stop_loss = pp(sl)
        signal.take_profit_1 = pp(tp1)
        signal.take_profit_2 = pp(tp2)
        signal.suggested_leverage = max(s.min_leverage, min(s.max_leverage, signal.suggested_leverage))
        return signal
