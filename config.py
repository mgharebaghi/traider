"""
Central configuration for the Crypto Futures Scalping Agent.

All values are read from environment variables (or a local `.env` file).
Every parameter has a safe default so the bot can boot in DRY_RUN mode with
only the Telegram + Gemini keys provided.
"""
from __future__ import annotations

import os
import math
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")


# --------------------------------------------------------------------------- #
# Env parsing helpers
# --------------------------------------------------------------------------- #
def _str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip().strip('"').strip("'")
    return value if value else default


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    value = value.strip().lower()
    if value in ("1", "true", "yes", "on", "y"):
        return True
    if value in ("0", "false", "no", "off", "n"):
        return False
    raise ValueError(f"Environment variable {name} must be true or false")


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return float(value.strip())
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be a number, got {value!r}") from exc


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer, got {value!r}") from exc


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Settings:
    # ---------------- Exchange ----------------
    exchange_id: str
    market_data_exchange_id: str
    api_key: str
    api_secret: str
    api_password: str
    symbol: str
    exchange_proxy: str
    use_testnet: bool

    # ---------------- Timeframes ----------------
    tf_trend: str
    tf_entry: str
    ohlcv_limit: int

    # ---------------- Gemini ----------------
    gemini_api_key: str
    gemini_model: str
    ai_min_confidence: int
    ai_max_calls_per_day: int
    ai_min_interval_sec: int
    ai_temperature: float

    # ---------------- Telegram ----------------
    telegram_token: str
    telegram_admin_chat_id: int
    telegram_proxy: str

    # ---------------- Mode / capital ----------------
    dry_run: bool
    starting_balance: float
    min_balance_to_trade: float

    # ---------------- Risk ----------------
    risk_per_trade_pct: float
    daily_profit_target_usd: float
    daily_profit_target_pct: float
    daily_max_loss_usd: float
    daily_max_loss_pct: float
    max_consecutive_losses: int
    circuit_breaker_hours: float
    min_leverage: int
    max_leverage: int
    max_margin_usage: float
    maintenance_margin_rate: float
    liquidation_buffer: float
    min_rr: float
    tp1_rr: float
    tp1_close_fraction: float
    min_sl_pct: float
    max_sl_pct: float
    maker_fee: float
    taker_fee: float
    slippage_pct: float
    breakeven_buffer_pct: float

    # ---------------- Execution ----------------
    entry_post_only: bool
    entry_timeout_min: float
    max_entry_deviation_pct: float

    # ---------------- Trailing stop ----------------
    trailing_method: str
    trailing_atr_mult: float
    trailing_tight_atr_mult: float
    trailing_ema_buffer_atr: float
    trail_min_step_pct: float
    close_at_tp2: bool

    # ---------------- Indicators / setup ----------------
    ema_fast: int
    ema_slow: int
    rsi_period: int
    atr_period: int
    pullback_atr_mult: float
    sr_proximity_atr: float
    rsi_oversold: float
    rsi_overbought: float
    momentum_body_atr: float

    # ---------------- Loops ----------------
    scan_interval_sec: int
    monitor_interval_sec: float
    trail_refresh_sec: int
    live_sync_sec: int

    # ---------------- Misc ----------------
    data_dir: Path
    log_level: str
    notify_ai_wait: bool
    display_timezone: str

    # ---------------- Derived helpers ----------------
    @property
    def has_exchange_keys(self) -> bool:
        return bool(self.api_key and self.api_secret)

    @property
    def effective_breakeven_buffer(self) -> float:
        """Price offset (fraction) added to entry when moving SL to break-even."""
        if self.breakeven_buffer_pct > 0:
            return self.breakeven_buffer_pct
        entry_fee = self.maker_fee if self.entry_post_only else self.taker_fee
        return entry_fee + self.taker_fee + 0.0001


