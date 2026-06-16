"""Unified A-share trading calendar + market-phase helpers.

Single source of truth for "is today a trading day" and "what phase is the
market in" across the project (replaces the scattered time checks that used to
live in ohlcv_cache._is_trading_hours, market_sync._maybe_run_daily_sync, and
the various loaders).

The holiday calendar is sourced from tpdog (``trading_day/year``) — more
reliable than akshare's sina endpoint and already token-configured in this
project. When tpdog is unavailable the calendar degrades to the weekend rule
(no holiday knowledge), so every function still returns a sensible answer.

Phases (CST / Asia-Shanghai):
    pre_open    < 09:30
    in_session  09:30–11:30  and  13:00–15:00
    lunch_break 11:30–13:00
    post_close  >= 15:00
    closed      non-trading day
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))  # Asia/Shanghai — avoid zoneinfo platform issues

# Trading-calendar cache state. We cache the full year on first need and
# refresh at most once per process hour (holidays only change yearly, but a
# long-running daemon shouldn't pin a stale year boundary).
_cal_lock = threading.Lock()
_cal_loaded_at: float = 0.0
_CAL_TTL_SECONDS = 3600


def now_cn() -> datetime:
    """Current wall-clock time in Asia/Shanghai."""
    return datetime.now(CN_TZ)


def cn_today_str() -> str:
    """Today's date (CST) as YYYY-MM-DD."""
    return now_cn().date().strftime("%Y-%m-%d")


def _parse_date(date_str: str) -> date:
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def _load_all_trading_days() -> set[date]:
    """Fetch the full A-share trading-day set from akshare (sina calendar).

    akshare's ``tool_trade_date_hist_sina`` returns every historical + planned
    trading day for the exchange — the de-facto standard calendar. We cache the
    whole set once (it's a few hundred dates per year) and refresh hourly.

    On any failure (akshare missing, network) we return an empty set so callers
    fall back to the weekend rule. NB: tpdog's ``trading_day/year`` was tried
    first but its data is unreliable (mid-2026 weeks show Fri=False/Sun=True),
    so we deliberately do NOT use it for the calendar.
    """
    import pandas as pd

    try:
        import akshare as ak

        df = ak.tool_trade_date_hist_sina()
    except Exception:  # noqa: BLE001 — calendar must never raise
        return set()
    if df is None or df.empty or "trade_date" not in df.columns:
        return set()
    out: set[date] = set()
    for pd_dt in pd.to_datetime(df["trade_date"], errors="coerce"):
        if str(pd_dt) != "NaT":
            out.add(pd_dt.date())
    return out


# Cache the full calendar (all years) rather than per-year, since the sina
# endpoint returns the entire history in one call.
_all_days_cache: Optional[set[date]] = None
_all_days_lock = threading.Lock()


def _trading_days(_year: int = 0) -> set[date]:
    """Cached full trading-day set (refreshed hourly). Year arg ignored."""
    global _all_days_cache, _cal_loaded_at
    import time as _time

    now_ts = _time.monotonic()
    with _all_days_lock:
        if _all_days_cache is not None and (now_ts - _cal_loaded_at) < _CAL_TTL_SECONDS:
            return _all_days_cache
        days = _load_all_trading_days()
        _all_days_cache = days
        _cal_loaded_at = now_ts
        return days


def is_trading_day(date_str: str) -> bool:
    """True when ``date_str`` (YYYY-MM-DD) is an A-share trading day.

    Uses the akshare (sina) holiday calendar when available; otherwise falls
    back to the weekend rule (Mon–Fri).
    """
    d = _parse_date(date_str)
    days = _trading_days(d.year)
    if days:
        return d in days
    return d.weekday() < 5  # weekend-rule fallback


def previous_trading_day(date_str: str) -> str:
    """The most recent trading day strictly before ``date_str`` (YYYY-MM-DD)."""
    d = _parse_date(date_str)
    cur = d - timedelta(days=1)
    # Look back up to ~15 calendar days (covers the longest holiday gaps).
    for _ in range(15):
        if is_trading_day(cur.strftime("%Y-%m-%d")):
            return cur.strftime("%Y-%m-%d")
        cur -= timedelta(days=1)
    # Last resort: most recent weekday.
    cur = d - timedelta(days=1)
    while cur.weekday() >= 5:
        cur -= timedelta(days=1)
    return cur.strftime("%Y-%m-%d")


def cn_market_phase(now: Optional[datetime] = None) -> str:
    """Return the current A-share market phase.

    One of ``pre_open`` / ``in_session`` / ``lunch_break`` / ``post_close`` /
    ``closed``. ``now`` defaults to now(CST); a tz-naive datetime is assumed CST.
    """
    now_dt = now or now_cn()
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=CN_TZ)
    else:
        now_dt = now_dt.astimezone(CN_TZ)

    today = now_dt.date().strftime("%Y-%m-%d")
    if not is_trading_day(today):
        return "closed"

    t = now_dt.time()
    if t < time(9, 30):
        return "pre_open"
    if time(9, 30) <= t < time(11, 30):
        return "in_session"
    if time(11, 30) <= t < time(13, 0):
        return "lunch_break"
    if time(13, 0) <= t < time(15, 0):
        return "in_session"
    return "post_close"


def is_trading_hours() -> bool:
    """True during live A-share sessions (09:30–11:30 or 13:00–15:00 CST)."""
    return cn_market_phase() == "in_session"


def cn_no_data_reason(date_str: str) -> str:
    """Human-readable explanation of why ``date_str`` has no bar yet."""
    if not is_trading_day(date_str):
        return "N/A：非交易日（A股休市）"
    today = cn_today_str()
    if date_str == today:
        phase = cn_market_phase()
        if phase == "pre_open":
            return "N/A：今日尚未开盘"
        if phase in ("in_session", "lunch_break"):
            return "N/A：今日盘中，日线未收盘（可参考实时价）"
        if phase == "post_close":
            return "N/A：今日已收盘，数据源尚未更新"
    return "N/A：该交易日暂无数据（可能停牌或数据延迟）"


def reset_calendar_cache() -> None:
    """Drop the in-memory calendar cache (for tests)."""
    global _cal_loaded_at, _all_days_cache
    with _all_days_lock:
        _all_days_cache = None
        _cal_loaded_at = 0.0
