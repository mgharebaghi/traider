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
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

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


SYSTEM_INSTRUCTION = """You are an elite BTC perpetual-futures scalper and risk manager.
You receive a JSON market snapshot (15m trend structure + 1m entry data, EMA9/EMA26, RSI14, ATR14,
support/resistance zones) and a technical pre-condition detected by a local algorithm.

Your job: decide whether to open a scalp NOW in the suggested direction, or WAIT.

Hard rules:
1. Only trade WITH the 15m trend. If the pre-condition looks weak, late, or price is in the middle of
   nowhere, answer WAIT. Capital preservation beats activity.
2. entry_price: a realistic LIMIT price very close to the current bid (LONG) or ask (SHORT),
   max 0.15% away from the current price.
3. stop_loss: behind the nearest 1m/15m structure (swing low/high, S/R zone or EMA26) with an ATR buffer.
   Distance between entry and stop must be between {min_sl:.2f}% and {max_sl:.2f}% of price.
4. take_profit_1 ≈ {tp1_rr:.1f}R (R = |entry - stop_loss|). take_profit_2 >= {min_rr:.1f}R and ideally at the
   next opposing S/R zone. For LONG: stop_loss < entry < tp1 < tp2. For SHORT: stop_loss > entry > tp1 > tp2.
5. suggested_leverage: integer between {min_lev} and {max_lev}. Tighter stop => higher leverage is acceptable.
6. Round-trip fees are about {fees:.3f}% of notional — the move to TP1 must comfortably exceed fees.
7. confidence: 0-100. Use >= {min_conf} only for genuinely high-probability setups.
8. reasoning_fa: a precise, professional explanation IN PERSIAN (Farsi) of why you enter or wait,
   mentioning trend, pullback, RSI/momentum, the chosen S/R levels and invalidation. 3-6 sentences.
9. If action is WAIT, still fill numeric fields with your best hypothetical levels (or current price).
Return ONLY the JSON object matching the schema."""


class AIAgent:
    USAGE_KEY = "ai_usage"
    MODEL_KEY = "gemini_model"
    FALLBACK_MODELS = (
        "gemini-3.8-flash",
        "gemini-4-argon",
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
        "gemini-2.5-flash",
    )

    def __init__(self, settings: Settings, store: StateStore, client: ExchangeClient) -> None:
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
        return model_name

    async def close(self) -> None:
        await self._genai.aio.aclose()
        self._genai.close()

    # ------------------------------------------------------------ budget
    def _usage(self) -> dict[str, Any]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        usage = self.store.get(self.USAGE_KEY) or {}
        if usage.get("date") != today:
            usage = {"date": today, "count": 0, "last_call_ts": usage.get("last_call_ts", 0.0), "keys": []}
            self.store.set(self.USAGE_KEY, usage)
        return usage

    def calls_today(self) -> int:
        return int(self._usage().get("count", 0))

    def can_call(self, setup_key: str) -> tuple[bool, str]:
        usage = self._usage()
        if usage["count"] >= self.s.ai_max_calls_per_day:
            return False, "daily AI budget exhausted"
        elapsed = time.time() - float(usage.get("last_call_ts", 0.0))
        if elapsed < self.s.ai_min_interval_sec:
            return False, f"AI cooldown ({int(self.s.ai_min_interval_sec - elapsed)}s left)"
        if setup_key in usage.get("keys", []):
            return False, "setup already evaluated"
        return True, ""

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

    @staticmethod
    def _user_prompt(snap: MarketSnapshot, setup: Setup) -> str:
        payload = {
            "symbol": "BTC/USDT perpetual",
            "suggested_direction": setup.direction,
            "pre_condition_reasons": setup.reasons,
            "market": snap.to_prompt_dict(),
        }
        return (
            "Analyse this live market snapshot and decide. "
            "Respond strictly with the JSON schema.\n\n" + json.dumps(payload, ensure_ascii=False)
        )

    # ----------------------------------------------------------- analyze
    async def analyze(self, snap: MarketSnapshot, setup: Setup) -> Optional[TradeSignal]:
        if not self.can_call(setup.key)[0]:
            return None
        config = types.GenerateContentConfig(
            system_instruction=self._system_instruction(),
            response_mime_type="application/json",
            response_schema=GeminiSignal,
            temperature=self.s.ai_temperature,
        )
        prompt = self._user_prompt(snap, setup)
        last_exc: Optional[Exception] = None
        candidates = [self.active_model] + [m for m in self.FALLBACK_MODELS if m != self.active_model]
        model_idx = 0
        for attempt in range(3):
            # Count every HTTP attempt, including failed/retried requests.
            if self.calls_today() >= self.s.ai_max_calls_per_day:
                break
            self._register_call(setup.key)
            model_name = candidates[min(model_idx, len(candidates) - 1)]
            try:
                response = await self._genai.aio.models.generate_content(
                    model=model_name, contents=prompt, config=config
                )
                text = (response.text or "").strip()
                if not text:
                    raise ValueError("Empty response from Gemini")
                parsed = GeminiSignal.model_validate_json(text)
                signal = self._normalise(parsed, snap, setup)
                log.info(
                    "Gemini (%s) -> %s conf=%s entry=%.1f sl=%.1f tp1=%.1f tp2=%.1f adj=%s",
                    model_name,
                    signal.action,
                    signal.confidence,
                    signal.entry_price,
                    signal.stop_loss,
                    signal.take_profit_1,
                    signal.take_profit_2,
                    signal.adjustments,
                )
                return signal
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                last_exc = exc
                log.warning("Invalid Gemini response (attempt %d): %s", attempt + 1, exc)
            except Exception as exc:  # noqa: BLE001 - network / quota / model not found errors
                last_exc = exc
                err_str = str(exc).lower()
                if any(k in err_str for k in ("not found", "404", "not supported", "invalid model")):
                    model_idx += 1
                    next_model = candidates[min(model_idx, len(candidates) - 1)]
                    log.warning("Model %s unavailable (%s); falling back to %s", model_name, exc, next_model)
                    continue
                log.warning("Gemini API error on %s (attempt %d): %s", model_name, attempt + 1, exc)
            if attempt < 2:
                await asyncio.sleep(3 * (attempt + 1))
        log.error("Gemini failed after retries: %s", last_exc)
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
