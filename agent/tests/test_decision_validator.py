"""Tests for the decision validator."""

from __future__ import annotations

from src.analysis.decision_validator import validate_fund_decision, validate_stock_decision


def test_clean_buy_no_warnings() -> None:
    meta = {"signal": "买入", "entry": 10, "target": 12, "stop_loss": 9, "size_pct": 15}
    assert validate_stock_decision(meta, latest_price=10) == []


def test_stop_above_entry_on_buy_warns() -> None:
    meta = {"signal": "BUY", "entry": 10, "target": 12, "stop_loss": 11}
    warnings = validate_stock_decision(meta)
    assert any("止损" in w and "入场价" in w for w in warnings)


def test_target_below_entry_on_buy_warns() -> None:
    meta = {"signal": "买入", "entry": 10, "target": 8, "stop_loss": 9}
    warnings = validate_stock_decision(meta)
    assert any("目标价" in w for w in warnings)


def test_oversize_position_warns() -> None:
    meta = {"signal": "买入", "entry": 10, "target": 12, "stop_loss": 9, "size_pct": 200}
    warnings = validate_stock_decision(meta)
    assert any("仓位" in w for w in warnings)


def test_stop_way_outside_band_warns() -> None:
    # stop 21 vs entry 10 → 110% off, almost certainly a unit error.
    meta = {"signal": "BUY", "entry": 10, "target": 12, "stop_loss": 21}
    warnings = validate_stock_decision(meta, latest_price=10)
    assert any("单位" in w for w in warnings)


def test_hold_skips_price_checks() -> None:
    meta = {"signal": "持有", "entry": 10, "target": 5, "stop_loss": 99}
    assert validate_stock_decision(meta) == []


def test_sell_target_above_entry_warns() -> None:
    meta = {"signal": "卖出", "entry": 10, "target": 12, "stop_loss": 9}
    warnings = validate_stock_decision(meta)
    assert any("目标价" in w and "减仓" in w for w in warnings)


def test_fund_negative_net_return_warns() -> None:
    meta = {"action": "溢价套利", "net_return_pct": -0.5}
    warnings = validate_fund_decision(meta)
    assert any("净收益" in w for w in warnings)


def test_fund_clean_no_warnings() -> None:
    meta = {"action": "折价套利", "premium_pct": -1.5, "net_return_pct": 0.6}
    assert validate_fund_decision(meta) == []
