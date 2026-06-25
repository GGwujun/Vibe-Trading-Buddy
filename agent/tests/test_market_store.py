"""Tests for the market-data SQLite store."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.data.market_store import MarketStore


@pytest.fixture
def store(tmp_path: Path) -> MarketStore:
    s = MarketStore(tmp_path / "market.db")
    yield s
    s._conn.close()


def _row(date: str, close: float = 1.0) -> dict:
    return {"date": date, "open": 1, "high": 1, "low": 1, "close": close,
            "volume": 1, "total_amt": 1, "rise_rate": 0.5, "t_rate": 0.1, "name": "X"}


def test_init_is_idempotent(store: MarketStore) -> None:
    # Second init must not raise (CREATE TABLE IF NOT EXISTS).
    store._init_db()
    store._init_db()


def test_upsert_and_get_daily(store: MarketStore) -> None:
    n = store.upsert_daily_bars("600206.SH", [_row("2026-06-10"), _row("2026-06-11")])
    assert n == 2
    df = store.get_daily_bars("600206.SH", days=10)
    assert df is not None
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.name == "date"
    assert len(df) == 2


def test_upsert_replaces_on_pk_conflict(store: MarketStore) -> None:
    store.upsert_daily_bars("600206.SH", [_row("2026-06-10", close=1.0)])
    store.upsert_daily_bars("600206.SH", [_row("2026-06-10", close=99.99)])
    df = store.get_daily_bars("600206.SH", start="2026-06-10", end="2026-06-10")
    assert df is not None and df["close"].iloc[0] == 99.99


def test_get_daily_returns_none_when_empty(store: MarketStore) -> None:
    assert store.get_daily_bars("000000.SH", days=5) is None


def test_last_daily_date(store: MarketStore) -> None:
    store.upsert_daily_bars("600206.SH", [_row("2026-06-10"), _row("2026-06-11")])
    assert store.last_daily_date("600206.SH") == "2026-06-11"
    assert store.last_daily_date("000000.SH") is None


def test_security_master_default_universe_filters_risky_names(store: MarketStore) -> None:
    n = store.upsert_security_master(
        [
            {"code": "000001.SZ", "symbol": "000001", "name": "平安银行", "list_status": "L", "list_date": "19910403"},
            {"code": "000002.SZ", "symbol": "000002", "name": "ST测试", "list_status": "L", "is_st": True},
            {"code": "000003.SZ", "symbol": "000003", "name": "退市测试", "list_status": "D", "is_delisting": True, "is_active": False},
            {"code": "430001.BJ", "symbol": "430001", "name": "北交测试", "list_status": "L", "is_bj": True},
        ]
    )
    assert n == 4
    assert store.security_master_count() == 4
    assert store.security_master_count(default_only=True) == 1
    assert store.default_strategy_codes() == ["000001.SZ"]
    lo, hi = store.date_range("security_master")
    assert lo == "19910403" and hi == "19910403"


def test_dragon_tiger_get_has(store: MarketStore) -> None:
    assert store.has_dragon_tiger("2026-06-11") is False
    store.upsert_dragon_tiger("2026-06-11", [{"code": "600206", "name": "X", "close": 10}])
    assert store.has_dragon_tiger("2026-06-11") is True
    rows = store.get_dragon_tiger("2026-06-11")
    assert len(rows) == 1 and rows[0]["code"] == "600206"


def test_pool_replace_semantics(store: MarketStore) -> None:
    # Second upsert for the same (pool_type, date) must fully replace, not append.
    store.upsert_pool("limitup", "2026-06-11", [{"code": "600206"}])
    store.upsert_pool("limitup", "2026-06-11", [{"code": "000001"}, {"code": "300750"}])
    codes = {p["code"] for p in store.get_pool("limitup", "2026-06-11")}
    assert codes == {"000001", "300750"}


def test_meta_round_trip(store: MarketStore) -> None:
    assert store.get_meta("k") is None
    store.set_meta("daemon:2026-06-11", "ts")
    assert store.get_meta("daemon:2026-06-11") == "ts"


def test_table_counts_and_range(store: MarketStore) -> None:
    store.upsert_daily_bars("600206.SH", [_row("2026-06-10"), _row("2026-06-11")])
    store.upsert_security_master([{"code": "600206.SH", "name": "X", "list_status": "L"}])
    counts = store.table_counts()
    assert counts["security_master"] == 1
    assert counts["bars_daily"] == 2
    lo, hi = store.date_range("bars_daily")
    assert lo == "2026-06-10" and hi == "2026-06-11"


def test_market_coverage_and_missing_daily_codes(store: MarketStore) -> None:
    store.upsert_security_master(
        [
            {"code": "000001.SZ", "symbol": "000001", "name": "A", "list_status": "L"},
            {"code": "000002.SZ", "symbol": "000002", "name": "B", "list_status": "L"},
            {"code": "000003.SZ", "symbol": "000003", "name": "ST B", "list_status": "L", "is_st": True},
            {"code": "430001.BJ", "symbol": "430001", "name": "BJ", "list_status": "L", "is_bj": True},
        ]
    )
    store.upsert_daily_bars("000001.SZ", [_row("2026-06-10")])

    cov = store.market_coverage()
    assert cov["security_total"] == 4
    assert cov["security_default"] == 2
    assert cov["daily_codes"] == 1
    assert cov["daily_default_codes"] == 1
    assert cov["daily_default_missing_codes"] == 1
    assert cov["date_ranges"]["bars_daily"] == ["2026-06-10", "2026-06-10"]
    assert store.missing_daily_codes() == ["000002.SZ"]


def test_fund_snapshot_codes_filters_type(store: MarketStore) -> None:
    store.upsert_fund_premium(
        "2026-06-24",
        [
            {"code": "510300", "type": "ETF"},
            {"code": "160105", "type": "LOF"},
            {"code": "159919", "type": "ETF"},
        ],
    )
    assert store.fund_snapshot_codes(fund_type="ETF") == ["159919", "510300"]
    assert store.fund_snapshot_codes() == ["159919", "160105", "510300"]


def test_has_etf_daily(store: MarketStore) -> None:
    assert store.has_etf_daily("510300", "2026-06-24") is False
    store.upsert_etf_daily("510300", [{"date": "2026-06-24", "close": 4.05}])
    assert store.has_etf_daily("510300", "2026-06-24") is True


def test_missing_etf_daily_codes(store: MarketStore) -> None:
    store.upsert_fund_premium(
        "2026-06-24",
        [
            {"code": "510300", "type": "ETF"},
            {"code": "159919", "type": "ETF"},
            {"code": "160105", "type": "LOF"},
        ],
    )
    store.upsert_etf_daily("510300", [{"date": "2026-06-24", "close": 4.05}])
    assert store.missing_etf_daily_codes("2026-06-24") == ["159919"]
