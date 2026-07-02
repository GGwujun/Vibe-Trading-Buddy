from __future__ import annotations

import pandas as pd

from src.api import opportunity_routes as routes


def _bars(close_values: list[float], volume: float = 1000.0) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=len(close_values), freq="D")
    return pd.DataFrame(
        {
            "open": close_values,
            "high": [v * 1.02 for v in close_values],
            "low": [v * 0.98 for v in close_values],
            "close": close_values,
            "volume": [volume] * len(close_values),
        },
        index=dates,
    )


def test_fetch_stocks_from_local_db_adds_bars_and_filters_invalid(monkeypatch):
    import src.data.market_data_service as mds

    codes = ["600001.SH", "000001.SZ", "600002.SH"]
    monkeypatch.setattr(mds, "default_strategy_codes", lambda: codes)
    monkeypatch.setattr(
        mds,
        "security_master",
        lambda default_only=False: [
            {"code": "600001.SH", "name": "Good A"},
            {"code": "000001.SZ", "name": "ST Bad"},
            {"code": "600002.SH", "name": "Tiny"},
        ],
    )
    monkeypatch.setattr(
        mds,
        "daily_bars_batch",
        lambda requested, days=90: {
            "600001.SH": _bars([10 + i * 0.05 for i in range(90)], volume=5000),
            "000001.SZ": _bars([8 + i * 0.03 for i in range(90)], volume=5000),
            "600002.SH": _bars([1.2 + i * 0.001 for i in range(90)], volume=5000),
        },
    )

    stocks = routes._fetch_stocks_from_local_db(limit=10)

    assert [item["symbol"] for item in stocks] == ["600001.SH"]
    assert stocks[0]["name"] == "Good A"
    assert "df" in stocks[0]
    assert stocks[0]["change_pct"] > 0


def test_fetch_top_stocks_prefers_local_db(monkeypatch):
    local = [{"symbol": "600001.SH", "df": _bars([10 + i for i in range(90)])}]

    monkeypatch.setattr(routes, "_fetch_stocks_from_local_db", lambda limit: local)
    monkeypatch.setattr(routes, "_fetch_stocks_akshare", lambda limit: (_ for _ in ()).throw(AssertionError("akshare should not run")))

    assert routes._fetch_top_stocks(10) is local


def test_quality_score_penalizes_hot_breakout_chase() -> None:
    calm = {
        "change_pct": 2.0,
        "df": _bars([10 + i * 0.03 for i in range(90)], volume=1000),
    }
    hot = {
        "change_pct": 9.0,
        "df": _bars([10 + i * 0.12 for i in range(90)], volume=1000),
    }

    calm_score, _ = routes._opportunity_quality_score(calm, {"confidence": 0.7}, "breakout")
    hot_score, _ = routes._opportunity_quality_score(hot, {"confidence": 0.7}, "breakout")

    assert calm_score > hot_score


def test_build_opportunities_scores_full_pool_before_top10(monkeypatch):
    stocks = [{"symbol": f"600{i:03d}.SH", "name": f"S{i}", "close": 10, "change_pct": 1} for i in range(12)]

    monkeypatch.setattr(routes, "_fetch_top_stocks", lambda limit: stocks)
    monkeypatch.setattr(routes, "_scan_limit", lambda: 12)
    monkeypatch.setattr(routes, "_detect_breakout", lambda s: None)
    monkeypatch.setattr(routes, "_detect_oversold", lambda s: None)
    monkeypatch.setattr(routes, "_detect_event_catalyst", lambda s: None)

    def trend_signal(s):
        idx = int(s["symbol"][3:6])
        return {"reason": "trend", "confidence": 0.5 + idx / 100}

    monkeypatch.setattr(routes, "_detect_trend", trend_signal)

    payload = routes._build_opportunities()
    trend = next(cat for cat in payload["categories"] if cat["id"] == "trend")

    assert len(trend["opportunities"]) == 10
    assert trend["opportunities"][0]["symbol"] == "600011.SH"
    assert "600000.SH" not in {item["symbol"] for item in trend["opportunities"]}
