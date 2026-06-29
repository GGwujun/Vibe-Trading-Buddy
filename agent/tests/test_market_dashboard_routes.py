from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pandas as pd

from src.api import market_dashboard_routes as routes


def _auth():
    return True


def test_market_dashboard_aggregates_sources(monkeypatch) -> None:
    app = FastAPI()
    routes.register_market_dashboard_routes(app, _auth, _auth)

    monkeypatch.setattr(
        routes,
        "_load_recommendations",
        lambda: {
            "items": [
                {
                    "id": "2026-06-24:morning:000001.SZ",
                    "date": "2026-06-24",
                    "slot": "morning",
                    "score": 0.8,
                    "ai_review": {"score": 0.7},
                    "factor_review": {"score": 0.6},
                }
            ],
            "date": "2026-06-24",
        },
    )
    monkeypatch.setattr(
        routes,
        "_load_opportunities",
        lambda: {
            "categories": [
                {
                    "id": "breakout",
                    "label": "突破",
                    "opportunities": [{"symbol": "000001.SZ", "change_pct": 6.2, "confidence": 0.8}],
                }
            ]
        },
    )
    monkeypatch.setattr(routes, "_load_news", lambda: {"articles": [{"title": "news"}]})
    monkeypatch.setattr(routes, "_load_events", lambda: {"categories": [{"events": [{"title": "event"}]}]})
    monkeypatch.setattr(routes, "_load_tracking", lambda: {"watchlist": [{"symbol": "000001.SZ"}], "tasks": [{"task_id": "t1"}]})
    monkeypatch.setattr(
        routes,
        "_load_market_overview",
        lambda: {
            "as_of": "2026-06-24T09:30:00+08:00",
            "breadth": {"advancers": 10, "decliners": 3},
            "indices": [{"name": "上证指数"}],
            "hot_sectors": [{"name": "机器人"}],
            "top_gainers": [],
            "top_losers": [],
        },
    )

    res = TestClient(app).get("/market-dashboard")

    assert res.status_code == 200
    body = res.json()
    assert body["counts"] == {
        "recommendations": 1,
        "opportunities": 1,
        "indices": 1,
        "hot_sectors": 1,
        "news": 1,
        "events": 1,
        "watchlist": 1,
        "tasks": 1,
        "tail_decisions": 1,
    }
    assert body["errors"] == []
    assert body["tail_decisions"][0]["symbol"] == "000001.SZ"
    assert body["mood"]["label"] == "进攻观察"


def test_market_dashboard_degrades_failed_source(monkeypatch) -> None:
    app = FastAPI()
    routes.register_market_dashboard_routes(app, _auth, _auth)

    monkeypatch.setattr(routes, "_load_recommendations", lambda: {"items": [], "date": "2026-06-24"})
    monkeypatch.setattr(routes, "_load_opportunities", lambda: (_ for _ in ()).throw(RuntimeError("scan failed")))
    monkeypatch.setattr(routes, "_load_news", lambda: {"articles": []})
    monkeypatch.setattr(routes, "_load_events", lambda: {"categories": []})
    monkeypatch.setattr(routes, "_load_tracking", lambda: {"watchlist": [], "tasks": []})
    monkeypatch.setattr(routes, "_load_market_overview", lambda: {"breadth": {}, "indices": [], "hot_sectors": []})

    res = TestClient(app).get("/market-dashboard")

    assert res.status_code == 200
    body = res.json()
    assert body["opportunities"] == []
    assert body["counts"]["opportunities"] == 0
    assert body["errors"] == [{"source": "opportunities", "message": "scan failed"}]


def test_tail_decisions_fall_back_to_opportunities() -> None:
    decisions = routes._build_tail_decisions(
        recommendations=[],
        opportunities=[
            {
                "symbol": "600000.SH",
                "name": "浦发银行",
                "confidence": 0.75,
                "change_pct": 8.1,
                "price": 9.2,
                "reason": "放量突破",
                "category_label": "突破",
            }
        ],
    )

    assert decisions[0]["symbol"] == "600000.SH"
    assert decisions[0]["source"] == "opportunity"
    assert decisions[0]["action"] == "等回落"


def test_market_dashboard_stage_endpoint(monkeypatch) -> None:
    app = FastAPI()
    routes.register_market_dashboard_routes(app, _auth, _auth)

    monkeypatch.setattr(routes, "_load_recommendations", lambda: {"items": [], "date": "2026-06-24"})
    monkeypatch.setattr(routes, "_load_opportunities", lambda: {"categories": []})
    monkeypatch.setattr(routes, "_load_news", lambda: {"articles": [{"title": "policy"}]})
    monkeypatch.setattr(routes, "_load_events", lambda: {"categories": []})
    monkeypatch.setattr(routes, "_load_tracking", lambda: {"watchlist": [], "tasks": []})
    monkeypatch.setattr(routes, "_load_market_overview", lambda: {"breadth": {"advancers": 3, "decliners": 1}, "indices": [], "hot_sectors": []})

    res = TestClient(app).get("/market-dashboard/stages/morning-brief")

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["stage"] == "morning-brief"
    assert body["data"]["title"] == "早盘内参"


