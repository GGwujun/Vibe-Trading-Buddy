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
