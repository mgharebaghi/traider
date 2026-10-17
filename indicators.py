"""
Multi-timeframe market analysis.

* 15m -> trend structure (EMA 9 / EMA 26), ATR, RSI and Support/Resistance zones.
* 1m  -> entry triggers (RSI 14 reversal/divergence, ATR 14, candle momentum).

Everything is computed locally with pandas + `ta`. The `detect_setup` method is
the cheap technical pre-filter that decides whether Gemini should be consulted.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd
import numpy as np
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
from ta.volatility import AverageTrueRange

from config import Settings
from exchange_client import ExchangeClient

log = logging.getLogger("indicators")


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #
@dataclass
class SRZone:
    price: float
    kind: str  # "support" | "resistance"
    touches: int


@dataclass
class MarketSnapshot:
    timestamp: datetime
    price: float
    bid: float
    ask: float
    df15: pd.DataFrame
    df1: pd.DataFrame
    trend15: str  # UP | DOWN | RANGE
    supports: list[SRZone]
    resistances: list[SRZone]
    divergence_1m: Optional[str]  # BULLISH | BEARISH | None

    @property
    def ema_fast_15(self) -> float:
        return float(self.df15["ema_fast"].iloc[-1])

    @property
    def ema_slow_15(self) -> float:
        return float(self.df15["ema_slow"].iloc[-1])

    @property
    def atr15(self) -> float:
        return float(self.df15["atr"].iloc[-1])

    @property
    def rsi15(self) -> float:
        return float(self.df15["rsi"].iloc[-1])

    @property
    def ema_fast_1(self) -> float:
        return float(self.df1["ema_fast"].iloc[-1])

    @property
    def ema_slow_1(self) -> float:
        return float(self.df1["ema_slow"].iloc[-1])

    @property
    def atr1(self) -> float:
        return float(self.df1["atr"].iloc[-1])

    @property
    def rsi1(self) -> float:
        return float(self.df1["rsi"].iloc[-1])

    def to_prompt_dict(self) -> dict[str, Any]:
        def candles(df: pd.DataFrame, n: int) -> list[dict[str, Any]]:
            out = []
            for _, r in df.tail(n).iterrows():
                out.append(
                    {
                        "t": r["ts"].strftime("%m-%d %H:%M"),
                        "o": round(float(r["open"]), 1),
                        "h": round(float(r["high"]), 1),
                        "l": round(float(r["low"]), 1),
                        "c": round(float(r["close"]), 1),
                        "v": round(float(r["volume"]), 2),
                        "ema9": round(float(r["ema_fast"]), 1),
                        "ema26": round(float(r["ema_slow"]), 1),
                        "rsi": round(float(r["rsi"]), 1),
                    }
                )
            return out

        return {
            "utc_time": self.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "price": {"last": self.price, "bid": self.bid, "ask": self.ask},
            "tf_15m": {
                "trend": self.trend15,
                "ema9": round(self.ema_fast_15, 2),
                "ema26": round(self.ema_slow_15, 2),
                "rsi14": round(self.rsi15, 2),
                "atr14": round(self.atr15, 2),
                "last_candles": candles(self.df15, 16),
            },
            "tf_1m": {
                "ema9": round(self.ema_fast_1, 2),
                "ema26": round(self.ema_slow_1, 2),
                "rsi14": round(self.rsi1, 2),
                "atr14": round(self.atr1, 2),
                "rsi_divergence": self.divergence_1m or "NONE",
                "last_candles": candles(self.df1, 30),
            },
            "support_zones": [{"price": round(z.price, 1), "touches": z.touches} for z in self.supports],
            "resistance_zones": [{"price": round(z.price, 1), "touches": z.touches} for z in self.resistances],
        }


@dataclass
class Setup:
    direction: str  # LONG | SHORT
    reasons: list[str] = field(default_factory=list)
    key: str = ""


# --------------------------------------------------------------------------- #
# Pure indicator helpers
# --------------------------------------------------------------------------- #
def ohlcv_to_df(raw: list[list[float]], drop_last: bool = True) -> pd.DataFrame:
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df = df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
    if drop_last and len(df) > 1:
        df = df.iloc[:-1].reset_index(drop=True)  # last candle is still forming
    return df


def add_indicators(df: pd.DataFrame, s: Settings) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = EMAIndicator(df["close"], window=s.ema_fast).ema_indicator()
    df["ema_slow"] = EMAIndicator(df["close"], window=s.ema_slow).ema_indicator()
    df["rsi"] = RSIIndicator(df["close"], window=s.rsi_period).rsi()
    df["atr"] = AverageTrueRange(df["high"], df["low"], df["close"], window=s.atr_period).average_true_range()
    df["atr"] = df["atr"].replace(0.0, float("nan")).astype(float)
    return df.dropna().reset_index(drop=True)


def pivot_indices(series: pd.Series, left: int, right: int, mode: str) -> list[int]:
    values = series.to_numpy()
    out: list[int] = []
    for i in range(left, len(values) - right):
        window = values[i - left : i + right + 1]
        if mode == "high" and values[i] == window.max():
            out.append(i)
        elif mode == "low" and values[i] == window.min():
            out.append(i)
    return out


def compute_sr_zones(
    df: pd.DataFrame, price: float, atr: float, lookback: int = 160, pivot_span: int = 3
) -> tuple[list[SRZone], list[SRZone]]:
    data = df.tail(lookback).reset_index(drop=True)
    levels: list[float] = []
    levels += [float(data["high"].iloc[i]) for i in pivot_indices(data["high"], pivot_span, pivot_span, "high")]
    levels += [float(data["low"].iloc[i]) for i in pivot_indices(data["low"], pivot_span, pivot_span, "low")]
    if not levels:
        return [], []
    levels.sort()
    tolerance = max(atr * 0.5, price * 0.0008)
    clusters: list[list[float]] = [[levels[0]]]
    for lvl in levels[1:]:
        if lvl - clusters[-1][-1] <= tolerance:
            clusters[-1].append(lvl)
        else:
            clusters.append([lvl])
    zones = [(sum(c) / len(c), len(c)) for c in clusters]
    supports = sorted(
        (SRZone(p, "support", n) for p, n in zones if p < price), key=lambda z: price - z.price
    )[:3]
    resistances = sorted(
        (SRZone(p, "resistance", n) for p, n in zones if p > price), key=lambda z: z.price - price
    )[:3]
    return supports, resistances


def detect_rsi_divergence(df: pd.DataFrame, lookback: int = 40, recent: int = 12) -> Optional[str]:
    data = df.tail(lookback).reset_index(drop=True)
    n = len(data)
    lows = pivot_indices(data["low"], 2, 2, "low")
    highs = pivot_indices(data["high"], 2, 2, "high")
    if len(lows) >= 2:
        p1, p2 = lows[-2], lows[-1]
        if p2 >= n - recent and p2 - p1 >= 3:
            if (
                data["low"].iloc[p2] < data["low"].iloc[p1]
                and data["rsi"].iloc[p2] > data["rsi"].iloc[p1] + 1.0
                and data["rsi"].iloc[p2] < 50
            ):
                return "BULLISH"
    if len(highs) >= 2:
        p1, p2 = highs[-2], highs[-1]
        if p2 >= n - recent and p2 - p1 >= 3:
            if (
                data["high"].iloc[p2] > data["high"].iloc[p1]
                and data["rsi"].iloc[p2] < data["rsi"].iloc[p1] - 1.0
                and data["rsi"].iloc[p2] > 50
            ):
                return "BEARISH"
    return None


# --------------------------------------------------------------------------- #
# Analyzer
# --------------------------------------------------------------------------- #
class MarketAnalyzer:
    def __init__(self, client: ExchangeClient, settings: Settings) -> None:
        self.client = client
        self.s = settings

    async def fetch_frame(self, timeframe: str) -> pd.DataFrame:
        raw = await self.client.fetch_ohlcv(timeframe, self.s.ohlcv_limit)
        candles = ohlcv_to_df(raw)
        if len(candles) < max(self.s.ema_slow, self.s.atr_period, self.s.rsi_period) + 30:
            raise RuntimeError(f"Not enough raw candles for {timeframe}: {len(candles)}")
        seconds = pd.to_timedelta(timeframe.replace("m", "min")).total_seconds()
        age = (pd.Timestamp.now(tz="UTC") - candles["ts"].iloc[-1]).total_seconds()
        if age > seconds * 2 + 60 or age < 0:
            raise RuntimeError(f"Stale or future candles for {timeframe}; trading skipped")
        values = candles[["open", "high", "low", "close", "volume"]]
        if not np.isfinite(values.to_numpy()).all() or (values[["open", "high", "low", "close"]] <= 0).any().any():
            raise RuntimeError("Invalid candle prices; trading skipped")
        df = add_indicators(candles, self.s)
        if len(df) < 30:
            raise RuntimeError(f"Not enough candles for {timeframe}: {len(df)}")
        return df

    async def fetch_entry_frame(self) -> pd.DataFrame:
        return await self.fetch_frame(self.s.tf_entry)

    async def snapshot(self) -> MarketSnapshot:
        df15, df1, ticker = await asyncio.gather(
            self.fetch_frame(self.s.tf_trend),
            self.fetch_frame(self.s.tf_entry),
            self.client.fetch_ticker("data"),
        )
        price = ticker["last"]
        atr15 = float(df15["atr"].iloc[-1])
        supports, resistances = compute_sr_zones(df15, price, atr15)
        return MarketSnapshot(
            timestamp=datetime.now(timezone.utc),
            price=price,
            bid=ticker["bid"],
            ask=ticker["ask"],
            df15=df15,
            df1=df1,
            trend15=self._trend(df15),
            supports=supports,
            resistances=resistances,
            divergence_1m=detect_rsi_divergence(df1),
        )

    @staticmethod
    def _trend(df15: pd.DataFrame) -> str:
        ema_f = float(df15["ema_fast"].iloc[-1])
        ema_s = float(df15["ema_slow"].iloc[-1])
        ema_s_prev = float(df15["ema_slow"].iloc[-6])
        if ema_f > ema_s and ema_s > ema_s_prev:
            return "UP"
        if ema_f < ema_s and ema_s < ema_s_prev:
            return "DOWN"
        return "RANGE"

    def detect_setup(self, snap: MarketSnapshot) -> Optional[Setup]:
        """Cheap local pre-condition. Returns a Setup only when Gemini is worth calling."""
        s = self.s
        df15, df1 = snap.df15, snap.df1
        price = snap.price
        ema26 = snap.ema_slow_15
        atr15 = snap.atr15
        atr1 = snap.atr1
        tol = s.pullback_atr_mult * atr15
        candle_key = df15["ts"].iloc[-1].strftime("%Y%m%d%H%M")

        last1 = df1.iloc[-1]
        prev1 = df1.iloc[-2]
        rsi_now = float(last1["rsi"])
        rsi_prev = float(prev1["rsi"])
        rsi_window = df1["rsi"].tail(10)
        body = abs(float(last1["close"]) - float(last1["open"]))

        if snap.trend15 == "UP":
            recent_low = min(float(df15["low"].tail(3).min()), price)
            near_ema = recent_low <= ema26 + tol and price >= ema26 - 2 * tol
            near_sup = bool(snap.supports) and abs(price - snap.supports[0].price) <= s.sr_proximity_atr * atr15
            if not (near_ema or near_sup):
                return None
            reasons = ["روند ۱۵ دقیقه صعودی (EMA9 بالای EMA26)"]
            reasons.append("پولبک قیمت به EMA26 در ۱۵ دقیقه" if near_ema else "برخورد قیمت به ناحیه حمایت ۱۵ دقیقه")
            triggers: list[str] = []
            if snap.divergence_1m == "BULLISH":
                triggers.append("واگرایی مثبت RSI در ۱ دقیقه")
            if float(rsi_window.min()) < s.rsi_oversold and rsi_now > rsi_prev and rsi_now > s.rsi_oversold:
                triggers.append("برگشت RSI از ناحیه اشباع فروش در ۱ دقیقه")
            if (
                float(last1["close"]) > float(last1["ema_fast"])
                and float(prev1["close"]) <= float(prev1["ema_fast"])
                and float(last1["close"]) > float(last1["open"])
                and body >= s.momentum_body_atr * atr1
            ):
                triggers.append("تغییر مومنتوم: کندل صعودی قوی بالای EMA9 در ۱ دقیقه")
            if triggers:
                return Setup("LONG", reasons + triggers, f"LONG:{candle_key}")
            return None

        if snap.trend15 == "DOWN":
            recent_high = max(float(df15["high"].tail(3).max()), price)
            near_ema = recent_high >= ema26 - tol and price <= ema26 + 2 * tol
            near_res = (
                bool(snap.resistances) and abs(snap.resistances[0].price - price) <= s.sr_proximity_atr * atr15
            )
            if not (near_ema or near_res):
                return None
            reasons = ["روند ۱۵ دقیقه نزولی (EMA9 زیر EMA26)"]
            reasons.append("پولبک قیمت به EMA26 در ۱۵ دقیقه" if near_ema else "برخورد قیمت به ناحیه مقاومت ۱۵ دقیقه")
            triggers = []
            if snap.divergence_1m == "BEARISH":
                triggers.append("واگرایی منفی RSI در ۱ دقیقه")
            if float(rsi_window.max()) > s.rsi_overbought and rsi_now < rsi_prev and rsi_now < s.rsi_overbought:
                triggers.append("برگشت RSI از ناحیه اشباع خرید در ۱ دقیقه")
            if (
                float(last1["close"]) < float(last1["ema_fast"])
                and float(prev1["close"]) >= float(prev1["ema_fast"])
                and float(last1["close"]) < float(last1["open"])
                and body >= s.momentum_body_atr * atr1
            ):
                triggers.append("تغییر مومنتوم: کندل نزولی قوی زیر EMA9 در ۱ دقیقه")
            if triggers:
                return Setup("SHORT", reasons + triggers, f"SHORT:{candle_key}")
            return None

        return None