def test_close_review_stage_uses_completed_session_date(monkeypatch) -> None:
    class FakeStore:
        def __init__(self) -> None:
            self.calls = []

        def get_market_stage_snapshot_fast(self, stage: str, trade_date: str | None = None):
            self.calls.append((stage, trade_date))
            if stage == "close-review" and trade_date == "2026-06-25":
                return {
                    "trade_date": "2026-06-25",
                    "stage": "close-review",
                    "payload": {"title": "close review", "trade_date": "2026-06-25"},
                    "source_tables": ["market_stage_snapshot"],
                    "updated_at": "2026-06-25T15:30:00+08:00",
                }
            if stage == "close-review" and trade_date == "2026-06-26":
                return {
                    "trade_date": "2026-06-26",
                    "stage": "close-review",
                    "payload": {"title": "wrong intraday review", "trade_date": "2026-06-26"},
                    "source_tables": ["market_stage_snapshot"],
                    "updated_at": "2026-06-26T10:00:00+08:00",
                }
            return None

    store = FakeStore()
    monkeypatch.setattr(routes, "_market_store", lambda: store)
    monkeypatch.setattr(routes, "_close_review_visible_trade_date", lambda: "2026-06-25")

    app = FastAPI()
    routes.register_market_dashboard_routes(app, _auth, _auth)

    res = TestClient(app).get("/market-dashboard/stages/close-review")

    assert res.status_code == 200
    body = res.json()
    assert body["stage"] == "close-review"
    assert body["date"] == "2026-06-25"
    assert body["data"]["title"] == "close review"
    assert store.calls == [("close-review", "2026-06-25")]


def test_market_overview_uses_live_index_rows(monkeypatch) -> None:
    monkeypatch.setattr(
        routes,
        "_db_market_overview",
        lambda: {
            "as_of": "2026-06-29T08:00:00+08:00",
            "source": "market_db",
            "trade_date": "2026-06-26",
            "breadth": {"total": 1, "advancers": 0, "decliners": 1},
            "indices": [{"symbol": "000001.SH", "price": 3000, "trade_date": "2026-06-26"}],
        },
    )
    monkeypatch.setattr(
        routes,
        "_live_index_rows",
        lambda: [{"symbol": "000001.SH", "price": 3100, "trade_date": "2026-06-29"}],
    )

    overview = routes._load_market_overview()

    assert overview["indices"] == [{"symbol": "000001.SH", "price": 3100, "trade_date": "2026-06-29"}]
    assert overview["index_source"] == "akshare.index_spot"
    assert overview["trade_date"] == "2026-06-26"


def test_build_index_rows_filters_to_dashboard_indices() -> None:
    rows = routes._build_index_rows(
        pd.DataFrame(
            [
                {"code": "000001", "name": "上证指数", "price": 4034.08, "change_pct": 0.17},
                {"code": "399330", "name": "深证100", "price": 6662.41, "change_pct": -1.32},
                {"code": "000300", "name": "沪深300", "price": 5200.0, "change_pct": -0.2},
            ]
        )
    )

    assert [row["symbol"] for row in rows] == ["000001", "000300"]


def test_apply_breadth_snapshot_maps_intraday_fields() -> None:
    overview = {
        "trade_date": "2026-06-26",
        "breadth": {
            "total": 5000,
            "advancers": 100,
            "decliners": 4900,
            "flat": 0,
            "turnover_billion": 900.0,
        },
    }
    snapshot = {
        "trade_date": "2026-06-29",
        "total": 5867,
        "advancers": 1911,
        "decliners": 3495,
        "unchanged": 104,
        "limit_up": 71,
        "limit_down": 37,
        "max_limit_up_height": 3,
        "turnover_billion": None,
        "source": "market_breadth_snapshot",
        "updated_at": "2026-06-29T12:22:00+08:00",
    }

    updated = routes._apply_breadth_snapshot(overview, snapshot)

    assert updated["trade_date"] == "2026-06-29"
    assert updated["breadth"] == {
        "total": 5867,
        "advancers": 1911,
        "decliners": 3495,
        "flat": 104,
        "turnover_billion": 900.0,
        "limit_up": 71,
        "limit_down": 37,
        "max_limit_up_height": 3,
    }
    assert updated["breadth_source"] == "market_breadth_snapshot"


def test_append_provisional_bar_replaces_same_date_and_trims() -> None:
    bars = [
        {"date": "2026-06-25", "close": 10},
        {"date": "2026-06-26", "close": 11},
        {"date": "2026-06-29", "close": 12},
    ]
    provisional = {"date": "2026-06-29", "close": 13, "provisional": True}

    out = routes._append_provisional_bar(bars, provisional, limit=2)

    assert out == [
        {"date": "2026-06-26", "close": 11},
        {"date": "2026-06-29", "close": 13, "provisional": True},
    ]


def test_stock_spot_provisional_daily_bar_builds_intraday_daily(monkeypatch) -> None:
    monkeypatch.setattr(
        routes,
        "_fetch_a_share_spot",
        lambda: pd.DataFrame(
            [
                {
                    "code": "600000",
                    "price": 12.34,
                    "open": 12.0,
                    "high": 12.5,
                    "low": 11.9,
                    "volume": 1000,
                }
            ]
        ),
    )
    monkeypatch.setattr(routes, "_now_cst", lambda: routes.datetime(2026, 6, 29, 10, 0, tzinfo=routes._CST))

    bar = routes._stock_spot_provisional_daily_bar("600000.SH", "2026-06-29")

    assert bar is not None
    assert bar["date"] == "2026-06-29"
    assert bar["open"] == 12.0
    assert bar["close"] == 12.34
    assert bar["high"] == 12.5
    assert bar["low"] == 11.9
    assert bar["volume"] == 1000.0
    assert bar["provisional"] is True
    assert bar["source"] == "akshare.stock_spot"


def test_provisional_daily_bar_skips_when_official_bar_is_today() -> None:
    assert routes._provisional_daily_bar(store=object(), project_code="000001.SH", latest_official_date=routes._today_cst()) is None
