"""SQLite-backed market-data store (``~/.vibe-trading/market.db``).

Persists A-share daily K-lines, ETF daily, fund-premium close snapshots,
dragon-tiger lists, stock capital flow, and stock pools so historical lookups
hit the local DB instead of re-paying tpdog credits on every request. Acts as
the *primary* read source for OHLCV after the first backfill: callers read DB
first, fall back to the live mootdx/tpdog/akshare chain when DB is cold, and
persist what they fetched.

Style mirrors :mod:`src.goal.store`: WAL + ``busy_timeout`` + a single
serialized connection guarded by an RLock, ``@_synchronized`` on public
methods, ``_write_transaction()`` for cross-statement writes, ``INSERT OR
REPLACE`` upserts, ``PRAGMA user_version`` for migrations.

The store is intentionally independent of tpdog_client — it only knows rows
and dates. Fetching/normalizing lives in :mod:`src.data.market_sync`.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

import pandas as pd

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path.home() / ".vibe-trading" / "market.db"
_DB_PATH_ENV = "VIBE_TRADING_MARKET_DB_PATH"
_BATCH = 500  # rows per executemany transaction

F = TypeVar("F", bound=Callable[..., Any])


def _synchronized(method: F) -> F:
    """Serialize access to the shared SQLite connection (GoalStore pattern)."""

    @wraps(method)
    def wrapper(self: "MarketStore", *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


def _default_db_path() -> Path:
    """Default DB path, overridable via ``VIBE_TRADING_MARKET_DB_PATH``."""
    env = os.getenv(_DB_PATH_ENV, "").strip()
    return Path(env) if env else _DEFAULT_DB_PATH


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars_daily (
    code TEXT NOT NULL, trade_date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, total_amt REAL, rise_rate REAL, t_rate REAL,
    name TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_bars_daily_date ON bars_daily(trade_date);

CREATE TABLE IF NOT EXISTS security_master (
    code TEXT PRIMARY KEY,
    symbol TEXT,
    name TEXT,
    area TEXT,
    industry TEXT,
    market TEXT,
    exchange TEXT,
    list_status TEXT,
    list_date TEXT,
    delist_date TEXT,
    is_hs TEXT,
    is_st INTEGER NOT NULL DEFAULT 0,
    is_delisting INTEGER NOT NULL DEFAULT 0,
    is_bj INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_security_master_status ON security_master(list_status);
CREATE INDEX IF NOT EXISTS idx_security_master_flags ON security_master(is_active, is_st, is_delisting, is_bj);

CREATE TABLE IF NOT EXISTS etf_daily (
    code TEXT NOT NULL, trade_date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, total_amt REAL, rise REAL, name TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_etf_daily_date ON etf_daily(trade_date);

CREATE TABLE IF NOT EXISTS fund_premium_snapshot (
    code TEXT NOT NULL, trade_date TEXT NOT NULL,
    name TEXT, type TEXT, price REAL, nav REAL, premium_rate REAL,
    amount REAL, change_pct REAL, redeem_status TEXT, subscribe_status TEXT,
    signal TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_fp_date ON fund_premium_snapshot(trade_date);

CREATE TABLE IF NOT EXISTS dragon_tiger (
    code TEXT NOT NULL, trade_date TEXT NOT NULL,
    name TEXT, close REAL, rise_rate REAL, net_amt REAL, buy_amt REAL, sell_amt REAL,
    extra_json TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_dt_date ON dragon_tiger(trade_date);

CREATE TABLE IF NOT EXISTS stock_capital_flow (
    code TEXT NOT NULL, trade_date TEXT NOT NULL, period INTEGER NOT NULL,
    m_in REAL, m_out REAL, m_net REAL, r_in REAL, r_out REAL, r_net REAL,
    extra_json TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (code, trade_date, period)
);
CREATE INDEX IF NOT EXISTS idx_scf_code_date ON stock_capital_flow(code, trade_date);

CREATE TABLE IF NOT EXISTS stock_pool (
    pool_type TEXT NOT NULL, trade_date TEXT NOT NULL, code TEXT NOT NULL,
    extra_json TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (pool_type, trade_date, code)
);
CREATE INDEX IF NOT EXISTS idx_pool_date ON stock_pool(trade_date);

CREATE TABLE IF NOT EXISTS sync_meta (
    key TEXT PRIMARY KEY, value TEXT, updated_at TEXT NOT NULL
);
"""

