"""Post-generation validation for LLM-produced trading decisions.

LLM agents (AlphaForge trader/PM, fund-arbitrage report_writer) propose
entry/target/stop/size in free-form text. ``validate_decision`` checks those
numbers against hard A-share rules so a malformed proposal (e.g. stop above
entry on a BUY, 200% position, stop outside the daily-limit band) is caught
and surfaced as a warning rather than silently shipped to the user.

This is the "LLM proposes, code guards" layer: the LLM still owns the
judgment, but structural impossibilities are rejected. Warnings are attached
to report metadata; nothing is auto-corrected (we don't want to silently move
a stop the user/PM chose).
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# A-share daily price limits. ST/创业板/科创板 vary; we use the loosest common
# bound for the sanity check — a stop further than ±20% from entry is almost
# certainly a data error, not an intended level.
_MAX_DAILY_LIMIT_PCT = 20.0
_MAX_POSITION_PCT = 100.0
_MIN_MEANINGFUL_PCT = 0.0


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None  # 0 / negative = "not applicable" sentinel


def validate_stock_decision(meta: dict, *, latest_price: Optional[float] = None) -> list[str]:
    """Validate a stock decision (AlphaForge trader/PM output).

    Args:
        meta: report metadata containing signal/entry/target/stop_loss/size_pct.
        latest_price: latest close (CNY), used for daily-limit sanity. Optional.

    Returns:
        A list of human-readable warning strings (empty = clean). Never raises.
    """
    warnings: list[str] = []
    action = str(meta.get("signal", "")).strip()
    # Normalize action to canonical BUY/SELL/HOLD.
    norm = action.upper()
    if "买" in action or "BUY" in norm:
        action_key = "BUY"
    elif "卖" in action or "SELL" in norm:
        action_key = "SELL"
    elif "持" in action or "HOLD" in norm:
        action_key = "HOLD"
    else:
        action_key = ""

    entry = _to_float(meta.get("entry"))
    target = _to_float(meta.get("target"))
    stop = _to_float(meta.get("stop_loss"))
    size = _to_float(meta.get("size_pct"))

    # Position size bounds.
    if size is not None:
        if size > _MAX_POSITION_PCT:
            warnings.append(f"仓位 {size}% 超过 100%，疑似笔误（建议复核）")
        elif size < _MIN_MEANINGFUL_PCT:
            warnings.append(f"仓位 {size}% 为零或负，无实际意义")

    if action_key == "HOLD":
        return warnings  # HOLD needs no price ordering

    # Price-ordering sanity.
    if entry and target:
        if action_key == "BUY" and target <= entry:
            warnings.append(f"做多但目标价 {target} ≤ 入场价 {entry}，方向与价位矛盾")
        if action_key == "SELL" and target >= entry:
            warnings.append(f"做空/减仓但目标价 {target} ≥ 入场价 {entry}，方向与价位矛盾")
    if entry and stop:
        if action_key == "BUY" and stop >= entry:
            warnings.append(f"做多但止损 {stop} ≥ 入场价 {entry}，止损无保护意义")
        if action_key == "SELL" and stop <= entry:
            warnings.append(f"做空/减仓但止损 {stop} ≤ 入场价 {entry}，止损无保护意义")

    # Daily-limit band: a stop more than ±20% from entry (or latest price) is
    # almost certainly a unit/data error (e.g. ¥0.21 written as 21).
    ref = latest_price or entry
    if ref and stop:
        band = abs(stop - ref) / ref * 100
        if band > _MAX_DAILY_LIMIT_PCT * 3:  # 60% — well beyond any realistic stop
            warnings.append(f"止损 {stop} 偏离参考价 {ref} 达 {band:.0f}%，疑似单位错误（复查是否为元/分）")

    return warnings


def _to_signed_float(v) -> Optional[float]:
    """Like _to_float but permits negative values (net return can be negative)."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def validate_fund_decision(meta: dict) -> list[str]:
    """Validate a fund-arbitrage decision.

    Checks that premium/net-return signs are internally consistent with the
    chosen action (e.g. 溢价套利 with a negative net return is self-defeating).
    """
    warnings: list[str] = []
    action = str(meta.get("action", "")).strip()
    raw_net = meta.get("net_return_pct") or str(meta.get("net_return", "")).rstrip("%")
    net = _to_signed_float(raw_net)

    if "溢价" in action and net is not None and net <= 0:
        warnings.append(f"选择溢价套利但净收益 {net}% ≤ 0，扣除成本后无利可图")
    if "折价" in action and net is not None and net <= 0:
        warnings.append(f"选择折价套利但净收益 {net}% ≤ 0，扣除成本后无利可图")
    return warnings


def fetch_latest_price(code: str) -> Optional[float]:
    """Best-effort latest close for an A-share code, via tpdog. None on failure."""
    try:
        from src.data.tpdog_client import call
        from src.data.ohlcv_cache import _to_tpdog_code

        tpdog_code = _to_tpdog_code(code)
        if tpdog_code is None:
            return None
        content = call("etf/daily" if code.startswith(("5", "1")) else "stock_his/daily",
                       code=tpdog_code)
        # stock_his/daily returns a list; take the last row's close.
        if not content:
            return None
        row = content[-1] if isinstance(content, list) else content
        close = _to_float(row.get("close"))
        return close
    except Exception:  # noqa: BLE001 — price is advisory only
        return None
