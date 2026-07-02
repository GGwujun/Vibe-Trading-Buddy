"""Tests for the market-sync daemon / run_daily_sync engine."""

from __future__ import annotations

from pathlib import Path
import sys
import types
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


def test_daily_falls_back_to_realtime_snapshot_when_settled_sources_empty(store: MarketStore) -> None:
    store.upsert_realtime_quotes(
        "2026-07-01",
        [
            {
                "code": "600000.SH",
                "name": "PF Bank",
                "price": 12.5,
                "pre_close": 12.0,
                "open": 12.1,
                "high": 12.8,
                "low": 12.0,
                "volume": 1000,
                "total_amt": 12500,
                "rise_rate": 4.17,
            }
        ],
    )

    with mock.patch.object(ms, "_today_cst_str", return_value="2026-07-01"), \
         mock.patch("src.data.trade_calendar.cn_market_phase", return_value="post_close"), \
         mock.patch.object(ms, "_sync_daily_tushare_by_date", return_value=0), \
         mock.patch.object(ms, "_fetch_daily_range_rows", return_value=[]) as m_fetch:
        res = ms.run_daily_sync(
            "2026-07-01",
            store=store,
            codes=["600000.SH"],
            datasets={"daily"},
            lookback_days=0,
        )

    assert res["daily"] == 1
    assert m_fetch.call_count == 0
    df = store.get_daily_bars("600000.SH", start="2026-07-01", end="2026-07-01")
    assert df is not None
    assert float(df["close"].iloc[0]) == 12.5
    assert float(df["high"].iloc[0]) == 12.8


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


def test_security_master_tushare_uses_single_active_call(store: MarketStore) -> None:
    class FakeApi:
        def __init__(self):
            self.calls = []

        def stock_basic(self, **kwargs):
            self.calls.append(kwargs)
            import pandas as pd
            return pd.DataFrame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "symbol": "000001",
                        "name": "平安银行",
                        "area": "深圳",
                        "industry": "银行",
                        "market": "主板",
                        "exchange": "SZSE",
                        "list_status": "L",
                        "list_date": "19910403",
                        "delist_date": None,
                        "is_hs": "S",
                    }
                ]
            )

    fake_api = FakeApi()
    with mock.patch.dict("os.environ", {"TUSHARE_TOKEN": "token"}), \
         mock.patch("tushare.pro_api", return_value=fake_api):
        written = ms._sync_security_master_tushare(store)

    assert written == 1
    assert fake_api.calls == [
        {
            "exchange": "",
            "list_status": "L",
            "fields": "ts_code,symbol,name,area,industry,market,exchange,list_status,list_date,delist_date,is_hs",
        }
    ]
    row = store.list_security_master()[0]
    assert row["name"] == "平安银行"
    assert row["industry"] == "银行"
    assert row["list_date"] == "19910403"


def test_realtime_quotes_akshare_falls_back_to_legacy_spot(store: MarketStore, monkeypatch: pytest.MonkeyPatch) -> None:
    import pandas as pd

    fake_ak = types.SimpleNamespace()

    def broken_em():
        raise ConnectionError("em disconnected")

    def legacy_spot():
        return pd.DataFrame(
            [
                {
                    "rank": 1,
                    "code": "600000",
                    "name": "PF Bank",
                    "price": 12.34,
                    "pre_close": 12.0,
                    "open": 12.1,
                    "high": 12.5,
                    "low": 12.0,
                    "volume": 1000,
                    "amount": 1234000,
                    "change": 0.34,
                    "change_pct": 2.83,
                    "turnover_rate": 1.2,
                }
            ]
        )

    fake_ak.stock_zh_a_spot_em = broken_em
    fake_ak.stock_zh_a_spot = legacy_spot
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)

    written = ms._sync_realtime_quotes_akshare(store, "2026-07-01")

    assert written == 1
    quote = store.get_latest_realtime_quote("600000.SH", "2026-07-01")
    assert quote is not None
    assert quote["price"] == 12.34
    assert quote["rise_rate"] == 2.83
    assert quote["source"] == "akshare.stock_zh_a_spot"


def test_etf_daily_uses_tushare_bulk_when_codes_omitted(store: MarketStore) -> None:
    class FakeApi:
        def fund_daily(self, **kwargs):
            assert kwargs == {"trade_date": "20260624"}
            import pandas as pd
            return pd.DataFrame(
                [
                    {
                        "ts_code": "510300.SH",
                        "open": 4.0,
                        "high": 4.1,
                        "low": 3.9,
                        "close": 4.05,
                        "vol": 100,
                        "amount": 400,
                        "pct_chg": 1.2,
                    }
                ]
            )

    with mock.patch.dict("os.environ", {"TUSHARE_TOKEN": "token"}), \
         mock.patch("tushare.pro_api", return_value=FakeApi()), \
         mock.patch.object(ms, "_today_cst_str", return_value="2026-06-25"):
        res = ms.run_daily_sync("2026-06-24", store=store, datasets={"etf"})

    assert res["etf"] == 1
    df = store.get_etf_daily("510300.SH", start="2026-06-24", end="2026-06-24")
    assert df is not None
    assert float(df["close"].iloc[0]) == 4.05


