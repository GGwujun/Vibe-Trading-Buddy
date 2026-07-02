from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import HTTPException

from src.api import daily_recommendation_routes as routes


class _FakeStore:
    def __init__(self, daily_latest: str | None, realtime_latest: str | None):
        self._daily_latest = daily_latest
        self._realtime_latest = realtime_latest

    def date_range(self, table: str):
        if table == "bars_daily":
            return (None, self._daily_latest)
        if table == "realtime_quote_snapshot":
            return (None, self._realtime_latest)
        return (None, None)

    def get_latest_realtime_quote(self, code: str, trade_date: str | None = None):
        return {
            "trade_date": trade_date,
            "code": code,
            "price": 12.34,
            "rise_rate": 2.5,
            "source": "test",
        }


def test_candidate_market_data_freshness_rejects_stale_daily(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.data.market_store as market_store

    monkeypatch.setattr(market_store, "get_market_store", lambda: _FakeStore("2026-06-26", "2026-07-02"))

    with pytest.raises(HTTPException) as exc:
        routes._assert_candidate_market_data_fresh()

    assert exc.value.status_code == 503
    assert "latest daily=2026-06-26" in str(exc.value.detail)


def test_candidate_market_data_freshness_allows_weekend_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.data.market_store as market_store

    monkeypatch.setattr(market_store, "get_market_store", lambda: _FakeStore("2026-06-26", "2026-06-29"))

    routes._assert_candidate_market_data_fresh()


def test_refresh_candidate_quote_uses_today_realtime_price(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.data.market_store as market_store

    monkeypatch.setattr(market_store, "get_market_store", lambda: _FakeStore("2026-07-01", "2026-07-02"))
    monkeypatch.setattr(routes, "_today_cst", lambda: "2026-07-02")

    item = routes._refresh_candidate_quote({"symbol": "600000.SH", "price": 10.0, "change_pct": -1.0})

    assert item["price"] == 12.34
    assert item["change_pct"] == 2.5
    assert item["quote_source"] == "test"


def _record(
    *,
    slot: str,
    category: str,
    score: float,
    change_pct: float,
    t1_return: float,
    ai_score: float = 0.6,
    factor_score: float = 0.6,
) -> dict:
    return {
        "slot": slot,
        "category": category,
        "strategy": category,
        "rank": 1,
        "score": score,
        "change_pct_at_pick": change_pct,
        "ai_review": {"score": ai_score},
        "factor_review": {"score": factor_score},
        "performance": {"t1": {"return_pct": t1_return}},
    }


def test_recommendation_attribution_groups_performance() -> None:
    records = [
        _record(slot="morning", category="breakout", score=0.77, change_pct=7.2, t1_return=-3.0, ai_score=0.8),
        _record(slot="morning", category="breakout", score=0.76, change_pct=6.4, t1_return=-1.0, ai_score=0.78),
        _record(slot="afternoon", category="trend", score=0.66, change_pct=1.2, t1_return=2.0, factor_score=0.7),
        _record(slot="afternoon", category="trend", score=0.64, change_pct=0.8, t1_return=1.0, factor_score=0.68),
    ]

    report = routes._recommendation_attribution(records, "t1")

    assert report["summary"]["completed_count"] == 4
    assert report["summary"]["win_rate"] == 50.0
    assert report["summary"]["avg_return"] == -0.25
    category_rows = {row["key"]: row for row in report["by_dimension"]["category"]}
    assert category_rows["breakout"]["avg_return"] == -2.0
    assert category_rows["trend"]["avg_return"] == 1.5


def test_recommendation_attribution_flags_weak_change_bucket() -> None:
    records = [
        _record(slot="morning", category="breakout", score=0.78, change_pct=6.2, t1_return=-2.0),
        _record(slot="morning", category="breakout", score=0.79, change_pct=7.0, t1_return=-3.0),
        _record(slot="morning", category="breakout", score=0.80, change_pct=8.5, t1_return=-1.0),
        _record(slot="afternoon", category="trend", score=0.68, change_pct=1.0, t1_return=1.5),
        _record(slot="afternoon", category="trend", score=0.69, change_pct=1.4, t1_return=2.0),
        _record(slot="afternoon", category="trend", score=0.70, change_pct=1.8, t1_return=1.0),
    ]

    report = routes._recommendation_attribution(records, "t1")

    change_rows = {row["key"]: row for row in report["by_dimension"]["change_bucket"]}
    assert change_rows[">=6%"]["avg_return"] == -2.0


def test_attribution_guardrails_penalize_morning_chase_breakout() -> None:
    item = {
        "category_id": "breakout",
        "change_pct": 7.0,
        "score": 0.82,
        "ai_review": {"score": 0.80},
        "factor_review": {"score": 0.62},
    }

    adjusted = routes._apply_attribution_guardrails(item, "morning")

    assert adjusted["score"] < 0.58
    assert "chase_high_penalty" in adjusted["attribution_adjustments"]
    assert "morning_hot_signal_penalty" in adjusted["attribution_adjustments"]


def test_attribution_guardrails_keep_moderate_trend() -> None:
    item = {
        "category_id": "trend",
        "change_pct": 1.8,
        "score": 0.62,
        "ai_review": {"score": 0.68},
        "factor_review": {"score": 0.69},
    }

    adjusted = routes._apply_attribution_guardrails(item, "afternoon")

    assert adjusted["score"] == 0.69
    assert "trend_prior" in adjusted["attribution_adjustments"]
    assert "moderate_intraday_move_prior" in adjusted["attribution_adjustments"]


def test_market_regime_from_closes_detects_risk_off() -> None:
    closes = [100 + i * 0.1 for i in range(40)] + [104 - i * 0.8 for i in range(20)]

    regime = routes._market_regime_from_closes(closes)

    assert regime["regime"] == "risk_off"


def test_market_regime_guardrail_penalizes_hot_signal_in_risk_off() -> None:
    item = {
        "category_id": "breakout",
        "change_pct": 7.0,
        "score": 0.82,
        "ai_review": {"score": 0.80},
        "factor_review": {"score": 0.62},
    }

    adjusted = routes._apply_attribution_guardrails(item, "morning", {"regime": "risk_off"})

    assert adjusted["score"] < 0.50
    assert "risk_off_hot_signal_penalty" in adjusted["attribution_adjustments"]
    assert "risk_off_chase_high_penalty" in adjusted["attribution_adjustments"]


def _candidate(symbol: str = "600000.SH") -> dict:
    return {
        "symbol": symbol,
        "name": symbol,
        "price": 10.0,
        "change_pct": 1.0,
        "score": 0.72,
        "category_id": "trend",
        "reason": "trend setup",
        "ai_review": {"summary": "ok", "risk": "risk", "score": 0.7, "decision": "recommend"},
        "factor_review": {"summary": "factor ok", "score": 0.7},
    }


def test_make_record_uses_explicit_phase_target_and_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "_now_cst", lambda: datetime(2026, 7, 2, 16, 0, tzinfo=routes._CST))

    record = routes._make_record(_candidate(), "post_close_base", "2026-07-03", 1, 2)

    assert record["id"] == "2026-07-03:post_close_base:v2:600000.SH"
    assert record["date"] == "2026-07-02"
    assert record["target_date"] == "2026-07-03"
    assert record["generation_phase"] == "post_close_base"
    assert record["status"] == "draft"
    assert record["version"] == 2


def test_generate_for_phase_versions_and_supersedes(monkeypatch: pytest.MonkeyPatch) -> None:
    storage: list[dict] = []

    monkeypatch.setattr(routes, "_now_cst", lambda: datetime(2026, 7, 2, 16, 0, tzinfo=routes._CST))
    monkeypatch.setattr(routes, "_reviewed_candidates", lambda slot, limit: [_candidate("600000.SH")])
    monkeypatch.setattr(routes, "_load_records", lambda: list(storage))
    monkeypatch.setattr(routes, "_save_records", lambda records: storage.__setitem__(slice(None), records))

    first = routes._generate_for_phase("post_close_base", 1, target_date="2026-07-03")
    second = routes._generate_for_phase("post_close_base", 1, target_date="2026-07-03")

    assert first[0]["version"] == 1
    assert second[0]["version"] == 2
    old = next(record for record in storage if record["id"] == first[0]["id"])
    assert old["status"] == "superseded"

    final = routes._generate_for_phase("morning_final", 1, target_date="2026-07-03")
    draft = next(record for record in storage if record["id"] == second[0]["id"])
    assert final[0]["status"] == "final"
    assert draft["status"] == "superseded"
