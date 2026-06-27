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

CREATE TABLE IF NOT EXISTS trade_calendar (
    trade_date TEXT PRIMARY KEY,
    is_trading INTEGER NOT NULL,
    market TEXT NOT NULL DEFAULT 'CN',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_daily_basic (
    code TEXT NOT NULL, trade_date TEXT NOT NULL,
    close REAL, turnover_rate REAL, turnover_rate_f REAL, volume_ratio REAL,
    pe REAL, pe_ttm REAL, pb REAL, ps REAL, ps_ttm REAL,
    dv_ratio REAL, dv_ttm REAL,
    total_share REAL, float_share REAL, free_share REAL,
    total_mv REAL, circ_mv REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_sdb_date ON stock_daily_basic(trade_date);

CREATE TABLE IF NOT EXISTS etf_master (
    code TEXT PRIMARY KEY,
    csname TEXT, extname TEXT, cname TEXT,
    index_code TEXT, index_name TEXT,
    setup_date TEXT, list_date TEXT, list_status TEXT,
    exchange TEXT, mgr_name TEXT, custod_name TEXT,
    mgt_fee REAL, etf_type TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_etf_master_status ON etf_master(list_status);

CREATE TABLE IF NOT EXISTS fund_daily (
    code TEXT NOT NULL, trade_date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, total_amt REAL, rise REAL, rise_rate REAL,
    nav REAL, iopv REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_fund_daily_date ON fund_daily(trade_date);

CREATE TABLE IF NOT EXISTS etf_daily (
    code TEXT NOT NULL, trade_date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, total_amt REAL, rise REAL, name TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_etf_daily_date ON etf_daily(trade_date);

CREATE TABLE IF NOT EXISTS etf_share_size (
    code TEXT NOT NULL, trade_date TEXT NOT NULL,
    name TEXT, total_share REAL, total_size REAL,
    nav REAL, close REAL, exchange TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_etf_share_size_date ON etf_share_size(trade_date);

CREATE TABLE IF NOT EXISTS index_master (
    code TEXT PRIMARY KEY,
    name TEXT,
    type TEXT,
    req_code TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_daily (
    code TEXT NOT NULL, trade_date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, pre_close REAL,
    change REAL, pct_chg REAL, volume REAL, total_amt REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_index_daily_date ON index_daily(trade_date);

CREATE TABLE IF NOT EXISTS board_master (
    code TEXT PRIMARY KEY,
    name TEXT,
    board_type TEXT,
    req_code TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_board_master_type ON board_master(board_type);

CREATE TABLE IF NOT EXISTS board_members (
    board_code TEXT NOT NULL,
    board_type TEXT,
    stock_code TEXT NOT NULL,
    stock_name TEXT,
    stock_exchange TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (board_code, stock_code)
);
CREATE INDEX IF NOT EXISTS idx_board_members_stock ON board_members(stock_code);

CREATE TABLE IF NOT EXISTS board_daily (
    board_code TEXT NOT NULL, trade_date TEXT NOT NULL,
    name TEXT, board_type TEXT,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, total_amt REAL, rise REAL, rise_rate REAL, turnover_rate REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (board_code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_board_daily_date_type ON board_daily(trade_date, board_type);

CREATE TABLE IF NOT EXISTS realtime_quote_snapshot (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    snapshot_at TEXT,
    name TEXT,
    price REAL,
    pre_close REAL,
    open REAL,
    high REAL,
    low REAL,
    volume REAL,
    total_amt REAL,
    rise REAL,
    rise_rate REAL,
    turnover_rate REAL,
    source TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, code)
);
CREATE INDEX IF NOT EXISTS idx_realtime_quote_snapshot_at ON realtime_quote_snapshot(snapshot_at);

CREATE TABLE IF NOT EXISTS fund_premium_snapshot (
    code TEXT NOT NULL, trade_date TEXT NOT NULL,
    name TEXT, type TEXT, price REAL, nav REAL, premium_rate REAL,
    amount REAL, change_pct REAL, redeem_status TEXT, subscribe_status TEXT,
    signal TEXT, iopv REAL, nav_date TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_fp_date ON fund_premium_snapshot(trade_date);

-- Static fund metadata (code/name/type). Refreshed once/day (post_close) by
-- _sync_fund_master — NOT on the 5-min market timer. LOF names have no other
-- daily home (etf_master is ETF-only); this table unifies ETF+LOF names so the
-- scan route can join a single authoritative name source.
CREATE TABLE IF NOT EXISTS fund_master (
    code TEXT PRIMARY KEY,
    name TEXT,
    type TEXT,
    updated_at TEXT NOT NULL
);

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

CREATE TABLE IF NOT EXISTS stock_capital_rank (
    trade_date TEXT NOT NULL, rank_type TEXT NOT NULL, code TEXT NOT NULL,
    name TEXT, main_net REAL, change_pct REAL,
    extra_json TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, rank_type, code)
);
CREATE INDEX IF NOT EXISTS idx_scr_date_type ON stock_capital_rank(trade_date, rank_type);

CREATE TABLE IF NOT EXISTS sector_capital_flow (
    trade_date TEXT NOT NULL, sector TEXT NOT NULL,
    main_net REAL, change_pct REAL,
    extra_json TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, sector)
);
CREATE INDEX IF NOT EXISTS idx_scf_sector_date ON sector_capital_flow(trade_date);

CREATE TABLE IF NOT EXISTS sector_snapshot (
    trade_date TEXT NOT NULL, board_type TEXT NOT NULL, name TEXT NOT NULL,
    change_pct REAL, advancers INTEGER, decliners INTEGER, leader TEXT,
    extra_json TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, board_type, name)
);
CREATE INDEX IF NOT EXISTS idx_sector_snapshot_date_type ON sector_snapshot(trade_date, board_type);

CREATE TABLE IF NOT EXISTS market_breadth_snapshot (
    trade_date TEXT PRIMARY KEY,
    total INTEGER, advancers INTEGER, decliners INTEGER, unchanged INTEGER,
    limit_up INTEGER, limit_down INTEGER, max_limit_up_height INTEGER,
    turnover_billion REAL, source TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_breadth_snapshot_date ON market_breadth_snapshot(trade_date);

CREATE TABLE IF NOT EXISTS global_market_index_daily (
    trade_date TEXT NOT NULL, symbol TEXT NOT NULL,
    name TEXT, open REAL, high REAL, low REAL, close REAL,
    prev_close REAL, change_pct REAL, currency TEXT, source TEXT,
    history_json TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_global_market_index_date ON global_market_index_daily(trade_date);

CREATE TABLE IF NOT EXISTS us_theme_snapshot (
    trade_date TEXT NOT NULL, theme_id TEXT NOT NULL,
    theme_name TEXT, proxy_symbol TEXT, proxy_name TEXT,
    close REAL, change_pct REAL, a_share_mapping_json TEXT,
    source TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, theme_id)
);
CREATE INDEX IF NOT EXISTS idx_us_theme_snapshot_date ON us_theme_snapshot(trade_date);

CREATE TABLE IF NOT EXISTS us_a_share_transmission (
    trade_date TEXT NOT NULL, theme_id TEXT NOT NULL,
    us_theme TEXT, a_share_themes_json TEXT,
    signal_strength REAL, direction TEXT, reason TEXT,
    source_data_json TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, theme_id)
);
CREATE INDEX IF NOT EXISTS idx_us_a_share_transmission_date ON us_a_share_transmission(trade_date);

CREATE TABLE IF NOT EXISTS premarket_news (
    trade_date TEXT NOT NULL, category TEXT NOT NULL, title TEXT NOT NULL,
    summary TEXT, url TEXT, source TEXT, published_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, category, title)
);
CREATE INDEX IF NOT EXISTS idx_premarket_news_date_category ON premarket_news(trade_date, category);

CREATE TABLE IF NOT EXISTS market_stage_snapshot (
    trade_date TEXT NOT NULL, stage TEXT NOT NULL,
    payload_json TEXT NOT NULL, source_tables TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, stage)
);
CREATE INDEX IF NOT EXISTS idx_market_stage_snapshot_stage_date ON market_stage_snapshot(stage, trade_date);

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
    "trade_calendar": ("trade_date",),
    "dragon_tiger": ("trade_date",),
    "stock_pool": ("trade_date",),
    "fund_daily": ("trade_date",),
    "fund_premium_snapshot": ("trade_date",),
    "board_daily": ("trade_date",),
    "realtime_quote_snapshot": ("trade_date",),
    "stock_capital_rank": ("trade_date",),
    "sector_capital_flow": ("trade_date",),
    "sector_snapshot": ("trade_date",),
    "market_breadth_snapshot": ("trade_date",),
    "global_market_index_daily": ("trade_date",),
    "us_theme_snapshot": ("trade_date",),
    "us_a_share_transmission": ("trade_date",),
    "premarket_news": ("trade_date",),
    "market_stage_snapshot": ("trade_date",),
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

    def _readonly_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=1000")
        return conn

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        """Add a column to an existing table if absent (additive migration).

        SQLite ``CREATE TABLE IF NOT EXISTS`` won't add columns to an existing
        table, and this project has no version-gated migration path. This helper
        introspects ``PRAGMA table_info`` and runs ``ALTER TABLE ... ADD COLUMN``
        when the column is missing. Idempotent; safe to call every startup.
        """
        cols = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def _init_db(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            if self._conn.execute("PRAGMA user_version").fetchone()[0] < 1:
                self._conn.execute("PRAGMA user_version=1")
            # Additive column migrations for pre-existing tables.
            self._ensure_column("fund_premium_snapshot", "nav_date", "TEXT")
            self._ensure_column("fund_premium_snapshot", "iopv", "REAL")
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

    # ------------------------------------------------------------------
    # Trading calendar
    # ------------------------------------------------------------------

    @_synchronized
    def upsert_trade_calendar(self, rows: list[dict], *, market: str = "CN") -> int:
        if not rows:
            return 0
        payload = []
        for r in rows:
            trade_date = r.get("trade_date") or r.get("date")
            if not trade_date:
                continue
            payload.append(
                (
                    trade_date,
                    1 if r.get("is_trading") else 0,
                    r.get("market") or market,
                    _now_iso(),
                )
            )
        if not payload:
            return 0
        return self._executemany_chunked(
            "INSERT OR REPLACE INTO trade_calendar "
            "(trade_date, is_trading, market, updated_at) VALUES (?, ?, ?, ?)",
            payload,
        )

    @_synchronized
    def is_calendar_trading_day(self, trade_date: str, *, market: str = "CN") -> bool | None:
        row = self._conn.execute(
            "SELECT is_trading FROM trade_calendar WHERE trade_date = ? AND market = ?",
            (trade_date, market),
        ).fetchone()
        if row is None:
            return None
        return bool(row["is_trading"])

    @_synchronized
    def trade_calendar_range(self, *, market: str = "CN") -> tuple[Optional[str], Optional[str]]:
        row = self._conn.execute(
            "SELECT MIN(trade_date) AS lo, MAX(trade_date) AS hi "
            "FROM trade_calendar WHERE market = ?",
            (market,),
        ).fetchone()
        if not row or not row["lo"]:
            return (None, None)
        return (row["lo"], row["hi"])

    # ------------------------------------------------------------------
    # Realtime quote snapshots (intraday)
    # ------------------------------------------------------------------

    @_synchronized
    def upsert_realtime_quotes(
        self, trade_date: str, rows: list[dict], *, snapshot_at: str | None = None
    ) -> int:
        if not rows:
            return 0
        payload = []
        for r in rows:
            code = str(r.get("code") or r.get("symbol") or "").upper()
            if not code:
                continue
            payload.append(
                (
                    r.get("trade_date") or trade_date,
                    code,
                    r.get("snapshot_at") or snapshot_at or _now_iso(),
                    r.get("name"),
                    _f(r.get("price") or r.get("close")),
                    _f(r.get("pre_close") or r.get("yt_close")),
                    _f(r.get("open")),
                    _f(r.get("high")),
                    _f(r.get("low")),
                    _f(r.get("volume")),
                    _f(r.get("total_amt") or r.get("amount")),
                    _f(r.get("rise") or r.get("change")),
                    _f(r.get("rise_rate") or r.get("change_pct") or r.get("pct_chg")),
                    _f(r.get("turnover_rate") or r.get("t_rate")),
                    r.get("source"),
                    _now_iso(),
                )
            )
        if not payload:
            return 0
        return self._executemany_chunked(
            "INSERT OR REPLACE INTO realtime_quote_snapshot "
            "(trade_date, code, snapshot_at, name, price, pre_close, open, high, low, "
            "volume, total_amt, rise, rise_rate, turnover_rate, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )

    @_synchronized
    def get_realtime_quotes(self, trade_date: str, limit: int = 5000) -> list[dict]:
        rows = self._conn.execute(
            "SELECT trade_date, code, snapshot_at, name, price, pre_close, open, high, low, "
            "volume, total_amt, rise, rise_rate, turnover_rate, source, updated_at "
            "FROM realtime_quote_snapshot WHERE trade_date = ? ORDER BY code LIMIT ?",
            (trade_date, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

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
            "stock_daily_basic_rows": "SELECT COUNT(*) FROM stock_daily_basic",
            "stock_daily_basic_codes": "SELECT COUNT(DISTINCT code) FROM stock_daily_basic",
            "trade_calendar_rows": "SELECT COUNT(*) FROM trade_calendar",
            "realtime_quote_rows": "SELECT COUNT(*) FROM realtime_quote_snapshot",
            "realtime_quote_codes": "SELECT COUNT(DISTINCT code) FROM realtime_quote_snapshot",
            "etf_master_rows": "SELECT COUNT(*) FROM etf_master",
            "fund_master_rows": "SELECT COUNT(*) FROM fund_master",
            "fund_daily_rows": "SELECT COUNT(*) FROM fund_daily",
            "fund_daily_codes": "SELECT COUNT(DISTINCT code) FROM fund_daily",
            "fund_premium_rows": "SELECT COUNT(*) FROM fund_premium_snapshot",
            "fund_premium_codes": "SELECT COUNT(DISTINCT code) FROM fund_premium_snapshot",
            "etf_daily_rows": "SELECT COUNT(*) FROM etf_daily",
            "etf_daily_codes": "SELECT COUNT(DISTINCT code) FROM etf_daily",
            "etf_share_size_rows": "SELECT COUNT(*) FROM etf_share_size",
            "etf_share_size_codes": "SELECT COUNT(DISTINCT code) FROM etf_share_size",
            "index_master_rows": "SELECT COUNT(*) FROM index_master",
            "index_daily_rows": "SELECT COUNT(*) FROM index_daily",
            "index_daily_codes": "SELECT COUNT(DISTINCT code) FROM index_daily",
            "board_master_rows": "SELECT COUNT(*) FROM board_master",
            "board_members_rows": "SELECT COUNT(*) FROM board_members",
            "board_daily_rows": "SELECT COUNT(*) FROM board_daily",
            "board_daily_codes": "SELECT COUNT(DISTINCT board_code) FROM board_daily",
            "dragon_tiger_rows": "SELECT COUNT(*) FROM dragon_tiger",
            "stock_capital_flow_rows": "SELECT COUNT(*) FROM stock_capital_flow",
            "stock_capital_rank_rows": "SELECT COUNT(*) FROM stock_capital_rank",
            "sector_capital_flow_rows": "SELECT COUNT(*) FROM sector_capital_flow",
            "sector_snapshot_rows": "SELECT COUNT(*) FROM sector_snapshot",
            "global_market_index_daily_rows": "SELECT COUNT(*) FROM global_market_index_daily",
            "us_theme_snapshot_rows": "SELECT COUNT(*) FROM us_theme_snapshot",
            "us_a_share_transmission_rows": "SELECT COUNT(*) FROM us_a_share_transmission",
            "premarket_news_rows": "SELECT COUNT(*) FROM premarket_news",
            "market_stage_snapshot_rows": "SELECT COUNT(*) FROM market_stage_snapshot",
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
            "trade_calendar",
            "security_master",
            "bars_daily",
            "stock_daily_basic",
            "etf_master",
            "fund_master",
            "fund_daily",
            "etf_daily",
            "etf_share_size",
            "index_master",
            "index_daily",
            "board_master",
            "board_members",
            "board_daily",
            "realtime_quote_snapshot",
            "fund_premium_snapshot",
            "dragon_tiger",
            "stock_capital_flow",
            "stock_capital_rank",
            "sector_capital_flow",
            "sector_snapshot",
            "global_market_index_daily",
            "us_theme_snapshot",
            "us_a_share_transmission",
            "premarket_news",
            "market_stage_snapshot",
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

    @_synchronized
    def security_names(self, codes: list[str]) -> dict[str, str]:
        """Return name lookup keyed by both project code and bare 6-digit code."""
        normalized: set[str] = set()
        for code in codes:
            raw = str(code or "").upper()
            if not raw:
                continue
            bare = raw.split(".", 1)[0]
            normalized.add(raw)
            if len(bare) == 6 and bare.isdigit():
                normalized.add(bare)
                if bare.startswith(("5", "6", "9")):
                    normalized.add(f"{bare}.SH")
                elif bare.startswith(("4", "8")):
                    normalized.add(f"{bare}.BJ")
                else:
                    normalized.add(f"{bare}.SZ")
        if not normalized:
            return {}
        out: dict[str, str] = {}
        values = sorted(normalized)
        for i in range(0, len(values), _BATCH):
            chunk = values[i : i + _BATCH]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"SELECT code, name FROM security_master WHERE code IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                code = str(row["code"] or "").upper()
                name = str(row["name"] or "")
                if not code or not name:
                    continue
                out[code] = name
                out[code.split(".", 1)[0]] = name
        return out

    @_synchronized
    def etf_names(self, codes: list[str]) -> dict[str, str]:
        """Return ETF name lookup keyed by both project code and bare 6-digit code."""
        normalized: set[str] = set()
        for code in codes:
            raw = str(code or "").upper()
            if not raw:
                continue
            bare = raw.split(".", 1)[0]
            normalized.add(raw)
            if len(bare) == 6 and bare.isdigit():
                normalized.add(bare)
                normalized.add(f"{bare}.SH" if bare.startswith("5") else f"{bare}.SZ")
        if not normalized:
            return {}
        out: dict[str, str] = {}
        values = sorted(normalized)
        for i in range(0, len(values), _BATCH):
            chunk = values[i : i + _BATCH]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"SELECT code, COALESCE(cname, extname, csname, code) AS name FROM etf_master WHERE code IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                code = str(row["code"] or "").upper()
                name = str(row["name"] or "")
                if not code or not name:
                    continue
                out[code] = name
                out[code.split(".", 1)[0]] = name
        return out

    # ------------------------------------------------------------------
    # ETF daily (etf_daily)
    # ------------------------------------------------------------------

    @_synchronized
    def upsert_stock_daily_basic(self, rows: list[dict]) -> int:
        """Upsert per-stock daily valuation/turnover indicators."""
        if not rows:
            return 0
        payload = []
        for r in rows:
            code = str(r.get("code") or r.get("ts_code") or "").upper()
            trade_date = r.get("date") or r.get("trade_date")
            if not code or not trade_date:
                continue
            payload.append(
                (
                    code,
                    trade_date,
                    _f(r.get("close")),
                    _f(r.get("turnover_rate")),
                    _f(r.get("turnover_rate_f")),
                    _f(r.get("volume_ratio")),
                    _f(r.get("pe")),
                    _f(r.get("pe_ttm")),
                    _f(r.get("pb")),
                    _f(r.get("ps")),
                    _f(r.get("ps_ttm")),
                    _f(r.get("dv_ratio")),
                    _f(r.get("dv_ttm")),
                    _f(r.get("total_share")),
                    _f(r.get("float_share")),
                    _f(r.get("free_share")),
                    _f(r.get("total_mv")),
                    _f(r.get("circ_mv")),
                    _now_iso(),
                )
            )
        if not payload:
            return 0
        return self._executemany_chunked(
            "INSERT OR REPLACE INTO stock_daily_basic "
            "(code, trade_date, close, turnover_rate, turnover_rate_f, volume_ratio, "
            "pe, pe_ttm, pb, ps, ps_ttm, dv_ratio, dv_ttm, total_share, float_share, "
            "free_share, total_mv, circ_mv, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )

    @_synchronized
    def upsert_etf_master(self, rows: list[dict]) -> int:
        """Upsert ETF metadata from Tushare etf_basic."""
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
                    r.get("csname"),
                    r.get("extname"),
                    r.get("cname"),
                    r.get("index_code"),
                    r.get("index_name"),
                    r.get("setup_date"),
                    r.get("list_date"),
                    r.get("list_status"),
                    r.get("exchange"),
                    r.get("mgr_name"),
                    r.get("custod_name"),
                    _f(r.get("mgt_fee")),
                    r.get("etf_type"),
                    _now_iso(),
                )
            )
        if not payload:
            return 0
        return self._executemany_chunked(
            "INSERT OR REPLACE INTO etf_master "
            "(code, csname, extname, cname, index_code, index_name, setup_date, "
            "list_date, list_status, exchange, mgr_name, custod_name, mgt_fee, etf_type, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )

    @_synchronized
    def upsert_fund_daily(self, code: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        payload = []
        for r in rows:
            trade_date = r.get("date") or r.get("trade_date")
            if not trade_date:
                continue
            payload.append(
                (
                    code,
                    trade_date,
                    _f(r.get("open")),
                    _f(r.get("high")),
                    _f(r.get("low")),
                    _f(r.get("close") or r.get("price")),
                    _f(r.get("volume")),
                    _f(r.get("total_amt") or r.get("amount")),
                    _f(r.get("rise") or r.get("change")),
                    _f(r.get("rise_rate") or r.get("change_pct") or r.get("pct_chg")),
                    _f(r.get("nav")),
                    _f(r.get("iopv")),
                    _now_iso(),
                )
            )
        if not payload:
            return 0
        return self._executemany_chunked(
            "INSERT OR REPLACE INTO fund_daily "
            "(code, trade_date, open, high, low, close, volume, total_amt, rise, rise_rate, nav, iopv, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )

    @_synchronized
    def get_fund_daily(self, code: str, *, start: str | None = None, end: str | None = None) -> list[dict]:
        clauses = ["code = ?"]
        params: list[Any] = [code]
        if start:
            clauses.append("trade_date >= ?")
            params.append(start)
        if end:
            clauses.append("trade_date <= ?")
            params.append(end)
        rows = self._conn.execute(
            "SELECT code, trade_date, open, high, low, close, volume, total_amt, rise, rise_rate, nav, iopv "
            f"FROM fund_daily WHERE {' AND '.join(clauses)} ORDER BY trade_date",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

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

    @_synchronized
    def has_etf_daily(self, code: str, trade_date: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM etf_daily WHERE code = ? AND trade_date = ? LIMIT 1",
            (code, trade_date),
        ).fetchone()
        return row is not None

    @_synchronized
    def has_index_daily(self, code: str, trade_date: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM index_daily WHERE code = ? AND trade_date = ? LIMIT 1",
            (code, trade_date),
        ).fetchone()
        return row is not None

    @_synchronized
    def count_index_daily(self, code: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM index_daily WHERE code = ?", (code,)
        ).fetchone()[0]

    # ------------------------------------------------------------------
    # Index and board master / board daily
    # ------------------------------------------------------------------

    @_synchronized
    def upsert_index_master(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        payload = []
        for r in rows:
            code = str(r.get("code") or r.get("req_code") or "").upper()
            if not code:
                continue
            payload.append(
                (
                    code,
                    r.get("name"),
                    r.get("type"),
                    r.get("req_code"),
                    _now_iso(),
                )
            )
        if not payload:
            return 0
        return self._executemany_chunked(
            "INSERT OR REPLACE INTO index_master (code, name, type, req_code, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            payload,
        )

    @_synchronized
    def list_index_master(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT code, name, type, req_code, updated_at FROM index_master ORDER BY code"
        ).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def upsert_board_master(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        payload = []
        for r in rows:
            code = str(r.get("code") or r.get("req_code") or "").strip()
            if not code:
                continue
            board_type = r.get("board_type") or r.get("type")
            req_code = r.get("req_code") or (
                f"{board_type}.{code}" if board_type and "." not in code else code
            )
            payload.append((code, r.get("name"), board_type, req_code, _now_iso()))
        if not payload:
            return 0
        return self._executemany_chunked(
            "INSERT OR REPLACE INTO board_master (code, name, board_type, req_code, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            payload,
        )

    @_synchronized
    def list_board_master(self, board_type: str | None = None) -> list[dict]:
        sql = "SELECT code, name, board_type, req_code, updated_at FROM board_master"
        params: list[Any] = []
        if board_type:
            sql += " WHERE board_type = ?"
            params.append(board_type)
        sql += " ORDER BY board_type, code"
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    @_synchronized
    def upsert_board_members(self, board_code: str, board_type: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        payload = []
        for r in rows:
            stock_code = str(r.get("code") or r.get("stock_code") or r.get("req_code") or "").upper()
            if not stock_code:
                continue
            payload.append(
                (
                    board_code,
                    board_type,
                    stock_code,
                    r.get("name") or r.get("stock_name"),
                    r.get("type") or r.get("stock_exchange"),
                    _now_iso(),
                )
            )
        if not payload:
            return 0
        with self._write_transaction():
            self._conn.execute("DELETE FROM board_members WHERE board_code = ?", (board_code,))
            for i in range(0, len(payload), _BATCH):
                self._conn.executemany(
                    "INSERT OR REPLACE INTO board_members "
                    "(board_code, board_type, stock_code, stock_name, stock_exchange, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    payload[i : i + _BATCH],
                )
        return len(payload)

    @_synchronized
    def get_board_members(self, board_code: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT board_code, board_type, stock_code, stock_name, stock_exchange "
            "FROM board_members WHERE board_code = ? ORDER BY stock_code",
            (board_code,),
        ).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def upsert_board_daily(self, board_code: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        payload = []
        for r in rows:
            trade_date = r.get("date") or r.get("trade_date")
            if not trade_date:
                continue
            payload.append(
                (
                    board_code,
                    trade_date,
                    r.get("name"),
                    r.get("board_type") or r.get("type"),
                    _f(r.get("open")),
                    _f(r.get("high")),
                    _f(r.get("low")),
                    _f(r.get("close") or r.get("price")),
                    _f(r.get("volume")),
                    _f(r.get("total_amt") or r.get("amount")),
                    _f(r.get("rise") or r.get("change")),
                    _f(r.get("rise_rate") or r.get("change_pct") or r.get("pct_chg")),
                    _f(r.get("turnover_rate") or r.get("t_rate")),
                    _now_iso(),
                )
            )
        if not payload:
            return 0
        return self._executemany_chunked(
            "INSERT OR REPLACE INTO board_daily "
            "(board_code, trade_date, name, board_type, open, high, low, close, volume, "
            "total_amt, rise, rise_rate, turnover_rate, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )

    @_synchronized
    def upsert_etf_share_size(self, rows: list[dict]) -> int:
        """Upsert ETF share/size snapshots."""
        if not rows:
            return 0
        payload = []
        for r in rows:
            code = str(r.get("code") or r.get("ts_code") or "").upper()
            trade_date = r.get("date") or r.get("trade_date")
            if not code or not trade_date:
                continue
            payload.append(
                (
                    code,
                    trade_date,
                    r.get("name"),
                    _f(r.get("total_share")),
                    _f(r.get("total_size")),
                    _f(r.get("nav")),
                    _f(r.get("close")),
                    r.get("exchange"),
                    _now_iso(),
                )
            )
        if not payload:
            return 0
        return self._executemany_chunked(
            "INSERT OR REPLACE INTO etf_share_size "
            "(code, trade_date, name, total_share, total_size, nav, close, exchange, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )

    @_synchronized
    def upsert_index_daily(self, code: str, rows: list[dict]) -> int:
        """Upsert index OHLCV rows for one index code."""
        if not rows:
            return 0
        payload = []
        for r in rows:
            row_code = str(r.get("code") or r.get("ts_code") or code).upper()
            trade_date = r.get("date") or r.get("trade_date")
            if not row_code or not trade_date:
                continue
            payload.append(
                (
                    row_code,
                    trade_date,
                    _f(r.get("open")),
                    _f(r.get("high")),
                    _f(r.get("low")),
                    _f(r.get("close")),
                    _f(r.get("pre_close")),
                    _f(r.get("change")),
                    _f(r.get("pct_chg")),
                    _f(r.get("volume")),
                    _f(r.get("total_amt")),
                    _now_iso(),
                )
            )
        if not payload:
            return 0
        return self._executemany_chunked(
            "INSERT OR REPLACE INTO index_daily "
            "(code, trade_date, open, high, low, close, pre_close, change, pct_chg, "
            "volume, total_amt, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )

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

    @_synchronized
    def upsert_stock_capital_rank(self, trade_date: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        payload = []
        for r in rows:
            code = str(r.get("code") or r.get("symbol") or "").upper()
            rank_type = str(r.get("rank_type") or "").strip()
            if not code or not rank_type:
                continue
            extra = {
                k: v for k, v in r.items()
                if k not in {"code", "symbol", "trade_date", "date", "rank_type", "name", "main_net", "change_pct"}
            }
            payload.append((
                trade_date,
                rank_type,
                code,
                r.get("name"),
                _f(r.get("main_net")),
                _f(r.get("change_pct")),
                json.dumps(extra, ensure_ascii=False) if extra else None,
                _now_iso(),
            ))
        if not payload:
            return 0
        with self._write_transaction():
            rank_types = sorted({row[1] for row in payload})
            for rank_type in rank_types:
                self._conn.execute(
                    "DELETE FROM stock_capital_rank WHERE trade_date = ? AND rank_type = ?",
                    (trade_date, rank_type),
                )
            for i in range(0, len(payload), _BATCH):
                self._conn.executemany(
                    "INSERT OR REPLACE INTO stock_capital_rank "
                    "(trade_date, rank_type, code, name, main_net, change_pct, extra_json, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    payload[i : i + _BATCH],
                )
        return len(payload)

    @_synchronized
    def get_stock_capital_rank(self, trade_date: str, rank_type: str, limit: int = 20) -> list[dict]:
        order = "ASC" if rank_type == "outflow" else "DESC"
        rows = self._conn.execute(
            "SELECT code, name, main_net, change_pct, extra_json "
            "FROM stock_capital_rank WHERE trade_date = ? AND rank_type = ? "
            f"ORDER BY main_net {order} LIMIT ?",
            (trade_date, rank_type, int(limit)),
        ).fetchall()
        out = _rows_with_extra(rows)
        for row in out:
            row["symbol"] = row.pop("code", "")
        return out

    @_synchronized
    def upsert_sector_capital(self, trade_date: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        payload = []
        for r in rows:
            sector = str(r.get("sector") or r.get("name") or "").strip()
            if not sector:
                continue
            extra = {
                k: v for k, v in r.items()
                if k not in {"sector", "name", "trade_date", "date", "main_net", "change_pct"}
            }
            payload.append((
                trade_date,
                sector,
                _f(r.get("main_net")),
                _f(r.get("change_pct")),
                json.dumps(extra, ensure_ascii=False) if extra else None,
                _now_iso(),
            ))
        if not payload:
            return 0
        with self._write_transaction():
            self._conn.execute("DELETE FROM sector_capital_flow WHERE trade_date = ?", (trade_date,))
            for i in range(0, len(payload), _BATCH):
                self._conn.executemany(
                    "INSERT OR REPLACE INTO sector_capital_flow "
                    "(trade_date, sector, main_net, change_pct, extra_json, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    payload[i : i + _BATCH],
                )
        return len(payload)

    @_synchronized
    def get_sector_capital(self, trade_date: str, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT sector, main_net, change_pct, extra_json "
            "FROM sector_capital_flow WHERE trade_date = ? "
            "ORDER BY main_net DESC LIMIT ?",
            (trade_date, int(limit)),
        ).fetchall()
        return _rows_with_extra(rows)

    @_synchronized
    def upsert_sector_snapshot(self, trade_date: str, board_type: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        payload = []
        for r in rows:
            name = str(r.get("name") or r.get("sector") or "").strip()
            if not name:
                continue
            extra = {
                k: v for k, v in r.items()
                if k not in {"trade_date", "date", "board_type", "name", "sector", "change_pct", "advancers", "decliners", "leader"}
            }
            chg = _f(r.get("change_pct"))
            payload.append((
                trade_date,
                board_type,
                name,
                round(chg, 2) if chg is not None else None,
                int(_f(r.get("advancers")) or 0),
                int(_f(r.get("decliners")) or 0),
                r.get("leader"),
                json.dumps(extra, ensure_ascii=False) if extra else None,
                _now_iso(),
            ))
        if not payload:
            return 0
        with self._write_transaction():
            self._conn.execute(
                "DELETE FROM sector_snapshot WHERE trade_date = ? AND board_type = ?",
                (trade_date, board_type),
            )
            for i in range(0, len(payload), _BATCH):
                self._conn.executemany(
                    "INSERT OR REPLACE INTO sector_snapshot "
                    "(trade_date, board_type, name, change_pct, advancers, decliners, leader, extra_json, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    payload[i : i + _BATCH],
                )
        return len(payload)

    @_synchronized
    def get_sector_snapshot(
        self, trade_date: str, board_type: str, limit: int = 40, *, order_by: str = "change_pct_desc"
    ) -> list[dict]:
        # order_by: change_pct_desc(默认,涨幅TOP) | abs(按|涨跌幅|,大涨大跌都靠前,适合热力图) | name
        order = {
            "change_pct_desc": "change_pct DESC",
            "abs": "ABS(change_pct) DESC",
            "name": "name",
        }.get(order_by, "change_pct DESC")
        rows = self._conn.execute(
            f"SELECT name, change_pct, advancers, decliners, leader, extra_json "
            f"FROM sector_snapshot WHERE trade_date = ? AND board_type = ? "
            f"ORDER BY {order} LIMIT ?",
            (trade_date, board_type, int(limit)),
        ).fetchall()
        return _rows_with_extra(rows)

    @_synchronized
    def upsert_market_breadth_snapshot(self, trade_date: str, row: dict) -> int:
        if not row:
            return 0
        with self._write_transaction():
            self._conn.execute(
                "INSERT OR REPLACE INTO market_breadth_snapshot "
                "(trade_date, total, advancers, decliners, unchanged, limit_up, limit_down, "
                "max_limit_up_height, turnover_billion, source, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trade_date,
                    int(_f(row.get("total")) or 0),
                    int(_f(row.get("advancers")) or 0),
                    int(_f(row.get("decliners")) or 0),
                    int(_f(row.get("unchanged")) or 0),
                    int(_f(row.get("limit_up")) or 0),
                    int(_f(row.get("limit_down")) or 0),
                    int(_f(row.get("max_limit_up_height")) or 0),
                    _f(row.get("turnover_billion")),
                    row.get("source"),
                    _now_iso(),
                ),
            )
        return 1

    @_synchronized
    def get_market_breadth_snapshot(self, trade_date: str) -> dict | None:
        row = self._conn.execute(
            "SELECT trade_date, total, advancers, decliners, unchanged, limit_up, limit_down, "
            "max_limit_up_height, turnover_billion, source, updated_at "
            "FROM market_breadth_snapshot WHERE trade_date = ?",
            (trade_date,),
        ).fetchone()
        return dict(row) if row else None

    @_synchronized
    def delete_market_breadth_snapshot(self, trade_date: str) -> int:
        with self._write_transaction():
            cur = self._conn.execute("DELETE FROM market_breadth_snapshot WHERE trade_date = ?", (trade_date,))
        return int(cur.rowcount or 0)

    @_synchronized
    def upsert_global_market_indices(self, trade_date: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        payload = []
        for r in rows:
            symbol = str(r.get("symbol") or "").upper().strip()
            if not symbol:
                continue
            payload.append((
                r.get("trade_date") or trade_date,
                symbol,
                r.get("name"),
                _f(r.get("open")),
                _f(r.get("high")),
                _f(r.get("low")),
                _f(r.get("close")),
                _f(r.get("prev_close")),
                _f(r.get("change_pct")),
                r.get("currency") or "USD",
                r.get("source"),
                json.dumps(r.get("history") or [], ensure_ascii=False),
                _now_iso(),
            ))
        if not payload:
            return 0
        with self._write_transaction():
            self._conn.execute("DELETE FROM global_market_index_daily WHERE trade_date = ?", (trade_date,))
            for i in range(0, len(payload), _BATCH):
                self._conn.executemany(
                    "INSERT OR REPLACE INTO global_market_index_daily "
                    "(trade_date, symbol, name, open, high, low, close, prev_close, change_pct, currency, source, history_json, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    payload[i : i + _BATCH],
                )
        return len(payload)

    @_synchronized
    def get_global_market_indices(self, trade_date: str | None = None, limit: int = 40) -> list[dict]:
        if trade_date:
            rows = self._conn.execute(
                "SELECT trade_date, symbol, name, open, high, low, close, prev_close, change_pct, currency, source, history_json "
                "FROM global_market_index_daily WHERE trade_date = ? ORDER BY symbol LIMIT ?",
                (trade_date, int(limit)),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT trade_date, symbol, name, open, high, low, close, prev_close, change_pct, currency, source, history_json "
                "FROM global_market_index_daily "
                "WHERE trade_date = (SELECT MAX(trade_date) FROM global_market_index_daily) "
                "ORDER BY symbol LIMIT ?",
                (int(limit),),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["history"] = json.loads(item.pop("history_json") or "[]")
            except (TypeError, ValueError):
                item["history"] = []
            out.append(item)
        return out

    @_synchronized
    def upsert_us_theme_snapshot(self, trade_date: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        payload = []
        for r in rows:
            theme_id = str(r.get("theme_id") or "").strip()
            if not theme_id:
                continue
            mapping = r.get("a_share_mapping") or r.get("a_share_mapping_json") or []
            payload.append((
                trade_date,
                theme_id,
                r.get("theme_name"),
                str(r.get("proxy_symbol") or "").upper(),
                r.get("proxy_name"),
                _f(r.get("close")),
                _f(r.get("change_pct")),
                json.dumps(mapping, ensure_ascii=False),
                r.get("source"),
                _now_iso(),
            ))
        if not payload:
            return 0
        with self._write_transaction():
            self._conn.execute("DELETE FROM us_theme_snapshot WHERE trade_date = ?", (trade_date,))
            for i in range(0, len(payload), _BATCH):
                self._conn.executemany(
                    "INSERT OR REPLACE INTO us_theme_snapshot "
                    "(trade_date, theme_id, theme_name, proxy_symbol, proxy_name, close, change_pct, "
                    "a_share_mapping_json, source, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    payload[i : i + _BATCH],
                )
        return len(payload)

    @_synchronized
    def get_us_theme_snapshot(self, trade_date: str, limit: int = 40) -> list[dict]:
        rows = self._conn.execute(
            "SELECT trade_date, theme_id, theme_name, proxy_symbol, proxy_name, close, change_pct, "
            "a_share_mapping_json, source FROM us_theme_snapshot WHERE trade_date = ? "
            "ORDER BY change_pct DESC LIMIT ?",
            (trade_date, int(limit)),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["a_share_mapping"] = json.loads(item.pop("a_share_mapping_json") or "[]")
            except (TypeError, ValueError):
                item["a_share_mapping"] = []
            out.append(item)
        return out

    @_synchronized
    def upsert_us_a_share_transmission(self, trade_date: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        payload = []
        for r in rows:
            theme_id = str(r.get("theme_id") or "").strip()
            if not theme_id:
                continue
            payload.append((
                trade_date,
                theme_id,
                r.get("us_theme"),
                json.dumps(r.get("a_share_themes") or [], ensure_ascii=False),
                _f(r.get("signal_strength")),
                r.get("direction"),
                r.get("reason"),
                json.dumps(r.get("source_data") or {}, ensure_ascii=False),
                _now_iso(),
            ))
        if not payload:
            return 0
        with self._write_transaction():
            self._conn.execute("DELETE FROM us_a_share_transmission WHERE trade_date = ?", (trade_date,))
            for i in range(0, len(payload), _BATCH):
                self._conn.executemany(
                    "INSERT OR REPLACE INTO us_a_share_transmission "
                    "(trade_date, theme_id, us_theme, a_share_themes_json, signal_strength, direction, reason, "
                    "source_data_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    payload[i : i + _BATCH],
                )
        return len(payload)

    @_synchronized
    def get_us_a_share_transmission(self, trade_date: str, limit: int = 30) -> list[dict]:
        rows = self._conn.execute(
            "SELECT trade_date, theme_id, us_theme, a_share_themes_json, signal_strength, direction, reason, "
            "source_data_json FROM us_a_share_transmission WHERE trade_date = ? "
            "ORDER BY ABS(signal_strength) DESC LIMIT ?",
            (trade_date, int(limit)),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["a_share_themes"] = json.loads(item.pop("a_share_themes_json") or "[]")
            except (TypeError, ValueError):
                item["a_share_themes"] = []
            try:
                item["source_data"] = json.loads(item.pop("source_data_json") or "{}")
            except (TypeError, ValueError):
                item["source_data"] = {}
            out.append(item)
        return out

    @_synchronized
    def upsert_premarket_news(self, trade_date: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        payload = []
        for r in rows:
            category = str(r.get("category") or "").strip()
            title = str(r.get("title") or "").strip()
            if not category or not title:
                continue
            payload.append((
                trade_date,
                category,
                title,
                r.get("summary") or r.get("snippet") or r.get("description"),
                r.get("url") or r.get("link"),
                r.get("source"),
                r.get("published_at") or r.get("published") or r.get("pub_date"),
                _now_iso(),
            ))
        if not payload:
            return 0
        with self._write_transaction():
            self._conn.execute("DELETE FROM premarket_news WHERE trade_date = ?", (trade_date,))
            for i in range(0, len(payload), _BATCH):
                self._conn.executemany(
                    "INSERT OR REPLACE INTO premarket_news "
                    "(trade_date, category, title, summary, url, source, published_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    payload[i : i + _BATCH],
                )
        return len(payload)

    @_synchronized
    def get_premarket_news(self, trade_date: str, limit: int = 40) -> list[dict]:
        rows = self._conn.execute(
            "SELECT trade_date, category, title, summary, url, source, published_at "
            "FROM premarket_news WHERE trade_date = ? ORDER BY category, published_at DESC LIMIT ?",
            (trade_date, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def latest_date(self, table: str) -> Optional[str]:
        lo, hi = self.date_range(table)
        return hi

    @_synchronized
    def upsert_market_stage_snapshot(
        self,
        trade_date: str,
        stage: str,
        payload: dict[str, Any],
        *,
        source_tables: list[str] | None = None,
    ) -> int:
        if not trade_date or not stage:
            return 0
        with self._write_transaction():
            self._conn.execute(
                "INSERT OR REPLACE INTO market_stage_snapshot "
                "(trade_date, stage, payload_json, source_tables, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    trade_date,
                    stage,
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(source_tables or [], ensure_ascii=False),
                    _now_iso(),
                ),
            )
        return 1

    @_synchronized
    def get_market_stage_snapshot(self, stage: str, trade_date: str | None = None) -> dict | None:
        if trade_date:
            row = self._conn.execute(
                "SELECT trade_date, stage, payload_json, source_tables, updated_at "
                "FROM market_stage_snapshot WHERE stage = ? AND trade_date = ?",
                (stage, trade_date),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT trade_date, stage, payload_json, source_tables, updated_at "
                "FROM market_stage_snapshot WHERE stage = ? ORDER BY trade_date DESC LIMIT 1",
                (stage,),
            ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            payload = {}
        try:
            source_tables = json.loads(row["source_tables"] or "[]")
        except (TypeError, ValueError):
            source_tables = []
        return {
            "trade_date": row["trade_date"],
            "stage": row["stage"],
            "payload": payload,
            "source_tables": source_tables,
            "updated_at": row["updated_at"],
        }

    def get_market_stage_snapshot_fast(self, stage: str, trade_date: str | None = None) -> dict | None:
        """Read a stage snapshot through a short-lived read-only connection.

        Stage pages are latency-sensitive and should not wait behind the shared
        sync connection's Python lock while background jobs fetch/write data.
        WAL lets this read the last committed snapshot concurrently.
        """
        try:
            with self._readonly_conn() as conn:
                if trade_date:
                    row = conn.execute(
                        "SELECT trade_date, stage, payload_json, source_tables, updated_at "
                        "FROM market_stage_snapshot WHERE stage = ? AND trade_date = ?",
                        (stage, trade_date),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT trade_date, stage, payload_json, source_tables, updated_at "
                        "FROM market_stage_snapshot WHERE stage = ? ORDER BY trade_date DESC LIMIT 1",
                        (stage,),
                    ).fetchone()
        except sqlite3.Error as exc:
            logger.debug("fast stage snapshot read failed, falling back: %s", exc)
            return self.get_market_stage_snapshot(stage, trade_date)
        if not row:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            payload = {}
        try:
            source_tables = json.loads(row["source_tables"] or "[]")
        except (TypeError, ValueError):
            source_tables = []
        return {
            "trade_date": row["trade_date"],
            "stage": row["stage"],
            "payload": payload,
            "source_tables": source_tables,
            "updated_at": row["updated_at"],
        }

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
                _f(r.get("iopv")) or None,
                r.get("nav_date") or r.get("trade_date") or "",
                _now_iso(),
            )
            for r in rows
        ]
        return self._executemany_chunked(
            "INSERT OR REPLACE INTO fund_premium_snapshot "
            "(code, trade_date, name, type, price, nav, premium_rate, amount, "
            "change_pct, redeem_status, subscribe_status, signal, iopv, nav_date, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )

    @_synchronized
    def get_fund_premium(self, trade_date: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT code, name, type, price, nav, premium_rate, amount, change_pct, "
            "redeem_status, subscribe_status, signal, iopv, nav_date, updated_at "
            "FROM fund_premium_snapshot WHERE trade_date = ?",
            (trade_date,),
        ).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def get_fund_premium_history(self, code: str, days: int = 30) -> list[dict]:
        """Recent snapshots for one code, for percentile computation.

        Returns rows (trade_date, premium_rate, amount) ordered by trade_date
        ascending. Caller decides if there's enough history to compute a
        percentile (see MIN_HISTORY_DAYS in the route).
        """
        rows = self._conn.execute(
            "SELECT trade_date, premium_rate, amount "
            "FROM fund_premium_snapshot WHERE code = ? "
            "ORDER BY trade_date DESC LIMIT ?",
            (code, int(days)),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- fund_master: daily-refreshed static metadata (name/type) ---

    @_synchronized
    def upsert_fund_master(self, rows: list[dict]) -> int:
        """Upsert static fund metadata (code/name/type). Refreshed once/day."""
        if not rows:
            return 0
        payload = [
            (r.get("code"), r.get("name"), r.get("type"), _now_iso())
            for r in rows
            if r.get("code")
        ]
        if not payload:
            return 0
        return self._executemany_chunked(
            "INSERT OR REPLACE INTO fund_master (code, name, type, updated_at) "
            "VALUES (?, ?, ?, ?)",
            payload,
        )

    @_synchronized
    def get_fund_master_names(self, codes: list[str]) -> dict[str, str]:
        """Return {code: name} for the given codes from fund_master. Skips blanks."""
        codes = [c for c in (str(c).strip() for c in codes) if c]
        if not codes:
            return {}
        out: dict[str, str] = {}
        for i in range(0, len(codes), _BATCH):
            chunk = codes[i:i + _BATCH]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"SELECT code, name FROM fund_master WHERE code IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                name = str(row["name"] or "").strip()
                if name:
                    out[str(row["code"])] = name
        return out

    @_synchronized
    def fund_master_updated_at(self) -> str | None:
        """Last time fund_master was refreshed (for the UI's 'basic info updated at')."""
        row = self._conn.execute("SELECT MAX(updated_at) FROM fund_master").fetchone()
        return row[0] if row else None

    @_synchronized
    def fund_snapshot_codes(self, *, fund_type: str | None = None) -> list[str]:
        sql = "SELECT DISTINCT code FROM fund_premium_snapshot"
        params: list[Any] = []
        if fund_type:
            sql += " WHERE UPPER(type) = ?"
            params.append(fund_type.upper())
        sql += " ORDER BY code"
        rows = self._conn.execute(sql, params).fetchall()
        return [r["code"] for r in rows if r["code"]]

    @_synchronized
    def missing_etf_daily_codes(self, trade_date: str, *, limit: int | None = None) -> list[str]:
        sql = (
            "SELECT DISTINCT f.code FROM fund_premium_snapshot f "
            "LEFT JOIN etf_daily e ON e.code = f.code AND e.trade_date = ? "
            "WHERE UPPER(f.type) = 'ETF' AND e.code IS NULL "
            "ORDER BY f.code"
        )
        params: list[Any] = [trade_date]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [r["code"] for r in rows if r["code"]]

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
        for t in (
            "trade_calendar",
            "security_master",
            "bars_daily",
            "stock_daily_basic",
            "etf_master",
            "fund_master",
            "fund_daily",
            "etf_daily",
            "etf_share_size",
            "index_master",
            "index_daily",
            "board_master",
            "board_members",
            "board_daily",
            "realtime_quote_snapshot",
            "fund_premium_snapshot",
            "dragon_tiger",
            "stock_capital_flow",
            "stock_capital_rank",
            "sector_capital_flow",
            "sector_snapshot",
            "global_market_index_daily",
            "us_theme_snapshot",
            "us_a_share_transmission",
            "premarket_news",
            "market_stage_snapshot",
            "stock_pool",
        ):
            row = self._conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()
            out[t] = int(row["c"]) if row else 0
        return out

    @_synchronized
    def date_range(self, table: str) -> tuple[Optional[str], Optional[str]]:
        if table not in {
            "trade_calendar",
            "security_master",
            "bars_daily",
            "stock_daily_basic",
            "etf_master",
            "fund_master",
            "fund_daily",
            "etf_daily",
            "etf_share_size",
            "index_master",
            "index_daily",
            "board_master",
            "board_members",
            "board_daily",
            "realtime_quote_snapshot",
            "fund_premium_snapshot",
            "dragon_tiger",
            "stock_capital_flow",
            "stock_capital_rank",
            "sector_capital_flow",
            "sector_snapshot",
            "global_market_index_daily",
            "us_theme_snapshot",
            "us_a_share_transmission",
            "premarket_news",
            "market_stage_snapshot",
            "stock_pool",
        }:
            raise ValueError(f"unknown table: {table}")
        if table in {"security_master", "etf_master"}:
            date_col = "list_date"
            if table == "etf_master":
                date_col = "list_date"
            row = self._conn.execute(
                f"SELECT MIN({date_col}) AS lo, MAX({date_col}) AS hi FROM {table}"
            ).fetchone()
            if not row or not row["lo"]:
                return (None, None)
            return (row["lo"], row["hi"])
        if table in {"fund_master", "index_master", "board_master", "board_members"}:
            row = self._conn.execute(
                f"SELECT MIN(updated_at) AS lo, MAX(updated_at) AS hi FROM {table}"
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
