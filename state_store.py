"""
Persistent state store backed by SQLite.

Stores:
  * key/value JSON blobs (open position, daily stats, bot mode, virtual balance,
    AI usage counters, halt state ...)
  * a `trades` table with the full history of closed trades.

SQLite with WAL journal is used so the state survives container restarts when
`DATA_DIR` is mounted on a persistent Docker volume.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS kv ("
                " key TEXT PRIMARY KEY,"
                " value TEXT NOT NULL,"
                " updated_at TEXT NOT NULL)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS trades ("
                " id TEXT PRIMARY KEY,"
                " mode TEXT NOT NULL,"
                " symbol TEXT NOT NULL,"
                " side TEXT NOT NULL,"
                " entry_price REAL,"
                " exit_price REAL,"
                " contracts REAL,"
                " leverage INTEGER,"
                " net_pnl REAL,"
                " fees REAL,"
                " close_reason TEXT,"
                " opened_at TEXT,"
                " closed_at TEXT,"
                " payload TEXT)"
            )

    # ------------------------------------------------------------------ kv
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def set(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False, default=str)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv(key, value, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, payload, now),
            )

    def delete(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM kv WHERE key = ?", (key,))

    # -------------------------------------------------------------- trades
    def record_trade(self, trade: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO trades(id, mode, symbol, side, entry_price, exit_price, contracts,"
                " leverage, net_pnl, fees, close_reason, opened_at, closed_at, payload)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trade["id"],
                    trade["mode"],
                    trade["symbol"],
                    trade["side"],
                    trade.get("fill_price"),
                    trade.get("exit_price_avg"),
                    trade.get("contracts"),
                    trade.get("leverage"),
                    trade.get("net_pnl"),
                    trade.get("fees_paid"),
                    trade.get("close_reason"),
                    trade.get("filled_at") or trade.get("created_at"),
                    trade.get("closed_at"),
                    json.dumps(trade, ensure_ascii=False, default=str),
                ),
            )

    def recent_trades(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trades ORDER BY closed_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def summary(self, mode: str | None = None) -> dict[str, Any]:
        query = (
            "SELECT COUNT(*) AS n, COALESCE(SUM(net_pnl), 0) AS pnl,"
            " COALESCE(SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END), 0) AS wins"
            " FROM trades"
        )
        params: tuple = ()
        if mode:
            query += " WHERE mode = ?"
            params = (mode,)
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        n = int(row["n"])
        wins = int(row["wins"])
        return {
            "trades": n,
            "wins": wins,
            "losses": n - wins,
            "net_pnl": float(row["pnl"]),
            "win_rate": (wins / n * 100.0) if n else 0.0,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
