"""Market-data sync daemon + one-shot sync entry points.

A single daemon thread (``start_market_sync_daemon``) wakes every 60s and, on
trading days after 15:05 CST, runs ``run_daily_sync(today)`` to pull that day's
bars / dragon-tiger / pools / ETF / fund-premium into :mod:`market_store`.

``run_daily_sync`` is also the engine behind the manual API and the backfill
CLI — it is idempotent (per-code ``last_daily_date`` short-circuits, INSERT OR
REPLACE for snapshots) and resumable (Ctrl-C persists everything upserted so
the next tick/invocation continues where it left off).

Daily-K is sliced into ≤28-day tpdog windows (the upstream caps each call at
one month). Only fully-elapsed bars (``trade_date < today``) are persisted — a
provisional intraday bar is never frozen into history. The full A-share daily
universe is the default scope; capital-flow defaults to the watchlist (full
market is opt-in via ``universe="all"`` because of credit cost).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.data.market_store import MarketStore, get_market_store

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))
_SYNC_TICK_SECONDS = 60
_TICK_DEADLINE_SECONDS = 600  # one sync tick must not run longer than 10 min
_POOL_TYPES = ("limitup", "limitdown", "strong", "fire", "secnew")
_SLEEP_BETWEEN_CALLS = 0.05  # tpdog caps at 30 calls/sec

_daemon_started = False
_daemon_lock = threading.Lock()

# Module-level handle the daemon uses; the manual API / CLI pass their own
# (usually the same singleton) so run_daily_sync is testable in isolation.
_default_store: Optional[MarketStore] = None


# ----------------------------------------------------------------------
# Time helpers
# ----------------------------------------------------------------------


def _now_cst() -> datetime:
    return datetime.now(_CST)


def _today_cst_str() -> str:
    return _now_cst().strftime("%Y-%m-%d")


def _is_trading_day(date_str: str) -> bool:
    """Holiday-aware trading-day check via the unified calendar (akshare-backed).

    tpdog's ``trading_day/is`` single-day endpoint returns unreliable data
    (mid-2026 weeks show Fri=False), so we route through trade_calendar which
    uses the sina exchange calendar instead. Falls back to the weekend rule.
    """
    try:
        from src.data.trade_calendar import is_trading_day
        return is_trading_day(date_str)
    except Exception:  # noqa: BLE001
        # Weekend-rule fallback.
        from datetime import datetime as _dt
        try:
            return _dt.strptime(date_str, "%Y-%m-%d").weekday() < 5
        except ValueError:
            return False


def _is_st_or_delisting_name(name: str) -> tuple[bool, bool]:
    upper = str(name or "").upper()
    is_st = "ST" in upper
    is_delisting = "退" in str(name or "")
    return is_st, is_delisting


def _sync_security_master_tushare(store: MarketStore) -> int:
    """Sync A-share security metadata via Tushare stock_basic."""
    token = os.getenv("TUSHARE_TOKEN", "").strip()
    if not token or token.lower() == "your-tushare-token":
        return 0
    try:
        import pandas as pd
        import tushare as ts
    except Exception:  # noqa: BLE001
        return 0

    fields = (
        "ts_code,symbol,name,area,industry,market,exchange,list_status,"
        "list_date,delist_date,is_hs"
    )
    frames = []
    try:
        api = ts.pro_api(token)
        for status in ("L", "P", "D"):
            df = api.stock_basic(exchange="", list_status=status, fields=fields)
            if df is not None and not df.empty:
                frames.append(df)
    except Exception as exc:  # noqa: BLE001
        logger.debug("tushare stock_basic failed: %s", exc)
        return 0
    if not frames:
        return 0

    df = pd.concat(frames, ignore_index=True)
    rows: list[dict] = []
    for _, r in df.iterrows():
        code = str(r.get("ts_code", "") or "").upper()
        if not code:
            continue
        name = str(r.get("name", "") or "")
        exchange = str(r.get("exchange", "") or "").upper()
        list_status = str(r.get("list_status", "") or "L").upper()
        is_st, name_delisting = _is_st_or_delisting_name(name)
        rows.append(
            {
                "code": code,
                "symbol": r.get("symbol"),
                "name": name,
                "area": r.get("area"),
                "industry": r.get("industry"),
                "market": r.get("market"),
                "exchange": exchange,
                "list_status": list_status,
                "list_date": r.get("list_date"),
                "delist_date": r.get("delist_date"),
                "is_hs": r.get("is_hs"),
                "is_st": is_st,
                "is_delisting": name_delisting or list_status == "D",
                "is_bj": exchange == "BSE" or code.endswith(".BJ"),
                "is_active": list_status == "L",
            }
        )
    written = store.upsert_security_master(rows)
    if written:
        logger.info("security master synced from tushare: %d rows", written)
    return written


def _sync_security_master_tpdog(store: MarketStore) -> int:
    """Fallback active A-share security metadata via TPDog stocks/list."""
    from src.data.tpdog_client import call

    rows: list[dict] = []
    for market, suffix, exchange in (("sh", ".SH", "SSE"), ("sz", ".SZ", "SZSE"), ("bj", ".BJ", "BSE")):
        try:
            content = call("stocks/list", type=market)
        except Exception as exc:  # noqa: BLE001
            logger.debug("stocks/list type=%s failed: %s", market, exc)
            continue
        for r in content:
            code = str(r.get("code", "")).strip()
            if len(code) != 6 or not code.isdigit():
                continue
            name = str(r.get("name", "") or "").strip()
            is_st, is_delisting = _is_st_or_delisting_name(name)
            rows.append(
                {
                    "code": code + suffix,
                    "symbol": code,
                    "name": name,
                    "market": r.get("market") or market,
                    "exchange": exchange,
                    "list_status": "L",
                    "is_st": is_st,
                    "is_delisting": is_delisting,
                    "is_bj": suffix == ".BJ",
                    "is_active": True,
                }
            )
    return store.upsert_security_master(rows)


def _sync_security_master(store: MarketStore) -> int:
    written = _sync_security_master_tushare(store)
    if written:
        return written
    return _sync_security_master_tpdog(store)


def _all_a_share_codes(store: MarketStore | None = None, *, default_only: bool = False) -> list[str]:
    """Return the full A-share code universe (sh + sz) from tpdog stocks/list.

    Codes are normalized to project form (``600206`` → ``600206.SH``). Returns
    [] on any failure (caller treats empty as "skip daily-K this tick").
    """
    if store is not None:
        try:
            rows = store.list_security_master(default_only=default_only)
            codes = [
                r["code"]
                for r in rows
                if r.get("is_active") and not r.get("is_bj")
            ]
            if codes:
                return codes
        except Exception:  # noqa: BLE001
            logger.debug("security_master code lookup failed", exc_info=True)

    from src.data.tpdog_client import call

    out: list[str] = []
    for market, prefix in (("sh", ".SH"), ("sz", ".SZ")):
        try:
            rows = call("stocks/list", type=market)
        except Exception as exc:  # noqa: BLE001
            logger.debug("stocks/list type=%s failed: %s", market, exc)
            continue
        for r in rows:
            code = str(r.get("code", "")).strip()
            if len(code) == 6 and code.isdigit():
                out.append(code + prefix)
    return out


def _to_tpdog_code(code: str) -> str | None:
    """Project code → tpdog (600206.SH → sh.600206); BJ → None."""
    upper = code.upper()
    if upper.endswith(".BJ"):
        return None
    if upper.endswith(".SH"):
        return "sh." + upper[:-3]
    if upper.endswith(".SZ"):
        return "sz." + upper[:-3]
    digits = code.replace(".", "")
    if len(digits) == 6 and digits.isdigit():
        if digits[0] in ("4", "8"):
            return None
        return ("sh." if digits[0] == "6" else "sz.") + digits
    return None


# ----------------------------------------------------------------------
# Per-dataset sync
# ----------------------------------------------------------------------


def _fetch_daily_range_rows(tpdog_code: str, start: str, end: str) -> list[dict]:
    """Fetch raw tpdog daily-K content rows over [start, end], 28-day sliced.

    Returns the full content dicts (with total_amt/rise_rate/t_rate) so the
    store can persist every column — NOT the 5-col DataFrame the read path uses.
    """
    from src.data.tpdog_client import call

    start_ts = datetime.strptime(start, "%Y-%m-%d")
    end_ts = datetime.strptime(end, "%Y-%m-%d")
    out: list[dict] = []
    cursor = end_ts
    while cursor >= start_ts:
        slice_start = max(start_ts, cursor - timedelta(days=27))
        content = call(
            "stock_his/daily",
            code=tpdog_code,
            start=slice_start.strftime("%Y-%m-%d"),
            end=cursor.strftime("%Y-%m-%d"),
        )
        if content:
            out.extend(content)
        cursor = slice_start - timedelta(days=1)
        time.sleep(_SLEEP_BETWEEN_CALLS)
    return out


def _sync_daily_tushare_by_date(
    store: MarketStore,
    trade_date: str,
    *,
    codes: Optional[list[str]] = None,
) -> int:
    """Fetch one settled A-share date in bulk via Tushare ``daily``.

    Tushare supports ``daily(trade_date=YYYYMMDD)`` for the whole market, which
    is the right shape for the daily after-close job. It avoids the expensive
    per-code TPDog loop; TPDog remains the fallback when Tushare is unavailable.
    """
    token = os.getenv("TUSHARE_TOKEN", "").strip()
    if not token or token.lower() == "your-tushare-token":
        return 0
    try:
        import tushare as ts
    except Exception:  # noqa: BLE001
        logger.debug("tushare package unavailable; daily bulk skipped")
        return 0

    wanted = {c.upper() for c in codes} if codes else None
    try:
        api = ts.pro_api(token)
        df = api.daily(trade_date=trade_date.replace("-", ""))
    except Exception as exc:  # noqa: BLE001
        logger.debug("tushare daily bulk failed for %s: %s", trade_date, exc)
        return 0
    if df is None or df.empty:
        return 0

    grouped: dict[str, list[dict]] = {}
    for _, r in df.iterrows():
        code = str(r.get("ts_code", "")).upper()
        if not code or (wanted is not None and code not in wanted):
            continue
        grouped.setdefault(code, []).append(
            {
                "date": trade_date,
                "open": r.get("open"),
                "high": r.get("high"),
                "low": r.get("low"),
                "close": r.get("close"),
                "volume": r.get("vol"),
                "total_amt": r.get("amount"),
                "rise_rate": r.get("pct_chg"),
            }
        )

    written = 0
    for code, rows in grouped.items():
        written += store.upsert_daily_bars(code, rows)
    if written:
        logger.info("tushare daily bulk wrote %d rows for %s", written, trade_date)
    return written


def _latest_settled_date_for_sync(trade_date: str, today_str: str) -> str | None:
    """Return the newest date safe to persist for this sync invocation.

    Historical dates are already settled. Today's bar is only settled after the
    market reaches post-close; before then it is still a provisional intraday
    bar and must stay out of ``bars_daily``.
    """
    if trade_date < today_str:
        return trade_date
    if trade_date > today_str:
        return None
    try:
        from src.data.trade_calendar import cn_market_phase
        return trade_date if cn_market_phase() == "post_close" else None
    except Exception:  # noqa: BLE001
        now = _now_cst()
        minutes = now.hour * 60 + now.minute
        return trade_date if minutes >= 15 * 60 + 5 else None


def _sync_daily_for_code(
    store: MarketStore, code: str, trade_date: str, today_str: str,
    lookback_days: int = 90, deadline: float | None = None,
) -> int:
    """Incremental daily-K upsert for one code. Returns rows written."""
    if deadline is not None and time.monotonic() > deadline:
        return 0
    tpdog_code = _to_tpdog_code(code)
    if tpdog_code is None:
        return 0
    last = store.last_daily_date(code)
    if last == trade_date:
        return 0  # already current
    # Start from the day after the last bar we have, or lookback_days back.
    if last:
        start = (datetime.strptime(last, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        start = (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    if start > trade_date:
        return 0
    try:
        rows = _fetch_daily_range_rows(tpdog_code, start, trade_date)
    except Exception as exc:  # noqa: BLE001 — skip this code, keep going
        logger.debug("daily sync %s failed: %s", code, exc)
        return 0
    latest_settled = _latest_settled_date_for_sync(trade_date, today_str)
    if latest_settled is None:
        return 0
    rows = [r for r in rows if r.get("date") and r["date"] <= latest_settled]
    if not rows:
        return 0
    return store.upsert_daily_bars(code, rows)


def _sync_dragon_tiger(store: MarketStore, trade_date: str) -> int:
    if store.has_dragon_tiger(trade_date):
        return 0
    from src.data.tpdog_client import call

    try:
        rows = call("board/bill", date=trade_date)
    except Exception as exc:  # noqa: BLE001
        logger.debug("dragon-tiger sync failed: %s", exc)
        return 0
    time.sleep(_SLEEP_BETWEEN_CALLS)
    if not rows:
        return 0
    return store.upsert_dragon_tiger(trade_date, rows)


def _sync_pools(store: MarketStore, trade_date: str) -> int:
    from src.data.tpdog_client import call

    total = 0
    for ptype in _POOL_TYPES:
        if store.has_pool(ptype, trade_date):
            continue
        try:
            rows = call(f"pool/v1/{ptype}/list", date=trade_date)
        except Exception as exc:  # noqa: BLE001
            logger.debug("pool %s sync failed: %s", ptype, exc)
            continue
        time.sleep(_SLEEP_BETWEEN_CALLS)
        if rows:
            total += store.upsert_pool(ptype, trade_date, rows)
    return total


def _sync_etf_daily(store: MarketStore, etf_codes: list[str], trade_date: str, today_str: str) -> int:
    from src.data.tpdog_client import call

    total = 0
    for code in etf_codes:
        try:
            rows = call("etf_his/daily", code=f"etf.{code}", start=trade_date, end=trade_date)
        except Exception as exc:  # noqa: BLE001
            logger.debug("etf %s sync failed: %s", code, exc)
            continue
        time.sleep(_SLEEP_BETWEEN_CALLS)
        latest_settled = _latest_settled_date_for_sync(trade_date, today_str)
        rows = [r for r in rows if latest_settled and r.get("date") and r["date"] <= latest_settled]
        if rows:
            total += store.upsert_etf_daily(code, rows)
    return total


def _sync_fund_premium_snapshot(store: MarketStore, trade_date: str) -> int:
    """Persist the day's fund-premium scan result as a close snapshot."""
    try:
        from src.data.fund_premium import scan_fund_premium
        rows = scan_fund_premium(fund_type="ALL", limit=200, use_cache=False)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fund-premium snapshot failed: %s", exc)
        return 0
    if not rows:
        return 0
    for r in rows:
        if not r.get("trade_date"):
            r["trade_date"] = trade_date
    return store.upsert_fund_premium(trade_date, rows)


