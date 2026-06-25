"""Tests for the market-sync daemon / run_daily_sync engine."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from src.data import market_sync as ms
from src.data.market_store import MarketStore


@pytest.fixture
def store(tmp_path: Path) -> MarketStore:
    s = MarketStore(tmp_path / "market.db")
    yield s
    s._conn.close()


def test_daily_incremental_and_no_intraday(store: MarketStore) -> None:
    """A code with last_daily_date=06-10 only fetches 06-11; today is filtered."""
    store.upsert_daily_bars("600206.SH", [
        {"date": "2026-06-10", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
    ])

    captured = {}

    def fake_fetch(tpdog_code, start, end):
        captured["start"] = start
        return [
            {"date": "2026-06-11", "open": 2, "high": 2, "low": 2, "close": 27.77, "volume": 2},
            {"date": "2026-06-15", "open": 3, "high": 3, "low": 3, "close": 28, "volume": 3},
        ]

    with mock.patch.object(ms, "_fetch_daily_range_rows", side_effect=fake_fetch), \
         mock.patch.object(ms, "_today_cst_str", return_value="2026-06-15"), \
         mock.patch.object(ms, "_all_a_share_codes", return_value=["600206.SH"]):
        res = ms.run_daily_sync("2026-06-11", store=store, datasets={"daily"})

    assert captured["start"] == "2026-06-11"  # resumed from last+1
    assert res["daily"] == 1  # today(06-15) filtered out
    df = store.get_daily_bars("600206.SH", start="2026-06-11", end="2026-06-11")
    assert df is not None and df["close"].iloc[0] == 27.77
    assert store.get_daily_bars("600206.SH", start="2026-06-15", end="2026-06-15") is None


def test_today_daily_persists_after_post_close(store: MarketStore) -> None:
    def fake_fetch(tpdog_code, start, end):
        return [
            {"date": "2026-06-25", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 10},
        ]

    with mock.patch.object(ms, "_fetch_daily_range_rows", side_effect=fake_fetch), \
         mock.patch.object(ms, "_today_cst_str", return_value="2026-06-25"), \
         mock.patch("src.data.trade_calendar.cn_market_phase", return_value="post_close"):
        res = ms.run_daily_sync(
            "2026-06-25",
            store=store,
            codes=["600206.SH"],
            datasets={"daily"},
            lookback_days=0,
        )

    assert res["daily"] == 1
    df = store.get_daily_bars("600206.SH", start="2026-06-25", end="2026-06-25")
    assert df is not None and float(df["close"].iloc[0]) == 2.0


def test_today_daily_skips_before_post_close(store: MarketStore) -> None:
    def fake_fetch(tpdog_code, start, end):
        return [
            {"date": "2026-06-25", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 10},
        ]

    with mock.patch.object(ms, "_fetch_daily_range_rows", side_effect=fake_fetch) as m_fetch, \
         mock.patch.object(ms, "_today_cst_str", return_value="2026-06-25"), \
         mock.patch("src.data.trade_calendar.cn_market_phase", return_value="in_session"):
        res = ms.run_daily_sync(
            "2026-06-25",
            store=store,
            codes=["600206.SH"],
            datasets={"daily"},
            lookback_days=0,
        )

    assert res["daily"] == 0
    assert m_fetch.call_count == 0
    assert store.get_daily_bars("600206.SH", start="2026-06-25", end="2026-06-25") is None


def test_daily_skips_already_synced_code(store: MarketStore) -> None:
    """last_daily_date == trade_date → no fetch call."""
    store.upsert_daily_bars("600206.SH", [
        {"date": "2026-06-11", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
    ])
    with mock.patch.object(ms, "_fetch_daily_range_rows") as m_fetch, \
         mock.patch.object(ms, "_all_a_share_codes", return_value=["600206.SH"]):
        res = ms.run_daily_sync("2026-06-11", store=store, datasets={"daily"})
    assert m_fetch.call_count == 0
    assert res.get("daily", 0) == 0


def test_daily_uses_tushare_bulk_before_per_code_fallback(store: MarketStore) -> None:
    with mock.patch.object(ms, "_today_cst_str", return_value="2026-06-26"), \
         mock.patch.object(ms, "_sync_daily_tushare_by_date", return_value=2) as m_bulk, \
         mock.patch.object(ms, "_fetch_daily_range_rows") as m_fetch:
        res = ms.run_daily_sync("2026-06-25", store=store, datasets={"daily"})

    assert res["daily"] == 2
    m_bulk.assert_called_once()
    assert m_fetch.call_count == 0


def test_default_daily_universe_filters_to_strategy_codes(store: MarketStore) -> None:
    store.upsert_security_master(
        [
            {"code": "000001.SZ", "symbol": "000001", "name": "A", "list_status": "L"},
            {"code": "000002.SZ", "symbol": "000002", "name": "ST B", "list_status": "L", "is_st": True},
            {"code": "430001.BJ", "symbol": "430001", "name": "BJ", "list_status": "L", "is_bj": True},
        ]
    )
    captured = {}

    def fake_bulk(store_arg, trade_date, *, codes=None):
        captured["codes"] = codes
        return 1

    with mock.patch.object(ms, "_today_cst_str", return_value="2026-06-26"), \
         mock.patch.object(ms, "_sync_daily_tushare_by_date", side_effect=fake_bulk):
        res = ms.run_daily_sync("2026-06-25", store=store, datasets={"daily"})

    assert res["daily"] == 1
    assert captured["codes"] == ["000001.SZ"]


def test_security_master_tpdog_fallback_marks_default_universe(store: MarketStore) -> None:
    def fake_call(path: str, **params):
        assert path == "stocks/list"
        if params["type"] == "sh":
            return [{"code": "600000", "name": "浦发银行"}, {"code": "600001", "name": "ST样本"}]
        if params["type"] == "sz":
            return [{"code": "000001", "name": "平安银行"}]
        if params["type"] == "bj":
            return [{"code": "430001", "name": "北交样本"}]
        return []

    with mock.patch.dict("os.environ", {"TUSHARE_TOKEN": ""}), \
         mock.patch("src.data.tpdog_client.call", side_effect=fake_call):
        written = ms._sync_security_master(store)

    assert written == 4
    assert store.security_master_count() == 4
    # Daily backfill can still use ST non-BJ names, but strategy default excludes ST and BJ.
    assert ms._all_a_share_codes(store) == ["000001.SZ", "600000.SH", "600001.SH"]
    assert store.default_strategy_codes() == ["000001.SZ", "600000.SH"]


def test_single_dataset_failure_does_not_block_siblings(store: MarketStore) -> None:
    with mock.patch.object(ms, "_sync_dragon_tiger", side_effect=RuntimeError("boom")), \
         mock.patch.object(ms, "_sync_pools", return_value=5):
        res = ms.run_daily_sync("2026-06-11", store=store, datasets={"dragon", "pool"})
    # dragon raised, pool still ran.
    assert res.get("pool") == 5
    assert "dragon" not in res  # failed dataset omitted


def test_maybe_run_sync_skips_when_already_done(store: MarketStore) -> None:
    """daemon:<today> meta present → no sync even when market is post_close."""
    today = "2026-06-11"
    store.set_meta(f"daemon:{today}", "ts")
    with mock.patch("src.data.trade_calendar.cn_market_phase", return_value="post_close"), \
         mock.patch.object(ms, "_today_cst_str", return_value=today), \
         mock.patch.object(ms, "run_daily_sync") as m_run:
        ms._maybe_run_daily_sync(store)
    assert m_run.call_count == 0  # already synced today


def test_maybe_run_sync_skips_before_close(store: MarketStore) -> None:
    """Market still in_session → daemon must not fire."""
    with mock.patch("src.data.trade_calendar.cn_market_phase", return_value="in_session"), \
         mock.patch.object(ms, "run_daily_sync") as m_run:
        ms._maybe_run_daily_sync(store)
    assert m_run.call_count == 0


def test_maybe_run_sync_runs_at_post_close(store: MarketStore) -> None:
    """post_close + not yet synced today → run_daily_sync fires."""
    today = "2026-06-11"
    with mock.patch("src.data.trade_calendar.cn_market_phase", return_value="post_close"), \
         mock.patch.object(ms, "_today_cst_str", return_value=today), \
         mock.patch.object(ms, "run_daily_sync", return_value={"dragon": 1}) as m_run:
        ms._maybe_run_daily_sync(store)
    assert m_run.call_count == 1
    # daemon:<today> meta should now be set.
    assert store.get_meta(f"daemon:{today}") is not None
