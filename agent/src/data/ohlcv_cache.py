"""Persistent OHLCV cache using pickle files.

Stores daily bars per A-share symbol under ``~/.vibe-trading/cache/ohlcv/``.
On fetch, loads cached data first, then only pulls new dates from mootdx.

Cache policy:
- During A-share trading hours (Mon-Fri 9:30-15:00 CST): always fetch fresh from mootdx
- Outside trading hours: use cache if last bar is from the most recent trading day
- Weekends/holidays: use Friday's data if within 2 days

Usage::

    from src.data.ohlcv_cache import fetch_with_cache

    df = fetch_with_cache("000001.SZ")  # returns DataFrame with all cached bars
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict

import pandas as pd

logger = logging.getLogger(__name__)

_CACHE_ROOT = Path.home() / ".vibe-trading" / "cache" / "ohlcv"

# A-share trading hours kept as a thin shim over the unified calendar module.
_CST = timezone(timedelta(hours=8))


def _is_trading_hours() -> bool:
    """Return True if we are currently within A-share live sessions.

    Delegates to :mod:`src.data.trade_calendar` (holiday-aware, CST-correct).
    Falls back to a weekend/minutes check if the calendar module is unavailable.
    """
    try:
        from src.data.trade_calendar import is_trading_hours
        return is_trading_hours()
    except Exception:  # noqa: BLE001 — never break reads on a calendar import
        now_cst = datetime.now(_CST)
        if now_cst.weekday() >= 5:
            return False
        minutes = now_cst.hour * 60 + now_cst.minute
        return (9 * 60 + 30 <= minutes <= 11 * 60 + 30) or (13 * 60 <= minutes <= 15 * 60)


def _needs_today_fetch(last_cached_date: pd.Timestamp) -> bool:
    """Return True if we should try to fetch today's bar from mootdx.

    During trading hours: always try (today's bar changes throughout the day).
    Outside trading hours: only if cache doesn't have today's bar yet.
    """
    today = pd.Timestamp.now().normalize()
    if last_cached_date.normalize() >= today:
        # Cache already has today's bar. Only re-fetch during trading hours.
        return _is_trading_hours()
    # Cache doesn't have today's bar — always try to fetch it
    return True


def _use_cached_historical(cached: pd.DataFrame, days: int) -> pd.DataFrame | None:
    """Return cached historical data if we have enough bars, else None."""
    if cached is not None and not cached.empty and len(cached) >= min(days, 20):
        return cached
    return None


def _cache_path(code: str) -> Path:
    """Return the cache file path for a stock code."""
    return _CACHE_ROOT / f"{code.replace('.', '_')}.pkl"


def load_cached(code: str) -> pd.DataFrame | None:
    """Load cached OHLCV data for a stock, or None if not cached."""
    path = _cache_path(code)
    if path.exists():
        try:
            df = pd.read_pickle(path)
            if not df.empty:
                if df.index.name != "date" and "date" in df.columns:
                    df["date"] = pd.to_datetime(df["date"])
                    df = df.set_index("date")
                return df.sort_index()
        except Exception:
            logger.debug("Failed to read cache for %s, will re-fetch", code)
    return None


def save_cache(code: str, df: pd.DataFrame) -> None:
    """Save OHLCV data to disk cache as pickle."""
    if df is None or df.empty:
        return
    _CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    path = _cache_path(code)
    try:
        df.to_pickle(path)
    except Exception as exc:
        logger.debug("Failed to save cache for %s: %s", code, exc)


def _fetch_bars_mootdx(codes: list[str], offset: int = 100) -> dict[str, pd.DataFrame]:
    """Fetch daily bars from mootdx for multiple codes. Returns {code: DataFrame}.

    Codes mootdx can't serve (unavailable, empty, or 北交所) fall back to tpdog
    daily-K so cloud deployments without a reachable TDX server still get data.
    """
    results: dict[str, pd.DataFrame] = {}
    remaining: list[str] = []
    try:
        from src.data.mootdx_helper import get_quotes
        client = get_quotes(timeout=15)
        for code in codes:
            raw = code.replace(".SZ", "").replace(".SH", "")
            try:
                df = client.bars(symbol=raw, frequency=9, start=0, offset=offset)
                if df is not None and not df.empty:
                    df = df.rename(columns={"open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume"})
                    df.index = pd.to_datetime(df.index)
                    df = df.sort_index()
                    results[code] = df[["open", "high", "low", "close", "volume"]]
                else:
                    remaining.append(code)
            except Exception:
                logger.debug("mootdx bars failed for %s", code)
                remaining.append(code)
    except Exception as exc:
        logger.warning("mootdx unavailable: %s", exc)
        remaining = list(codes)

    # Fallback: anything mootdx didn't serve → tpdog daily-K
    for code in remaining:
        df = _fetch_bars_tpdog(code, offset=offset)
        if df is not None and not df.empty:
            results[code] = df
            logger.debug("tpdog fallback served %s (%d bars)", code, len(df))
    return results


def _fetch_incremental_mootdx(code: str, since_date: str) -> pd.DataFrame | None:
    """Fetch only new bars from mootdx since a given date (YYYY-MM-DD).

    Falls back to tpdog daily-K when mootdx is unavailable or returns nothing.
    """
    try:
        from src.data.mootdx_helper import get_quotes
        client = get_quotes(timeout=10)
        raw = code.replace(".SZ", "").replace(".SH", "")
        df = client.bars(symbol=raw, frequency=9, start=0, offset=30)
        if df is not None and not df.empty:
            df = df.rename(columns={"open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume"})
            df.index = pd.to_datetime(df.index)
            df = df.sort_index()
            cutoff = pd.Timestamp(since_date)
            new = df[df.index > cutoff]
            return new[["open", "high", "low", "close", "volume"]] if not new.empty else None
    except Exception:
        pass
    # mootdx unavailable/empty → tpdog fallback
    return _fetch_incremental_tpdog(code, since_date)


def _to_tpdog_code(code: str) -> str | None:
    """Convert project code (000001.SZ / 600206.SH) to tpdog form (sz.000001).

    tpdog uses lowercase ``sh./sz.`` prefixes with the bare ticker. Only A-share
    沪深 symbols are supported (北交所 BJ has no tpdog coverage here).
    """
    upper = code.upper()
    if upper.endswith(".SH"):
        return "sh." + upper[:-3]
    if upper.endswith(".SZ"):
        return "sz." + upper[:-3]
    # Bare 6-digit: 6xxxxx 沪, 0xxxxx/3xxxxx 深
    digits = code.replace(".", "")
    if len(digits) == 6 and digits.isdigit():
        return ("sh." if digits[0] == "6" else "sz.") + digits
    return None


def _tpdog_bars_to_df(rows: list[dict]) -> pd.DataFrame:
    """Normalize tpdog daily-K content rows into the mootdx OHLCV shape.

    Index = date (Timestamp), columns open/high/low/close/volume — matching what
    ``_fetch_bars_mootdx`` / ``_fetch_incremental_mootdx`` return.
    """
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return df[["open", "high", "low", "close", "volume"]]


def _fetch_bars_tpdog(code: str, start: str | None = None, end: str | None = None,
                       offset: int = 100) -> pd.DataFrame | None:
    """Fetch daily bars from tpdog as a mootdx fallback.

    tpdog only exposes a date-range API (no "latest N"), and caps each call at
    a 1-month window (error 5002). When called without an explicit window we
    walk back from today in ~28-day slices until ``offset`` calendar days are
    covered, then concatenate.
    """
    try:
        from src.data.tpdog_client import call
    except ImportError:
        return None
    tpdog_code = _to_tpdog_code(code)
    if tpdog_code is None:
        logger.debug("tpdog fallback: unsupported code %s", code)
        return None

    if start and end:
        # Explicit window — split into ≤28-day slices (tpdog caps at 1 month).
        return _tpdog_range_sliced(tpdog_code, start, end)

    # No window: look back `offset` calendar days from today, in 28-day slices.
    today = pd.Timestamp.now().normalize()
    lookback_days = max(offset, 30)
    window_start = (today - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    return _tpdog_range_sliced(tpdog_code, window_start, today.strftime("%Y-%m-%d"))


def _tpdog_range_sliced(tpdog_code: str, start: str, end: str) -> pd.DataFrame | None:
    """Fetch a [start, end] range from tpdog, splitting into ≤28-day slices."""
    from src.data.tpdog_client import call

    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    frames: list[pd.DataFrame] = []
    # Walk from the end backward in 28-day slices until we cover start_ts.
    cursor = end_ts
    while cursor >= start_ts:
        slice_start = max(start_ts, cursor - pd.Timedelta(days=27))
        try:
            content = call(
                "stock_his/daily",
                code=tpdog_code,
                start=slice_start.strftime("%Y-%m-%d"),
                end=cursor.strftime("%Y-%m-%d"),
            )
        except Exception as exc:  # noqa: BLE001 — fallback must never raise
            logger.debug("tpdog slice %s..%s failed: %s", slice_start.date(), cursor.date(), exc)
            break
        if content:
            frames.append(_tpdog_bars_to_df(content))
        cursor = slice_start - pd.Timedelta(days=1)
    if not frames:
        return None
    df = pd.concat(frames)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df if not df.empty else None


def _fetch_incremental_tpdog(code: str, since_date: str) -> pd.DataFrame | None:
    """tpdog fallback for incremental today fetch — bars strictly after since_date."""
    df = _fetch_bars_tpdog(code, start=since_date)
    if df is None or df.empty:
        return None
    cutoff = pd.Timestamp(since_date)
    new = df[df.index > cutoff]
    return new if not new.empty else None


# ---------------------------------------------------------------------------
# Market-DB integration (primary read + fire-and-forget persist)
# ---------------------------------------------------------------------------


def _market_store():
    """Return the MarketStore singleton, or None when DB reads are off/unavailable."""
    try:
        from src.data.market_store import db_read_enabled, get_market_store
    except Exception:  # noqa: BLE001
        return None
    if not db_read_enabled():
        return None
    try:
        return get_market_store()
    except Exception:  # noqa: BLE001 — never break reads on a DB init failure
        return None


def _try_db_read(code: str, days: int) -> pd.DataFrame | None:
    """Try reading OHLCV from the market DB before hitting the network.

    Returns a DataFrame when DB has ≥ min(days,20) settled bars AND no intraday
    supplement is needed. When DB has data but the latest bar is stale and today
    is a trading day, the caller's normal incremental path still runs — so this
    only short-circuits the fully-satisfied case.
    """
    store = _market_store()
    if store is None:
        return None
    try:
        df = store.get_daily_bars(code, days=days)
    except Exception:  # noqa: BLE001
        logger.debug("market_db read failed for %s", code, exc_info=True)
        return None
    if df is None or df.empty or len(df) < min(days, 20):
        return None
    if _needs_today_fetch(df.index.max()):
        return None  # let the caller supplement today's bar via the normal path
    return df.tail(days)


def _try_persist_daily(code: str, df: pd.DataFrame) -> None:
    """Persist settled daily bars to the market DB (fire-and-forget).

    Only bars strictly before today are written — a provisional intraday bar
    is never frozen. Skips entirely when the DB already covers the cache's
    latest bar (the common steady-state case), and otherwise only writes dates
    newer than the DB's last — so the hot pickle-hit path does no redundant
    writes once a code is back-filled. Any failure is logged and swallowed.
    """
    store = _market_store()
    if store is None or df is None or df.empty:
        return
    try:
        today = pd.Timestamp.now(tz=None).normalize().strftime("%Y-%m-%d")
        rows = []
        for ts, r in df.iterrows():
            date_str = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
            if date_str >= today:
                continue
            rows.append({
                "date": date_str,
                "open": r.get("open"), "high": r.get("high"),
                "low": r.get("low"), "close": r.get("close"),
                "volume": r.get("volume"),
            })
        if not rows:
            return
        # Incremental: only write dates after the DB's last settled bar. When
        # the DB is already current (rows all ≤ last), this is a no-op.
        last = store.last_daily_date(code)
        if last:
            rows = [r for r in rows if r["date"] > last]
            if not rows:
                return
        store.upsert_daily_bars(code, rows)
    except Exception:  # noqa: BLE001
        logger.debug("market_db persist failed for %s", code, exc_info=True)


def fetch_with_cache(code: str, days: int = 90) -> pd.DataFrame | None:
    """Get OHLCV data for a stock, using disk cache + incremental today fetch.

    Strategy:
    0. Market DB (primary): if settled bars cover the window, return immediately
    1. Historical bars (yesterday and earlier): disk cache
    2. Today's bar: incremental fetch from mootdx if needed (trading hours or missing)
    3. Merge and save; if mootdx fails fall back to cache
    Persist any freshly-fetched settled bars to the market DB (fire-and-forget).
    """
    # 0. Market DB short-circuit (no intraday supplement needed).
    db_df = _try_db_read(code, days)
    if db_df is not None and not db_df.empty:
        return db_df

    cached = load_cached(code)

    if cached is not None and not cached.empty:
        last_date = cached.index.max()

        if _needs_today_fetch(last_date):
            # Try to get only today's new bars
            since_str = last_date.strftime("%Y-%m-%d")
            new_bars = _fetch_incremental_mootdx(code, since_str)
            if new_bars is not None and not new_bars.empty:
                merged = pd.concat([cached, new_bars])
                merged = merged[~merged.index.duplicated(keep="last")].sort_index()
                save_cache(code, merged)
                _try_persist_daily(code, merged)
                return merged.tail(days)

        # No new bars needed, or incremental fetch failed — use cache.
        # Persist to the market DB too (pickle hit is the common path; without
        # this the DB never gets back-filled from existing pickle caches).
        if len(cached) >= min(days, 20):
            _try_persist_daily(code, cached)
            return cached.tail(days)

    # No cache or too few bars — full historical fetch + save
    result = _fetch_bars_mootdx([code], offset=max(days + 20, 100))
    df = result.get(code)
    if df is not None and not df.empty:
        # Merge with any partial cache
        if cached is not None and not cached.empty:
            df = pd.concat([cached, df])
            df = df[~df.index.duplicated(keep="last")].sort_index()
        save_cache(code, df)
        _try_persist_daily(code, df)
        return df.tail(days)

    # mootdx completely failed — return cached if available
    if cached is not None and not cached.empty:
        return cached.tail(days)
    return None


def fetch_batch(codes: list[str], days: int = 90) -> dict[str, pd.DataFrame]:
    """Fetch OHLCV for multiple codes — historical from cache, today incremental.

    Strategy per code:
    1. Load historical bars from disk cache
    2. If today's bar is needed (trading hours or missing), incremental fetch
    3. Merge today's bar with cached history; save back
    4. If no cache at all, full fetch from mootdx
    5. mootdx failure → fall back to cache
    """
    results: dict[str, pd.DataFrame] = {}
    need_full_fetch: list[str] = []
    need_incremental: dict[str, pd.DataFrame] = {}  # code → cached_df

    for code in codes:
        # Market DB short-circuit (no intraday supplement needed).
        db_df = _try_db_read(code, days)
        if db_df is not None and not db_df.empty:
            results[code] = db_df
            continue
        cached = load_cached(code)
        if cached is not None and not cached.empty:
            last_date = cached.index.max()
            if _needs_today_fetch(last_date):
                need_incremental[code] = cached
            else:
                # Pickle hit (no intraday supplement needed). Persist to the
                # market DB so existing caches back-fill it over time.
                _try_persist_daily(code, cached)
                results[code] = cached.tail(days)
        else:
            need_full_fetch.append(code)

    # Incremental fetch for codes that have cache but need today's bar
    for code, cached in need_incremental.items():
        last_date = cached.index.max()
        since_str = last_date.strftime("%Y-%m-%d")
        new_bars = _fetch_incremental_mootdx(code, since_str)
        if new_bars is not None and not new_bars.empty:
            merged = pd.concat([cached, new_bars])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
            save_cache(code, merged)
            _try_persist_daily(code, merged)
            results[code] = merged.tail(days)
        else:
            # Incremental failed — use cache as-is
            results[code] = cached.tail(days)

    # Full fetch for codes with no cache at all
    if need_full_fetch:
        fresh = _fetch_bars_mootdx(need_full_fetch, offset=max(days + 20, 100))
        for code, df in fresh.items():
            if df is not None and not df.empty:
                save_cache(code, df)
                _try_persist_daily(code, df)
                results[code] = df.tail(days)
        # Codes that failed full fetch — try any cached fallback
        for code in need_full_fetch:
            if code not in results:
                cached = load_cached(code)
                if cached is not None:
                    results[code] = cached.tail(days)

    return results