def test_etf_daily_falls_back_to_snapshot_codes(store: MarketStore) -> None:
    store.upsert_fund_premium("2026-06-24", [{"code": "510300", "type": "ETF"}])

    def fake_call(path: str, **params):
        assert path == "etf_his/daily"
        assert params["code"] == "etf.510300"
        return [
            {"date": "2026-06-24", "open": 4, "high": 4.1, "low": 3.9, "close": 4.05, "volume": 100}
        ]

    with mock.patch.dict("os.environ", {"TUSHARE_TOKEN": ""}), \
         mock.patch("src.data.tpdog_client.call", side_effect=fake_call), \
         mock.patch.object(ms, "_today_cst_str", return_value="2026-06-25"):
        res = ms.run_daily_sync("2026-06-24", store=store, datasets={"etf"})

    assert res["etf"] == 1
    df = store.get_etf_daily("510300", start="2026-06-24", end="2026-06-24")
    assert df is not None
    assert float(df["close"].iloc[0]) == 4.05


def test_capital_uses_tushare_moneyflow(store: MarketStore) -> None:
    class FakeApi:
        def moneyflow(self, **kwargs):
            assert kwargs == {"trade_date": "20260624"}
            import pandas as pd
            return pd.DataFrame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "buy_sm_amount": 10,
                        "sell_sm_amount": 4,
                        "buy_lg_amount": 20,
                        "sell_lg_amount": 8,
                        "buy_elg_amount": 30,
                        "sell_elg_amount": 12,
                        "net_mf_vol": 100,
                        "net_mf_amount": 30,
                    }
                ]
            )

    with mock.patch.dict("os.environ", {"TUSHARE_TOKEN": "token"}), \
         mock.patch("tushare.pro_api", return_value=FakeApi()):
        res = ms.run_daily_sync("2026-06-24", store=store, datasets={"capital"})

    assert res["capital"] == 1
    rows = store.get_stock_capital("000001.SZ", start="2026-06-24", end="2026-06-24")
    assert len(rows) == 1
    assert rows[0]["m_in"] == 50
    assert rows[0]["m_out"] == 20
    assert rows[0]["m_net"] == 30
    assert rows[0]["r_net"] == 6


def test_daily_basic_uses_tushare(store: MarketStore) -> None:
    class FakeApi:
        def daily_basic(self, **kwargs):
            assert kwargs["trade_date"] == "20260624"
            assert "fields" in kwargs
            import pandas as pd
            return pd.DataFrame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "close": 12.3,
                        "turnover_rate": 1.2,
                        "turnover_rate_f": 1.5,
                        "volume_ratio": 0.9,
                        "pe": 8,
                        "pe_ttm": 7,
                        "pb": 1.1,
                        "ps": 2.1,
                        "ps_ttm": 2.0,
                        "dv_ratio": 3.0,
                        "dv_ttm": 2.8,
                        "total_share": 100,
                        "float_share": 80,
                        "free_share": 60,
                        "total_mv": 123,
                        "circ_mv": 100,
                    },
                    {"ts_code": "000002.SZ", "close": 9.9},
                ]
            )

    with mock.patch.dict("os.environ", {"TUSHARE_TOKEN": "token"}), \
         mock.patch("tushare.pro_api", return_value=FakeApi()):
        res = ms.run_daily_sync(
            "2026-06-24",
            store=store,
            codes=["000001.SZ"],
            datasets={"daily_basic"},
        )

    assert res["daily_basic"] == 1
    assert store.table_counts()["stock_daily_basic"] == 1
    assert store.date_range("stock_daily_basic") == ("2026-06-24", "2026-06-24")