def load_settings() -> Settings:
    data_dir = Path(_str("DATA_DIR", str(Path(__file__).resolve().parent / "data")))
    data_dir.mkdir(parents=True, exist_ok=True)

    admin_raw = _str("TELEGRAM_ADMIN_CHAT_ID", "0")
    try:
        admin_id = int(admin_raw)
    except ValueError as exc:
        raise ValueError("TELEGRAM_ADMIN_CHAT_ID must be a numeric chat id") from exc

    settings = Settings(
        exchange_id=_str("EXCHANGE_ID", "lbank").lower(),
        market_data_exchange_id=_str("MARKET_DATA_EXCHANGE_ID", "").lower(),
        api_key=_str("EXCHANGE_API_KEY"),
        api_secret=_str("EXCHANGE_API_SECRET"),
        api_password=_str("EXCHANGE_API_PASSWORD"),
        symbol=_str("SYMBOL", "BTC/USDT:USDT"),
        exchange_proxy=_str("EXCHANGE_PROXY_URL"),
        use_testnet=_bool("USE_TESTNET", False),
        tf_trend=_str("TF_TREND", "15m"),
        tf_entry=_str("TF_ENTRY", "1m"),
        ohlcv_limit=_int("OHLCV_LIMIT", 300),
        gemini_api_key=_str("GEMINI_API_KEY"),
        gemini_model=_str("GEMINI_MODEL", "gemini-3.8-flash"),
        ai_min_confidence=_int("AI_MIN_CONFIDENCE", 70),
        ai_max_calls_per_day=_int("AI_MAX_CALLS_PER_DAY", 45),
        ai_min_interval_sec=_int("AI_MIN_INTERVAL_SEC", 900),
        ai_temperature=_float("AI_TEMPERATURE", 0.2),
        telegram_token=_str("TELEGRAM_BOT_TOKEN"),
        telegram_admin_chat_id=admin_id,
        telegram_proxy=_str("TELEGRAM_PROXY_URL"),
        dry_run=_bool("DRY_RUN", True),
        starting_balance=_float("STARTING_BALANCE", 10.0),
        min_balance_to_trade=_float("MIN_BALANCE_TO_TRADE", 3.0),
        risk_per_trade_pct=_float("RISK_PER_TRADE_PCT", 0.04),
        daily_profit_target_usd=_float("DAILY_PROFIT_TARGET_USD", 1.0),
        daily_profit_target_pct=_float("DAILY_PROFIT_TARGET_PCT", 0.10),
        daily_max_loss_usd=_float("DAILY_MAX_LOSS_USD", 0.80),
        daily_max_loss_pct=_float("DAILY_MAX_LOSS_PCT", 0.08),
        max_consecutive_losses=_int("MAX_CONSECUTIVE_LOSSES", 2),
        circuit_breaker_hours=_float("CIRCUIT_BREAKER_HOURS", 24),
        min_leverage=_int("MIN_LEVERAGE", 10),
        max_leverage=_int("MAX_LEVERAGE", 35),
        max_margin_usage=_float("MAX_MARGIN_USAGE", 0.90),
        maintenance_margin_rate=_float("MAINTENANCE_MARGIN_RATE", 0.005),
        liquidation_buffer=_float("LIQUIDATION_BUFFER", 1.5),
        min_rr=_float("MIN_RR", 2.0),
        tp1_rr=_float("TP1_RR", 1.5),
        tp1_close_fraction=_float("TP1_CLOSE_FRACTION", 0.5),
        min_sl_pct=_float("MIN_SL_PCT", 0.0012),
        max_sl_pct=_float("MAX_SL_PCT", 0.012),
        maker_fee=_float("MAKER_FEE", 0.0002),
        taker_fee=_float("TAKER_FEE", 0.0006),
        slippage_pct=_float("SLIPPAGE_PCT", 0.0002),
        breakeven_buffer_pct=_float("BREAKEVEN_BUFFER_PCT", 0.0),
        entry_post_only=_bool("ENTRY_POST_ONLY", True),
        entry_timeout_min=_float("ENTRY_TIMEOUT_MIN", 5),
        max_entry_deviation_pct=_float("MAX_ENTRY_DEVIATION_PCT", 0.003),
        trailing_method=_str("TRAILING_METHOD", "hybrid").lower(),
        trailing_atr_mult=_float("TRAILING_ATR_MULT", 1.5),
        trailing_tight_atr_mult=_float("TRAILING_TIGHT_ATR_MULT", 0.8),
        trailing_ema_buffer_atr=_float("TRAILING_EMA_BUFFER_ATR", 0.25),
        trail_min_step_pct=_float("TRAIL_MIN_STEP_PCT", 0.0005),
        close_at_tp2=_bool("CLOSE_AT_TP2", False),
        ema_fast=_int("EMA_FAST", 9),
        ema_slow=_int("EMA_SLOW", 26),
        rsi_period=_int("RSI_PERIOD", 14),
        atr_period=_int("ATR_PERIOD", 14),
        pullback_atr_mult=_float("PULLBACK_ATR_MULT", 0.35),
        sr_proximity_atr=_float("SR_PROXIMITY_ATR", 0.4),
        rsi_oversold=_float("RSI_OVERSOLD", 35),
        rsi_overbought=_float("RSI_OVERBOUGHT", 65),
        momentum_body_atr=_float("MOMENTUM_BODY_ATR", 0.4),
        scan_interval_sec=_int("SCAN_INTERVAL_SEC", 20),
        monitor_interval_sec=_float("MONITOR_INTERVAL_SEC", 3),
        trail_refresh_sec=_int("TRAIL_REFRESH_SEC", 20),
        live_sync_sec=_int("LIVE_SYNC_SEC", 10),
        data_dir=data_dir,
        log_level=_str("LOG_LEVEL", "INFO").upper(),
        notify_ai_wait=_bool("NOTIFY_AI_WAIT", True),
        display_timezone=_str("DISPLAY_TIMEZONE", "Asia/Tehran"),
    )
    _validate(settings)
    return settings