# ----------------------------------------------------------------------
# run_daily_sync — the engine
# ----------------------------------------------------------------------


def run_daily_sync(
    trade_date: str,
    *,
    store: Optional[MarketStore] = None,
    codes: Optional[list[str]] = None,
    datasets: Optional[set[str]] = None,
    universe: str = "default",
    etf_codes: Optional[list[str]] = None,
    deadline_seconds: int = _TICK_DEADLINE_SECONDS,
    lookback_days: int = 90,
) -> dict[str, int]:
    """Pull and persist one trade_date's market data. Idempotent + resumable.

    Args:
        trade_date: The trading day to sync (YYYY-MM-DD).
        store: Store instance (default: the process singleton).
        codes: Daily-K code universe; None → full A-share via stocks/list.
        datasets: Which datasets to sync (subset of daily/dragon/pool/etf/
            premium/capital). None → all except capital (capital opt-in).
        universe: ``"default"`` (capital → watchlist) or ``"all"``.
        etf_codes: ETF codes for the etf dataset; None → skipped.
        deadline_seconds: Hard wall-clock cap for the whole call.

    Returns a dict of rows written per dataset.
    """
    store = store or get_market_store()
    if store is None:
        return {}
    today_str = _today_cst_str()
    deadline = time.monotonic() + deadline_seconds
    datasets = datasets or {"master", "daily", "dragon", "pool", "etf", "premium"}
    result: dict[str, int] = {}

    if "master" in datasets:
        result["master"] = _sync_security_master(store)

    if "daily" in datasets:
        latest_settled = _latest_settled_date_for_sync(trade_date, today_str)
        if latest_settled is None:
            result["daily"] = 0
            universe_codes = []
        elif latest_settled == trade_date:
            daily_codes = (
                codes if codes is not None else _all_a_share_codes(
                    store, default_only=(universe == "default")
                )
            )
            written = _sync_daily_tushare_by_date(store, trade_date, codes=daily_codes)
            if written:
                result["daily"] = written
                universe_codes = []
            else:
                universe_codes = daily_codes
        else:
            universe_codes = (
                codes if codes is not None else _all_a_share_codes(
                    store, default_only=(universe == "default")
                )
            )
        if universe_codes:
            written = 0
            for i, code in enumerate(universe_codes):
                written += _sync_daily_for_code(
                    store,
                    code,
                    trade_date,
                    today_str,
                    lookback_days=lookback_days,
                    deadline=deadline,
                )
                if i and i % 200 == 0:
                    logger.info("daily sync: %d/%d codes (%d rows)", i, len(universe_codes), written)
                if time.monotonic() > deadline:
                    logger.warning("daily sync hit deadline at %d/%d codes", i, len(universe_codes))
                    break
            result["daily"] = written

    def _run(name: str, fn: Any) -> None:
        """Run one dataset, capture failures so they don't block siblings."""
        if time.monotonic() > deadline or name not in datasets:
            return
        try:
            result[name] = fn()
        except Exception:  # noqa: BLE001 — one dataset failing must not abort the rest
            logger.exception("market_sync: %s dataset failed", name)

    _run("dragon", lambda: _sync_dragon_tiger(store, trade_date))
    _run("pool", lambda: _sync_pools(store, trade_date))
    if etf_codes:
        _run("etf", lambda: _sync_etf_daily(store, etf_codes, trade_date, today_str))
    _run("premium", lambda: _sync_fund_premium_snapshot(store, trade_date))

    return result