def test_etf_master_size_and_index_use_tushare(store: MarketStore) -> None:
    class FakeApi:
        def etf_basic(self, **kwargs):
            assert kwargs["list_status"] == "L"
            import pandas as pd
            return pd.DataFrame(
                [
                    {
                        "ts_code": "510300.SH",
                        "csname": "CSI300ETF",
                        "index_code": "000300.SH",
                        "list_date": "20120528",
                        "list_status": "L",
                        "mgt_fee": 0.5,
                    }
                ]
            )

        def etf_share_size(self, **kwargs):
            assert kwargs["trade_date"] == "20260624"
            import pandas as pd
            return pd.DataFrame(
                [
                    {
                        "ts_code": "510300.SH",
                        "trade_date": "20260624",
                        "name": "ETF",
                        "total_share": 100,
                        "total_size": 400,
                        "nav": 4.0,
                        "close": 4.05,
                        "exchange": "SSE",
                    }
                ]
            )

        def index_daily(self, **kwargs):
            assert kwargs["start_date"] == "20260624"
            import pandas as pd
            return pd.DataFrame(
                [
                    {
                        "ts_code": kwargs["ts_code"],
                        "open": 1,
                        "high": 2,
                        "low": 1,
                        "close": 2,
                        "pre_close": 1.9,
                        "change": 0.1,
                        "pct_chg": 5,
                        "vol": 1000,
                        "amount": 2000,
                    }
                ]
            )

    with mock.patch.dict("os.environ", {"TUSHARE_TOKEN": "token"}), \
         mock.patch("tushare.pro_api", return_value=FakeApi()), \
         mock.patch.object(ms, "_DEFAULT_INDEX_CODES", ("000300.SH",)):
        res = ms.run_daily_sync(
            "2026-06-24",
            store=store,
            datasets={"etf_master", "etf_size", "index"},
        )

    assert res["etf_master"] == 1
    assert res["etf_size"] == 1
    assert res["index"] == 1
    counts = store.table_counts()
    assert counts["etf_master"] == 1
    assert counts["etf_share_size"] == 1
    assert counts["index_daily"] == 1


def test_index_daily_defaults_to_all_core_indices_per_run(store: MarketStore) -> None:
    class FakeApi:
        def __init__(self):
            self.calls = []

        def index_daily(self, **kwargs):
            self.calls.append(kwargs["ts_code"])
            import pandas as pd
            return pd.DataFrame(
                [
                    {
                        "ts_code": kwargs["ts_code"],
                        "open": 1,
                        "high": 2,
                        "low": 1,
                        "close": 2,
                    }
                ]
            )

    fake = FakeApi()
    with mock.patch.dict("os.environ", {"TUSHARE_TOKEN": "token"}), \
         mock.patch("tushare.pro_api", return_value=fake), \
         mock.patch.object(ms, "_DEFAULT_INDEX_CODES", ("000001.SH", "399001.SZ")):
        res = ms.run_daily_sync("2026-06-24", store=store, datasets={"index"})

    assert res["index"] == 2
    assert fake.calls == ["000001.SH", "399001.SZ"]


def test_index_daily_fills_missing_core_indices_after_partial_tushare(store: MarketStore) -> None:
    class FakeApi:
        def index_daily(self, **kwargs):
            import pandas as pd

            if kwargs["ts_code"] == "000001.SH":
                return pd.DataFrame(
                    [
                        {
                            "ts_code": kwargs["ts_code"],
                            "open": 1,
                            "high": 2,
                            "low": 1,
                            "close": 2,
                        }
                    ]
                )
            return pd.DataFrame()

    def fake_call(path: str, **params):
        assert path == "stock/daily"
        assert params["code"] == "zssz.399001"
        return [{"date": params["date"], "open": 3, "high": 4, "low": 3, "close": 4, "volume": 100}]

    with mock.patch.dict("os.environ", {"TUSHARE_TOKEN": "token"}), \
         mock.patch("tushare.pro_api", return_value=FakeApi()), \
         mock.patch.object(ms, "_DEFAULT_INDEX_CODES", ("000001.SH", "399001.SZ")), \
         mock.patch("src.data.tpdog_client.call", side_effect=fake_call):
        res = ms.run_daily_sync("2026-06-24", store=store, datasets={"index"})

    assert res["index"] == 2
    assert store.has_index_daily("000001.SH", "2026-06-24")
    assert store.has_index_daily("399001.SZ", "2026-06-24")


def test_etf_master_falls_back_to_tpdog(store: MarketStore) -> None:
    def fake_call(path: str, **params):
        assert path == "etfs/list"
        return [{"code": "510300", "name": "CSI300ETF", "type": "etf"}]

    with mock.patch.object(ms, "_sync_etf_master_tushare", return_value=0), \
         mock.patch("src.data.tpdog_client.call", side_effect=fake_call):
        res = ms.run_daily_sync("2026-06-24", store=store, datasets={"etf_master"})

    assert res["etf_master"] == 1
    assert store.table_counts()["etf_master"] == 1


