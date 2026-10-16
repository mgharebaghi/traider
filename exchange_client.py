"""
Async exchange wrapper around `ccxt.async_support`.

* `trade_ex`  -> exchange used for orders/balance (EXCHANGE_ID, e.g. lbank).
* `data_ex`   -> exchange used for OHLCV/ticker (MARKET_DATA_EXCHANGE_ID or the
                 same instance as `trade_ex`).

All exchange-specific quirks (Binance futures default type, Bybit margin-mode
params, unsupported stop orders ...) are isolated here so the rest of the code
works with a small, unified API.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import ccxt.async_support as ccxt

from config import Settings

log = logging.getLogger("exchange")


def _build_exchange(exchange_id: str, settings: Settings, with_keys: bool) -> ccxt.Exchange:
    if not hasattr(ccxt, exchange_id):
        raise ValueError(f"Exchange '{exchange_id}' is not supported by ccxt")
    cls = getattr(ccxt, exchange_id)

    options: dict[str, Any] = {"defaultMarginMode": "isolated"}
    if exchange_id == "binance":
        options["defaultType"] = "future"
    else:
        options["defaultType"] = "swap"

    cfg: dict[str, Any] = {
        "enableRateLimit": True,
        "timeout": 20000,
        "options": options,
    }
    if with_keys and settings.has_exchange_keys:
        cfg["apiKey"] = settings.api_key
        cfg["secret"] = settings.api_secret
        if settings.api_password:
            cfg["password"] = settings.api_password

    proxy = settings.exchange_proxy
    if proxy:
        if proxy.lower().startswith("socks"):
            cfg["socksProxy"] = proxy
        else:
            cfg["httpsProxy"] = proxy

    exchange = cls(cfg)
    if settings.use_testnet:
        exchange.set_sandbox_mode(True)  # Never silently fall back to production.
    return exchange


class ExchangeClient:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.symbol = settings.symbol
        self.trade_ex = _build_exchange(settings.exchange_id, settings, with_keys=True)
        data_id = settings.market_data_exchange_id or settings.exchange_id
        if data_id == settings.exchange_id:
            self.data_ex = self.trade_ex
        else:
            self.data_ex = _build_exchange(data_id, settings, with_keys=False)
        self.market: dict[str, Any] = {}

    # ------------------------------------------------------------------ init
    async def initialize(self) -> None:
        await self.trade_ex.load_markets()
        if self.symbol not in self.trade_ex.markets:
            raise ValueError(
                f"Symbol {self.symbol} not found on {self.trade_ex.id}. "
                f"Check SYMBOL / EXCHANGE_ID in .env"
            )
        self.market = self.trade_ex.market(self.symbol)
        if not self.market.get("swap") or not self.market.get("linear") or self.market.get("settle") != "USDT":
            raise ValueError("Only linear USDT-settled perpetual markets are supported")
        if self.data_ex.id == "lbank":
            log.info(
                "Using LBank kline endpoint (%s) for candle analysis alongside LBank swap ticker (%s).",
                self.symbol.split(":")[0],
                self.symbol,
            )
        if not self.s.dry_run:
            self.validate_live_support()
        if self.data_ex is not self.trade_ex:
            await self.data_ex.load_markets()
            if self.symbol not in self.data_ex.markets:
                raise ValueError(f"Symbol {self.symbol} not found on data exchange {self.data_ex.id}")
        log.info(
            "Exchange ready: trade=%s data=%s symbol=%s contractSize=%s minAmount=%s",
            self.trade_ex.id,
            self.data_ex.id,
            self.symbol,
            self.contract_size,
            self.min_amount,
        )

    def validate_live_support(self) -> None:
        # CCXT's LBank adapter currently routes create_order to spot endpoints.
        if self.trade_ex.id == "lbank":
            raise RuntimeError("LBank futures execution is not implemented in this adapter; see README_LBANK_FA.md")
        required = ("createOrder", "fetchOrder", "cancelOrder", "fetchBalance", "setLeverage", "setMarginMode")
        missing = [name for name in required if not self.trade_ex.has.get(name)]
        if not (self.trade_ex.has.get("fetchPositions") or self.trade_ex.has.get("fetchPosition")):
            missing.append("fetchPositions/fetchPosition")
        if missing:
            raise RuntimeError("Missing live exchange capabilities: " + ", ".join(missing))

    def _require_live(self) -> None:
        self.validate_live_support()

    async def close(self) -> None:
        await self.trade_ex.close()
        if self.data_ex is not self.trade_ex:
            await self.data_ex.close()

    # ------------------------------------------------------------ metadata
    @property
    def contract_size(self) -> float:
        value = self.market.get("contractSize")
        return float(value) if value else 1.0

    @property
    def min_amount(self) -> float:
        value = (self.market.get("limits") or {}).get("amount", {}).get("min")
        return float(value) if value else 0.0

    @property
    def min_cost(self) -> float:
        value = (self.market.get("limits") or {}).get("cost", {}).get("min")
        return float(value) if value else 0.0

    def amount_to_precision(self, amount: float) -> float:
        if amount <= 0:
            return 0.0
        try:
            return float(self.trade_ex.amount_to_precision(self.symbol, amount))
        except Exception:  # noqa: BLE001 - amount below precision raises
            return 0.0

    def price_to_precision(self, price: float) -> float:
        try:
            return float(self.trade_ex.price_to_precision(self.symbol, price))
        except Exception:  # noqa: BLE001
            return float(price)

    # ----------------------------------------------------------- market data
    async def fetch_ohlcv(self, timeframe: str, limit: int) -> list[list[float]]:
        if self.data_ex.id == "lbank" and ":" in self.symbol:
            return await self.data_ex.fetch_ohlcv(self.symbol.split(":")[0], timeframe=timeframe, limit=limit)
        try:
            return await self.data_ex.fetch_ohlcv(self.symbol, timeframe=timeframe, limit=limit)
        except Exception:
            if ":" in self.symbol:
                return await self.data_ex.fetch_ohlcv(self.symbol.split(":")[0], timeframe=timeframe, limit=limit)
            raise

    async def fetch_ticker(self, source: str = "data") -> dict[str, float]:
        ex = self.data_ex if source == "data" else self.trade_ex
        t = await ex.fetch_ticker(self.symbol)
        last = float(t.get("last") or t.get("close") or 0.0)
        bid = float(t.get("bid") or last)
        ask = float(t.get("ask") or last)
        if last <= 0:
            raise RuntimeError("Ticker returned no price")
        return {"last": last, "bid": bid, "ask": ask}

    # --------------------------------------------------------------- account
    async def fetch_free_usdt(self) -> float:
        last_exc: Optional[Exception] = None
        for params in ({"type": "swap"}, {"type": "future"}, {}):
            try:
                bal = await self.trade_ex.fetch_balance(params)
                usdt = bal.get("USDT") or {}
                free = usdt.get("free")
                if free is None:
                    free = (bal.get("free") or {}).get("USDT")
                if free is not None:
                    return float(free)
                if isinstance(bal, dict) and ("free" in bal or "info" in bal):
                    return 0.0
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        raise RuntimeError(f"Unable to fetch USDT balance: {last_exc}")

    async def fetch_spot_usdt(self) -> Optional[float]:
        """Checks Spot wallet USDT balance so we can tell the user if their funds are in Spot instead of Futures."""
        try:
            bal = await self.trade_ex.fetch_balance({"type": "spot"})
            usdt = bal.get("USDT") or {}
            free = usdt.get("free")
            if free is None:
                free = (bal.get("free") or {}).get("USDT")
            if free is not None:
                return float(free)
            if isinstance(bal, dict) and ("free" in bal or "info" in bal):
                return 0.0
            return None
        except Exception:  # noqa: BLE001
            return None

    async def set_isolated_and_leverage(self, leverage: int) -> list[str]:
        """Sets ISOLATED margin + leverage. Returns a list of non-fatal warnings."""
        warnings: list[str] = []
        ex = self.trade_ex
        if ex.has.get("fetchPositionMode"):
            mode = await ex.fetch_position_mode(self.symbol)
            if mode.get("hedged"):
                raise RuntimeError("Use one-way/net position mode; hedge mode is not supported")
        if ex.has.get("setMarginMode"):
            try:
                await ex.set_margin_mode("isolated", self.symbol, {"leverage": leverage})
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).lower()
                if not any(k in msg for k in ("not modified", "no need", "already", "same")):
                    raise RuntimeError(f"Cannot confirm isolated margin: {exc}") from exc
        else:
            raise RuntimeError("Exchange cannot set isolated margin")

        if ex.has.get("setLeverage"):
            try:
                await ex.set_leverage(leverage, self.symbol, {"marginMode": "isolated"})
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).lower()
                if not any(k in msg for k in ("not modified", "no need", "same")):
                    raise RuntimeError(f"set_leverage failed: {exc}") from exc
        else:
            raise RuntimeError("Exchange cannot set leverage")
        return warnings

    # ---------------------------------------------------------------- orders
    async def create_limit(self, side: str, amount: float, price: float, post_only: bool) -> dict[str, Any]:
        self._require_live()
        amount = self.amount_to_precision(amount)
        price = self.price_to_precision(price)
        if post_only:
            return await self.trade_ex.create_order(
                self.symbol, "limit", side, amount, price, {"postOnly": True, "marginMode": "isolated"}
            )
        return await self.trade_ex.create_order(self.symbol, "limit", side, amount, price, {"marginMode": "isolated"})

    async def create_market(self, side: str, amount: float, reduce_only: bool) -> dict[str, Any]:
        self._require_live()
        amount = self.amount_to_precision(amount)
        params = {"marginMode": "isolated"}
        if reduce_only:
            params["reduceOnly"] = True
        return await self.trade_ex.create_order(self.symbol, "market", side, amount, None, params)

    async def create_stop_market(self, side: str, amount: float, stop_price: float) -> Optional[str]:
        """
        Places an exchange-native reduce-only stop-market order.
        Uses only the unified ccxt helpers that raise NotSupported when the
        exchange lacks the feature (never risks sending a plain market order).
        Returns the order id or None if not supported/failed.
        """
        ex = self.trade_ex
        self._require_live()
        amount = self.amount_to_precision(amount)
        price = self.price_to_precision(stop_price)
        if amount <= 0:
            return None
        params = {"reduceOnly": True, "marginMode": "isolated"}
        attempts = []
        if ex.has.get("createStopLossOrder"):
            attempts.append(
                lambda: ex.create_stop_loss_order(self.symbol, "market", side, amount, None, price, dict(params))
            )
        if ex.has.get("createTriggerOrder"):
            attempts.append(
                lambda: ex.create_trigger_order(self.symbol, "market", side, amount, None, price, dict(params))
            )
        if ex.has.get("createStopMarketOrder"):
            attempts.append(lambda: ex.create_stop_market_order(self.symbol, side, amount, price, dict(params)))
        for attempt in attempts:
            try:
                order = await attempt()
                if order.get("status") == "closed" and float(order.get("filled") or 0) > 0:
                    log.error("Stop order executed immediately (price already beyond stop): %s", order.get("id"))
                return str(order["id"])
            except Exception as exc:  # noqa: BLE001
                log.warning("Native stop attempt failed: %s", exc)
        return None

    async def cancel_order(self, order_id: str) -> None:
        if not order_id:
            return
        try:
            await self.trade_ex.cancel_order(order_id, self.symbol)
        except Exception as exc:  # noqa: BLE001
            # conditional orders sometimes need a flag to be cancelled
            try:
                await self.trade_ex.cancel_order(order_id, self.symbol, {"stop": True, "trigger": True})
            except Exception as retry_exc:  # noqa: BLE001
                raise RuntimeError(f"Cancellation of {order_id} is unconfirmed: {exc}") from retry_exc

    async def cancel_all(self) -> None:
        ex = self.trade_ex
        if ex.has.get("cancelAllOrders"):
            for params in ({}, {"stop": True, "trigger": True}):
                try:
                    await ex.cancel_all_orders(self.symbol, params)
                except Exception as exc:  # noqa: BLE001
                    log.warning("cancel_all_orders(%s) failed: %s", params, exc)
        else:
            try:
                for order in await ex.fetch_open_orders(self.symbol):
                    await self.cancel_order(str(order["id"]))
            except Exception as exc:  # noqa: BLE001
                log.warning("Manual cancel-all failed: %s", exc)

    async def fetch_order(self, order_id: str) -> dict[str, Any]:
        try:
            return await self.trade_ex.fetch_order(order_id, self.symbol)
        except Exception:  # noqa: BLE001
            return await self.trade_ex.fetch_order(order_id, self.symbol, {"stop": True, "trigger": True})

    async def fetch_position_contracts(self) -> float:
        """Absolute open contracts for the configured symbol (0 if flat)."""
        ex = self.trade_ex
        positions: list[dict[str, Any]] = []
        if ex.has.get("fetchPositions"):
            positions = await ex.fetch_positions([self.symbol])
        elif ex.has.get("fetchPosition"):
            positions = [await ex.fetch_position(self.symbol)]
        else:
            raise RuntimeError(f"{ex.id} does not support fetching positions")
        total = 0.0
        for p in positions:
            if not p or p.get("symbol") != self.symbol:
                continue
            total += abs(float(p.get("contracts") or 0.0))
        return total

    async def close_all_positions(self) -> int:
        """Market-closes every open position on the symbol (used by /panic). Returns count closed."""
        ex = self.trade_ex
        if not ex.has.get("fetchPositions"):
            return 0
        closed = 0
        for p in await ex.fetch_positions([self.symbol]):
            contracts = abs(float(p.get("contracts") or 0.0))
            if not p or p.get("symbol") != self.symbol or contracts <= 0:
                continue
            close_side = "sell" if str(p.get("side", "")).lower() == "long" else "buy"
            order = await self.create_market(close_side, contracts, reduce_only=True)
            order = await self.fetch_order(str(order["id"]))
            if order.get("status") != "closed" or float(order.get("filled") or 0) < contracts:
                raise RuntimeError("Emergency close submitted but fill is unconfirmed; inspect the exchange")
            closed += 1
        return closed

    @staticmethod
    def order_avg_price(order: dict[str, Any], fallback: float) -> float:
        for key in ("average", "price", "triggerPrice", "stopPrice"):
            value = order.get(key)
            if value:
                return float(value)
        return float(fallback)

    @staticmethod
    def order_fee(order: dict[str, Any]) -> Optional[float]:
        fee = order.get("fee") or {}
        cost = fee.get("cost")
        currency = str(fee.get("currency") or "USDT").upper()
        if cost is None or currency not in ("USDT", "USD"):
            return None
        return abs(float(cost))