# Tables that carry a per-(date) market-wide snapshot.
_DATE_KEYED_TABLES = {
    "dragon_tiger": ("trade_date",),
    "stock_pool": ("trade_date",),
    "fund_premium_snapshot": ("trade_date",),
}


class MarketStore:
    """Thread-safe SQLite store for market data tables."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            if self._conn.execute("PRAGMA user_version").fetchone()[0] < 1:
                self._conn.execute("PRAGMA user_version=1")
            self._conn.commit()

    @contextmanager
    def _write_transaction(self):
        """Open an immediate write transaction (GoalStore pattern)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    # ------------------------------------------------------------------
    # Generic helpers
    # ------------------------------------------------------------------

    def _executemany_chunked(self, sql: str, rows: list[tuple]) -> int:
        """Run executemany in ≤_BATCH-row transactions; return rows written."""
        written = 0
        with self._write_transaction():
            for i in range(0, len(rows), _BATCH):
                chunk = rows[i : i + _BATCH]
                self._conn.executemany(sql, chunk)
                written += len(chunk)
        return written

    @staticmethod
    def _rows_to_ohlcv_df(rows: list[sqlite3.Row]) -> Optional[pd.DataFrame]:
        """Build the canonical OHLCV DataFrame (index=date, 5 fixed cols).

        Mirrors the canonical OHLCV DataFrame shape so
        alpha_signals / position_routes / opportunity_routes see no difference.
        """
        if not rows:
            return None
        df = pd.DataFrame(
            [
                {
                    "date": r["trade_date"],
                    "open": r["open"],
                    "high": r["high"],
                    "low": r["low"],
                    "close": r["close"],
                    "volume": r["volume"],
                }
                for r in rows
            ]
        )
        df["date"] = pd.to_datetime(df["date"])
        return df.set_index("date").sort_index()

    # ------------------------------------------------------------------
    # Daily K-lines (bars_daily)
    # ------------------------------------------------------------------

    @_synchronized
    def upsert_daily_bars(self, code: str, rows: list[dict]) -> int:
        """Upsert daily-K rows for one code. Each row needs a ``date`` key."""
        if not rows:
            return 0
        payload = []
        for r in rows:
            payload.append(
                (
                    code,
                    r.get("date") or r.get("trade_date"),
                    _f(r.get("open")),
                    _f(r.get("high")),
                    _f(r.get("low")),
                    _f(r.get("close")),
                    _f(r.get("volume")),
                    _f(r.get("total_amt")),
                    _f(r.get("rise_rate")),
                    _f(r.get("t_rate")),
                    r.get("name"),
                    _now_iso(),
                )
            )
        return self._executemany_chunked(
            "INSERT OR REPLACE INTO bars_daily "
            "(code, trade_date, open, high, low, close, volume, total_amt, "
            "rise_rate, t_rate, name, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )

    @_synchronized
    def get_daily_bars(
        self,
        code: str,
        *,
        days: int | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> Optional[pd.DataFrame]:
        """Return OHLCV for a code, optionally clipped by days / [start, end].

        Returns ``None`` when no rows match. Result columns are fixed to
        ``[open, high, low, close, volume]`` with a datetime ``date`` index.
        """
        clauses = ["code = ?"]
        params: list[Any] = [code]
        if start:
            clauses.append("trade_date >= ?")
            params.append(start)
        if end:
            clauses.append("trade_date <= ?")
            params.append(end)
        order = "trade_date ASC" if (start or end or days) else "trade_date DESC"
        sql = (
            f"SELECT trade_date, open, high, low, close, volume FROM bars_daily "
            f"WHERE {' AND '.join(clauses)} ORDER BY {order}"
        )
        if days is not None and not (start or end):
            sql += f" LIMIT {int(days)}"
        rows = self._conn.execute(sql, params).fetchall()
        if not rows:
            return None
        df = self._rows_to_ohlcv_df(rows if (start or end or days) else list(reversed(rows)))
        if df is None:
            return None
        if days is not None:
            df = df.tail(days)
        return df

    @_synchronized
    def last_daily_date(self, code: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT MAX(trade_date) AS d FROM bars_daily WHERE code = ?", (code,)
        ).fetchone()
        return row["d"] if row and row["d"] else None

    @_synchronized
    def codes_with_data(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT code FROM bars_daily ORDER BY code"
        ).fetchall()
        return [r["code"] for r in rows]

    # ------------------------------------------------------------------
    # Security master / universes
    # ------------------------------------------------------------------

    @_synchronized
    def upsert_security_master(self, rows: list[dict]) -> int:
        """Upsert normalized A-share metadata rows."""
        if not rows:
            return 0
        payload = []
        for r in rows:
            code = str(r.get("code") or r.get("ts_code") or "").upper()
            if not code:
                continue
            payload.append(
                (
                    code,
                    r.get("symbol"),
                    r.get("name"),
                    r.get("area"),
                    r.get("industry"),
                    r.get("market"),
                    r.get("exchange"),
                    r.get("list_status"),
                    r.get("list_date"),
                    r.get("delist_date"),
                    r.get("is_hs"),
                    1 if r.get("is_st") else 0,
                    1 if r.get("is_delisting") else 0,
                    1 if r.get("is_bj") else 0,
                    1 if r.get("is_active", True) else 0,
                    _now_iso(),
                )
            )
        if not payload:
            return 0
        return self._executemany_chunked(
            "INSERT OR REPLACE INTO security_master "
            "(code, symbol, name, area, industry, market, exchange, list_status, "
            "list_date, delist_date, is_hs, is_st, is_delisting, is_bj, is_active, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )

    @_synchronized
    def security_master_count(self, *, default_only: bool = False) -> int:
        sql = "SELECT COUNT(*) AS c FROM security_master"
        if default_only:
            sql += " WHERE is_active = 1 AND is_st = 0 AND is_delisting = 0 AND is_bj = 0"
        row = self._conn.execute(sql).fetchone()
        return int(row["c"]) if row else 0

    @_synchronized
    def list_security_master(self, *, default_only: bool = False) -> list[dict]:
        sql = (
            "SELECT code, symbol, name, area, industry, market, exchange, "
            "list_status, list_date, delist_date, is_hs, is_st, is_delisting, is_bj, is_active "
            "FROM security_master"
        )
        if default_only:
            sql += " WHERE is_active = 1 AND is_st = 0 AND is_delisting = 0 AND is_bj = 0"
        sql += " ORDER BY code"
        return [dict(r) for r in self._conn.execute(sql).fetchall()]

    @_synchronized
    def default_strategy_codes(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT code FROM security_master "
            "WHERE is_active = 1 AND is_st = 0 AND is_delisting = 0 AND is_bj = 0 "
            "ORDER BY code"
        ).fetchall()
        return [r["code"] for r in rows]

    @_synchronized
    def market_coverage(self) -> dict[str, Any]:
        """Return high-level local-data coverage for operator/status views."""
        scalar_sql = {
            "security_total": "SELECT COUNT(*) FROM security_master",
            "security_active": "SELECT COUNT(*) FROM security_master WHERE is_active = 1",
            "security_default": (
                "SELECT COUNT(*) FROM security_master "
                "WHERE is_active = 1 AND is_st = 0 AND is_delisting = 0 AND is_bj = 0"
            ),
            "security_st_active": (
                "SELECT COUNT(*) FROM security_master WHERE is_active = 1 AND is_st = 1"
            ),
            "security_bj_active": (
                "SELECT COUNT(*) FROM security_master WHERE is_active = 1 AND is_bj = 1"
            ),
            "security_delisting": "SELECT COUNT(*) FROM security_master WHERE is_delisting = 1",
            "daily_rows": "SELECT COUNT(*) FROM bars_daily",
            "daily_codes": "SELECT COUNT(DISTINCT code) FROM bars_daily",
            "daily_default_codes": (
                "SELECT COUNT(DISTINCT b.code) FROM bars_daily b "
                "JOIN security_master s ON s.code = b.code "
                "WHERE s.is_active = 1 AND s.is_st = 0 AND s.is_delisting = 0 AND s.is_bj = 0"
            ),
            "fund_premium_rows": "SELECT COUNT(*) FROM fund_premium_snapshot",
            "fund_premium_codes": "SELECT COUNT(DISTINCT code) FROM fund_premium_snapshot",
            "etf_daily_rows": "SELECT COUNT(*) FROM etf_daily",
            "etf_daily_codes": "SELECT COUNT(DISTINCT code) FROM etf_daily",
            "dragon_tiger_rows": "SELECT COUNT(*) FROM dragon_tiger",
            "stock_capital_flow_rows": "SELECT COUNT(*) FROM stock_capital_flow",
            "stock_pool_rows": "SELECT COUNT(*) FROM stock_pool",
        }
        out: dict[str, Any] = {}
        for key, sql in scalar_sql.items():
            row = self._conn.execute(sql).fetchone()
            out[key] = int(row[0]) if row and row[0] is not None else 0
        out["daily_default_missing_codes"] = max(
            0, out["security_default"] - out["daily_default_codes"]
        )
        ranges: dict[str, list[str | None]] = {}
        for table in (
            "security_master",
            "bars_daily",
            "etf_daily",
            "fund_premium_snapshot",
            "dragon_tiger",
            "stock_capital_flow",
            "stock_pool",
        ):
            ranges[table] = list(self.date_range(table))
        out["date_ranges"] = ranges
        return out

    @_synchronized
    def missing_daily_codes(self, *, default_only: bool = True, limit: int = 100) -> list[str]:
        """Return strategy/universe codes that have no local daily bars yet."""
        where = ""
        if default_only:
            where = "WHERE s.is_active = 1 AND s.is_st = 0 AND s.is_delisting = 0 AND s.is_bj = 0"
        rows = self._conn.execute(
            "SELECT s.code FROM security_master s "
            "LEFT JOIN bars_daily b ON b.code = s.code "
            f"{where} "
            "GROUP BY s.code "
            "HAVING COUNT(b.trade_date) = 0 "
            "ORDER BY s.code "
            "LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [r["code"] for r in rows]

    # ------------------------------------------------------------------
    # ETF daily (etf_daily)
    # ------------------------------------------------------------------

    @_synchronized
    def upsert_etf_daily(self, code: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        payload = [
            (
                code,
                r.get("date") or r.get("trade_date"),
                _f(r.get("open")),
                _f(r.get("high")),
                _f(r.get("low")),
                _f(r.get("close")),
                _f(r.get("volume")),
                _f(r.get("total_amt")),
                _f(r.get("rise")),
                r.get("name"),
                _now_iso(),
            )
            for r in rows
        ]
        return self._executemany_chunked(
            "INSERT OR REPLACE INTO etf_daily "
            "(code, trade_date, open, high, low, close, volume, total_amt, rise, name, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )

    @_synchronized
    def get_etf_daily(
        self, code: str, *, start: str | None = None, end: str | None = None
    ) -> Optional[pd.DataFrame]:
        clauses = ["code = ?"]
        params: list[Any] = [code]
        if start:
            clauses.append("trade_date >= ?")
            params.append(start)
        if end:
            clauses.append("trade_date <= ?")
            params.append(end)
        rows = self._conn.execute(
            f"SELECT trade_date AS date, open, high, low, close, volume, total_amt, rise "
            f"FROM etf_daily WHERE {' AND '.join(clauses)} ORDER BY trade_date",
            params,
        ).fetchall()
        if not rows:
            return None
        df = pd.DataFrame([dict(r) for r in rows])
        df["date"] = pd.to_datetime(df["date"])
        return df.set_index("date").sort_index()

    # ------------------------------------------------------------------
    # Dragon-tiger list
    # ------------------------------------------------------------------

    @_synchronized
    def upsert_dragon_tiger(self, trade_date: str, rows: list[dict]) -> int:
        return _upsert_market_wide(
            self,
            "dragon_tiger",
            trade_date,
            rows,
            pk_cols=("code", "trade_date"),
            value_cols=("name", "close", "rise_rate", "net_amt", "buy_amt", "sell_amt"),
        )

    @_synchronized
    def get_dragon_tiger(self, trade_date: str) -> list[dict]:
        return _get_market_wide(self, "dragon_tiger", trade_date)

    @_synchronized
    def has_dragon_tiger(self, trade_date: str) -> bool:
        return _has_market_wide(self, "dragon_tiger", trade_date)

    # ------------------------------------------------------------------
    # Stock capital flow
    # ------------------------------------------------------------------

    @_synchronized
    def upsert_stock_capital(
        self, code: str, trade_date: str, period: int, rows: list[dict]
    ) -> int:
        if not rows:
            return 0
        payload = []
        for r in rows:
            extra = {k: v for k, v in r.items()
                     if k not in {"code", "date", "trade_date", "period",
                                  "m_in", "m_out", "m_net", "r_in", "r_out", "r_net", "name"}}
            payload.append(
                (
                    code,
                    r.get("date") or trade_date,
                    int(period),
                    _f(r.get("m_in")),
                    _f(r.get("m_out")),
                    _f(r.get("m_net")),
                    _f(r.get("r_in")),
                    _f(r.get("r_out")),
                    _f(r.get("r_net")),
                    json.dumps(extra, ensure_ascii=False) if extra else None,
                    _now_iso(),
                )
            )
        return self._executemany_chunked(
            "INSERT OR REPLACE INTO stock_capital_flow "
            "(code, trade_date, period, m_in, m_out, m_net, r_in, r_out, r_net, extra_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )

    @_synchronized
    def get_stock_capital(
        self, code: str, *, start: str, end: str, period: int = 1
    ) -> list[dict]:
        rows = self._conn.execute(
            "SELECT trade_date, m_in, m_out, m_net, r_in, r_out, r_net, extra_json "
            "FROM stock_capital_flow WHERE code = ? AND period = ? "
            "AND trade_date >= ? AND trade_date <= ? ORDER BY trade_date",
            (code, int(period), start, end),
        ).fetchall()
        return _rows_with_extra(rows)

    # ------------------------------------------------------------------
    # Stock pool (limit-up / limit-down / strong / fire / new)
    # ------------------------------------------------------------------

    @_synchronized
    def upsert_pool(self, pool_type: str, trade_date: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        # Delete existing codes for this (pool_type, date) then insert — a pool
        # membership changes intraday until close, so a full replace per date is
        # the correct semantics (not individual-row upsert).
        payload = []
        for r in rows:
            extra = {k: v for k, v in r.items() if k not in {"code", "date", "trade_date"}}
            payload.append(
                (pool_type, r.get("date") or trade_date, r.get("code"),
                 json.dumps(extra, ensure_ascii=False) if extra else None, _now_iso())
            )
        with self._write_transaction():
            self._conn.execute(
                "DELETE FROM stock_pool WHERE pool_type = ? AND trade_date = ?",
                (pool_type, trade_date),
            )
            for i in range(0, len(payload), _BATCH):
                self._conn.executemany(
                    "INSERT OR REPLACE INTO stock_pool "
                    "(pool_type, trade_date, code, extra_json, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    payload[i : i + _BATCH],
                )
        return len(payload)

    @_synchronized
    def get_pool(self, pool_type: str, trade_date: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT code, extra_json FROM stock_pool "
            "WHERE pool_type = ? AND trade_date = ?",
            (pool_type, trade_date),
        ).fetchall()
        return _rows_with_extra(rows, extra_key="extra_json", base_cols=("code",))

    @_synchronized
    def has_pool(self, pool_type: str, trade_date: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM stock_pool WHERE pool_type = ? AND trade_date = ? LIMIT 1",
            (pool_type, trade_date),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Fund premium close snapshot
    # ------------------------------------------------------------------

    @_synchronized
    def upsert_fund_premium(self, trade_date: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        payload = [
            (
                r.get("code"),
                r.get("trade_date") or trade_date,
                r.get("name"),
                r.get("type"),
                _f(r.get("price")),
                _f(r.get("nav")),
                _f(r.get("premium_rate")),
                _f(r.get("amount")),
                _f(r.get("change_pct")),
                r.get("redeem_status"),
                r.get("subscribe_status"),
                r.get("signal"),
                _now_iso(),
            )
            for r in rows
        ]
        return self._executemany_chunked(
            "INSERT OR REPLACE INTO fund_premium_snapshot "
            "(code, trade_date, name, type, price, nav, premium_rate, amount, "
            "change_pct, redeem_status, subscribe_status, signal, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )

    @_synchronized
    def get_fund_premium(self, trade_date: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT code, name, type, price, nav, premium_rate, amount, change_pct, "
            "redeem_status, subscribe_status, signal "
            "FROM fund_premium_snapshot WHERE trade_date = ?",
            (trade_date,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # sync_meta
    # ------------------------------------------------------------------

    @_synchronized
    def get_meta(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM sync_meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    @_synchronized
    def set_meta(self, key: str, value: str) -> None:
        with self._write_transaction():
            self._conn.execute(
                "INSERT OR REPLACE INTO sync_meta (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, _now_iso()),
            )

    # ------------------------------------------------------------------
    # Stats for status API
    # ------------------------------------------------------------------

    @_synchronized
    def table_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in ("security_master", "bars_daily", "etf_daily", "fund_premium_snapshot",
                  "dragon_tiger", "stock_capital_flow", "stock_pool"):
            row = self._conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()
            out[t] = int(row["c"]) if row else 0
        return out

    @_synchronized
    def date_range(self, table: str) -> tuple[Optional[str], Optional[str]]:
        if table not in {"security_master", "bars_daily", "etf_daily", "fund_premium_snapshot",
                         "dragon_tiger", "stock_capital_flow", "stock_pool"}:
            raise ValueError(f"unknown table: {table}")
        if table == "security_master":
            row = self._conn.execute(
                "SELECT MIN(list_date) AS lo, MAX(list_date) AS hi FROM security_master"
            ).fetchone()
            if not row or not row["lo"]:
                return (None, None)
            return (row["lo"], row["hi"])
        row = self._conn.execute(
            f"SELECT MIN(trade_date) AS lo, MAX(trade_date) AS hi FROM {table}"
        ).fetchone()
        if not row or not row["lo"]:
            return (None, None)
        return (row["lo"], row["hi"])


# ----------------------------------------------------------------------
# Module-level helpers (free functions used by the store + sync layer)
# ----------------------------------------------------------------------


def _f(v: Any) -> Optional[float]:
    """Best-effort float coercion; None on failure."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rows_with_extra(
    rows: list[sqlite3.Row],
    *,
    extra_key: str = "extra_json",
    base_cols: tuple[str, ...] = (),
) -> list[dict]:
    """Expand rows: base columns + decoded extra_json keys."""
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        extra_raw = d.pop(extra_key, None)
        if extra_raw:
            try:
                d.update(json.loads(extra_raw))
            except (TypeError, ValueError):
                pass
        out.append(d)
    return out


def _upsert_market_wide(
    store: "MarketStore",
    table: str,
    trade_date: str,
    rows: list[dict],
    *,
    pk_cols: tuple[str, ...],
    value_cols: tuple[str, ...],
) -> int:
    """Upsert a market-wide (per-date) table: delete-then-insert for the date.

    ``code`` (when in pk_cols) comes from the row; ``trade_date`` is always the
    passed argument (rows may omit it). Text cols (name/code) are passed
    through; numeric value_cols are coerced via :func:`_f`.
    """
    if not rows:
        return 0
    cols = pk_cols + value_cols
    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(cols)
    text_cols = {"code", "trade_date", "name", "type", "signal",
                 "redeem_status", "subscribe_status"}
    payload = []
    for r in rows:
        vals = []
        for c in cols:
            if c == "trade_date":
                vals.append(r.get("trade_date") or trade_date)
            elif c in text_cols:
                vals.append(r.get(c))
            else:
                vals.append(_f(r.get(c)))
        vals.append(_now_iso())
        payload.append(tuple(vals))
    with store._write_transaction():
        store._conn.execute(
            f"DELETE FROM {table} WHERE trade_date = ?", (trade_date,)
        )
        for i in range(0, len(payload), _BATCH):
            store._conn.executemany(
                f"INSERT OR REPLACE INTO {table} ({col_list}, updated_at) "
                f"VALUES ({placeholders}, ?)",
                payload[i : i + _BATCH],
            )
    return len(payload)


def _get_market_wide(store: "MarketStore", table: str, trade_date: str) -> list[dict]:
    rows = store._conn.execute(
        f"SELECT * FROM {table} WHERE trade_date = ?", (trade_date,)
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        extra_raw = d.pop("extra_json", None)
        if extra_raw:
            try:
                d.update(json.loads(extra_raw))
            except (TypeError, ValueError):
                pass
        out.append(d)
    return out


def _has_market_wide(store: "MarketStore", table: str, trade_date: str) -> bool:
    row = store._conn.execute(
        f"SELECT 1 FROM {table} WHERE trade_date = ? LIMIT 1", (trade_date,)
    ).fetchone()
    return row is not None


# ----------------------------------------------------------------------
# Module-level singleton accessor (read-path safe)
# ----------------------------------------------------------------------

_store_singleton: Optional[MarketStore] = None
_store_lock = threading.Lock()


def get_market_store() -> Optional[MarketStore]:
    """Return the process-wide MarketStore singleton, or None on any failure.

    Callers in read paths (market_data_service, routes) MUST tolerate ``None`` and fall
    back to the live data chain — a DB init failure must never break reads.
    """
    global _store_singleton
    if _store_singleton is not None:
        return _store_singleton
    with _store_lock:
        if _store_singleton is None:
            try:
                _store_singleton = MarketStore()
            except Exception:
                logger.debug("MarketStore init failed; reads will bypass DB", exc_info=True)
                return None
    return _store_singleton


def db_read_enabled() -> bool:
    """True when the DB-read feature flag is on (default on)."""
    return os.getenv("VIBE_TRADING_MARKET_DB_READ", "1").strip() not in ("0", "false", "False")