def _validate(s: Settings) -> None:
    errors: list[str] = []
    for name, value in vars(s).items():
        if isinstance(value, float) and not math.isfinite(value):
            errors.append(f"{name} must be finite")
    positive = (
        "starting_balance", "min_balance_to_trade", "max_consecutive_losses", "circuit_breaker_hours",
        "scan_interval_sec", "monitor_interval_sec", "trail_refresh_sec", "live_sync_sec",
        "entry_timeout_min", "ai_max_calls_per_day", "ema_fast", "ema_slow", "rsi_period", "atr_period",
        "min_rr", "tp1_rr", "trailing_atr_mult", "trailing_tight_atr_mult", "liquidation_buffer",
    )
    for name in positive:
        if getattr(s, name) <= 0:
            errors.append(f"{name} must be positive")
    if not 0 < s.max_margin_usage < 1:
        errors.append("MAX_MARGIN_USAGE must be in (0, 1)")
    if not 0 < s.min_sl_pct <= s.max_sl_pct < 1:
        errors.append("MIN_SL_PCT/MAX_SL_PCT must satisfy 0 < min <= max < 1")
    if s.ohlcv_limit < max(s.ema_slow, s.ema_fast, s.rsi_period, s.atr_period) + 40:
        errors.append("OHLCV_LIMIT must leave at least 40 candles after indicator warm-up")
    for name in ("maker_fee", "taker_fee", "slippage_pct", "breakeven_buffer_pct", "maintenance_margin_rate",
                 "max_entry_deviation_pct", "trail_min_step_pct", "ai_min_interval_sec",
                 "daily_profit_target_pct", "daily_max_loss_pct", "daily_profit_target_usd", "daily_max_loss_usd"):
        if getattr(s, name) < 0:
            errors.append(f"{name} cannot be negative")
    if not 0 <= s.ai_min_confidence <= 100:
        errors.append("AI_MIN_CONFIDENCE must be in [0, 100]")
    if s.daily_profit_target_pct == 0 and s.daily_profit_target_usd <= 0:
        errors.append("A positive daily profit target is required")
    if s.daily_max_loss_pct == 0 and s.daily_max_loss_usd <= 0:
        errors.append("A positive daily loss limit is required")
    if s.ema_fast >= s.ema_slow:
        errors.append("EMA_FAST must be smaller than EMA_SLOW")
    if not 0 < s.rsi_oversold < s.rsi_overbought < 100:
        errors.append("RSI thresholds must satisfy 0 < oversold < overbought < 100")
    if s.telegram_admin_chat_id < 0:
        errors.append("TELEGRAM_ADMIN_CHAT_ID must be a private user chat id (positive)")
    if not s.telegram_token:
        errors.append("TELEGRAM_BOT_TOKEN is required")
    if s.telegram_admin_chat_id == 0:
        errors.append("TELEGRAM_ADMIN_CHAT_ID is required")
    if not s.gemini_api_key:
        errors.append("GEMINI_API_KEY is required")
    if not 0 < s.risk_per_trade_pct <= 0.2:
        errors.append("RISK_PER_TRADE_PCT must be in (0, 0.2]")
    if s.min_leverage < 1 or s.max_leverage < s.min_leverage:
        errors.append("MIN_LEVERAGE/MAX_LEVERAGE are inconsistent")
    if not 0 < s.tp1_close_fraction < 1:
        errors.append("TP1_CLOSE_FRACTION must be between 0 and 1")
    if s.tp1_rr >= s.min_rr:
        errors.append("TP1_RR must be smaller than MIN_RR (TP2 R/R)")
    if s.trailing_method not in ("atr", "ema", "hybrid"):
        errors.append("TRAILING_METHOD must be one of: atr, ema, hybrid")
    if not s.dry_run and not s.has_exchange_keys:
        errors.append("DRY_RUN=False requires EXCHANGE_API_KEY and EXCHANGE_API_SECRET")
    if not s.dry_run and s.exchange_id == "okx" and not s.api_password:
        errors.append("OKX live/demo execution requires EXCHANGE_API_PASSWORD (API passphrase)")
    if errors:
        raise ValueError("Invalid configuration:\n - " + "\n - ".join(errors))