# ----------------------------------------------------------------------
# Daemon
# ----------------------------------------------------------------------


def _maybe_run_daily_sync(store: MarketStore) -> None:
    """One tick: decide whether to run today's sync and run it.

    Fires only when the market is past close on a trading day (holiday-aware
    via the unified calendar). Previously this hard-coded ``>= 15:05 CST`` and a
    raw weekday check; ``cn_market_phase()=="post_close"`` captures both
    correctly and handles holiday/makeup-trading days automatically.
    """
    from src.data.trade_calendar import cn_market_phase

    if cn_market_phase() != "post_close":
        return
    today = _today_cst_str()
    # Skip if already synced today.
    if store.get_meta(f"daemon:{today}"):
        return
    try:
        logger.info("market-sync daemon: starting sync for %s", today)
        run_daily_sync(today, store=store)
        store.set_meta(f"daemon:{today}", _now_cst().isoformat())
        logger.info("market-sync daemon: done for %s", today)
    except Exception:  # noqa: BLE001 — never kill the loop
        logger.exception("market-sync daemon tick failed")


def _loop() -> None:
    from src.data.rate_limiter import mark_background, reset_background

    # Mark this daemon thread as background so the limiter reserves slots for
    # foreground requests and reclaims permits if a sync stalls.
    token = mark_background(True)
    store = get_market_store()
    if store is None:
        logger.warning("market-sync daemon: store unavailable, exiting loop")
        reset_background(token)
        return
    while True:
        try:
            _maybe_run_daily_sync(store)
        except Exception:  # noqa: BLE001
            logger.exception("market-sync loop error")
        time.sleep(_SYNC_TICK_SECONDS)


def start_market_sync_daemon() -> None:
    """Start the background market-sync daemon thread (idempotent)."""
    global _daemon_started
    with _daemon_lock:
        if _daemon_started:
            return
        _daemon_started = True
    thread = threading.Thread(target=_loop, name="market-sync", daemon=True)
    thread.start()
    logger.info("market-sync daemon started")
