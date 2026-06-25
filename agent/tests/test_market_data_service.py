"""Tests for the canonical market-data read service."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from src.data import market_data_service as svc
from src.data.market_store import MarketStore


def test_normalize_code() -> None:
    assert svc.normalize_code("000001") == "000001.SZ"
    assert svc.normalize_code("600000") == "600000.SH"
    assert svc.normalize_code("300750.sz") == "300750.SZ"


def test_daily_bars_batch_reads_db_only(tmp_path: Path) -> None:
    store = MarketStore(tmp_path / "market.db")
    try:
        store.upsert_daily_bars(
            "000001.SZ",
            [
                {"date": "2026-06-24", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 10},
            ],
        )
        with mock.patch.object(svc, "get_market_store", return_value=store):
            out = svc.daily_bars_batch(["000001"], days=5)
        assert set(out) == {"000001.SZ"}
        assert float(out["000001.SZ"]["close"].iloc[-1]) == 2.0
    finally:
        store._conn.close()