def test_index_daily_falls_back_to_tpdog_daily(store: MarketStore) -> None:
    def fake_call(path: str, **params):
        assert path == "stock/daily"
        assert params["code"] in {"zssh.000001", "zssz.399001"}
        return [{"date": params["date"], "open": 1, "high": 2, "low": 1, "close": 2, "volume": 100}]

    with mock.patch.object(ms, "_sync_index_daily_tushare", return_value=0), \
         mock.patch.object(ms, "_DEFAULT_INDEX_CODES", ("000001.SH", "399001.SZ")), \
         mock.patch("src.data.tpdog_client.call", side_effect=fake_call):
        res = ms.run_daily_sync("2026-06-25", store=store, datasets={"index"})

    assert res["index"] == 2
    assert store.table_counts()["index_daily"] == 2


def test_capital_falls_back_to_tpdog_current_funds(store: MarketStore) -> None:
    def fake_call(path: str, **params):
        assert path == "current/funds"
        if params["zs_type"] == "zssh":
            return [{"code": "600000", "name": "A", "m_in": 10, "m_out": 4, "m_net": 6}]
        if params["zs_type"] == "zssz":
            return [{"code": "000001", "name": "B", "m_in": 8, "m_out": 3, "m_net": 5}]
        return []

    with mock.patch.object(ms, "_sync_stock_capital_tushare_by_date", return_value=0), \
         mock.patch.object(ms, "_today_cst_str", return_value="2026-06-25"), \
         mock.patch("src.data.trade_calendar.cn_market_phase", return_value="post_close"), \
         mock.patch("src.data.tpdog_client.call", side_effect=fake_call):
        res = ms.run_daily_sync("2026-06-25", store=store, datasets={"capital"})

    assert res["capital"] == 2
    rows = store.get_stock_capital("600000.SH", start="2026-06-25", end="2026-06-25")
    assert len(rows) == 1
    assert rows[0]["m_in"] == 10
    assert rows[0]["source"] == "tpdog_current_funds"


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


def test_premarket_sync_slot_boundaries() -> None:
    from datetime import time

    assert ms._premarket_sync_slot(time(5, 29)) is None
    assert ms._premarket_sync_slot(time(5, 30)) == "overnight-0530"
    assert ms._premarket_sync_slot(time(7, 29)) == "overnight-0530"
    assert ms._premarket_sync_slot(time(7, 30)) == "warmup-0730"
    assert ms._premarket_sync_slot(time(8, 49)) == "warmup-0730"
    assert ms._premarket_sync_slot(time(8, 50)) == "official-0850"


def test_premarket_slot_datasets_match_data_timing() -> None:
    assert ms._premarket_slot_datasets("overnight-0530") == {"global_indices", "us_theme", "us_transmission"}
    assert "premarket_news" in ms._premarket_slot_datasets("warmup-0730")
    assert "stage_snapshot" in ms._premarket_slot_datasets("official-0850")


def test_yahoo_chart_last_rows_uses_overseas_proxy(monkeypatch) -> None:
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [1782691200, 1782777600],
                    "indicators": {
                        "quote": [
                            {
                                "open": [10.0, 11.0],
                                "high": [12.0, 13.0],
                                "low": [9.0, 10.5],
                                "close": [11.0, 12.5],
                                "volume": [100, 200],
                            }
                        ]
                    },
                }
            ]
        }
    }

    class Resp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if "query1.finance.yahoo.com" in url:
            raise RuntimeError("rate limited")
        return Resp({"content": ms.json.dumps(payload)})

    monkeypatch.setenv("OVERSEAS_PROXY_URL", "http://proxy.local")
    monkeypatch.setenv("PROXY_SECRET", "secret")
    monkeypatch.setattr("requests.get", fake_get)

    rows = ms._yahoo_chart_last_rows(["NVDA", "AVGO"])

    assert rows["NVDA"][-1]["close"] == 12.5
    assert rows["NVDA"][-1]["volume"] == 200
    assert rows["AVGO"][-1]["close"] == 12.5
    assert calls[1][0] == "http://proxy.local/fetch"
    assert calls[1][1]["params"]["strategy"] == "raw"
    assert calls[1][1]["headers"]["X-Proxy-Key"] == "secret"


def test_maybe_run_premarket_sync_runs_official_after_warmup(store: MarketStore) -> None:
    from datetime import datetime

    today = "2026-06-30"
    store.set_meta(f"daemon:premarket:{today}:warmup-0730", "ts")
    now = datetime(2026, 6, 30, 8, 50, tzinfo=ms._CST)
    with mock.patch.object(ms, "_now_cst", return_value=now), \
         mock.patch("src.data.trade_calendar.is_trading_day", return_value=True), \
         mock.patch("src.data.trade_calendar.cn_market_phase", return_value="pre_open"), \
         mock.patch.object(ms, "run_daily_sync", return_value={"stage_snapshot": 4}) as m_run:
        ms._maybe_run_premarket_sync(store)

    assert m_run.call_count == 1
    assert store.get_meta(f"daemon:premarket:{today}:official-0850") is not None
