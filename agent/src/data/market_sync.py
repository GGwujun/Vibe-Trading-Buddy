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
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.data.market_store import MarketStore, get_market_store

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))
_SYNC_TICK_SECONDS = 60
_TICK_DEADLINE_SECONDS = 600  # one sync tick must not run longer than 10 min
_POOL_TYPES = ("limitup", "limitdown", "strong", "fire", "secnew", "previous")
_SLEEP_BETWEEN_CALLS = 0.05  # tpdog caps at 30 calls/sec
_DEFAULT_INDEX_CODES = (
    "000001.SH",  # 上证指数
    "399001.SZ",  # 深证成指
    "399006.SZ",  # 创业板指
    "000300.SH",  # 沪深300
    "000905.SH",  # 中证500
    "000852.SH",  # 中证1000
    "000688.SH",  # 科创50
    "899050.BJ",  # 北证50
)

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
    token = _env_token("TUSHARE_TOKEN")
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
        # stock_basic can be limited as low as 1 request/hour. Pull the active
        # universe in one call; delisted/pending history can be backfilled by a
        # separate low-frequency job if we need it later.
        df = api.stock_basic(exchange="", list_status="L", fields=fields)
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


def _sync_trade_calendar_tpdog(store: MarketStore, trade_date: str) -> int:
    """Sync the full-year CN trading calendar from TPDog."""
    from src.data.tpdog_client import call

    year = trade_date[:4]
    try:
        rows = call("trading_day/year", year=year)
    except Exception as exc:  # noqa: BLE001
        logger.debug("tpdog trading_day/year failed for %s: %s", year, exc)
        _set_sync_error(store, "calendar", "tpdog.trading_day_year", exc)
        return 0
    payload = [
        {"date": r.get("date"), "is_trading": bool(r.get("is_trading")), "market": "CN"}
        for r in rows
        if r.get("date")
    ]
    written = store.upsert_trade_calendar(payload, market="CN")
    if written:
        _clear_sync_error(store, "calendar", "tpdog.trading_day_year")
    return written


def _sync_index_master_tpdog(store: MarketStore) -> int:
    """Sync index universe from TPDog /api/zs/list."""
    rows: list[dict[str, Any]] = []
    for idx_type in ("zs", "zssh", "zssz"):
        try:
            content = _tpdog_root_call("zs/list", type=idx_type)
        except Exception as exc:  # noqa: BLE001
            logger.debug("tpdog zs/list failed for %s: %s", idx_type, exc)
            _set_sync_error(store, "index_master", f"tpdog.zs_list.{idx_type}", exc)
            continue
        for r in content:
            code = str(r.get("code") or "").strip()
            req_code = str(r.get("req_code") or f"{r.get('type') or idx_type}.{code}").strip()
            if not code:
                continue
            rows.append(
                {
                    "code": req_code.upper(),
                    "name": r.get("name"),
                    "type": r.get("type") or idx_type,
                    "req_code": req_code,
                }
            )
    written = store.upsert_index_master(rows)
    if written:
        _clear_sync_error(store, "index_master", "tpdog.zs_list")
    return written


_BOARD_TYPES = (("bki", "industry"), ("bkc", "concept"), ("bkr", "area"))


def _sync_board_master_tpdog(store: MarketStore) -> int:
    """Sync industry/concept/area board universe from TPDog /api/bk/list."""
    rows: list[dict[str, Any]] = []
    for tpdog_type, board_type in _BOARD_TYPES:
        try:
            content = _tpdog_root_call("bk/list", type=tpdog_type)
        except Exception as exc:  # noqa: BLE001
            logger.debug("tpdog bk/list failed for %s: %s", tpdog_type, exc)
            _set_sync_error(store, "board_master", f"tpdog.bk_list.{tpdog_type}", exc)
            continue
        for r in content:
            code = str(r.get("code") or "").strip()
            req_code = str(r.get("req_code") or f"{r.get('type') or tpdog_type}.{code}").strip()
            if not code:
                continue
            rows.append(
                {
                    "code": req_code,
                    "name": r.get("name"),
                    "board_type": board_type,
                    "type": r.get("type") or tpdog_type,
                    "req_code": req_code,
                }
            )
    written = store.upsert_board_master(rows)
    if written:
        _clear_sync_error(store, "board_master", "tpdog.bk_list")
    return written


def _sync_board_members_tpdog(store: MarketStore, *, limit: int | None = None) -> int:
    """Sync board constituents. Limit is for foreground/manual small runs."""
    from src.data.tpdog_client import call

    boards = store.list_board_master()
    if not boards:
        _sync_board_master_tpdog(store)
        boards = store.list_board_master()
    total = 0
    for i, board in enumerate(boards):
        if limit is not None and i >= limit:
            break
        req_code = str(board.get("req_code") or board.get("code") or "")
        if not req_code:
            continue
        try:
            rows = call("stocks/list_board", code=req_code)
        except Exception as exc:  # noqa: BLE001
            logger.debug("tpdog stocks/list_board failed for %s: %s", req_code, exc)
            _set_sync_error(store, "board_members", f"tpdog.list_board.{req_code}", exc)
            continue
        total += store.upsert_board_members(req_code, str(board.get("board_type") or ""), rows)
        time.sleep(_SLEEP_BETWEEN_CALLS)
    if total:
        _clear_sync_error(store, "board_members", "tpdog.list_board")
    return total


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


def _a_share_code(code: Any) -> str:
    raw = str(code or "").strip().upper()
    if "." in raw:
        return raw
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) != 6:
        return raw
    if digits.startswith(("6", "9")):
        return f"{digits}.SH"
    if digits.startswith(("4", "8")):
        return f"{digits}.BJ"
    return f"{digits}.SZ"


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _pick_column(df: Any, contains: tuple[str, ...], fallback_idx: int | None = None) -> str | None:
    columns = [str(c) for c in getattr(df, "columns", [])]
    for col in columns:
        if all(part in col for part in contains):
            return col
    if fallback_idx is not None and 0 <= fallback_idx < len(columns):
        return columns[fallback_idx]
    return None


def _column_at(df: Any, idx: int) -> str | None:
    columns = [str(c) for c in getattr(df, "columns", [])]
    return columns[idx] if 0 <= idx < len(columns) else None


def _env_token(name: str) -> str:
    token = os.getenv(name, "").strip()
    if token:
        return token
    env_path = os.getenv("VIBE_TRADING_ENV_PATH", "")
    if env_path:
        candidates = [env_path]
    else:
        candidates = [
            os.path.join(os.getcwd(), "agent", ".env"),
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".env"),
        ]
    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    raw = line.strip()
                    if not raw or raw.startswith("#") or "=" not in raw:
                        continue
                    key, value = raw.split("=", 1)
                    if key.strip() == name:
                        return value.strip().strip("\"'")
        except OSError:
            continue
    return ""


def _set_sync_error(store: MarketStore, dataset: str, source: str, exc: Any) -> None:
    message = str(exc)
    if len(message) > 500:
        message = message[:500]
    try:
        store.set_meta(
            f"sync_error:{dataset}:{source}",
            json.dumps(
                {
                    "dataset": dataset,
                    "source": source,
                    "message": message,
                    "at": _now_cst().isoformat(),
                },
                ensure_ascii=False,
            ),
        )
    except Exception:  # noqa: BLE001
        logger.debug("failed to persist sync error for %s/%s", dataset, source, exc_info=True)


def _clear_sync_error(store: MarketStore, dataset: str, source: str) -> None:
    try:
        store.set_meta(
            f"sync_error:{dataset}:{source}",
            json.dumps(
                {
                    "dataset": dataset,
                    "source": source,
                    "message": "",
                    "at": _now_cst().isoformat(),
                    "ok": True,
                },
                ensure_ascii=False,
            ),
        )
    except Exception:  # noqa: BLE001
        logger.debug("failed to clear sync error for %s/%s", dataset, source, exc_info=True)


def _tushare_api() -> Any | None:
    token = _env_token("TUSHARE_TOKEN")
    if not token or token.lower() == "your-tushare-token":
        return None
    try:
        import tushare as ts
        return ts.pro_api(token)
    except Exception as exc:  # noqa: BLE001
        logger.debug("tushare api unavailable: %s", exc)
        return None


def _pick_any_column(df: Any, names: tuple[str, ...]) -> str | None:
    columns = [str(c) for c in getattr(df, "columns", [])]
    lower = {c.lower(): c for c in columns}
    for name in names:
        if name in columns:
            return name
        if name.lower() in lower:
            return lower[name.lower()]
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
    token = _env_token("TUSHARE_TOKEN")
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
    return total + _sync_pools_akshare(store, trade_date)


def _pool_rows_from_akshare_df(df: Any, trade_date: str, *, source: str) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    code_col = _column_at(df, 1)
    name_col = _column_at(df, 2)
    pct_col = _column_at(df, 3)
    close_col = _column_at(df, 4)
    amount_col = _column_at(df, 6)
    seal_amount_col = _column_at(df, 9)
    first_limit_time_col = _column_at(df, 11)
    fire_count_col = _column_at(df, 12)
    limit_stat_col = _column_at(df, 13)
    c_times_col = _column_at(df, 14)
    industry_col = _column_at(df, 15)
    rows: list[dict[str, Any]] = []
    for _, raw in df.iterrows():
        code = _a_share_code(raw.get(code_col)) if code_col else ""
        if not code or len(code) < 6:
            continue
        c_times = int(_num(raw.get(c_times_col))) if c_times_col else 1
        rows.append({
            "date": trade_date,
            "code": code,
            "name": str(raw.get(name_col) or code) if name_col else code,
            "rise_rate": _num(raw.get(pct_col)) if pct_col else 0.0,
            "close": _num(raw.get(close_col)) if close_col else 0.0,
            "amount": _num(raw.get(amount_col)) if amount_col else 0.0,
            "seal_amount": _num(raw.get(seal_amount_col)) if seal_amount_col else 0.0,
            "first_limit_time": str(raw.get(first_limit_time_col) or "") if first_limit_time_col else "",
            "fire_count": int(_num(raw.get(fire_count_col))) if fire_count_col else 0,
            "limit_stat": str(raw.get(limit_stat_col) or "") if limit_stat_col else "",
            "c_times": max(1, c_times),
            "industry": str(raw.get(industry_col) or "") if industry_col else "",
            "source": source,
        })
    return rows


def _sync_pools_akshare(store: MarketStore, trade_date: str) -> int:
    try:
        import akshare as ak
    except Exception as exc:  # noqa: BLE001
        _set_sync_error(store, "stock_pool", "akshare", exc)
        return 0
    date_compact = trade_date.replace("-", "")
    total = 0
    sources = (
        ("limitup", "akshare.stock_zt_pool_em", lambda: ak.stock_zt_pool_em(date=date_compact)),
        ("limitdown", "akshare.stock_zt_pool_dtgc_em", lambda: ak.stock_zt_pool_dtgc_em(date=date_compact)),
        ("strong", "akshare.stock_zt_pool_strong_em", lambda: ak.stock_zt_pool_strong_em(date=date_compact)),
        ("fire", "akshare.stock_zt_pool_zbgc_em", lambda: ak.stock_zt_pool_zbgc_em(date=date_compact)),
        ("secnew", "akshare.stock_zt_pool_sub_new_em", lambda: ak.stock_zt_pool_sub_new_em(date=date_compact)),
        ("previous", "akshare.stock_zt_pool_previous_em", lambda: ak.stock_zt_pool_previous_em(date=date_compact)),
    )
    for pool_type, source, loader in sources:
        try:
            df = loader()
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s failed: %s", source, exc)
            _set_sync_error(store, "stock_pool", source, exc)
            continue
        rows = _pool_rows_from_akshare_df(df, trade_date, source=source)
        if rows:
            total += store.upsert_pool(pool_type, trade_date, rows)
            _clear_sync_error(store, "stock_pool", source)
        else:
            _set_sync_error(store, "stock_pool", source, "empty result")
    return total


def _sync_etf_daily_tushare_by_date(
    store: MarketStore,
    trade_date: str,
    *,
    etf_codes: Optional[list[str]] = None,
) -> int:
    """Fetch one settled ETF date in bulk via Tushare ``fund_daily``."""
    token = _env_token("TUSHARE_TOKEN")
    if not token or token.lower() == "your-tushare-token":
        return 0
    try:
        import tushare as ts
    except Exception:  # noqa: BLE001
        logger.debug("tushare package unavailable; ETF daily bulk skipped")
        return 0

    wanted = {c.upper() for c in etf_codes} if etf_codes else None
    try:
        api = ts.pro_api(token)
        df = api.fund_daily(trade_date=trade_date.replace("-", ""))
    except Exception as exc:  # noqa: BLE001
        logger.debug("tushare ETF daily bulk failed for %s: %s", trade_date, exc)
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
                "rise": r.get("pct_chg"),
            }
        )

    written = 0
    for code, rows in grouped.items():
        written += store.upsert_etf_daily(code, rows)
        store.upsert_fund_daily(code, rows)
    if written:
        logger.info("tushare ETF daily bulk wrote %d rows for %s", written, trade_date)
    return written


def _sync_stock_daily_basic_tushare_by_date(
    store: MarketStore,
    trade_date: str,
    *,
    codes: Optional[list[str]] = None,
) -> int:
    """Fetch one settled A-share valuation/turnover date via Tushare daily_basic."""
    token = _env_token("TUSHARE_TOKEN")
    if not token or token.lower() == "your-tushare-token":
        return 0
    try:
        import tushare as ts
    except Exception:  # noqa: BLE001
        logger.debug("tushare package unavailable; daily_basic skipped")
        return 0

    fields = (
        "ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,"
        "pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,total_share,float_share,"
        "free_share,total_mv,circ_mv"
    )
    try:
        api = ts.pro_api(token)
        df = api.daily_basic(trade_date=trade_date.replace("-", ""), fields=fields)
    except Exception as exc:  # noqa: BLE001
        logger.debug("tushare daily_basic failed for %s: %s", trade_date, exc)
        return 0
    if df is None or df.empty:
        return 0

    wanted = {c.upper() for c in codes} if codes else None
    rows: list[dict] = []
    for _, r in df.iterrows():
        code = str(r.get("ts_code", "")).upper()
        if not code or (wanted is not None and code not in wanted):
            continue
        rows.append(
            {
                "code": code,
                "trade_date": trade_date,
                "close": r.get("close"),
                "turnover_rate": r.get("turnover_rate"),
                "turnover_rate_f": r.get("turnover_rate_f"),
                "volume_ratio": r.get("volume_ratio"),
                "pe": r.get("pe"),
                "pe_ttm": r.get("pe_ttm"),
                "pb": r.get("pb"),
                "ps": r.get("ps"),
                "ps_ttm": r.get("ps_ttm"),
                "dv_ratio": r.get("dv_ratio"),
                "dv_ttm": r.get("dv_ttm"),
                "total_share": r.get("total_share"),
                "float_share": r.get("float_share"),
                "free_share": r.get("free_share"),
                "total_mv": r.get("total_mv"),
                "circ_mv": r.get("circ_mv"),
            }
        )
    written = store.upsert_stock_daily_basic(rows)
    if written:
        logger.info("tushare daily_basic wrote %d rows for %s", written, trade_date)
    return written


def _sync_etf_master_tushare(store: MarketStore) -> int:
    """Sync ETF metadata via Tushare etf_basic."""
    token = _env_token("TUSHARE_TOKEN")
    if not token or token.lower() == "your-tushare-token":
        return 0
    try:
        import tushare as ts
    except Exception:  # noqa: BLE001
        logger.debug("tushare package unavailable; etf_basic skipped")
        return 0

    fields = (
        "ts_code,csname,extname,cname,index_code,index_name,setup_date,list_date,"
        "list_status,exchange,mgr_name,custod_name,mgt_fee,etf_type"
    )
    try:
        api = ts.pro_api(token)
        df = api.etf_basic(list_status="L", fields=fields)
    except Exception as exc:  # noqa: BLE001
        logger.debug("tushare etf_basic failed: %s", exc)
        return 0
    if df is None or df.empty:
        return 0

    rows: list[dict] = []
    for _, r in df.iterrows():
        code = str(r.get("ts_code", "")).upper()
        if not code:
            continue
        rows.append(
            {
                "code": code,
                "csname": r.get("csname"),
                "extname": r.get("extname"),
                "cname": r.get("cname"),
                "index_code": r.get("index_code"),
                "index_name": r.get("index_name"),
                "setup_date": r.get("setup_date"),
                "list_date": r.get("list_date"),
                "list_status": r.get("list_status"),
                "exchange": r.get("exchange"),
                "mgr_name": r.get("mgr_name"),
                "custod_name": r.get("custod_name"),
                "mgt_fee": r.get("mgt_fee"),
                "etf_type": r.get("etf_type"),
            }
        )
    written = store.upsert_etf_master(rows)
    if written:
        logger.info("tushare etf_basic wrote %d rows", written)
    return written


def _sync_etf_master_tpdog(store: MarketStore) -> int:
    """Fallback ETF metadata via TPDog etfs/list."""
    from src.data.tpdog_client import call

    try:
        rows = call("etfs/list")
    except Exception as exc:  # noqa: BLE001
        logger.debug("tpdog etfs/list failed: %s", exc)
        return 0
    payload: list[dict] = []
    for r in rows:
        code = str(r.get("code", "")).strip()
        if not code:
            continue
        payload.append(
            {
                "code": code,
                "csname": r.get("name"),
                "cname": r.get("name"),
                "list_status": "L",
                "exchange": "SZSE" if code.startswith(("1", "3")) else "SSE",
                "etf_type": r.get("type") or "etf",
            }
        )
    written = store.upsert_etf_master(payload)
    if written:
        logger.info("tpdog etfs/list wrote %d rows", written)
    return written


def _sync_etf_master(store: MarketStore) -> int:
    written = _sync_etf_master_tushare(store)
    if written:
        return written
    return _sync_etf_master_tpdog(store)


def _sync_fund_master(store: MarketStore) -> int:
    """Refresh static fund metadata (code/name/type) once/day (post_close).

    ETF names come from the authoritative, daily-refreshed ``etf_master``;
    LOF names from ``fund_open_fund_daily_em`` (the 简称 column) via
    ``_fetch_lof_nav_em`` — a cheap openfund call, no quotes needed. This is
    intentionally separate from the 5-min premium scan: metadata barely
    changes, so a daily refresh matches the data's real cadence and keeps the
    market path write-light. Must run AFTER etf_master (it reads that table).
    """
    rows: list[dict] = []
    # ETF — straight from etf_master (already refreshed earlier in the tick).
    try:
        etf_rows = store._conn.execute(  # noqa: SLF001 — read-only sibling table
            "SELECT code, COALESCE(cname, extname, csname, code) AS name "
            "FROM etf_master"
        ).fetchall()
        for r in etf_rows:
            code = str(r["code"] or "").strip()
            name = str(r["name"] or "").strip()
            if code and name:
                rows.append({"code": code, "name": name, "type": "ETF"})
    except Exception as exc:  # noqa: BLE001
        logger.debug("fund_master ETF read failed: %s", exc)

    # LOF — openfund daily 简称 (names only, no quotes).
    try:
        from src.data.fund_premium import _fetch_lof_nav_em
        nav = _fetch_lof_nav_em() or {}
        for code, info in nav.items():
            name = str((info or {}).get("name") or "").strip()
            if code and name:
                rows.append({"code": code, "name": name, "type": "LOF"})
    except Exception as exc:  # noqa: BLE001
        logger.debug("fund_master LOF name fetch failed: %s", exc)

    if not rows:
        return 0
    written = store.upsert_fund_master(rows)
    logger.info("fund_master refreshed: %d rows", written)
    return written


def _sync_etf_share_size_tushare_by_date(store: MarketStore, trade_date: str) -> int:
    """Fetch ETF share/size snapshot for one date via Tushare etf_share_size."""
    token = _env_token("TUSHARE_TOKEN")
    if not token or token.lower() == "your-tushare-token":
        return 0
    try:
        import pandas as pd
        import tushare as ts
    except Exception:  # noqa: BLE001
        logger.debug("tushare package unavailable; etf_share_size skipped")
        return 0

    api = ts.pro_api(token)
    frames = []
    for exchange in ("", "SSE", "SZSE"):
        try:
            kwargs: dict[str, Any] = {"trade_date": trade_date.replace("-", "")}
            if exchange:
                kwargs["exchange"] = exchange
            df = api.etf_share_size(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.debug("tushare etf_share_size failed for %s/%s: %s", trade_date, exchange or "ALL", exc)
            continue
        if df is not None and not df.empty:
            frames.append(df)
            if not exchange:
                break
    if not frames:
        return 0
    df = pd.concat(frames, ignore_index=True).drop_duplicates(
        subset=["ts_code", "trade_date"], keep="last"
    )
    rows: list[dict] = []
    for _, r in df.iterrows():
        code = str(r.get("ts_code", "")).upper()
        if not code:
            continue
        rows.append(
            {
                "code": code,
                "trade_date": trade_date,
                "name": r.get("name"),
                "total_share": r.get("total_share"),
                "total_size": r.get("total_size"),
                "nav": r.get("nav"),
                "close": r.get("close"),
                "exchange": r.get("exchange"),
            }
        )
    written = store.upsert_etf_share_size(rows)
    if written:
        logger.info("tushare etf_share_size wrote %d rows for %s", written, trade_date)
    return written


def _sync_etf_share_size_akshare_by_date(store: MarketStore, trade_date: str) -> int:
    """Fetch ETF share snapshots from exchange disclosure via AkShare."""
    try:
        import akshare as ak
    except Exception:  # noqa: BLE001
        logger.debug("akshare unavailable; etf_share_size fallback skipped")
        return 0

    date_key = trade_date.replace("-", "")
    rows: list[dict] = []

    try:
        df_sse = ak.fund_etf_scale_sse(date=date_key)
    except Exception as exc:  # noqa: BLE001
        logger.debug("akshare fund_etf_scale_sse failed for %s: %s", trade_date, exc)
    else:
        if df_sse is not None and not df_sse.empty:
            for _, r in df_sse.iterrows():
                values = list(r.values)
                if len(values) < 6:
                    continue
                rows.append(
                    {
                        "code": str(values[1] or "").strip(),
                        "trade_date": trade_date,
                        "name": values[2],
                        "total_share": _num(values[5]),
                        "exchange": "SSE",
                    }
                )

    try:
        df_szse = ak.fund_scale_daily_szse(start_date=date_key, end_date=date_key, symbol="ETF")
    except Exception as exc:  # noqa: BLE001
        logger.debug("akshare fund_scale_daily_szse failed for %s: %s", trade_date, exc)
    else:
        if df_szse is not None and not df_szse.empty:
            for _, r in df_szse.iterrows():
                values = list(r.values)
                if len(values) < 4:
                    continue
                rows.append(
                    {
                        "code": str(values[1] or "").strip(),
                        "trade_date": trade_date,
                        "name": values[2],
                        "total_share": _num(values[3]),
                        "exchange": "SZSE",
                    }
                )

    rows = [r for r in rows if r.get("code")]
    if not rows:
        return 0
    written = store.upsert_etf_share_size(rows)
    if written:
        logger.info("akshare ETF share size wrote %d rows for %s", written, trade_date)
    return written


def _sync_etf_share_size_by_date(store: MarketStore, trade_date: str) -> int:
    written = _sync_etf_share_size_tushare_by_date(store, trade_date)
    if written:
        return written
    return _sync_etf_share_size_akshare_by_date(store, trade_date)


def _sync_index_daily_tushare(
    store: MarketStore,
    trade_date: str,
    *,
    index_codes: Optional[list[str]] = None,
) -> int:
    """Fetch core index daily OHLCV rows for one date via Tushare index_daily."""
    token = _env_token("TUSHARE_TOKEN")
    if not token or token.lower() == "your-tushare-token":
        return 0
    try:
        import tushare as ts
    except Exception:  # noqa: BLE001
        logger.debug("tushare package unavailable; index_daily skipped")
        return 0

    codes = index_codes or list(_DEFAULT_INDEX_CODES)
    api = ts.pro_api(token)
    total = 0
    date_key = trade_date.replace("-", "")
    for code in codes:
        try:
            df = api.index_daily(ts_code=code, start_date=date_key, end_date=date_key)
        except Exception as exc:  # noqa: BLE001
            logger.debug("tushare index_daily failed for %s/%s: %s", code, trade_date, exc)
            continue
        if df is None or df.empty:
            continue
        rows = []
        for _, r in df.iterrows():
            rows.append(
                {
                    "code": str(r.get("ts_code", code)).upper(),
                    "trade_date": trade_date,
                    "open": r.get("open"),
                    "high": r.get("high"),
                    "low": r.get("low"),
                    "close": r.get("close"),
                    "pre_close": r.get("pre_close"),
                    "change": r.get("change"),
                    "pct_chg": r.get("pct_chg"),
                    "volume": r.get("vol"),
                    "total_amt": r.get("amount"),
                }
            )
        total += store.upsert_index_daily(code, rows)
    if total:
        logger.info("tushare index_daily wrote %d rows for %s", total, trade_date)
    return total


def _to_tpdog_index_code(code: str) -> str:
    digits = code.split(".", 1)[0]
    if code.upper().endswith(".SZ"):
        return f"zssz.{digits}"
    return f"zssh.{digits}"


def _sync_index_daily_tpdog(
    store: MarketStore,
    trade_date: str,
    *,
    index_codes: Optional[list[str]] = None,
) -> int:
    """Fallback index daily K via TPDog stock/daily, which supports index codes."""
    from src.data.tpdog_client import call

    codes = index_codes or list(_DEFAULT_INDEX_CODES)
    total = 0
    for code in codes:
        try:
            rows = call("stock/daily", code=_to_tpdog_index_code(code), date=trade_date)
        except Exception as exc:  # noqa: BLE001
            logger.debug("tpdog stock/daily index failed for %s/%s: %s", code, trade_date, exc)
            continue
        payload = []
        for r in rows:
            payload.append(
                {
                    "code": code,
                    "trade_date": r.get("date") or trade_date,
                    "open": r.get("open"),
                    "high": r.get("high"),
                    "low": r.get("low"),
                    "close": r.get("close"),
                    "change": r.get("rise"),
                    "pct_chg": r.get("rise_rate"),
                    "volume": r.get("volume"),
                    "total_amt": r.get("total_amt"),
                }
            )
        total += store.upsert_index_daily(code, payload)
    if total:
        logger.info("tpdog index daily wrote %d rows for %s", total, trade_date)
    return total


# akshare stock_zh_index_daily_em 的 symbol 前缀映射（项目 code -> akshare symbol）
_AK_INDEX_SYMBOL = {
    "000001.SH": "sh000001", "399001.SZ": "sz399001", "399006.SZ": "sz399006",
    "000300.SH": "sh000300", "000905.SH": "sh000905", "000852.SH": "sh000852",
    "000688.SH": "sh000688", "899050.BJ": "bj899050",
}


def _backfill_index_history_akshare(
    store: MarketStore,
    *,
    index_codes: Optional[list[str]] = None,
    min_rows: int = 60,
) -> int:
    """Backfill long-term index history via akshare when DB rows are thin.

    tpdog/spot only keep the latest day or two; the K-line sparkline needs
    months of history. akshare's ``stock_zh_index_daily_em`` returns the full
    series (2019+), so any index below ``min_rows`` gets a full refresh.
    Idempotent: upserts by (code, trade_date), safe to run repeatedly.
    """
    codes = index_codes or list(_DEFAULT_INDEX_CODES)
    import akshare as ak

    total = 0
    for code in codes:
        sym = _AK_INDEX_SYMBOL.get(code)
        if not sym:
            continue
        existing = store.count_index_daily(code)
        if existing >= min_rows:
            continue
        try:
            df = ak.stock_zh_index_daily_em(symbol=sym)
        except Exception as exc:  # noqa: BLE001
            logger.debug("akshare index history %s failed: %s", code, exc)
            continue
        if df is None or df.empty:
            continue
        # akshare 历史接口无涨跌幅列，按日期排序后用前日 close 自算 pct_chg。
        df = df.copy()
        date_col = "date" if "date" in df.columns else "trade_date"
        df[date_col] = df[date_col].astype(str)
        df = df.sort_values(date_col).reset_index(drop=True)
        payload = []
        prev_close = None
        for _, r in df.iterrows():
            d = str(r.get(date_col) or "")
            if not d:
                continue
            close = r.get("close")
            change = round(close - prev_close, 4) if (prev_close is not None and close is not None) else None
            pct = round(change / prev_close * 100, 2) if (prev_close and change is not None) else None
            payload.append({
                "code": code,
                "trade_date": d[:10],
                "open": r.get("open"), "high": r.get("high"), "low": r.get("low"),
                "close": close, "pre_close": prev_close, "change": change,
                "pct_chg": pct, "volume": r.get("volume"), "total_amt": None,
            })
            prev_close = close if close is not None else prev_close
        n = store.upsert_index_daily(code, payload)
        total += n
        logger.info("akshare index history backfilled %s: %d rows (had %d)", code, n, existing)
    return total


def _sync_index_daily_akshare_missing(
    store: MarketStore,
    trade_date: str,
    *,
    index_codes: Optional[list[str]] = None,
) -> int:
    """Backfill any default index still missing `trade_date`.

    Two-pronged fallback when tushare daily left gaps:
    1. 北证50 (899050.BJ) — via its own daily endpoint (only reliable source).
    2. The rest — via the realtime spot endpoint ``stock_zh_index_spot_em``.
       On a non-trading day (weekend/holiday) spot returns the last trading
       day's frozen close, which equals `trade_date`'s settle, so it is safe
       to stamp with `trade_date`. open/high/low are unavailable from spot and
       left None (the dashboard only reads close + pct_chg).
    """
    codes = index_codes or list(_DEFAULT_INDEX_CODES)
    if not codes:
        return 0

    # Skip codes that already have this trade_date — never overwrite good data.
    missing = [c for c in codes if not store.has_index_daily(c, trade_date)]
    if not missing:
        return 0

    import akshare as ak

    written = 0

    # --- 北证50: dedicated daily endpoint (spot list omits it) ---
    if "899050.BJ" in missing:
        try:
            df = ak.stock_zh_index_daily(symbol="bj899050")
        except Exception as exc:  # noqa: BLE001
            logger.debug("akshare bj899050 index daily failed for %s: %s", trade_date, exc)
            df = None
        if df is not None and not df.empty:
            working = df.copy()
            working["date"] = working["date"].astype(str)
            working = working[working["date"] <= trade_date]
            if not working.empty:
                latest = working.tail(2)
                prev_close = _num(latest.iloc[0].get("close")) if len(latest) >= 2 else None
                row = latest.iloc[-1]
                close = _num(row.get("close"))
                change = close - prev_close if prev_close else None
                pct_chg = (change / prev_close * 100) if prev_close else None
                written += store.upsert_index_daily("899050.BJ", [{
                    "code": "899050.BJ",
                    "trade_date": str(row.get("date")),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": close,
                    "pre_close": prev_close,
                    "change": change,
                    "pct_chg": pct_chg,
                    "volume": row.get("volume"),
                }])

    # --- the rest: realtime spot (frozen close = trade_date settle on holidays) ---
    spot_codes = [c for c in missing if c != "899050.BJ"]
    if spot_codes:
        # index_daily code -> akshare spot 代码 (前6位)
        want = {c.replace(".SH", "").replace(".SZ", "").replace(".BJ", ""): c for c in spot_codes}
        try:
            df = ak.stock_zh_index_spot_em(symbol="沪深重要指数")
        except Exception as exc:  # noqa: BLE001
            logger.debug("akshare index spot failed for %s: %s", trade_date, exc)
            df = None
        if df is not None and not df.empty:
            cols = list(df.columns)
            code_col = "代码" if "代码" in cols else cols[0]
            close_col = next((c for c in cols if "最新价" in c), None)
            pct_col = next((c for c in cols if "涨跌幅" in c), None)
            if code_col and close_col:
                rows_by_code: dict[str, dict] = {}
                for _, r in df.iterrows():
                    ak_code = str(r[code_col]).strip()
                    idx_code = want.get(ak_code)
                    if not idx_code:
                        continue
                    close = _num(r[close_col])
                    if not close:
                        continue
                    rows_by_code[idx_code] = {
                        "code": idx_code,
                        "trade_date": trade_date,
                        "open": None, "high": None, "low": None,
                        "close": close,
                        "pre_close": None, "change": None,
                        "pct_chg": _num(r[pct_col]) if pct_col else None,
                        "volume": None,
                    }
                for idx_code, row in rows_by_code.items():
                    written += store.upsert_index_daily(idx_code, [row])
    return written


def _sync_index_daily(
    store: MarketStore,
    trade_date: str,
    *,
    index_codes: Optional[list[str]] = None,
) -> int:
    selected_codes = index_codes or list(_DEFAULT_INDEX_CODES)
    written = _sync_index_daily_tushare(store, trade_date, index_codes=selected_codes)
    missing_codes = [code for code in selected_codes if not store.has_index_daily(code, trade_date)]
    if not missing_codes:
        return written
    written += _sync_index_daily_tpdog(store, trade_date, index_codes=missing_codes)
    missing_codes = [code for code in selected_codes if not store.has_index_daily(code, trade_date)]
    written += _sync_index_daily_akshare_missing(store, trade_date, index_codes=missing_codes)
    return written


def _sync_board_daily_tpdog(store: MarketStore, trade_date: str, *, limit: int | None = None) -> int:
    """Sync board daily K via TPDog stock/daily using board req_code."""
    from src.data.tpdog_client import call

    boards = store.list_board_master()
    if not boards:
        _sync_board_master_tpdog(store)
        boards = store.list_board_master()
    total = 0
    for i, board in enumerate(boards):
        if limit is not None and i >= limit:
            break
        req_code = str(board.get("req_code") or board.get("code") or "")
        if not req_code:
            continue
        try:
            rows = call("stock/daily", code=req_code, date=trade_date)
        except Exception as exc:  # noqa: BLE001
            logger.debug("tpdog board stock/daily failed for %s/%s: %s", req_code, trade_date, exc)
            _set_sync_error(store, "board_daily", f"tpdog.stock_daily.{req_code}", exc)
            continue
        payload = []
        for r in rows:
            payload.append(
                {
                    **r,
                    "name": r.get("name") or board.get("name"),
                    "board_type": board.get("board_type"),
                    "type": board.get("board_type"),
                }
            )
        total += store.upsert_board_daily(req_code, payload)
        time.sleep(_SLEEP_BETWEEN_CALLS)
    if total:
        _clear_sync_error(store, "board_daily", "tpdog.stock_daily")
    return total


def _sync_etf_daily(store: MarketStore, etf_codes: list[str], trade_date: str, today_str: str) -> int:
    from src.data.tpdog_client import call

    total = 0
    for code in etf_codes:
        if store.has_etf_daily(code, trade_date):
            continue
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
            store.upsert_fund_daily(code, rows)
        if total and total % 50 == 0:
            logger.info("ETF daily sync %s: wrote %d rows", trade_date, total)
    return total


def _sync_fund_daily_from_etf_daily(store: MarketStore, trade_date: str) -> int:
    """Mirror persisted ETF daily rows into the fund_daily unified table."""
    try:
        rows = store._conn.execute(  # noqa: SLF001 - local store bridge
            "SELECT code, trade_date, open, high, low, close, volume, total_amt, rise "
            "FROM etf_daily WHERE trade_date = ?",
            (trade_date,),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug("fund_daily mirror from etf_daily failed: %s", exc)
        return 0
    total = 0
    for r in rows:
        total += store.upsert_fund_daily(
            r["code"],
            [
                {
                    "trade_date": r["trade_date"],
                    "open": r["open"],
                    "high": r["high"],
                    "low": r["low"],
                    "close": r["close"],
                    "volume": r["volume"],
                    "total_amt": r["total_amt"],
                    "rise": r["rise"],
                }
            ],
        )
    return total


def _sync_realtime_quotes_tpdog(store: MarketStore, trade_date: str) -> int:
    """Sync intraday stock quote/fund-flow rows into realtime_quote_snapshot."""
    from src.data.tpdog_client import call

    rows: list[dict[str, Any]] = []
    snapshot_at = _now_cst().isoformat()
    for zs_type, suffix in (("zssh", ".SH"), ("zssz", ".SZ"), ("zsbj", ".BJ")):
        try:
            content = call("current/funds", zs_type=zs_type, sort=1, t=1)
        except Exception as exc:  # noqa: BLE001
            logger.debug("tpdog realtime current/funds failed for %s: %s", zs_type, exc)
            _set_sync_error(store, "realtime", f"tpdog.current_funds.{zs_type}", exc)
            continue
        for raw in content:
            code = str(raw.get("code") or "").strip()
            if len(code) != 6 or not code.isdigit():
                continue
            rows.append(
                {
                    "code": code + suffix,
                    "name": raw.get("name"),
                    "price": raw.get("price") or raw.get("close"),
                    "pre_close": raw.get("yt_close") or raw.get("pre_close"),
                    "open": raw.get("open"),
                    "high": raw.get("high"),
                    "low": raw.get("low"),
                    "volume": raw.get("volume"),
                    "total_amt": raw.get("total_amt") or raw.get("amount"),
                    "rise": raw.get("rise"),
                    "rise_rate": raw.get("rise_rate") or raw.get("change_pct"),
                    "turnover_rate": raw.get("t_rate"),
                    "source": "tpdog.current/funds",
                    "snapshot_at": snapshot_at,
                }
            )
    written = store.upsert_realtime_quotes(trade_date, rows, snapshot_at=snapshot_at)
    if written:
        _clear_sync_error(store, "realtime", "tpdog.current_funds")
        return written
    return _sync_realtime_quotes_akshare(store, trade_date)


def _sync_realtime_quotes_akshare(store: MarketStore, trade_date: str) -> int:
    """Fallback realtime A-share quotes via akshare spot."""
    try:
        import akshare as ak
    except Exception as exc:  # noqa: BLE001
        _set_sync_error(store, "realtime", "akshare.import", exc)
        return 0

    attempts: list[tuple[str, Any]] = []
    if hasattr(ak, "stock_zh_a_spot_em"):
        attempts.append(("akshare.stock_zh_a_spot_em", ak.stock_zh_a_spot_em))
    if hasattr(ak, "stock_zh_a_spot"):
        attempts.append(("akshare.stock_zh_a_spot", ak.stock_zh_a_spot))

    df = None
    source = "akshare.stock_spot"
    for candidate_source, loader in attempts:
        source = candidate_source
        try:
            df = loader()
        except Exception as exc:  # noqa: BLE001
            _set_sync_error(store, "realtime", candidate_source, exc)
            continue
        if df is not None and not df.empty:
            break
        _set_sync_error(store, "realtime", candidate_source, "empty result")
        df = None

    if df is None or df.empty:
        _set_sync_error(store, "realtime", "akshare.stock_spot", "all spot fallbacks failed")
        return 0

    code_col = _pick_column(df, ("代码",), 1) or _pick_any_column(df, ("code", "symbol"))
    name_col = _pick_column(df, ("名称",), 2) or _pick_any_column(df, ("name",))
    price_col = _pick_column(df, ("最新价",), 3) or _pick_any_column(df, ("price", "trade", "close"))
    pre_close_col = _pick_column(df, ("昨收",), None) or _pick_any_column(df, ("pre_close", "yt_close"))
    open_col = _pick_column(df, ("今开",), None) or _pick_column(df, ("开盘",), None) or _pick_any_column(df, ("open",))
    high_col = _pick_column(df, ("最高",), None) or _pick_any_column(df, ("high",))
    low_col = _pick_column(df, ("最低",), None) or _pick_any_column(df, ("low",))
    volume_col = _pick_column(df, ("成交量",), None) or _pick_any_column(df, ("volume",))
    amount_col = _pick_column(df, ("成交额",), None) or _pick_column(df, ("成交金额",), None) or _pick_any_column(df, ("amount", "turnover"))
    rise_col = _pick_column(df, ("涨跌额",), None) or _pick_any_column(df, ("change", "rise"))
    rise_rate_col = _pick_column(df, ("涨跌幅",), None) or _pick_any_column(df, ("change_pct", "pct_chg"))
    turnover_col = _pick_column(df, ("换手率",), None) or _pick_any_column(df, ("turnover_rate", "t_rate"))
    if not code_col or not price_col:
        _set_sync_error(store, "realtime", source, f"missing columns: {list(df.columns)}")
        return 0

    rows: list[dict[str, Any]] = []
    snapshot_at = _now_cst().isoformat()
    for _, raw in df.iterrows():
        code = _a_share_code(raw.get(code_col))
        if not code or len("".join(ch for ch in code if ch.isdigit())) != 6:
            continue
        price = _num(raw.get(price_col))
        if price <= 0:
            continue
        rows.append({
            "code": code,
            "name": raw.get(name_col) if name_col else None,
            "price": price,
            "pre_close": raw.get(pre_close_col) if pre_close_col else None,
            "open": raw.get(open_col) if open_col else None,
            "high": raw.get(high_col) if high_col else None,
            "low": raw.get(low_col) if low_col else None,
            "volume": raw.get(volume_col) if volume_col else None,
            "total_amt": raw.get(amount_col) if amount_col else None,
            "rise": raw.get(rise_col) if rise_col else None,
            "rise_rate": raw.get(rise_rate_col) if rise_rate_col else None,
            "turnover_rate": raw.get(turnover_col) if turnover_col else None,
            "source": source,
            "snapshot_at": snapshot_at,
        })
    written = store.upsert_realtime_quotes(trade_date, rows, snapshot_at=snapshot_at)
    if written:
        _clear_sync_error(store, "realtime", source)
    else:
        _set_sync_error(store, "realtime", source, "empty mapped result")
    return written


def _sync_daily_from_realtime_snapshot(
    store: MarketStore,
    trade_date: str,
    *,
    codes: Optional[list[str]] = None,
) -> int:
    """Persist settled daily bars from the latest realtime quote snapshot.

    This is a post-close degraded fallback for environments where paid daily
    sources are unavailable. It keeps downstream scanners from repeatedly
    trading against stale daily bars when realtime snapshots are fresher.
    """
    wanted = {str(c).upper() for c in codes} if codes else None
    rows_by_code: dict[str, dict[str, Any]] = {}
    for quote in store.get_realtime_quotes(trade_date, limit=10000):
        code = str(quote.get("code") or "").upper()
        if not code or (wanted is not None and code not in wanted):
            continue
        price = _num(quote.get("price"))
        if price <= 0:
            continue
        open_price = _num(quote.get("open")) or price
        high = max(_num(quote.get("high")) or price, price, open_price)
        low_values = [v for v in (_num(quote.get("low")), price, open_price) if v > 0]
        low = min(low_values) if low_values else price
        rows_by_code[code] = {
            "date": trade_date,
            "open": open_price,
            "high": high,
            "low": low,
            "close": price,
            "volume": quote.get("volume"),
            "total_amt": quote.get("total_amt"),
            "rise_rate": quote.get("rise_rate"),
            "name": quote.get("name"),
        }

    written = 0
    for code, row in rows_by_code.items():
        written += store.upsert_daily_bars(code, [row])
    if written:
        logger.warning(
            "daily sync used realtime snapshot fallback for %s (%d rows)",
            trade_date,
            written,
        )
    return written


def _sync_stock_capital_tushare_by_date(store: MarketStore, trade_date: str) -> int:
    """Fetch one settled stock money-flow date in bulk via Tushare ``moneyflow``."""
    token = _env_token("TUSHARE_TOKEN")
    if not token or token.lower() == "your-tushare-token":
        return 0
    try:
        import tushare as ts
    except Exception:  # noqa: BLE001
        logger.debug("tushare package unavailable; moneyflow skipped")
        return 0

    try:
        api = ts.pro_api(token)
        df = api.moneyflow(trade_date=trade_date.replace("-", ""))
    except Exception as exc:  # noqa: BLE001
        logger.debug("tushare moneyflow failed for %s: %s", trade_date, exc)
        return 0
    if df is None or df.empty:
        return 0

    written = 0
    for _, r in df.iterrows():
        code = str(r.get("ts_code", "")).upper()
        if not code:
            continue
        buy_lg = _num(r.get("buy_lg_amount"))
        buy_elg = _num(r.get("buy_elg_amount"))
        sell_lg = _num(r.get("sell_lg_amount"))
        sell_elg = _num(r.get("sell_elg_amount"))
        buy_sm = _num(r.get("buy_sm_amount"))
        sell_sm = _num(r.get("sell_sm_amount"))
        row = {
            "date": trade_date,
            "m_in": buy_lg + buy_elg,
            "m_out": sell_lg + sell_elg,
            "m_net": r.get("net_mf_amount"),
            "r_in": buy_sm,
            "r_out": sell_sm,
            "r_net": buy_sm - sell_sm,
            "buy_sm_amount": r.get("buy_sm_amount"),
            "sell_sm_amount": r.get("sell_sm_amount"),
            "buy_md_amount": r.get("buy_md_amount"),
            "sell_md_amount": r.get("sell_md_amount"),
            "buy_lg_amount": r.get("buy_lg_amount"),
            "sell_lg_amount": r.get("sell_lg_amount"),
            "buy_elg_amount": r.get("buy_elg_amount"),
            "sell_elg_amount": r.get("sell_elg_amount"),
            "net_mf_vol": r.get("net_mf_vol"),
            "net_mf_amount": r.get("net_mf_amount"),
        }
        written += store.upsert_stock_capital(code, trade_date, 1, [row])
    if written:
        logger.info("tushare moneyflow wrote %d rows for %s", written, trade_date)
    return written


def _sync_stock_capital_tpdog_current(store: MarketStore, trade_date: str) -> int:
    """Fallback whole-market stock money flow via TPDog current/funds."""
    from src.data.tpdog_client import call

    if _latest_settled_date_for_sync(trade_date, _today_cst_str()) != trade_date:
        return 0
    total = 0
    for zs_type, suffix in (("zssh", ".SH"), ("zssz", ".SZ"), ("zsbj", ".BJ")):
        try:
            rows = call("current/funds", zs_type=zs_type, sort=1)
        except Exception as exc:  # noqa: BLE001
            logger.debug("tpdog current/funds failed for %s: %s", zs_type, exc)
            continue
        for r in rows:
            code = str(r.get("code", "")).strip()
            if len(code) != 6 or not code.isdigit():
                continue
            row = {
                "date": trade_date,
                "m_in": r.get("m_in"),
                "m_out": r.get("m_out"),
                "m_net": r.get("m_net"),
                "r_in": r.get("r_in"),
                "r_out": r.get("r_out"),
                "r_net": r.get("r_net"),
                "m_in_ratio": r.get("m_in_ratio"),
                "m_out_ratio": r.get("m_out_ratio"),
                "r_in_ratio": r.get("r_in_ratio"),
                "r_out_ratio": r.get("r_out_ratio"),
                "name": r.get("name"),
                "source": "tpdog_current_funds",
            }
            total += store.upsert_stock_capital(code + suffix, trade_date, 1, [row])
    if total:
        logger.info("tpdog current/funds wrote %d rows for %s", total, trade_date)
    return total


def _tpdog_stock_req_code(code: str) -> str:
    raw = str(code or "").strip().upper()
    symbol = raw.split(".", 1)[0]
    if raw.endswith(".SH") or symbol.startswith(("5", "6", "9")):
        return f"sh.{symbol}"
    if raw.endswith(".BJ") or symbol.startswith(("4", "8")):
        return f"bj.{symbol}"
    return f"sz.{symbol}"


def _sync_stock_capital_tpdog_history(store: MarketStore, trade_date: str) -> int:
    """Fallback whole-market stock money flow via TPDog historical fund/stock."""
    from src.data.tpdog_client import call

    securities = store.list_security_master()
    total = 0
    for security in securities:
        code = str(security.get("code") or "").upper()
        if not code:
            continue
        if code.endswith(".BJ"):
            continue
        try:
            existing = store._conn.execute(  # noqa: SLF001 - cheap resumability check
                "SELECT 1 FROM stock_capital_flow WHERE code = ? AND trade_date = ? LIMIT 1",
                (code, trade_date),
            ).fetchone()
        except Exception:  # noqa: BLE001
            existing = None
        if existing:
            continue
        try:
            rows = call("fund/stock", code=_tpdog_stock_req_code(code), date=trade_date, period=1)
        except Exception as exc:  # noqa: BLE001
            logger.debug("tpdog fund/stock failed for %s/%s: %s", code, trade_date, exc)
            continue
        payload = []
        for row in rows:
            payload.append(
                {
                    **row,
                    "date": row.get("end") or row.get("date") or trade_date,
                    "source": "tpdog_fund_stock",
                }
            )
        if payload:
            total += store.upsert_stock_capital(code, trade_date, 1, payload)
        time.sleep(_SLEEP_BETWEEN_CALLS)
    if total:
        logger.info("tpdog fund/stock wrote %d rows for %s", total, trade_date)
    return total


def _sync_stock_capital(store: MarketStore, trade_date: str) -> int:
    written = _sync_stock_capital_tushare_by_date(store, trade_date)
    if written:
        return written
    written = _sync_stock_capital_tpdog_history(store, trade_date)
    if written:
        return written
    return _sync_stock_capital_tpdog_current(store, trade_date)


def _sync_stock_capital_rank(store: MarketStore, trade_date: str) -> int:
    """Persist whole-market stock main-force inflow/outflow rankings."""
    try:
        import akshare as ak
        df = ak.stock_individual_fund_flow_rank(indicator="今日")
    except Exception as exc:  # noqa: BLE001
        logger.debug("stock capital rank sync failed: %s", exc)
        return 0
    if df is None or df.empty:
        return 0

    code_col = _pick_column(df, ("代码",), 1)
    name_col = _pick_column(df, ("名称",), 2)
    net_col = _pick_column(df, ("主力净流入", "净额"), None) or _pick_column(df, ("主力", "净流入"), 3)
    pct_col = _pick_column(df, ("涨跌幅",), 4)
    if not code_col or not net_col:
        return 0

    rows: list[dict[str, Any]] = []
    working = df.copy()
    working["_main_net"] = working[net_col].map(_num)
    working["_change_pct"] = working[pct_col].map(_num) if pct_col else 0.0

    def _row(raw: Any, rank_type: str) -> dict[str, Any]:
        return {
            "rank_type": rank_type,
            "code": str(raw.get(code_col, "")),
            "name": str(raw.get(name_col, "")) if name_col else "",
            "main_net": raw.get("_main_net", 0.0),
            "change_pct": raw.get("_change_pct", 0.0),
            "source": "akshare.stock_individual_fund_flow_rank",
        }

    rows.extend(_row(raw, "inflow") for _, raw in working.sort_values("_main_net", ascending=False).head(50).iterrows())
    rows.extend(_row(raw, "outflow") for _, raw in working.sort_values("_main_net", ascending=True).head(50).iterrows())
    return store.upsert_stock_capital_rank(trade_date, rows)


def _sync_sector_capital(store: MarketStore, trade_date: str) -> int:
    """Persist industry sector fund-flow ranking."""
    try:
        import akshare as ak
        df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
    except Exception as exc:  # noqa: BLE001
        logger.debug("sector capital sync failed: %s", exc)
        return 0
    if df is None or df.empty:
        return 0

    name_col = _pick_column(df, ("名称",), 1)
    net_col = _pick_column(df, ("主力净流入", "净额"), None) or _pick_column(df, ("主力", "净流入"), 3)
    pct_col = _pick_column(df, ("涨跌幅",), 2)
    if not name_col or not net_col:
        return 0
    rows = [
        {
            "sector": str(raw.get(name_col, "")),
            "main_net": _num(raw.get(net_col)),
            "change_pct": _num(raw.get(pct_col)) if pct_col else 0.0,
            "source": "akshare.stock_sector_fund_flow_rank",
        }
        for _, raw in df.head(80).iterrows()
    ]
    return store.upsert_sector_capital(trade_date, rows)


def _build_sector_snapshot_rows(df: Any, source: str) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    name_col = _pick_column(df, ("名称",), 1)
    pct_col = _pick_column(df, ("涨跌幅",), None) or _pick_column(df, ("涨跌",), 5)
    up_col = _pick_column(df, ("上涨家数",), None)
    down_col = _pick_column(df, ("下跌家数",), None)
    leader_col = _pick_column(df, ("领涨",), None)
    if not name_col:
        return []
    rows = []
    for _, raw in df.head(120).iterrows():
        rows.append({
            "name": str(raw.get(name_col, "")),
            "change_pct": _num(raw.get(pct_col)) if pct_col else 0.0,
            "advancers": int(_num(raw.get(up_col))) if up_col else 0,
            "decliners": int(_num(raw.get(down_col))) if down_col else 0,
            "leader": str(raw.get(leader_col, "")) if leader_col else "",
            "source": source,
        })
    return rows


def _sync_sector_snapshot(store: MarketStore, trade_date: str) -> int:
    """Persist industry and concept board snapshots for themes/heatmaps."""
    try:
        import akshare as ak
    except Exception as exc:  # noqa: BLE001
        logger.debug("sector snapshot sync import failed: %s", exc)
        return 0

    total = 0
    try:
        industry = ak.stock_board_industry_name_em()
        total += store.upsert_sector_snapshot(
            trade_date,
            "industry",
            _build_sector_snapshot_rows(industry, "akshare.stock_board_industry_name_em"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("industry snapshot sync failed: %s", exc)
    try:
        concept = ak.stock_board_concept_name_em()
        total += store.upsert_sector_snapshot(
            trade_date,
            "concept",
            _build_sector_snapshot_rows(concept, "akshare.stock_board_concept_name_em"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("concept snapshot sync failed: %s", exc)
    return total


def _sync_stock_capital_rank_akshare(store: MarketStore, trade_date: str) -> int:
    try:
        import akshare as ak
        df = ak.stock_individual_fund_flow_rank(indicator="\u4eca\u65e5")
    except Exception as exc:  # noqa: BLE001
        logger.debug("stock capital rank akshare failed: %s", exc)
        _set_sync_error(store, "capital_rank", "akshare.stock_individual_fund_flow_rank", exc)
        return 0
    if df is None or df.empty:
        _set_sync_error(store, "capital_rank", "akshare.stock_individual_fund_flow_rank", "empty result")
        return 0
    code_col = _pick_column(df, ("\u4ee3\u7801",), 1) or _pick_any_column(df, ("code", "代码"))
    name_col = _pick_column(df, ("\u540d\u79f0",), 2) or _pick_any_column(df, ("name", "名称"))
    net_col = (
        _pick_column(df, ("\u4e3b\u529b\u51c0\u6d41\u5165", "\u51c0\u989d"), None)
        or _pick_column(df, ("\u4e3b\u529b", "\u51c0\u6d41\u5165"), 3)
        or _pick_any_column(df, ("main_net", "net_amount", "net_mf_amount"))
    )
    pct_col = _pick_column(df, ("\u6da8\u8dcc\u5e45",), 4) or _pick_any_column(df, ("pct_chg", "change_pct"))
    if not code_col or not net_col:
        _set_sync_error(store, "capital_rank", "akshare.stock_individual_fund_flow_rank", f"missing columns: {list(df.columns)}")
        return 0
    working = df.copy()
    working["_main_net"] = working[net_col].map(_num)
    working["_change_pct"] = working[pct_col].map(_num) if pct_col else 0.0

    def _row(raw: Any, rank_type: str) -> dict[str, Any]:
        return {
            "rank_type": rank_type,
            "code": str(raw.get(code_col, "")),
            "name": str(raw.get(name_col, "")) if name_col else "",
            "main_net": raw.get("_main_net", 0.0),
            "change_pct": raw.get("_change_pct", 0.0),
            "source": "akshare.stock_individual_fund_flow_rank",
        }

    rows = []
    rows.extend(_row(raw, "inflow") for _, raw in working.sort_values("_main_net", ascending=False).head(50).iterrows())
    rows.extend(_row(raw, "outflow") for _, raw in working.sort_values("_main_net", ascending=True).head(50).iterrows())
    written = store.upsert_stock_capital_rank(trade_date, rows)
    if written:
        _clear_sync_error(store, "capital_rank", "akshare.stock_individual_fund_flow_rank")
    return written


def _sync_stock_capital_rank_tushare(store: MarketStore, trade_date: str) -> int:
    api = _tushare_api()
    if api is None:
        _set_sync_error(store, "capital_rank", "tushare.moneyflow", "TUSHARE_TOKEN not configured for sync process")
        return 0
    try:
        df = api.moneyflow(trade_date=trade_date.replace("-", ""))
    except Exception as exc:  # noqa: BLE001
        logger.debug("stock capital rank tushare failed: %s", exc)
        _set_sync_error(store, "capital_rank", "tushare.moneyflow", exc)
        return 0
    if df is None or df.empty:
        _set_sync_error(store, "capital_rank", "tushare.moneyflow", "empty result")
        return 0
    code_col = _pick_any_column(df, ("ts_code", "code"))
    net_col = _pick_any_column(df, ("net_mf_amount", "net_amount", "main_net"))
    pct_col = _pick_any_column(df, ("pct_chg", "change_pct"))
    if not code_col or not net_col:
        _set_sync_error(store, "capital_rank", "tushare.moneyflow", f"missing columns: {list(df.columns)}")
        return 0
    names = store.security_names([str(v) for v in df[code_col].head(200).tolist()])
    working = df.copy()
    working["_main_net"] = working[net_col].map(_num)
    working["_change_pct"] = working[pct_col].map(_num) if pct_col else 0.0

    def _row(raw: Any, rank_type: str) -> dict[str, Any]:
        code = str(raw.get(code_col, "")).upper()
        return {
            "rank_type": rank_type,
            "code": code,
            "name": names.get(code) or names.get(code.split(".", 1)[0]) or code,
            "main_net": raw.get("_main_net", 0.0),
            "change_pct": raw.get("_change_pct", 0.0),
            "source": "tushare.moneyflow",
        }

    rows = []
    rows.extend(_row(raw, "inflow") for _, raw in working.sort_values("_main_net", ascending=False).head(50).iterrows())
    rows.extend(_row(raw, "outflow") for _, raw in working.sort_values("_main_net", ascending=True).head(50).iterrows())
    written = store.upsert_stock_capital_rank(trade_date, rows)
    if written:
        _clear_sync_error(store, "capital_rank", "tushare.moneyflow")
    return written


def _eastmoney_clist_rows(
    url: str,
    params: dict[str, Any],
    dataset: str,
    source: str,
    store: MarketStore,
) -> list[dict[str, Any]]:
    import requests

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://quote.eastmoney.com/center/boardlist.html",
    }
    last_exc: Any = None
    for _ in range(3):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            diff = ((data or {}).get("data") or {}).get("diff") or []
            if isinstance(diff, dict):
                diff = list(diff.values())
            if isinstance(diff, list):
                return [row for row in diff if isinstance(row, dict)]
            return []
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(0.8)
    _set_sync_error(store, dataset, source, last_exc or "empty result")
    return []


def _sync_stock_capital_rank_eastmoney(store: MarketStore, trade_date: str) -> int:
    rows = _eastmoney_clist_rows(
        "https://push2.eastmoney.com/api/qt/clist/get",
        {
            "fid": "f62",
            "po": "1",
            "pz": "200",
            "pn": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "ut": "b2884a393a59ad64002292a3e90d46a5",
            "fs": "m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2,m:0+t:7+f:!2,m:1+t:3+f:!2",
            "fields": "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205,f124",
        },
        "capital_rank",
        "eastmoney.clist.stock_fund_flow",
        store,
    )
    if not rows:
        return 0

    def _suffix(code: str) -> str:
        if code.startswith(("6", "9")):
            return ".SH"
        if code.startswith(("4", "8")):
            return ".BJ"
        return ".SZ"

    mapped = []
    for raw in rows:
        code = str(raw.get("f12") or "").strip()
        if len(code) != 6 or not code.isdigit():
            continue
        mapped.append({
            "code": code + _suffix(code),
            "name": str(raw.get("f14") or code),
            "main_net": _num(raw.get("f62")),
            "change_pct": _num(raw.get("f3")),
            "source": "eastmoney.clist.stock_fund_flow",
        })
    if not mapped:
        _set_sync_error(store, "capital_rank", "eastmoney.clist.stock_fund_flow", "empty mapped result")
        return 0
    ranked = sorted(mapped, key=lambda item: _num(item.get("main_net")), reverse=True)
    out = [{**row, "rank_type": "inflow"} for row in ranked[:50]]
    out.extend({**row, "rank_type": "outflow"} for row in ranked[-50:])
    written = store.upsert_stock_capital_rank(trade_date, out)
    if written:
        _clear_sync_error(store, "capital_rank", "eastmoney.clist.stock_fund_flow")
    return written


def _ths_amount(value: Any) -> float:
    text = str(value or "").strip().replace(",", "").replace("%", "")
    if not text or text in {"-", "--", "nan", "None"}:
        return 0.0
    multiplier = 1.0
    if "\u4ebf" in text:
        multiplier = 100000000.0
    elif "\u4e07" in text:
        multiplier = 10000.0
    cleaned = (
        text.replace("\u4ebf", "")
        .replace("\u4e07", "")
        .replace("\u5143", "")
        .strip()
    )
    try:
        return float(cleaned) * multiplier
    except ValueError:
        return 0.0


def _ths_percent(value: Any) -> float:
    return _ths_amount(str(value or "").replace("%", ""))


def _ths_fund_flow_table(path: str, field: str, order: str, page: int = 1) -> Any:
    from io import StringIO

    import pandas as pd
    import py_mini_racer
    import requests
    from akshare.stock_feature.stock_fund_flow import _get_file_content_ths

    js_code = py_mini_racer.MiniRacer()
    js_code.eval(_get_file_content_ths("ths.js"))
    v_code = js_code.call("v")
    headers = {
        "Accept": "text/html, */*; q=0.01",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "hexin-v": v_code,
        "Host": "data.10jqka.com.cn",
        "Pragma": "no-cache",
        "Referer": f"http://data.10jqka.com.cn/funds/{path}/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "X-Requested-With": "XMLHttpRequest",
    }
    url = f"http://data.10jqka.com.cn/funds/{path}/field/{field}/order/{order}/page/{page}/ajax/1/free/1/"
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    frames = pd.read_html(StringIO(resp.text))
    return frames[0] if frames else pd.DataFrame()


def _sync_stock_capital_rank_ths(store: MarketStore, trade_date: str) -> int:
    rows = []
    try:
        for order, rank_type in (("desc", "inflow"), ("asc", "outflow")):
            df = _ths_fund_flow_table("ggzjl", "zjjlr", order, 1)
            if df is None or df.empty:
                continue
            code_col = _column_at(df, 1)
            name_col = _column_at(df, 2)
            pct_col = _column_at(df, 4)
            net_col = _column_at(df, 8)
            for _, raw in df.head(50).iterrows():
                code = str(raw.get(code_col, "")).strip()
                if len(code) != 6 or not code.isdigit():
                    continue
                suffix = ".SH" if code.startswith(("6", "9")) else ".BJ" if code.startswith(("4", "8")) else ".SZ"
                rows.append({
                    "rank_type": rank_type,
                    "code": code + suffix,
                    "name": str(raw.get(name_col, "")).strip() if name_col else code,
                    "main_net": _ths_amount(raw.get(net_col)) if net_col else 0.0,
                    "change_pct": _ths_percent(raw.get(pct_col)) if pct_col else 0.0,
                    "source": "ths.stock_fund_flow_individual",
                })
    except Exception as exc:  # noqa: BLE001
        _set_sync_error(store, "capital_rank", "ths.stock_fund_flow_individual", exc)
        return 0
    if not rows:
        _set_sync_error(store, "capital_rank", "ths.stock_fund_flow_individual", "empty result")
        return 0
    written = store.upsert_stock_capital_rank(trade_date, rows)
    if written:
        _clear_sync_error(store, "capital_rank", "ths.stock_fund_flow_individual")
    return written


def _sync_stock_capital_rank(store: MarketStore, trade_date: str) -> int:
    written = _sync_stock_capital_rank_akshare(store, trade_date)
    if written:
        return written
    written = _sync_stock_capital_rank_eastmoney(store, trade_date)
    if written:
        return written
    written = _sync_stock_capital_rank_ths(store, trade_date)
    if written:
        return written
    written = _tpdog_current_funds_rank(store, trade_date)
    if written:
        return written
    return _sync_stock_capital_rank_tushare(store, trade_date)


def _sync_sector_capital_akshare(store: MarketStore, trade_date: str) -> int:
    try:
        import akshare as ak
        df = ak.stock_sector_fund_flow_rank(indicator="\u4eca\u65e5", sector_type="\u884c\u4e1a\u8d44\u91d1\u6d41")
    except Exception as exc:  # noqa: BLE001
        logger.debug("sector capital akshare failed: %s", exc)
        _set_sync_error(store, "sector_capital", "akshare.stock_sector_fund_flow_rank", exc)
        return 0
    if df is None or df.empty:
        _set_sync_error(store, "sector_capital", "akshare.stock_sector_fund_flow_rank", "empty result")
        return 0
    name_col = _pick_column(df, ("\u540d\u79f0",), 1) or _pick_any_column(df, ("name", "industry", "行业"))
    net_col = (
        _pick_column(df, ("\u4e3b\u529b\u51c0\u6d41\u5165", "\u51c0\u989d"), None)
        or _pick_column(df, ("\u4e3b\u529b", "\u51c0\u6d41\u5165"), 3)
        or _pick_any_column(df, ("net_amount", "net_mf_amount", "main_net"))
    )
    pct_col = _pick_column(df, ("\u6da8\u8dcc\u5e45",), 2) or _pick_any_column(df, ("pct_change", "pct_chg", "change_pct"))
    if not name_col or not net_col:
        _set_sync_error(store, "sector_capital", "akshare.stock_sector_fund_flow_rank", f"missing columns: {list(df.columns)}")
        return 0
    rows = [
        {
            "sector": str(raw.get(name_col, "")),
            "main_net": _num(raw.get(net_col)),
            "change_pct": _num(raw.get(pct_col)) if pct_col else 0.0,
            "source": "akshare.stock_sector_fund_flow_rank",
        }
        for _, raw in df.head(80).iterrows()
    ]
    written = store.upsert_sector_capital(trade_date, rows)
    if written:
        _clear_sync_error(store, "sector_capital", "akshare.stock_sector_fund_flow_rank")
    return written


def _sync_sector_capital_eastmoney(store: MarketStore, trade_date: str) -> int:
    rows = _eastmoney_clist_rows(
        "https://push2.eastmoney.com/api/qt/clist/get",
        {
            "fid0": "f62",
            "fid": "f62",
            "po": "1",
            "pz": "100",
            "pn": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "stat": "1",
            "ut": "b2884a393a59ad64002292a3e90d46a5",
            "fs": "m:90+t:2",
            "fields": "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205,f124",
        },
        "sector_capital",
        "eastmoney.clist.industry_fund_flow",
        store,
    )
    mapped = [
        {
            "sector": str(raw.get("f14") or raw.get("f12") or ""),
            "name": str(raw.get("f14") or raw.get("f12") or ""),
            "main_net": _num(raw.get("f62")),
            "change_pct": _num(raw.get("f3")),
            "leader": "",
            "source": "eastmoney.clist.industry_fund_flow",
        }
        for raw in rows
        if raw.get("f14") or raw.get("f12")
    ]
    if not mapped:
        _set_sync_error(store, "sector_capital", "eastmoney.clist.industry_fund_flow", "empty mapped result")
        return 0
    written = store.upsert_sector_capital(trade_date, mapped[:100])
    if written:
        _clear_sync_error(store, "sector_capital", "eastmoney.clist.industry_fund_flow")
    return written


def _sync_sector_capital_ths(store: MarketStore, trade_date: str) -> int:
    try:
        import akshare as ak
        df = ak.stock_fund_flow_industry(symbol="\u5373\u65f6")
    except Exception as exc:  # noqa: BLE001
        _set_sync_error(store, "sector_capital", "ths.stock_fund_flow_industry", exc)
        return 0
    if df is None or df.empty:
        _set_sync_error(store, "sector_capital", "ths.stock_fund_flow_industry", "empty result")
        return 0
    name_col = _column_at(df, 1)
    pct_col = _column_at(df, 3)
    net_col = _column_at(df, 6)
    leader_col = _column_at(df, 8)
    if not name_col:
        _set_sync_error(store, "sector_capital", "ths.stock_fund_flow_industry", f"missing columns: {list(df.columns)}")
        return 0
    rows = [
        {
            "sector": str(raw.get(name_col, "")),
            "name": str(raw.get(name_col, "")),
            "main_net": _ths_amount(raw.get(net_col)) if net_col else 0.0,
            "change_pct": _ths_percent(raw.get(pct_col)) if pct_col else 0.0,
            "leader": str(raw.get(leader_col, "")) if leader_col else "",
            "source": "ths.stock_fund_flow_industry",
        }
        for _, raw in df.head(120).iterrows()
        if str(raw.get(name_col, "")).strip()
    ]
    written = store.upsert_sector_capital(trade_date, rows)
    if written:
        _clear_sync_error(store, "sector_capital", "ths.stock_fund_flow_industry")
    return written


def _moneyflow_ind_dc_rows(store: MarketStore, trade_date: str) -> list[dict[str, Any]]:
    api = _tushare_api()
    if api is None:
        _set_sync_error(store, "sector_capital", "tushare.moneyflow_ind_dc", "TUSHARE_TOKEN not configured for sync process")
        return []
    try:
        df = api.moneyflow_ind_dc(trade_date=trade_date.replace("-", ""))
    except Exception as exc:  # noqa: BLE001
        logger.debug("moneyflow_ind_dc failed: %s", exc)
        _set_sync_error(store, "sector_capital", "tushare.moneyflow_ind_dc", exc)
        return []
    if df is None or df.empty:
        _set_sync_error(store, "sector_capital", "tushare.moneyflow_ind_dc", "empty result")
        return []
    name_col = _pick_any_column(df, ("industry", "name", "板块名称", "行业名称"))
    net_col = _pick_any_column(df, ("net_amount", "net_mf_amount", "main_net", "主力净流入净额"))
    pct_col = _pick_any_column(df, ("pct_change", "pct_chg", "change_pct", "涨跌幅"))
    leader_col = _pick_any_column(df, ("lead_stock", "leader", "领涨股"))
    if not name_col:
        _set_sync_error(store, "sector_capital", "tushare.moneyflow_ind_dc", f"missing columns: {list(df.columns)}")
        return []
    rows = []
    for _, raw in df.head(120).iterrows():
        rows.append({
            "sector": str(raw.get(name_col, "")),
            "name": str(raw.get(name_col, "")),
            "main_net": _num(raw.get(net_col)) if net_col else 0.0,
            "change_pct": _num(raw.get(pct_col)) if pct_col else 0.0,
            "leader": str(raw.get(leader_col, "")) if leader_col else "",
            "source": "tushare.moneyflow_ind_dc",
        })
    _clear_sync_error(store, "sector_capital", "tushare.moneyflow_ind_dc")
    return rows


def _sync_sector_capital(store: MarketStore, trade_date: str) -> int:
    written = _sync_sector_capital_akshare(store, trade_date)
    if written:
        return written
    written = _sync_sector_capital_eastmoney(store, trade_date)
    if written:
        return written
    written = _sync_sector_capital_ths(store, trade_date)
    if written:
        return written
    written = _sync_sector_capital_tpdog(store, trade_date)
    if written:
        return written
    rows = _moneyflow_ind_dc_rows(store, trade_date)
    return store.upsert_sector_capital(trade_date, rows) if rows else 0


def _sync_sector_snapshot_akshare(store: MarketStore, trade_date: str) -> int:
    try:
        import akshare as ak
    except Exception as exc:  # noqa: BLE001
        _set_sync_error(store, "sector_snapshot", "akshare", exc)
        return 0
    total = 0
    try:
        industry = ak.stock_board_industry_name_em()
        total += store.upsert_sector_snapshot(
            trade_date,
            "industry",
            _build_sector_snapshot_rows(industry, "akshare.stock_board_industry_name_em"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("industry snapshot akshare failed: %s", exc)
        _set_sync_error(store, "sector_snapshot", "akshare.stock_board_industry_name_em", exc)
    try:
        concept = ak.stock_board_concept_name_em()
        total += store.upsert_sector_snapshot(
            trade_date,
            "concept",
            _build_sector_snapshot_rows(concept, "akshare.stock_board_concept_name_em"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("concept snapshot akshare failed: %s", exc)
        _set_sync_error(store, "sector_snapshot", "akshare.stock_board_concept_name_em", exc)
    if total:
        _clear_sync_error(store, "sector_snapshot", "akshare")
    return total


def _eastmoney_sector_snapshot_rows(
    store: MarketStore,
    board_type: str,
    url: str,
    fs: str,
    fields: str,
) -> list[dict[str, Any]]:
    rows = _eastmoney_clist_rows(
        url,
        {
            "pn": "1",
            "pz": "150",
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": "f3",
            "fs": fs,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fields": fields,
        },
        "sector_snapshot",
        f"eastmoney.clist.{board_type}",
        store,
    )
    mapped = []
    for raw in rows:
        name = str(raw.get("f14") or raw.get("f12") or "").strip()
        if not name:
            continue
        mapped.append({
            "name": name,
            "change_pct": _num(raw.get("f3")),
            "turnover": _num(raw.get("f6")),  # 板块成交额，单位：元（面积权重用）
            "advancers": int(_num(raw.get("f104"))),
            "decliners": int(_num(raw.get("f105"))),
            "leader": str(raw.get("f128") or ""),
            "source": f"eastmoney.clist.{board_type}",
            "code": raw.get("f12"),
            "board_type": board_type,
        })
    return mapped


def _sync_sector_snapshot_eastmoney(store: MarketStore, trade_date: str) -> int:
    industry = _eastmoney_sector_snapshot_rows(
        store,
        "industry",
        "https://17.push2.eastmoney.com/api/qt/clist/get",
        "m:90 t:2 f:!50",
        "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21,f23,f24,f25,f26,f22,f33,f11,f62,f128,f136,f115,f152,f124,f107,f104,f105,f140,f141,f207,f208,f209,f222",
    )
    concept = _eastmoney_sector_snapshot_rows(
        store,
        "concept",
        "https://79.push2.eastmoney.com/api/qt/clist/get",
        "m:90 t:3 f:!50",
        "f2,f3,f4,f6,f8,f12,f14,f15,f16,f17,f18,f20,f21,f24,f25,f22,f33,f11,f62,f128,f124,f107,f104,f105,f136",
    )
    total = 0
    total += store.upsert_sector_snapshot(trade_date, "industry", industry)
    total += store.upsert_sector_snapshot(trade_date, "concept", concept)
    if total:
        _clear_sync_error(store, "sector_snapshot", "eastmoney.clist.industry")
        _clear_sync_error(store, "sector_snapshot", "eastmoney.clist.concept")
    else:
        _set_sync_error(store, "sector_snapshot", "eastmoney.clist", "empty mapped result")
    return total


def _ths_sector_snapshot_rows(df: Any, board_type: str, source: str) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    name_col = _column_at(df, 1)
    pct_col = _column_at(df, 3)
    leader_col = _column_at(df, 8)
    leader_pct_col = _column_at(df, 9)
    if not name_col:
        return []
    rows = []
    for _, raw in df.head(160).iterrows():
        name = str(raw.get(name_col, "")).strip()
        if not name:
            continue
        rows.append({
            "name": name,
            "change_pct": _ths_percent(raw.get(pct_col)) if pct_col else 0.0,
            "advancers": 0,
            "decliners": 0,
            "leader": str(raw.get(leader_col, "")) if leader_col else "",
            "source": source,
            "leader_change_pct": _ths_percent(raw.get(leader_pct_col)) if leader_pct_col else 0.0,
            "board_type": board_type,
        })
    return rows


def _sync_sector_snapshot_ths(store: MarketStore, trade_date: str) -> int:
    try:
        import akshare as ak
        industry = ak.stock_fund_flow_industry(symbol="\u5373\u65f6")
    except Exception as exc:  # noqa: BLE001
        _set_sync_error(store, "sector_snapshot", "ths.stock_fund_flow_industry", exc)
        industry = None
    try:
        import akshare as ak
        concept = ak.stock_fund_flow_concept(symbol="\u5373\u65f6")
    except Exception as exc:  # noqa: BLE001
        _set_sync_error(store, "sector_snapshot", "ths.stock_fund_flow_concept", exc)
        concept = None
    total = 0
    total += store.upsert_sector_snapshot(
        trade_date,
        "industry",
        _ths_sector_snapshot_rows(industry, "industry", "ths.stock_fund_flow_industry"),
    )
    total += store.upsert_sector_snapshot(
        trade_date,
        "concept",
        _ths_sector_snapshot_rows(concept, "concept", "ths.stock_fund_flow_concept"),
    )
    if total:
        _clear_sync_error(store, "sector_snapshot", "ths.stock_fund_flow_industry")
        _clear_sync_error(store, "sector_snapshot", "ths.stock_fund_flow_concept")
    else:
        _set_sync_error(store, "sector_snapshot", "ths", "empty result")
    return total


def _tushare_index_snapshot_rows(api: Any, fn_name: str, board_type: str) -> list[dict[str, Any]]:
    try:
        df = getattr(api, fn_name)()
    except Exception as exc:  # noqa: BLE001
        logger.debug("%s failed: %s", fn_name, exc)
        return []
    if df is None or df.empty:
        return []
    name_col = _pick_any_column(df, ("name", "index_name", "指数名称"))
    pct_col = _pick_any_column(df, ("pct_change", "pct_chg", "change_pct", "涨跌幅"))
    if not name_col:
        return []
    return [
        {
            "name": str(raw.get(name_col, "")),
            "change_pct": _num(raw.get(pct_col)) if pct_col else 0.0,
            "advancers": 0,
            "decliners": 0,
            "leader": "",
            "source": f"tushare.{fn_name}",
            "board_type": board_type,
        }
        for _, raw in df.head(120).iterrows()
    ]


def _tpdog_current_funds_rank(store: MarketStore, trade_date: str) -> int:
    try:
        from src.data.tpdog_client import call
    except Exception as exc:  # noqa: BLE001
        _set_sync_error(store, "capital_rank", "tpdog.current_funds", exc)
        return 0
    rows = []
    for zs_type, suffix in (("zssh", ".SH"), ("zssz", ".SZ"), ("zsbj", ".BJ")):
        try:
            content = call("current/funds", zs_type=zs_type, sort=1, t=1)
        except Exception as exc:  # noqa: BLE001
            logger.debug("tpdog current/funds rank failed for %s: %s", zs_type, exc)
            _set_sync_error(store, "capital_rank", f"tpdog.current_funds.{zs_type}", exc)
            continue
        for raw in content:
            code = str(raw.get("code") or "").strip()
            if len(code) != 6 or not code.isdigit():
                continue
            rows.append({
                "code": code + suffix,
                "name": raw.get("name"),
                "main_net": _num(raw.get("m_net")),
                "change_pct": _num(raw.get("rise_rate") or raw.get("change_pct")),
                "source": "tpdog.current/funds",
            })
    if not rows:
        return 0
    ranked = sorted(rows, key=lambda item: _num(item.get("main_net")), reverse=True)
    out = [{**row, "rank_type": "inflow"} for row in ranked[:50]]
    out.extend({**row, "rank_type": "outflow"} for row in ranked[-50:])
    written = store.upsert_stock_capital_rank(trade_date, out)
    if written:
        _clear_sync_error(store, "capital_rank", "tpdog.current_funds")
    return written


def _tpdog_bk_funds(store: MarketStore, trade_date: str, bk_type: str) -> list[dict[str, Any]]:
    try:
        from src.data.tpdog_client import call
        content = call("current/bk_funds", bk_type=bk_type, sort=1, t=1)
    except Exception as exc:  # noqa: BLE001
        logger.debug("tpdog current/bk_funds failed for %s: %s", bk_type, exc)
        _set_sync_error(store, "sector_capital", f"tpdog.current_bk_funds.{bk_type}", exc)
        return []
    rows = []
    for raw in content:
        rows.append({
            "sector": str(raw.get("name") or raw.get("code") or ""),
            "name": str(raw.get("name") or raw.get("code") or ""),
            "main_net": _num(raw.get("m_net")),
            "change_pct": _num(raw.get("rise_rate") or raw.get("change_pct")),
            "leader": "",
            "source": "tpdog.current/bk_funds",
            "code": raw.get("code"),
            "type": raw.get("type") or bk_type,
        })
    return rows


def _tpdog_root_call(path: str, **params: Any) -> list[dict[str, Any]]:
    from src.data.tpdog_client import DEFAULT_TIMEOUT, TpdogError, get_token
    from src.data.rate_limiter import market_limiter
    import requests

    query = {k: v for k, v in params.items() if v is not None and v != ""}
    query["token"] = get_token()
    url = f"https://www.tpdog.com/api/{path.lstrip('/')}"
    with market_limiter:
        resp = requests.get(url, params=query, timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 1000:
        raise TpdogError(data.get("code"), str(data.get("message", "unknown error")))
    content = data.get("content")
    if isinstance(content, list):
        return content
    if isinstance(content, dict):
        return [content]
    return []


def _tpdog_bk_scan_rows(store: MarketStore, bk_type: str, board_type: str) -> list[dict[str, Any]]:
    try:
        content = _tpdog_root_call("current/bk_scans", bk_type=bk_type, sort=2, field="v_rate", filter="", t=1)
    except Exception as exc:  # noqa: BLE001
        logger.debug("tpdog current/bk_scans failed for %s: %s", bk_type, exc)
        _set_sync_error(store, "sector_snapshot", f"tpdog.current_bk_scans.{bk_type}", exc)
        return []
    rows = []
    for raw in content[:120]:
        rows.append({
            "name": str(raw.get("name") or raw.get("code") or ""),
            "change_pct": _num(raw.get("rise_rate") or raw.get("change_pct")),
            "advancers": int(_num(raw.get("ups"))),
            "decliners": int(_num(raw.get("dns"))),
            "leader": str(raw.get("u_name") or ""),
            "source": "tpdog.current/bk_scans",
            "code": raw.get("code"),
            "board_type": board_type,
        })
    return rows


def _sync_sector_capital_tpdog(store: MarketStore, trade_date: str) -> int:
    rows = _tpdog_bk_funds(store, trade_date, "bki")
    if not rows:
        return 0
    written = store.upsert_sector_capital(trade_date, rows)
    if written:
        _clear_sync_error(store, "sector_capital", "tpdog.current_bk_funds")
    return written


def _sync_sector_snapshot_tpdog(store: MarketStore, trade_date: str) -> int:
    total = 0
    industry = _tpdog_bk_scan_rows(store, "bki", "industry")
    if not industry:
        industry = [
            {
                "name": r.get("name") or r.get("sector"),
                "change_pct": r.get("change_pct"),
                "advancers": 0,
                "decliners": 0,
                "leader": r.get("leader"),
                "source": "tpdog.current/bk_funds",
            }
            for r in _tpdog_bk_funds(store, trade_date, "bki")
        ]
    total += store.upsert_sector_snapshot(trade_date, "industry", industry)
    concept = _tpdog_bk_scan_rows(store, "bkc", "concept")
    if not concept:
        concept = [
            {
                "name": r.get("name") or r.get("sector"),
                "change_pct": r.get("change_pct"),
                "advancers": 0,
                "decliners": 0,
                "leader": r.get("leader"),
                "source": "tpdog.current/bk_funds",
            }
            for r in _tpdog_bk_funds(store, trade_date, "bkc")
        ]
    total += store.upsert_sector_snapshot(trade_date, "concept", concept)
    if total:
        _clear_sync_error(store, "sector_snapshot", "tpdog")
    return total


def _sync_sector_snapshot_tushare(store: MarketStore, trade_date: str) -> int:
    api = _tushare_api()
    if api is None:
        _set_sync_error(store, "sector_snapshot", "tushare", "TUSHARE_TOKEN not configured for sync process")
        return 0
    total = 0
    industry_rows = _tushare_index_snapshot_rows(api, "dc_index", "industry")
    if not industry_rows:
        moneyflow_rows = _moneyflow_ind_dc_rows(store, trade_date)
        industry_rows = [
            {
                "name": r.get("name") or r.get("sector"),
                "change_pct": r.get("change_pct"),
                "advancers": 0,
                "decliners": 0,
                "leader": r.get("leader"),
                "source": "tushare.moneyflow_ind_dc",
            }
            for r in moneyflow_rows
        ]
    total += store.upsert_sector_snapshot(trade_date, "industry", industry_rows)
    concept_rows = _tushare_index_snapshot_rows(api, "ths_index", "concept")
    total += store.upsert_sector_snapshot(trade_date, "concept", concept_rows)
    if total:
        _clear_sync_error(store, "sector_snapshot", "tushare")
    else:
        _set_sync_error(store, "sector_snapshot", "tushare", "empty result from dc_index/ths_index/moneyflow_ind_dc")
    return total


def _sync_sector_snapshot(store: MarketStore, trade_date: str) -> int:
    written = _sync_sector_snapshot_akshare(store, trade_date)
    if written:
        return written
    written = _sync_sector_snapshot_eastmoney(store, trade_date)
    if written:
        return written
    written = _sync_sector_snapshot_ths(store, trade_date)
    if written:
        return written
    written = _sync_sector_snapshot_tpdog(store, trade_date)
    if written:
        return written
    return _sync_sector_snapshot_tushare(store, trade_date)


_GLOBAL_INDEX_SYMBOLS = (
    ("^DJI", "道琼斯工业指数"),
    ("^IXIC", "纳斯达克综合指数"),
    ("^GSPC", "标普500"),
    ("^RUT", "罗素2000"),
)

_US_THEME_PROXIES = (
    {
        "theme_id": "semiconductor",
        "theme_name": "半导体 / AI算力",
        "proxy_symbol": "SMH",
        "proxy_name": "VanEck Semiconductor ETF",
        "a_share_mapping": ["半导体", "芯片", "算力", "光模块", "CPO"],
    },
    {
        "theme_id": "software_ai",
        "theme_name": "AI软件 / 云计算",
        "proxy_symbol": "IGV",
        "proxy_name": "iShares Expanded Tech-Software ETF",
        "a_share_mapping": ["AI应用", "软件服务", "云计算", "数据要素"],
    },
    {
        "theme_id": "biotech",
        "theme_name": "生物科技 / 创新药",
        "proxy_symbol": "IBB",
        "proxy_name": "iShares Biotechnology ETF",
        "a_share_mapping": ["创新药", "生物医药", "医疗服务"],
    },
    {
        "theme_id": "ev_battery",
        "theme_name": "新能源车 / 锂电",
        "proxy_symbol": "DRIV",
        "proxy_name": "Global X Autonomous & Electric Vehicles ETF",
        "a_share_mapping": ["新能源汽车", "锂电池", "智能驾驶"],
    },
    {
        "theme_id": "aerospace_defense",
        "theme_name": "军工航天",
        "proxy_symbol": "ITA",
        "proxy_name": "iShares U.S. Aerospace & Defense ETF",
        "a_share_mapping": ["国防军工", "商业航天", "低空经济"],
    },
    {
        "theme_id": "financials",
        "theme_name": "金融",
        "proxy_symbol": "XLF",
        "proxy_name": "Financial Select Sector SPDR Fund",
        "a_share_mapping": ["证券", "银行", "保险"],
    },
    {
        "theme_id": "energy",
        "theme_name": "能源",
        "proxy_symbol": "XLE",
        "proxy_name": "Energy Select Sector SPDR Fund",
        "a_share_mapping": ["石油石化", "煤炭", "油服"],
    },
    {
        "theme_id": "gold",
        "theme_name": "黄金",
        "proxy_symbol": "GLD",
        "proxy_name": "SPDR Gold Shares",
        "a_share_mapping": ["黄金", "贵金属", "有色金属"],
    },
)


_GLOBAL_INDEX_SYMBOLS = (
    ("^IXIC", "纳斯达克"),
    ("^GSPC", "标普500"),
    ("^SOX", "费城半导体"),
    ("^DJI", "道琼斯"),
    ("^HSI", "恒生指数"),
    ("HSTECH", "恒生科技"),
)

_US_THEME_PROXIES = (
    {
        "theme_id": "ai_compute",
        "theme_name": "AI/算力(NVDA)",
        "proxy_symbol": "NVDA",
        "proxy_name": "NVDA",
        "a_share_mapping": ["A股算力/服务器"],
    },
    {
        "theme_id": "cpo_optical",
        "theme_name": "CPO/光(AVGO · COHR · LITE)",
        "proxy_symbol": "AVGO",
        "proxy_name": "AVGO · COHR · LITE",
        "a_share_mapping": ["A股光模块/CPO"],
    },
    {
        "theme_id": "semiconductor",
        "theme_name": "半导体(SOX)",
        "proxy_symbol": "SOXX",
        "proxy_name": "SOXX",
        "a_share_mapping": ["A股半导体/设备"],
    },
    {
        "theme_id": "robotics",
        "theme_name": "机器人(TSLA)",
        "proxy_symbol": "TSLA",
        "proxy_name": "TSLA",
        "a_share_mapping": ["A股机器人/减速器"],
    },
    {
        "theme_id": "solar_energy",
        "theme_name": "新能源(ENPH · FSLR)",
        "proxy_symbol": "ENPH",
        "proxy_name": "ENPH · FSLR",
        "a_share_mapping": ["A股光伏"],
    },
    {
        "theme_id": "biotech",
        "theme_name": "创新药(IBB)",
        "proxy_symbol": "IBB",
        "proxy_name": "IBB",
        "a_share_mapping": ["A股创新药/医药"],
    },
    {
        "theme_id": "defense_space",
        "theme_name": "军工航天(ITA)",
        "proxy_symbol": "ITA",
        "proxy_name": "ITA",
        "a_share_mapping": ["A股军工/商业航天"],
    },
    {
        "theme_id": "gold",
        "theme_name": "黄金(GLD)",
        "proxy_symbol": "GLD",
        "proxy_name": "GLD",
        "a_share_mapping": ["A股黄金/贵金属"],
    },
)

_A_SHARE_FOCUS_STOCKS = {
    "ai_compute": ["工业富联", "浪潮信息", "中际旭创"],
    "cpo_optical": ["新易盛", "中际旭创", "太辰光"],
    "semiconductor": ["北方华创", "中微公司", "华海清科"],
    "robotics": ["绿的谐波", "中大力德", "华中数控"],
    "solar_energy": ["阳光电源", "通威股份", "晶澳科技"],
    "biotech": ["恒瑞医药", "百济神州", "药明康德"],
    "defense_space": ["中航沈飞", "航天电子", "中国卫星"],
    "gold": ["山东黄金", "中金黄金", "赤峰黄金"],
}


def _yf_last_rows(symbols: list[str], *, period: str = "10d") -> dict[str, list[dict[str, Any]]]:
    try:
        import yfinance as yf
    except Exception as exc:  # noqa: BLE001
        logger.debug("yfinance unavailable: %s", exc)
        yf = None
    out: dict[str, list[dict[str, Any]]] = {}
    if yf is not None:
        for symbol in symbols:
            try:
                hist = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=False)
            except Exception as exc:  # noqa: BLE001
                logger.debug("yfinance history failed for %s: %s", symbol, exc)
                continue
            if hist is None or hist.empty:
                continue
            rows: list[dict[str, Any]] = []
            for idx, r in hist.tail(8).iterrows():
                rows.append({
                    "trade_date": idx.strftime("%Y-%m-%d"),
                    "open": _num(r.get("Open")),
                    "high": _num(r.get("High")),
                    "low": _num(r.get("Low")),
                    "close": _num(r.get("Close")),
                    "volume": _num(r.get("Volume")),
                })
            if rows:
                out[symbol] = rows
    missing = [symbol for symbol in symbols if symbol not in out]
    if missing:
        out.update(_yahoo_chart_last_rows(missing, period=period))
    return out


def _yahoo_chart_last_rows(symbols: list[str], *, period: str = "10d") -> dict[str, list[dict[str, Any]]]:
    """Fallback daily bars via Yahoo chart JSON, optionally through overseas proxy.

    The production CN host frequently gets Yahoo/yfinance 403/429 responses.
    The overseas proxy is already part of the deployment for foreign sources,
    so use it as a second leg and keep the returned row shape identical to
    ``_yf_last_rows``.
    """
    import requests
    from urllib.parse import quote as url_quote

    out: dict[str, list[dict[str, Any]]] = {}
    range_arg = "10d" if period == "10d" else period
    headers = {"User-Agent": "Mozilla/5.0"}
    proxy = os.getenv("OVERSEAS_PROXY_URL", "").strip().rstrip("/")
    secret = os.getenv("PROXY_SECRET", "").strip()

    def _fetch_json(url: str) -> dict[str, Any] | None:
        try:
            resp = requests.get(url, headers=headers, timeout=12)
            resp.raise_for_status()
            return resp.json()
        except Exception as direct_exc:  # noqa: BLE001
            logger.debug("yahoo chart direct failed for %s: %s", url, direct_exc)
        if not proxy or not secret:
            return None
        try:
            resp = requests.get(
                f"{proxy}/fetch",
                params={"url": url, "strategy": "raw"},
                headers={"X-Proxy-Key": secret},
                timeout=20,
            )
            resp.raise_for_status()
            payload = resp.json()
            content = payload.get("content")
            return json.loads(content) if isinstance(content, str) else None
        except Exception as proxy_exc:  # noqa: BLE001
            logger.debug("yahoo chart proxy failed for %s: %s", url, proxy_exc)
            return None

    for symbol in symbols:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{url_quote(symbol, safe='')}?range={range_arg}&interval=1d"
        payload = _fetch_json(url)
        try:
            result = ((payload or {}).get("chart") or {}).get("result") or []
            item = result[0]
            timestamps = item.get("timestamp") or []
            quote_row = (((item.get("indicators") or {}).get("quote") or [{}])[0]) or {}
            rows: list[dict[str, Any]] = []
            start = max(0, len(timestamps) - 8)
            opens = quote_row.get("open") or []
            highs = quote_row.get("high") or []
            lows = quote_row.get("low") or []
            closes = quote_row.get("close") or []
            volumes = quote_row.get("volume") or []
            for i, ts in enumerate(timestamps[start:], start=start):
                close = _num(closes[i] if i < len(closes) else None)
                if close <= 0:
                    continue
                rows.append({
                    "trade_date": datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d"),
                    "open": _num(opens[i] if i < len(opens) else None),
                    "high": _num(highs[i] if i < len(highs) else None),
                    "low": _num(lows[i] if i < len(lows) else None),
                    "close": close,
                    "volume": _num(volumes[i] if i < len(volumes) else None),
                })
            if rows:
                out[symbol] = rows
        except Exception as exc:  # noqa: BLE001
            logger.debug("yahoo chart parse failed for %s: %s", symbol, exc)
    return out


def _ak_hk_index_history(symbol: str) -> list[dict[str, Any]]:
    if symbol != "HSTECH":
        return []
    try:
        import akshare as ak
        df = ak.stock_hk_index_daily_sina(symbol=symbol)
    except Exception as exc:  # noqa: BLE001
        logger.debug("akshare hk index history failed for %s: %s", symbol, exc)
        return []
    if df is None or df.empty:
        return []
    rows: list[dict[str, Any]] = []
    for _, r in df.tail(8).iterrows():
        rows.append({
            "trade_date": str(r.get("date") or ""),
            "open": _num(r.get("open")),
            "high": _num(r.get("high")),
            "low": _num(r.get("low")),
            "close": _num(r.get("close")),
            "volume": _num(r.get("volume")),
        })
    return rows


def _sync_global_market_indices(store: MarketStore, trade_date: str) -> int:
    histories = _yf_last_rows([symbol for symbol, _ in _GLOBAL_INDEX_SYMBOLS])
    rows: list[dict[str, Any]] = []
    for symbol, name in _GLOBAL_INDEX_SYMBOLS:
        hist = histories.get(symbol) or _ak_hk_index_history(symbol)
        if not hist:
            continue
        last = hist[-1]
        prev = hist[-2] if len(hist) > 1 else {}
        prev_close = _num(prev.get("close"))
        close = _num(last.get("close"))
        rows.append({
            "trade_date": trade_date,
            "symbol": symbol,
            "name": name,
            "open": last.get("open"),
            "high": last.get("high"),
            "low": last.get("low"),
            "close": close,
            "prev_close": prev_close,
            "change_pct": ((close - prev_close) / prev_close * 100.0) if prev_close else 0.0,
            "currency": "USD",
            "source": "yfinance",
            "history": hist,
            "source_trade_date": last.get("trade_date"),
        })
    return store.upsert_global_market_indices(trade_date, rows)


def _sync_us_theme_snapshot(store: MarketStore, trade_date: str) -> int:
    histories = _yf_last_rows([str(item["proxy_symbol"]) for item in _US_THEME_PROXIES])
    rows: list[dict[str, Any]] = []
    for item in _US_THEME_PROXIES:
        symbol = str(item["proxy_symbol"])
        hist = histories.get(symbol) or []
        if not hist:
            continue
        last = hist[-1]
        prev = hist[-2] if len(hist) > 1 else {}
        close = _num(last.get("close"))
        prev_close = _num(prev.get("close"))
        rows.append({
            **item,
            "close": close,
            "change_pct": ((close - prev_close) / prev_close * 100.0) if prev_close else 0.0,
            "source": "yfinance",
            "history": hist,
            "source_trade_date": last.get("trade_date"),
        })
    return store.upsert_us_theme_snapshot(trade_date, rows)


def _sync_us_a_share_transmission(store: MarketStore, trade_date: str) -> int:
    themes = store.get_us_theme_snapshot(trade_date, limit=80)
    if not themes:
        return 0
    rows = []
    for theme in themes:
        change = _num(theme.get("change_pct"))
        if change > 0.35:
            direction = "positive"
            reason = "美股代理题材收涨，A股对应方向盘前关注承接强弱。"
        elif change < -0.35:
            direction = "negative"
            reason = "美股代理题材收跌，A股对应方向盘前关注风险释放。"
        else:
            direction = "neutral"
            reason = "美股代理题材波动有限，A股对应方向不生成强传导结论。"
        rows.append({
            "theme_id": theme.get("theme_id"),
            "us_theme": theme.get("theme_name"),
            "a_share_themes": theme.get("a_share_mapping") or [],
            "signal_strength": change,
            "direction": direction,
            "reason": reason,
            "source_data": {
                "proxy_symbol": theme.get("proxy_symbol"),
                "proxy_name": theme.get("proxy_name"),
                "change_pct": change,
                "source": theme.get("source"),
            },
        })
    return store.upsert_us_a_share_transmission(trade_date, rows)


def _sync_us_a_share_transmission(store: MarketStore, trade_date: str) -> int:
    themes = store.get_us_theme_snapshot(trade_date, limit=80)
    if not themes:
        return 0
    rows = []
    for theme in themes:
        theme_id = str(theme.get("theme_id") or "")
        change = _num(theme.get("change_pct"))
        a_share_themes = theme.get("a_share_mapping") or []
        focus_stocks = _A_SHARE_FOCUS_STOCKS.get(theme_id, [])
        if change > 0.8:
            direction = "strong"
            reason = f"{theme.get('proxy_name') or theme.get('proxy_symbol')}走强，映射方向盘前关注承接强弱"
        elif change > 0.2:
            direction = "medium"
            reason = f"{theme.get('proxy_name') or theme.get('proxy_symbol')}温和走强，关注是否形成开盘催化"
        elif change < -0.5:
            direction = "weak"
            reason = f"{theme.get('proxy_name') or theme.get('proxy_symbol')}偏弱，对应方向盘前先看风险释放"
        else:
            direction = "neutral"
            reason = f"{theme.get('proxy_name') or theme.get('proxy_symbol')}波动有限，对应方向不生成强传导结论"
        rows.append({
            "theme_id": theme_id,
            "us_theme": theme.get("theme_name"),
            "a_share_themes": a_share_themes,
            "signal_strength": change,
            "direction": direction,
            "reason": reason,
            "source_data": {
                "proxy_symbol": theme.get("proxy_symbol"),
                "proxy_name": theme.get("proxy_name"),
                "change_pct": change,
                "focus_stocks": focus_stocks,
                "source": theme.get("source"),
            },
        })
    return store.upsert_us_a_share_transmission(trade_date, rows)


_NEWS_CATEGORY_KEYWORDS = {
    "policy": ("政策", "监管", "央行", "证监", "发改委", "财政", "国务院", "利率", "降准"),
    "industry": ("产业", "行业", "半导体", "AI", "人工智能", "新能源", "医药", "地产", "消费", "军工"),
    "catalyst": ("催化", "订单", "涨价", "发布", "签约", "突破", "大会", "并购", "业绩", "预增"),
    "risk": ("风险", "下跌", "制裁", "调查", "退市", "亏损", "减持", "违约", "冲突", "关税"),
}


def _categorize_news(title: str, summary: str) -> str:
    text = f"{title} {summary}"
    for category, keywords in _NEWS_CATEGORY_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return category
    return "industry"


_NEWS_CATEGORY_KEYWORDS = {
    "policy": ("政策", "监管", "央行", "证监", "发改委", "财政", "国务院", "利率", "降准", "关税", "主权债券"),
    "industry": ("产业", "行业", "半导体", "AI", "人工智能", "芯片", "新能源", "医药", "地产", "消费", "军工", "苹果", "算力"),
    "catalyst": ("催化", "订单", "涨价", "发布", "签约", "突破", "大会", "并购", "业绩", "预增", "跃迁", "协议", "发行"),
    "risk": ("风险", "下跌", "制裁", "调查", "退市", "亏损", "减持", "违约", "冲突", "暴利", "法律红线", "库存变动", "波动性"),
}


def _is_premarket_news_like(title: str, summary: str, url: str) -> bool:
    text = f"{title} {summary} {url}".lower()
    block_words = ("股吧", "行情", "走势图", "百度百科", "盘口", "f10", "实时行情", "官网", "指数行情")
    return bool(title.strip()) and not any(word.lower() in text for word in block_words)


def _fetch_premarket_news_akshare(trade_date: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        import akshare as ak
        df = ak.stock_news_main_cx()
        if df is not None and not df.empty:
            for _, raw in df.head(40).iterrows():
                summary = str(raw.get("summary") or "").strip()
                if not summary:
                    continue
                title = summary[:42] + ("..." if len(summary) > 42 else "")
                url = str(raw.get("url") or "")
                category = _categorize_news(str(raw.get("tag") or ""), summary)
                rows.append({
                    "category": category,
                    "title": title,
                    "summary": summary,
                    "url": url,
                    "source": "财新数据",
                    "published_at": trade_date,
                })
    except Exception as exc:  # noqa: BLE001
        logger.debug("premarket akshare caixin news failed: %s", exc)

    try:
        import akshare as ak
        df = ak.news_economic_baidu(date=trade_date.replace("-", ""))
        if df is not None and not df.empty:
            working = df.sort_values("重要性", ascending=False).head(12)
            for _, raw in working.iterrows():
                event = str(raw.get("事件") or "").strip()
                if not event:
                    continue
                region = str(raw.get("地区") or "").strip()
                time_text = str(raw.get("时间") or "").strip()
                actual = raw.get("公布")
                expected = raw.get("预期")
                previous = raw.get("前值")
                summary = f"{region} {time_text} {event}；公布 {actual}，预期 {expected}，前值 {previous}"
                rows.append({
                    "category": _categorize_news(event, summary) if "利率" in event or "财政" in event or "央行" in event else "risk",
                    "title": event,
                    "summary": summary,
                    "url": "",
                    "source": "百度股市通宏观日历",
                    "published_at": f"{trade_date} {time_text}",
                })
    except Exception as exc:  # noqa: BLE001
        logger.debug("premarket akshare economic calendar failed: %s", exc)
    return rows


def _sync_premarket_news(store: MarketStore, trade_date: str) -> int:
    news: list[dict[str, Any]] = _fetch_premarket_news_akshare(trade_date)
    try:
        from src.data.news_feed import get_news
        news.extend(get_news(limit_per_feed=8).get("all", []))
    except Exception as exc:  # noqa: BLE001
        logger.debug("premarket news sync failed: %s", exc)
    if not news:
        try:
            from src.api.news_routes import _build_news_list
            news = _build_news_list().get("articles", [])
        except Exception as exc:  # noqa: BLE001
            logger.debug("premarket news fallback failed: %s", exc)
            return 0
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in news:
        title = str(item.get("title") or "").strip()
        if not title or title in seen:
            continue
        summary = str(item.get("summary") or item.get("description") or item.get("snippet") or "").strip()
        url = str(item.get("url") or item.get("link") or "")
        if not _is_premarket_news_like(title, summary, url):
            continue
        seen.add(title)
        rows.append({
            "category": item.get("category") or _categorize_news(title, summary),
            "title": title,
            "summary": summary,
            "url": url,
            "source": item.get("source"),
            "published_at": item.get("published_at") or item.get("published") or item.get("pub_date"),
        })
        if len(rows) >= 36:
            break
    return store.upsert_premarket_news(trade_date, rows)


def _build_morning_brief_payload(store: MarketStore, trade_date: str, common: dict[str, Any]) -> dict[str, Any]:
    indices = store.get_global_market_indices(trade_date, limit=20)
    themes = store.get_us_theme_snapshot(trade_date, limit=20)
    transmissions = store.get_us_a_share_transmission(trade_date, limit=20)
    news = store.get_premarket_news(trade_date, limit=40)
    missing = list(common.get("missing_tables") or [])
    if not indices:
        missing.append("global_market_index_daily")
    if not themes:
        missing.append("us_theme_snapshot")
    if not transmissions:
        missing.append("us_a_share_transmission")
    if not news:
        missing.append("premarket_news")

    up_count = sum(1 for item in indices if _num(item.get("change_pct")) > 0)
    down_count = sum(1 for item in indices if _num(item.get("change_pct")) < 0)
    positive_themes = [item for item in themes if _num(item.get("change_pct")) > 0.35]
    negative_themes = [item for item in themes if _num(item.get("change_pct")) < -0.35]
    if indices and themes:
        prediction = (
            f"隔夜美股关键指数{up_count}涨{down_count}跌；"
            f"强势映射方向{len(positive_themes)}个，承压方向{len(negative_themes)}个。"
            "早盘重点观察对应A股题材的竞价强度、开盘承接和资金流同步情况。"
        )
    else:
        prediction = "盘前预测所需表未同步完整，不生成方向性预测总结。"

    grouped_news = {key: [] for key in ("policy", "industry", "catalyst", "risk")}
    for item in news:
        grouped_news.setdefault(str(item.get("category") or "industry"), []).append(item)
    payload = {
        **common,
        "data_status": "partial" if missing else "ok",
        "missing_tables": sorted(set(missing)),
        "title": "早盘内参",
        "prediction_summary": prediction,
        "overnight_indices": indices,
        "us_theme_mapping": themes,
        "transmission_analysis": transmissions,
        "premarket_news": grouped_news,
    }
    return payload


def _pool_payload(store: MarketStore, trade_date: str) -> dict[str, Any]:
    limit_up = store.get_pool("limitup", trade_date)
    limit_down = store.get_pool("limitdown", trade_date)
    fire = store.get_pool("fire", trade_date)
    previous = store.get_pool("previous", trade_date)
    names = store.security_names([str(r.get("code") or "") for r in [*limit_up, *limit_down, *fire]])

    def symbol(row: dict[str, Any]) -> str:
        return str(row.get("code") or "")

    def name(row: dict[str, Any]) -> str:
        code = symbol(row)
        return names.get(code.upper()) or names.get(code.split(".", 1)[0]) or str(row.get("name") or code)

    def days(row: dict[str, Any]) -> int:
        value = row.get("c_times") or row.get("days") or row.get("lianban") or 1
        try:
            return max(1, int(float(value)))
        except (TypeError, ValueError):
            return 1

    ladder: dict[int, list[dict[str, Any]]] = {}
    for row in limit_up:
        d = days(row)
        ladder.setdefault(d, []).append({"symbol": symbol(row), "name": name(row), "days": d})
    non_st_limit_up = sum(1 for row in limit_up if "ST" not in name(row).upper())
    st_limit_up = max(0, len(limit_up) - non_st_limit_up)
    touched_limit_up_count = len(limit_up) + len(fire)
    fail_rate = round(len(fire) / touched_limit_up_count * 100, 2) if touched_limit_up_count else None
    promoted_count = sum(1 for row in limit_up if days(row) >= 2)
    promotion_rate = round(promoted_count / len(previous) * 100, 2) if previous else None
    sealed_amount_billion = round(sum(_num(row.get("seal_amount")) for row in limit_up) / 100_000_000, 2)
    return {
        "trade_date": trade_date,
        "limit_up_count": len(limit_up),
        "limit_down_count": len(limit_down),
        "touched_limit_up_count": touched_limit_up_count,
        "failed_limit_up_count": len(fire),
        "non_st_limit_up_count": non_st_limit_up,
        "st_limit_up_count": st_limit_up,
        "fail_rate": fail_rate,
        "promotion_rate": promotion_rate,
        "sealed_amount_billion": sealed_amount_billion,
        "max_limit_up_height": max(ladder.keys()) if ladder else 0,
        "limit_up_list": [{"symbol": symbol(r), "name": name(r), "days": days(r)} for r in limit_up[:30]],
        "limit_down_list": [{"symbol": symbol(r), "name": name(r), "days": 0} for r in limit_down[:30]],
        "failed_limit_up_list": [{"symbol": symbol(r), "name": name(r), "days": 0} for r in fire[:30]],
        "limitup_ladder": [
            {"days": d, "count": len(items), "stocks": items}
            for d, items in sorted(ladder.items(), reverse=True)
        ],
    }


def _sync_market_breadth_snapshot(store: MarketStore, trade_date: str) -> int:
    def _write_from_rows(rows: list[dict[str, Any]], *, source: str) -> int:
        if len(rows) < 1000:
            store.delete_market_breadth_snapshot(trade_date)
            _set_sync_error(store, "market_breadth", source, f"incomplete market breadth rows: {len(rows)}")
            return 0
        changes = [_num(row.get("change_pct")) for row in rows]
        amount = sum(_num(row.get("amount")) for row in rows)
        pool = _pool_payload(store, trade_date)
        written = store.upsert_market_breadth_snapshot(
            trade_date,
            {
                "total": len(rows),
                "advancers": sum(1 for value in changes if value > 0),
                "decliners": sum(1 for value in changes if value < 0),
                "unchanged": sum(1 for value in changes if value == 0),
                "limit_up": pool["limit_up_count"],
                "limit_down": pool["limit_down_count"],
                "max_limit_up_height": pool["max_limit_up_height"],
                "turnover_billion": round(amount / 100_000_000, 2) if amount else None,
                "source": source,
            },
        )
        if written:
            _clear_sync_error(store, "market_breadth", source)
        return written

    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
    except Exception as exc:  # noqa: BLE001
        _set_sync_error(store, "market_breadth", "akshare.stock_zh_a_spot_em", exc)
        df = None
    if df is None or df.empty:
        _set_sync_error(store, "market_breadth", "akshare.stock_zh_a_spot_em", "empty result")
    else:
        pct_col = _pick_column(df, ("涨跌幅",), None) or _pick_any_column(df, ("pct_chg", "change_pct"))
        amount_col = _pick_column(df, ("成交额",), None) or _pick_any_column(df, ("amount", "turnover"))
        if pct_col:
            rows = [
                {"change_pct": raw.get(pct_col), "amount": raw.get(amount_col) if amount_col else 0}
                for _, raw in df.iterrows()
            ]
            written = _write_from_rows(rows, source="akshare.stock_zh_a_spot_em")
            if written:
                return written
        else:
            _set_sync_error(store, "market_breadth", "akshare.stock_zh_a_spot_em", f"missing pct column: {list(df.columns)}")

    try:
        import akshare as ak
        df_alt = ak.stock_zh_a_spot()
    except Exception as exc:  # noqa: BLE001
        _set_sync_error(store, "market_breadth", "akshare.stock_zh_a_spot", exc)
        df_alt = None
    if df_alt is not None and not df_alt.empty:
        pct_col = _pick_column(df_alt, ("涨跌幅",), None) or _pick_any_column(df_alt, ("pct_chg", "change_pct"))
        amount_col = _pick_column(df_alt, ("成交金额",), None) or _pick_column(df_alt, ("成交额",), None) or _pick_any_column(df_alt, ("amount", "turnover"))
        if pct_col:
            rows = [
                {"change_pct": raw.get(pct_col), "amount": raw.get(amount_col) if amount_col else 0}
                for _, raw in df_alt.iterrows()
            ]
            written = _write_from_rows(rows, source="akshare.stock_zh_a_spot")
            if written:
                return written
        else:
            _set_sync_error(store, "market_breadth", "akshare.stock_zh_a_spot", f"missing pct column: {list(df_alt.columns)}")

    raw_rows: list[dict[str, Any]] = []
    for page in range(1, 80):
        page_rows = _eastmoney_clist_rows(
            "https://push2.eastmoney.com/api/qt/clist/get",
            {
                "fid": "f3",
                "po": "1",
                "pz": "100",
                "pn": str(page),
                "np": "1",
                "fltt": "2",
                "invt": "2",
                "ut": "b2884a393a59ad64002292a3e90d46a5",
                "fs": "m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2,m:0+t:7+f:!2,m:1+t:3+f:!2",
                "fields": "f12,f14,f3,f6",
            },
            "market_breadth",
            "eastmoney.clist.a_spot",
            store,
        )
        if not page_rows:
            break
        raw_rows.extend(page_rows)
        if len(page_rows) < 100:
            break
        time.sleep(0.15)
    rows = [{"change_pct": raw.get("f3"), "amount": raw.get("f6")} for raw in raw_rows]
    written = _write_from_rows(rows, source="eastmoney.clist.a_spot")
    if not written:
        _set_sync_error(store, "market_breadth", "eastmoney.clist.a_spot", "empty mapped result")
    return written


def _sync_stage_snapshots(store: MarketStore, trade_date: str) -> int:
    """Materialize the four stage pages from already-synced formal tables."""
    written = 0
    pool = _pool_payload(store, trade_date)
    breadth_snapshot = store.get_market_breadth_snapshot(trade_date) or {}
    sector_capital = store.get_sector_capital(trade_date, limit=10)
    stock_inflow = store.get_stock_capital_rank(trade_date, "inflow", limit=10)
    stock_outflow = store.get_stock_capital_rank(trade_date, "outflow", limit=10)
    concepts = store.get_sector_snapshot(trade_date, "concept", limit=40)
    industries = store.get_sector_snapshot(trade_date, "industry", limit=20)

    missing = []
    if not pool["limit_up_count"] and not pool["limit_down_count"]:
        missing.append("stock_pool")
    if not sector_capital:
        missing.append("sector_capital_flow")
    if not stock_inflow and not stock_outflow:
        missing.append("stock_capital_rank")
    if not concepts and not industries:
        missing.append("sector_snapshot")
    if not breadth_snapshot:
        missing.append("market_breadth_snapshot")

    board_rows = concepts or industries
    main_lines = [
        {"name": r.get("name"), "change_pct": r.get("change_pct"), "leader": r.get("leader")}
        for r in board_rows[:5]
    ]

    common = {
        "trade_date": trade_date,
        "data_status": "partial" if missing else "ok",
        "missing_tables": missing,
        "source_policy": "db_only",
    }
    emotion_score = min(100.0, max(0.0, 50.0 + pool["limit_up_count"] * 0.35 - pool["limit_down_count"] * 0.55 + pool["max_limit_up_height"] * 2.5))
    if emotion_score >= 70:
        emotion_phase = "升温"
        emotion_note = "涨停与连板结构偏强，关注主线承接。"
    elif emotion_score <= 35:
        emotion_phase = "退潮"
        emotion_note = "跌停或亏钱效应偏强，优先控制回撤。"
    else:
        emotion_phase = "震荡"
        emotion_note = "情绪温和，观察资金是否形成一致方向。"
    hot_boards = concepts[:8] or industries[:8]
    limitup_ladder = pool.get("limitup_ladder") or []
    tail_watch = []
    for item in (pool.get("limit_up_list") or [])[:8]:
        tail_watch.append({
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "label": f"{item.get('days') or 1}板",
            "basis": "来自 stock_pool 涨停池；仅作为观察池，不生成交易指令。",
        })
    close_topic_cards = [
        {
            "name": row.get("name"),
            "change_pct": row.get("change_pct"),
            "leader": row.get("leader"),
            "basis": "来自 sector_snapshot 收盘题材/行业快照。",
        }
        for row in hot_boards[:6]
    ]

    snapshots = {
        "morning-brief": {
            **common,
            "title": "早盘内参",
            "brief": "盘前内参只展示已落库的隔夜、板块、资金和事件数据；缺失项不做推断。",
            "indices": [],
            "overnight_markets": [],
            "mapped_themes": main_lines,
            "key_events": [],
            "risk_note": "缺少隔夜市场/事件正式快照表时，不生成盘前映射结论。",
        },
        "intraday-monitor": {
            **common,
            "title": "盘中监控",
            "emotion": {
                "score": round(emotion_score, 1),
                "phase": emotion_phase,
                "note": emotion_note,
                "temperature": "高" if emotion_score >= 70 else ("低" if emotion_score <= 35 else "温和"),
            },
            "breadth": {
                "total": breadth_snapshot.get("total"),
                "advancers": breadth_snapshot.get("advancers"),
                "decliners": breadth_snapshot.get("decliners"),
                "unchanged": breadth_snapshot.get("unchanged"),
                "limit_up": pool["limit_up_count"],
                "limit_down": pool["limit_down_count"],
                "max_limit_up_height": pool["max_limit_up_height"],
                "turnover_billion": breadth_snapshot.get("turnover_billion"),
                "source": breadth_snapshot.get("source"),
            },
            "hot_sectors": industries[:10],
            "hot_themes": hot_boards,
            "limitup_ladder": limitup_ladder,
            "sector_capital_top": sector_capital[:5],
            "stock_inflow_top": stock_inflow[:8],
            "stock_outflow_top": stock_outflow[:8],
            "alerts": [
                {"title": emotion_phase, "message": emotion_note, "source": "stock_pool"},
            ] if pool["limit_up_count"] or pool["limit_down_count"] else [],
            "scheduled_tasks": [],
        },
        "tail-strategy": {
            **common,
            "title": "尾盘策略",
            "pools": pool,
            "emotion": {
                "score": round(emotion_score, 1),
                "phase": emotion_phase,
                "note": emotion_note,
                "temperature": "高" if emotion_score >= 70 else ("低" if emotion_score <= 35 else "温和"),
            },
            "sector_capital_top": sector_capital[:5],
            "watchlist": tail_watch,
            "next_day_plan": [
                {
                    "title": "强势延续观察",
                    "items": [row.get("name") for row in tail_watch[:4] if row.get("name")],
                    "basis": "来自涨停池和连板高度；次日仅观察竞价与承接。",
                },
                {
                    "title": "风险规避",
                    "items": [row.get("name") for row in (pool.get("limit_down_list") or [])[:4] if row.get("name")],
                    "basis": "来自跌停池；情绪转弱时优先避开。",
                },
            ],
            "risk_blocks": [
                {"title": "亏钱效应", "value": pool["limit_down_count"], "basis": "stock_pool.limitdown"},
                {"title": "连板高度", "value": pool["max_limit_up_height"], "basis": "stock_pool.limitup"},
                {"title": "资金表", "value": "已同步" if sector_capital else "未同步", "basis": "sector_capital_flow"},
            ],
            "decisions": [],
            "groups": {},
            "rules": [
                "尾盘动作卡必须来自正式策略/持仓/风险表；缺表时不生成候选。",
                "涨跌停和连板只读取 stock_pool。",
            ],
        },
        "close-review": {
            **common,
            "title": "收盘复盘",
            "pools": pool,
            "sector_capital_top": sector_capital[:5],
            "themes": {"concept_sectors": concepts, "industry_sectors": industries, "main_lines": main_lines},
            "topic_cards": close_topic_cards,
            "summary": {
                "emotion_score": round(emotion_score, 1),
                "emotion_phase": emotion_phase,
                "limit_up": pool["limit_up_count"],
                "limit_down": pool["limit_down_count"],
                "max_limit_up_height": pool["max_limit_up_height"],
            },
            "dig_records": close_topic_cards,
            "tomorrow_watch": [],
            "review_questions": [
                "今日涨跌停与连板结构如何？",
                "资金流正式表是否已同步？",
                "主线/题材快照是否已同步？",
            ],
        },
    }
    for stage, payload in snapshots.items():
        written += store.upsert_market_stage_snapshot(
            trade_date,
            stage,
            payload,
            source_tables=["stock_pool", "sector_capital_flow", "stock_capital_rank", "sector_snapshot"],
        )
    return written


def _sync_stage_snapshots(store: MarketStore, trade_date: str) -> int:
    """Materialize the four stage pages from already-synced formal tables."""
    written = 0
    pool = _pool_payload(store, trade_date)
    sector_capital = store.get_sector_capital(trade_date, limit=10)
    stock_inflow = store.get_stock_capital_rank(trade_date, "inflow", limit=10)
    stock_outflow = store.get_stock_capital_rank(trade_date, "outflow", limit=10)
    concepts = store.get_sector_snapshot(trade_date, "concept", limit=40)
    industries = store.get_sector_snapshot(trade_date, "industry", limit=20)

    missing = []
    if not pool["limit_up_count"] and not pool["limit_down_count"]:
        missing.append("stock_pool")
    if not sector_capital:
        missing.append("sector_capital_flow")
    if not stock_inflow and not stock_outflow:
        missing.append("stock_capital_rank")
    if not concepts and not industries:
        missing.append("sector_snapshot")

    board_rows = concepts or industries
    main_lines = [
        {"name": r.get("name"), "change_pct": r.get("change_pct"), "leader": r.get("leader")}
        for r in board_rows[:5]
    ]
    common = {
        "trade_date": trade_date,
        "data_status": "partial" if missing else "ok",
        "missing_tables": missing,
        "source_policy": "db_only",
    }
    morning_common = {
        "trade_date": trade_date,
        "data_status": "ok",
        "missing_tables": [],
        "source_policy": "db_only",
    }
    emotion_score = min(
        100.0,
        max(0.0, 50.0 + pool["limit_up_count"] * 0.35 - pool["limit_down_count"] * 0.55 + pool["max_limit_up_height"] * 2.5),
    )
    if emotion_score >= 70:
        emotion_phase = "\u56de\u6696"
        emotion_temperature = "\u504f\u70ed"
        emotion_note = "\u6da8\u505c\u548c\u8fde\u677f\u7ed3\u6784\u504f\u5f3a\uff0c\u91cd\u70b9\u89c2\u5bdf\u4e3b\u7ebf\u627f\u63a5\u3002"
    elif emotion_score <= 35:
        emotion_phase = "\u9000\u6f6e"
        emotion_temperature = "\u504f\u51b7"
        emotion_note = "\u8dcc\u505c\u6216\u4e8f\u94b1\u6548\u5e94\u504f\u5f3a\uff0c\u4f18\u5148\u63a7\u5236\u56de\u64a4\u3002"
    else:
        emotion_phase = "\u9707\u8361"
        emotion_temperature = "\u6e29\u548c"
        emotion_note = "\u60c5\u7eea\u6e29\u548c\uff0c\u89c2\u5bdf\u8d44\u91d1\u662f\u5426\u5f62\u6210\u4e00\u81f4\u65b9\u5411\u3002"
    emotion = {
        "score": round(emotion_score, 1),
        "phase": emotion_phase,
        "temperature": emotion_temperature,
        "note": emotion_note,
    }
    hot_themes = concepts[:8] or industries[:8]
    tail_watch = [
        {
            "symbol": row.get("symbol"),
            "name": row.get("name"),
            "label": f"{row.get('days') or 1}\u677f",
            "basis": "\u6765\u81ea stock_pool \u6da8\u505c\u6c60\uff1b\u4ec5\u4f5c\u89c2\u5bdf\uff0c\u4e0d\u751f\u6210\u4ea4\u6613\u6307\u4ee4\u3002",
        }
        for row in (pool.get("limit_up_list") or [])[:8]
    ]
    tail_candidates = []
    for idx, row in enumerate((pool.get("limit_up_list") or [])[:6], start=1):
        days = int(_num(row.get("days") or 1))
        theme = (hot_themes[(idx - 1) % len(hot_themes)] if hot_themes else {}) or {}
        category = "\u4e3b\u5347\u4e2d\u6bb5" if days >= 3 else ("\u52a0\u901f\u6bb5" if days == 2 else "\u8d8b\u52bf\u4e2d\u6bb5")
        tail_candidates.append({
            "rank": idx,
            "symbol": row.get("symbol"),
            "name": row.get("name"),
            "theme": theme.get("name") or "\u8fde\u677f\u60c5\u7eea",
            "line_type": "\u4e3b\u7ebf" if days >= 3 else "\u6b21\u4e3b\u7ebf",
            "stage": category,
            "reason": f"{days}\u677f\u8fde\u677f\u6c60\uff0c\u5c3e\u76d8\u89c2\u5bdf\u5c01\u677f\u5f3a\u5ea6\u548c\u6b21\u65e5\u627f\u63a5\u3002",
            "position_note": "\u89c2\u5bdf\u4ed3\uff1b\u4e0d\u751f\u6210\u4e70\u5165\u6307\u4ee4",
            "stop_note": "\u8dcc\u7834\u5c01\u677f\u627f\u63a5\u6216\u6b21\u65e5\u4f4e\u5f00\u4e0d\u4fee\u590d",
            "risk": "\u9ad8\u4f4d\u8fde\u677f\u5206\u6b67\u98ce\u9669",
            "rr": round(1.8 + min(days, 5) * 0.25, 1),
            "source": "stock_pool.limitup",
        })
    watch_groups = [
        {
            "title": "\u5c3e\u76d8\u8d44\u91d1\u56de\u6d41",
            "items": [
                {
                    "name": row.get("sector"),
                    "note": f"\u4e3b\u529b\u51c0\u6d41 {round(_num(row.get('main_net')) / 100000000, 2)}\u4ebf\uff0c\u5c3e\u76d8\u89c2\u5bdf\u627f\u63a5\u3002",
                }
                for row in sector_capital[:2]
            ],
        },
        {
            "title": "\u6b21\u65e5\u6709\u9884\u671f",
            "items": [
                {"name": row.get("name"), "note": f"{row.get('label')}\u9ad8\u5ea6\uff0c\u6b21\u65e5\u770b\u5206\u6b67\u8f6c\u4e00\u81f4\u3002"}
                for row in tail_watch[:2]
            ],
        },
        {
            "title": "\u5f3a\u52bf\u677f\u5757 \u00b7 \u53ef\u80fd\u5ef6\u7eed",
            "items": [
                {"name": row.get("name"), "note": f"\u677f\u5757\u6da8\u5e45 {round(_num(row.get('change_pct')), 2)}%\uff0c\u8ddf\u8e2a\u4e3b\u7ebf\u627f\u63a5\u3002"}
                for row in hot_themes[:2]
            ],
        },
        {
            "title": "\u8d8b\u52bf\u7968 \u00b7 \u7ee7\u7eed\u8ddf\u8e2a",
            "items": [
                {"name": row.get("name"), "note": "\u8fde\u677f\u6c60\u5f3a\u52bf\u6837\u672c\uff0c\u4ec5\u505a\u8d8b\u52bf\u89c2\u5bdf\u3002"}
                for row in tail_watch[2:4]
            ],
        },
        {
            "title": "\u9ad8\u76c8\u4e8f\u6bd4",
            "items": [
                {"name": row.get("name"), "note": f"\u76c8\u4e8f\u6bd4 {row.get('rr')}\uff0c\u98ce\u9669\uff1a{row.get('risk')}"}
                for row in tail_candidates[:2]
            ],
        },
    ]
    next_day_plan = [
        {
            "title": "\u5f3a\u52bf\u5ef6\u7eed\u89c2\u5bdf",
            "items": [row.get("name") for row in tail_watch[:4] if row.get("name")],
            "basis": "\u6765\u81ea\u6da8\u505c\u6c60\u548c\u8fde\u677f\u9ad8\u5ea6\uff0c\u6b21\u65e5\u53ea\u89c2\u5bdf\u7ade\u4ef7\u4e0e\u627f\u63a5\u3002",
        },
        {
            "title": "\u98ce\u9669\u89c4\u907f",
            "items": [row.get("name") for row in (pool.get("limit_down_list") or [])[:4] if row.get("name")],
            "basis": "\u6765\u81ea\u8dcc\u505c\u6c60\uff1b\u60c5\u7eea\u8f6c\u5f31\u65f6\u4f18\u5148\u89c4\u907f\u3002",
        },
    ]
    risk_blocks = [
        {"title": "\u8dcc\u505c\u5bb6\u6570", "value": pool["limit_down_count"], "basis": "stock_pool.limitdown"},
        {"title": "\u8fde\u677f\u9ad8\u5ea6", "value": pool["max_limit_up_height"], "basis": "stock_pool.limitup"},
        {"title": "\u884c\u4e1a\u8d44\u91d1", "value": "\u5df2\u540c\u6b65" if sector_capital else "\u672a\u540c\u6b65", "basis": "sector_capital_flow"},
    ]
    topic_cards = [
        {
            "name": row.get("name"),
            "change_pct": row.get("change_pct"),
            "leader": row.get("leader"),
            "basis": "\u6765\u81ea sector_snapshot \u9898\u6750/\u884c\u4e1a\u5feb\u7167\u3002",
        }
        for row in hot_themes[:6]
    ]
    snapshots = {
        "morning-brief": _build_morning_brief_payload(store, trade_date, morning_common),
        "intraday-monitor": {
            **common,
            "title": "盘中监控",
            "breadth": {
                "limit_up": pool["limit_up_count"],
                "limit_down": pool["limit_down_count"],
                "max_limit_up_height": pool["max_limit_up_height"],
            },
            "hot_sectors": industries[:10],
            "sector_capital_top": sector_capital[:5],
            "stock_inflow_top": stock_inflow[:8],
            "stock_outflow_top": stock_outflow[:8],
            "alerts": [],
            "scheduled_tasks": [],
        },
        "tail-strategy": {
            **common,
            "title": "尾盘策略",
            "pools": pool,
            "sector_capital_top": sector_capital[:5],
            "decisions": [],
            "groups": {},
            "rules": [
                "尾盘动作卡必须来自正式策略/持仓/风险表；缺表时不生成候选。",
                "涨跌停和连板只读取 stock_pool。",
            ],
        },
        "close-review": {
            **common,
            "title": "收盘复盘",
            "pools": pool,
            "sector_capital_top": sector_capital[:5],
            "themes": {"concept_sectors": concepts, "industry_sectors": industries, "main_lines": main_lines},
            "summary": {},
            "tomorrow_watch": [],
            "review_questions": [
                "今日涨跌停与连板结构如何？",
                "资金流正式表是否已同步？",
                "主线/题材快照是否已同步？",
            ],
        },
    }
    for stage, payload in snapshots.items():
        source_tables = (
            ["global_market_index_daily", "us_theme_snapshot", "us_a_share_transmission", "premarket_news"]
            if stage == "morning-brief"
            else ["stock_pool", "sector_capital_flow", "stock_capital_rank", "sector_snapshot"]
        )
        written += store.upsert_market_stage_snapshot(
            trade_date,
            stage,
            payload,
            source_tables=source_tables,
        )
    return written


def _sync_stage_snapshots(store: MarketStore, trade_date: str) -> int:
    """Materialize the four stage pages from synced DB tables only."""
    written = 0
    pool = _pool_payload(store, trade_date)
    breadth_snapshot = store.get_market_breadth_snapshot(trade_date) or {}
    sector_capital = store.get_sector_capital(trade_date, limit=10)
    stock_inflow = store.get_stock_capital_rank(trade_date, "inflow", limit=10)
    stock_outflow = store.get_stock_capital_rank(trade_date, "outflow", limit=10)
    concepts = store.get_sector_snapshot(trade_date, "concept", limit=40)
    industries = store.get_sector_snapshot(trade_date, "industry", limit=20)

    missing = []
    if not pool["limit_up_count"] and not pool["limit_down_count"]:
        missing.append("stock_pool")
    if not sector_capital:
        missing.append("sector_capital_flow")
    if not stock_inflow and not stock_outflow:
        missing.append("stock_capital_rank")
    if not concepts and not industries:
        missing.append("sector_snapshot")

    intraday_missing = [*missing]
    if not breadth_snapshot:
        intraday_missing.append("market_breadth_snapshot")

    common = {
        "trade_date": trade_date,
        "data_status": "partial" if missing else "ok",
        "missing_tables": missing,
        "source_policy": "db_only",
    }
    intraday_common = {
        "trade_date": trade_date,
        "data_status": "partial" if intraday_missing else "ok",
        "missing_tables": intraday_missing,
        "source_policy": "db_only",
    }
    morning_common = {
        "trade_date": trade_date,
        "data_status": "ok",
        "missing_tables": [],
        "source_policy": "db_only",
    }

    board_rows = concepts or industries
    main_lines = [
        {"name": r.get("name"), "change_pct": r.get("change_pct"), "leader": r.get("leader")}
        for r in board_rows[:5]
    ]
    emotion_score = min(
        100.0,
        max(0.0, 50.0 + pool["limit_up_count"] * 0.35 - pool["limit_down_count"] * 0.55 + pool["max_limit_up_height"] * 2.5),
    )
    if emotion_score >= 70:
        emotion_phase = "\u56de\u6696"
        emotion_temperature = "\u504f\u70ed"
        emotion_note = "\u6da8\u505c\u548c\u8fde\u677f\u7ed3\u6784\u504f\u5f3a\uff0c\u91cd\u70b9\u89c2\u5bdf\u4e3b\u7ebf\u627f\u63a5\u3002"
    elif emotion_score <= 35:
        emotion_phase = "\u9000\u6f6e"
        emotion_temperature = "\u504f\u51b7"
        emotion_note = "\u8dcc\u505c\u6216\u4e8f\u94b1\u6548\u5e94\u504f\u5f3a\uff0c\u4f18\u5148\u63a7\u5236\u56de\u64a4\u3002"
    else:
        emotion_phase = "\u9707\u8361"
        emotion_temperature = "\u6e29\u548c"
        emotion_note = "\u60c5\u7eea\u6e29\u548c\uff0c\u89c2\u5bdf\u8d44\u91d1\u662f\u5426\u5f62\u6210\u4e00\u81f4\u65b9\u5411\u3002"
    emotion = {
        "score": round(emotion_score, 1),
        "phase": emotion_phase,
        "temperature": emotion_temperature,
        "note": emotion_note,
    }
    hot_themes = concepts[:8] or industries[:8]
    tail_watch = [
        {
            "symbol": row.get("symbol"),
            "name": row.get("name"),
            "label": f"{row.get('days') or 1}\u677f",
            "basis": "\u6765\u81ea stock_pool \u6da8\u505c\u6c60\uff1b\u4ec5\u4f5c\u89c2\u5bdf\uff0c\u4e0d\u751f\u6210\u4ea4\u6613\u6307\u4ee4\u3002",
        }
        for row in (pool.get("limit_up_list") or [])[:8]
    ]
    tail_candidates = []
    for idx, row in enumerate((pool.get("limit_up_list") or [])[:6], start=1):
        days = int(_num(row.get("days") or 1))
        theme = (hot_themes[(idx - 1) % len(hot_themes)] if hot_themes else {}) or {}
        stage_label = "\u4e3b\u5347\u4e2d\u6bb5" if days >= 3 else ("\u52a0\u901f\u6bb5" if days == 2 else "\u8d8b\u52bf\u4e2d\u6bb5")
        tail_candidates.append({
            "rank": idx,
            "symbol": row.get("symbol"),
            "name": row.get("name"),
            "theme": theme.get("name") or "\u8fde\u677f\u60c5\u7eea",
            "line_type": "\u4e3b\u7ebf" if days >= 3 else "\u6b21\u4e3b\u7ebf",
            "stage": stage_label,
            "reason": f"{days}\u677f\u8fde\u677f\u6c60\uff0c\u5c3e\u76d8\u89c2\u5bdf\u5c01\u677f\u5f3a\u5ea6\u548c\u6b21\u65e5\u627f\u63a5\u3002",
            "position_note": "\u89c2\u5bdf\u4ed3\uff1b\u4e0d\u751f\u6210\u4e70\u5165\u6307\u4ee4",
            "stop_note": "\u8dcc\u7834\u5c01\u677f\u627f\u63a5\u6216\u6b21\u65e5\u4f4e\u5f00\u4e0d\u4fee\u590d",
            "risk": "\u9ad8\u4f4d\u8fde\u677f\u5206\u6b67\u98ce\u9669",
            "rr": round(1.8 + min(days, 5) * 0.25, 1),
            "source": "stock_pool.limitup",
        })
    watch_groups = [
        {
            "title": "\u5c3e\u76d8\u8d44\u91d1\u56de\u6d41",
            "items": [
                {
                    "name": row.get("sector"),
                    "note": f"\u4e3b\u529b\u51c0\u6d41 {round(_num(row.get('main_net')) / 100000000, 2)}\u4ebf\uff0c\u5c3e\u76d8\u89c2\u5bdf\u627f\u63a5\u3002",
                }
                for row in sector_capital[:2]
            ],
        },
        {
            "title": "\u6b21\u65e5\u6709\u9884\u671f",
            "items": [
                {"name": row.get("name"), "note": f"{row.get('label')}\u9ad8\u5ea6\uff0c\u6b21\u65e5\u770b\u5206\u6b67\u8f6c\u4e00\u81f4\u3002"}
                for row in tail_watch[:2]
            ],
        },
        {
            "title": "\u5f3a\u52bf\u677f\u5757 \u00b7 \u53ef\u80fd\u5ef6\u7eed",
            "items": [
                {"name": row.get("name"), "note": f"\u677f\u5757\u6da8\u5e45 {round(_num(row.get('change_pct')), 2)}%\uff0c\u8ddf\u8e2a\u4e3b\u7ebf\u627f\u63a5\u3002"}
                for row in hot_themes[:2]
            ],
        },
        {
            "title": "\u8d8b\u52bf\u7968 \u00b7 \u7ee7\u7eed\u8ddf\u8e2a",
            "items": [
                {"name": row.get("name"), "note": "\u8fde\u677f\u6c60\u5f3a\u52bf\u6837\u672c\uff0c\u4ec5\u505a\u8d8b\u52bf\u89c2\u5bdf\u3002"}
                for row in tail_watch[2:4]
            ],
        },
        {
            "title": "\u9ad8\u76c8\u4e8f\u6bd4",
            "items": [
                {"name": row.get("name"), "note": f"\u76c8\u4e8f\u6bd4 {row.get('rr')}\uff0c\u98ce\u9669\uff1a{row.get('risk')}"}
                for row in tail_candidates[:2]
            ],
        },
    ]
    next_day_plan = [
        {
            "title": "\u5f3a\u52bf\u5ef6\u7eed\u89c2\u5bdf",
            "items": [row.get("name") for row in tail_watch[:4] if row.get("name")],
            "basis": "\u6765\u81ea\u6da8\u505c\u6c60\u548c\u8fde\u677f\u9ad8\u5ea6\uff0c\u6b21\u65e5\u53ea\u89c2\u5bdf\u7ade\u4ef7\u4e0e\u627f\u63a5\u3002",
        },
        {
            "title": "\u98ce\u9669\u89c4\u907f",
            "items": [row.get("name") for row in (pool.get("limit_down_list") or [])[:4] if row.get("name")],
            "basis": "\u6765\u81ea\u8dcc\u505c\u6c60\uff1b\u60c5\u7eea\u8f6c\u5f31\u65f6\u4f18\u5148\u89c4\u907f\u3002",
        },
    ]
    risk_blocks = [
        {"title": "\u8dcc\u505c\u5bb6\u6570", "value": pool["limit_down_count"], "basis": "stock_pool.limitdown"},
        {"title": "\u8fde\u677f\u9ad8\u5ea6", "value": pool["max_limit_up_height"], "basis": "stock_pool.limitup"},
        {"title": "\u884c\u4e1a\u8d44\u91d1", "value": "\u5df2\u540c\u6b65" if sector_capital else "\u672a\u540c\u6b65", "basis": "sector_capital_flow"},
    ]
    topic_cards = [
        {
            "name": row.get("name"),
            "change_pct": row.get("change_pct"),
            "leader": row.get("leader"),
            "basis": "\u6765\u81ea sector_snapshot \u9898\u6750/\u884c\u4e1a\u5feb\u7167\u3002",
        }
        for row in hot_themes[:6]
    ]
    top_money = sector_capital[:3]
    money_names = "、".join(str(row.get("sector") or "-") for row in top_money if row.get("sector")) or "未同步"
    losing_names = "、".join(str(row.get("name") or "-") for row in (pool.get("limit_down_list") or [])[:3] if row.get("name")) or "无跌停池数据"
    leader_names = "、".join(str(row.get("name") or "-") for row in (pool.get("limit_up_list") or [])[:3] if row.get("name")) or "无涨停池数据"
    main_line_names = "、".join(str(row.get("name") or "-") for row in hot_themes[:3] if row.get("name")) or "题材快照未同步"
    attack_or_defense = "进攻偏谨慎" if emotion_score >= 60 and pool["limit_up_count"] >= pool["limit_down_count"] else "防守优先"
    style_preference = "连板/趋势共振" if pool["max_limit_up_height"] >= 4 else "低吸/防守"
    close_review_cards = [
        {
            "title": "今天的钱在哪?",
            "tone": "red",
            "summary": f"{money_names} 资金居前，主线观察集中在 {main_line_names}。",
        },
        {
            "title": "亏钱效应在哪?",
            "tone": "green",
            "summary": f"跌停 {pool['limit_down_count']} 家；重点观察 {losing_names} 代表的亏钱扩散。",
        },
        {
            "title": "当前情绪阶段?",
            "tone": "amber",
            "summary": f"{emotion_phase}，温度 {round(emotion_score, 1)}，最高连板 {pool['max_limit_up_height']}。",
        },
        {
            "title": "明天进攻还是防守?",
            "tone": "amber",
            "summary": f"{attack_or_defense}；涨停 {pool['limit_up_count']}、跌停 {pool['limit_down_count']}，次日看主线承接。",
        },
        {
            "title": "主线是否还有持续性?",
            "tone": "red",
            "summary": f"主线样本：{main_line_names}；若高标断层扩大则转防守。",
        },
        {
            "title": "高标有无亏钱效应?",
            "tone": "green",
            "summary": f"最高 {pool['max_limit_up_height']} 板，代表样本 {leader_names}；观察断板后的反馈。",
        },
        {
            "title": "龙头战法今天能不能做?",
            "tone": "amber",
            "summary": "只能做强承接样本；缺少交易策略/持仓风控表时不生成交易动作。",
        },
        {
            "title": "市场风格偏好?",
            "tone": "red",
            "summary": f"{style_preference}；由连板高度、涨跌停和题材快照综合生成。",
        },
    ]
    close_style_panels = [
        {
            "title": "市场风格偏好",
            "subtitle": "连板 / 趋势 / 反包 / 单波 / 低吸",
            "metrics": [
                {"name": "连板", "value": min(100, pool["max_limit_up_height"] * 14)},
                {"name": "打板", "value": min(100, pool["limit_up_count"] * 1.6)},
                {"name": "低吸", "value": max(10, 70 - pool["limit_down_count"] * 2)},
                {"name": "单波", "value": 45 if emotion_score >= 50 else 60},
                {"name": "趋势", "value": min(100, 40 + len(hot_themes) * 6)},
            ],
        },
        {
            "title": "主力 / 游资活跃",
            "subtitle": "龙虎榜席位",
            "items": [
                {
                    "name": row.get("sector"),
                    "amount": row.get("main_net"),
                    "tag": (hot_themes[idx % len(hot_themes)].get("name") if hot_themes else "资金"),
                }
                for idx, row in enumerate(sector_capital[:3])
            ],
        },
        {
            "title": "监管票专项",
            "subtitle": "影响短线 / 高标 / 接力",
            "name": ((pool.get("limit_up_list") or [{}])[0]).get("name") or "-",
            "tag": f"{pool['max_limit_up_height']}板 · 关注",
            "summary": f"最高连板 {pool['max_limit_up_height']}，跌停 {pool['limit_down_count']}。若高标断板负反馈扩大，短线接力需降速。",
        },
    ]
    new_theme_tracking = [
        {
            "name": row.get("name"),
            "judgement": "有溢价" if _num(row.get("change_pct")) > 0 else "待确认",
            "summary": f"涨幅 {round(_num(row.get('change_pct')), 2)}%，领涨 {row.get('leader') or '-'}；判断能否继续扩散。",
        }
        for row in hot_themes[:5]
    ]
    holding_sector_strength: list[dict[str, Any]] = []
    try:
        from src.data.watchlist_store import load_watchlist

        watchlist = load_watchlist()
    except Exception:  # noqa: BLE001
        watchlist = []
    if watchlist:
        security_rows = {str(row.get("code") or "").upper(): row for row in store.list_security_master(default_only=False)}
        codes = [str(row.get("symbol") or "").upper() for row in watchlist if row.get("symbol")]
        stock_names = store.security_names(codes)
        etf_names = store.etf_names(codes)
        board_by_leader: dict[str, dict[str, Any]] = {}
        for row in [*concepts, *industries]:
            leader = str(row.get("leader") or "").strip()
            if leader and leader not in board_by_leader:
                board_by_leader[leader] = row
        capital_by_sector = {str(row.get("sector") or ""): row for row in sector_capital}

        for item in watchlist[:20]:
            symbol = str(item.get("symbol") or "").upper()
            if not symbol:
                continue
            bare = symbol.split(".", 1)[0]
            meta = security_rows.get(symbol) or security_rows.get(bare) or {}
            name = stock_names.get(symbol) or stock_names.get(bare) or etf_names.get(symbol) or etf_names.get(bare) or symbol
            is_etf = bare.startswith(("5", "15", "16"))
            sector = str(meta.get("industry") or "").strip()
            if not sector and is_etf:
                sector = "ETF/基金"
            leader_board = board_by_leader.get(name)
            if not sector and leader_board:
                sector = str(leader_board.get("name") or "")
            if not sector:
                sector = "板块待匹配"

            close = None
            prev_close = None
            source_date = None
            change_pct = None
            quote = None if is_etf else (store.get_latest_realtime_quote(symbol, trade_date) or store.get_latest_realtime_quote(bare, trade_date))
            if quote:
                close = _num(quote.get("price"))
                prev_close = _num(quote.get("pre_close"))
                source_date = str(quote.get("trade_date") or trade_date)
                if quote.get("rise_rate") is not None:
                    change_pct = round(_num(quote.get("rise_rate")), 2)
            if not close:
                df = store.get_etf_daily(bare, end=trade_date) if is_etf else store.get_daily_bars(symbol, days=2, end=trade_date)
                if df is not None and not df.empty:
                    tail = df.tail(2)
                    close = float(tail["close"].iloc[-1])
                    prev_close = float(tail["close"].iloc[-2]) if len(tail) >= 2 else None
                    source_date = tail.index[-1].strftime("%Y-%m-%d")
                if change_pct is None and close and prev_close:
                    change_pct = round((close - prev_close) / prev_close * 100, 2)
                if change_pct is None and is_etf and df is not None and not df.empty and "rise" in df.columns:
                    change_pct = round(_num(df["rise"].iloc[-1]) * 100, 2)
            cost = _num(item.get("cost"))
            shares = _num(item.get("shares"))
            pnl_pct = round((close - cost) / cost * 100, 2) if close and cost else None
            market_value = round(close * shares, 2) if close and shares else None

            sector_row = next((row for row in [*concepts, *industries] if str(row.get("name") or "") == sector), None) or leader_board
            capital_row = capital_by_sector.get(sector)
            strength_bits = []
            if sector_row:
                strength_bits.append(f"板块涨幅 {round(_num(sector_row.get('change_pct')), 2)}%")
            if capital_row:
                strength_bits.append(f"主力净流 {round(_num(capital_row.get('main_net')), 2)}亿")
            if pnl_pct is not None:
                strength_bits.append(f"持仓浮盈 {pnl_pct:+.2f}%")
            if source_date != trade_date:
                strength_bits.append(f"行情日期 {source_date or '未同步'}")

            holding_sector_strength.append({
                "symbol": symbol,
                "name": name,
                "sector": sector,
                "change_pct": change_pct,
                "pnl_pct": pnl_pct,
                "market_value": market_value,
                "cost": cost or None,
                "shares": shares or None,
                "sector_change_pct": sector_row.get("change_pct") if sector_row else None,
                "sector_main_net": capital_row.get("main_net") if capital_row else None,
                "note": "；".join(str(bit) for bit in strength_bits) or "持仓行情/板块强弱待同步",
                "source_date": source_date,
            })

    snapshots = {
        "morning-brief": _build_morning_brief_payload(store, trade_date, morning_common),
        "intraday-monitor": {
            **intraday_common,
            "title": "\u76d8\u4e2d\u76d1\u63a7",
            "emotion": emotion,
            "breadth": {
                "total": breadth_snapshot.get("total"),
                "advancers": breadth_snapshot.get("advancers"),
                "decliners": breadth_snapshot.get("decliners"),
                "unchanged": breadth_snapshot.get("unchanged"),
                "limit_up": pool["limit_up_count"],
                "limit_down": pool["limit_down_count"],
                "max_limit_up_height": pool["max_limit_up_height"],
                "turnover_billion": breadth_snapshot.get("turnover_billion"),
                "source": breadth_snapshot.get("source"),
            },
            "hot_sectors": industries[:10],
            "hot_themes": hot_themes,
            "limitup_ladder": pool.get("limitup_ladder") or [],
            "sector_capital_top": sector_capital[:5],
            "stock_inflow_top": stock_inflow[:8],
            "stock_outflow_top": stock_outflow[:8],
            "holding_sector_strength": holding_sector_strength,
            "alerts": [{"title": emotion_phase, "message": emotion_note, "source": "stock_pool"}],
            "scheduled_tasks": [],
        },
        "tail-strategy": {
            **common,
            "title": "\u5c3e\u76d8\u7b56\u7565",
            "pools": pool,
            "emotion": emotion,
            "sector_capital_top": sector_capital[:5],
            "watchlist": tail_watch,
            "tail_candidates": tail_candidates,
            "watch_groups": watch_groups,
            "next_day_plan": next_day_plan,
            "risk_blocks": risk_blocks,
            "decisions": [],
            "groups": {},
            "rules": [
                "\u5c3e\u76d8\u52a8\u4f5c\u5361\u5fc5\u987b\u6765\u81ea\u6b63\u5f0f\u7b56\u7565/\u6301\u4ed3/\u98ce\u9669\u8868\uff1b\u7f3a\u8868\u65f6\u4e0d\u751f\u6210\u5019\u9009\u3002",
                "\u6da8\u8dcc\u505c\u548c\u8fde\u677f\u53ea\u8bfb\u53d6 stock_pool\u3002",
            ],
        },
        "close-review": {
            **common,
            "title": "\u6536\u76d8\u590d\u76d8",
            "pools": pool,
            "sector_capital_top": sector_capital[:5],
            "themes": {"concept_sectors": concepts, "industry_sectors": industries, "main_lines": main_lines},
            "topic_cards": topic_cards,
            "close_review_cards": close_review_cards,
            "close_style_panels": close_style_panels,
            "new_theme_tracking": new_theme_tracking,
            "holding_sector_strength": holding_sector_strength,
            "summary": {
                "emotion_score": round(emotion_score, 1),
                "emotion_phase": emotion_phase,
                "limit_up": pool["limit_up_count"],
                "limit_down": pool["limit_down_count"],
                "max_limit_up_height": pool["max_limit_up_height"],
            },
            "dig_records": topic_cards,
            "tomorrow_watch": [],
            "review_questions": [
                "\u4eca\u65e5\u6da8\u8dcc\u505c\u4e0e\u8fde\u677f\u7ed3\u6784\u5982\u4f55\uff1f",
                "\u8d44\u91d1\u6d41\u6b63\u5f0f\u8868\u662f\u5426\u5df2\u540c\u6b65\uff1f",
                "\u4e3b\u7ebf/\u9898\u6750\u5feb\u7167\u662f\u5426\u5df2\u540c\u6b65\uff1f",
            ],
        },
    }
    for stage, payload in snapshots.items():
        source_tables = (
            ["global_market_index_daily", "us_theme_snapshot", "us_a_share_transmission", "premarket_news"]
            if stage == "morning-brief"
            else ["stock_pool", "sector_capital_flow", "stock_capital_rank", "sector_snapshot"]
        )
        written += store.upsert_market_stage_snapshot(
            trade_date,
            stage,
            payload,
            source_tables=source_tables,
        )
    return written


def _sync_fund_premium_snapshot(store: MarketStore, trade_date: str) -> int:
    """Persist the day's fund-premium scan result as a close snapshot.

    ``limit`` is set high (3000) so the full ETF+LOF universe (~1500+~440) is
    preserved — the old ``limit=200`` silently dropped most of the market.
    """
    try:
        from src.data.fund_premium import scan_fund_premium
        rows = scan_fund_premium(fund_type="ALL", limit=3000, use_cache=False)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fund-premium snapshot failed: %s", exc)
        return 0
    if not rows:
        return 0
    for r in rows:
        # trade_date is the PK + the value latest_date() queries on, so it must
        # be the sync date. The actual NAV date lives in the separate nav_date
        # column (LOF rows carry it; em rows leave it empty → upsert falls back
        # to trade_date, i.e. ETF never flags as stale-nav, which is safe).
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
    datasets = datasets or {
        "calendar",
        "master",
        "index_master",
        "board_master",
        "daily",
        "daily_basic",
        "dragon",
        "pool",
        "etf",
        "fund_daily",
        "etf_master",
        "fund_master",
        "etf_size",
        "index",
        "board",
        "capital",
        "capital_rank",
        "sector_capital",
        "sector_snapshot",
        "market_breadth",
        "global_indices",
        "us_theme",
        "us_transmission",
        "premarket_news",
        "stage_snapshot",
        "premium",
    }
    result: dict[str, int] = {}

    if "master" in datasets:
        result["master"] = _sync_security_master(store)

    def _run(name: str, fn: Any) -> None:
        """Run one dataset, capture failures so they don't block siblings."""
        if time.monotonic() > deadline or name not in datasets:
            return
        try:
            result[name] = fn()
        except Exception:  # noqa: BLE001
            logger.exception("market_sync: %s dataset failed", name)

    _run("calendar", lambda: _sync_trade_calendar_tpdog(store, trade_date))
    _run("index_master", lambda: _sync_index_master_tpdog(store))
    _run("board_master", lambda: _sync_board_master_tpdog(store))
    _run("board_members", lambda: _sync_board_members_tpdog(store))
    _run("realtime", lambda: _sync_realtime_quotes_tpdog(store, trade_date))

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
                written = _sync_daily_from_realtime_snapshot(store, trade_date, codes=daily_codes)
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
            if written == 0 and latest_settled == trade_date:
                written = _sync_daily_from_realtime_snapshot(store, trade_date, codes=universe_codes)
            result["daily"] = written

    def _run(name: str, fn: Any) -> None:
        """Run one dataset, capture failures so they don't block siblings."""
        if time.monotonic() > deadline or name not in datasets:
            return
        try:
            result[name] = fn()
        except Exception:  # noqa: BLE001 — one dataset failing must not abort the rest
            logger.exception("market_sync: %s dataset failed", name)

    _run("daily_basic", lambda: _sync_stock_daily_basic_tushare_by_date(store, trade_date, codes=codes))
    _run("etf_master", lambda: _sync_etf_master(store))
    _run("fund_master", lambda: _sync_fund_master(store))  # after etf_master (reads it)
    _run("dragon", lambda: _sync_dragon_tiger(store, trade_date))
    _run("pool", lambda: _sync_pools(store, trade_date))

    def _run_etf() -> int:
        latest_settled = _latest_settled_date_for_sync(trade_date, today_str)
        if latest_settled != trade_date:
            return 0
        resolved_etf_codes = etf_codes
        if resolved_etf_codes is None:
            try:
                resolved_etf_codes = store.fund_snapshot_codes(fund_type="ETF")
                if not resolved_etf_codes:
                    resolved_etf_codes = None
            except Exception:  # noqa: BLE001
                resolved_etf_codes = None
        written = _sync_etf_daily_tushare_by_date(store, trade_date, etf_codes=resolved_etf_codes)
        if written or not resolved_etf_codes:
            return written
        return _sync_etf_daily(store, resolved_etf_codes, trade_date, today_str)

    _run("etf", _run_etf)
    _run("fund_daily", lambda: _sync_fund_daily_from_etf_daily(store, trade_date))
    _run("etf_size", lambda: _sync_etf_share_size_by_date(store, trade_date))
    _run("index", lambda: _sync_index_daily(store, trade_date))
    _run("index_history", lambda: _backfill_index_history_akshare(store))
    _run("board", lambda: _sync_board_daily_tpdog(store, trade_date))
    _run("capital", lambda: _sync_stock_capital(store, trade_date))
    _run("capital_rank", lambda: _sync_stock_capital_rank(store, trade_date))
    _run("sector_capital", lambda: _sync_sector_capital(store, trade_date))
    _run("sector_snapshot", lambda: _sync_sector_snapshot(store, trade_date))
    _run("market_breadth", lambda: _sync_market_breadth_snapshot(store, trade_date))
    _run("global_indices", lambda: _sync_global_market_indices(store, trade_date))
    _run("us_theme", lambda: _sync_us_theme_snapshot(store, trade_date))
    _run("us_transmission", lambda: _sync_us_a_share_transmission(store, trade_date))
    _run("premarket_news", lambda: _sync_premarket_news(store, trade_date))
    _run("stage_snapshot", lambda: _sync_stage_snapshots(store, trade_date))
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


_PREMARKET_DATASETS = {"global_indices", "us_theme", "us_transmission", "premarket_news", "stage_snapshot"}
_PREMARKET_OVERNIGHT_DATASETS = {"global_indices", "us_theme", "us_transmission"}
_PREMARKET_WARMUP_DATASETS = {"global_indices", "us_theme", "us_transmission", "premarket_news", "stage_snapshot"}
_PREMARKET_OFFICIAL_DATASETS = _PREMARKET_DATASETS
_PREMARKET_OVERNIGHT_TIME = (5, 30)
_PREMARKET_WARMUP_TIME = (7, 30)
_PREMARKET_OFFICIAL_TIME = (8, 50)
_INTRADAY_DATASETS = {
    "realtime",
    "pool",
    "capital_rank",
    "sector_capital",
    "sector_snapshot",
    "market_breadth",
    "stage_snapshot",
}
_INTRADAY_SYNC_INTERVAL_MINUTES = 5


def _premarket_sync_slot(now_time: Any) -> str | None:
    """Return the due premarket slot name for the current CST time."""
    from datetime import time as _time

    if now_time >= _time(*_PREMARKET_OFFICIAL_TIME):
        return "official-0850"
    if now_time >= _time(*_PREMARKET_WARMUP_TIME):
        return "warmup-0730"
    if now_time >= _time(*_PREMARKET_OVERNIGHT_TIME):
        return "overnight-0530"
    return None


def _premarket_slot_datasets(slot: str) -> set[str]:
    if slot == "overnight-0530":
        return set(_PREMARKET_OVERNIGHT_DATASETS)
    if slot == "warmup-0730":
        return set(_PREMARKET_WARMUP_DATASETS)
    return set(_PREMARKET_OFFICIAL_DATASETS)


def _maybe_run_premarket_sync(store: MarketStore) -> None:
    """Generate today's morning brief before the A-share open.

    The 07:30 warmup prepares a draft; the 08:50 official slot refreshes it
    shortly before users open the market page. Each slot is idempotent.
    """
    from src.data.trade_calendar import cn_market_phase, is_trading_day

    now = _now_cst()
    today = now.strftime("%Y-%m-%d")
    if not is_trading_day(today):
        return
    if cn_market_phase(now) != "pre_open":
        return
    slot = _premarket_sync_slot(now.time())
    if slot is None:
        return
    meta_key = f"daemon:premarket:{today}:{slot}"
    if store.get_meta(meta_key):
        return
    try:
        logger.info("market-sync daemon: starting premarket sync for %s slot=%s", today, slot)
        run_daily_sync(today, store=store, datasets=_premarket_slot_datasets(slot), deadline_seconds=240)
        store.set_meta(meta_key, _now_cst().isoformat())
        logger.info("market-sync daemon: premarket done for %s slot=%s", today, slot)
    except Exception:  # noqa: BLE001
        logger.exception("market-sync daemon premarket tick failed")


def _maybe_run_intraday_sync(store: MarketStore) -> None:
    """Refresh intraday monitor inputs on a small cadence during trading."""
    from src.data.trade_calendar import cn_market_phase, is_trading_day

    now = _now_cst()
    today = now.strftime("%Y-%m-%d")
    if not is_trading_day(today):
        return
    phase = cn_market_phase(now)
    if phase not in {"in_session", "lunch_break"}:
        return
    minute_of_day = now.hour * 60 + now.minute
    slot = minute_of_day // _INTRADAY_SYNC_INTERVAL_MINUTES
    meta_key = f"daemon:intraday:{today}:{slot}"
    if store.get_meta(meta_key):
        return
    try:
        logger.info("market-sync daemon: starting intraday sync for %s slot=%s", today, slot)
        run_daily_sync(today, store=store, datasets=_INTRADAY_DATASETS, deadline_seconds=240)
        store.set_meta(meta_key, _now_cst().isoformat())
        logger.info("market-sync daemon: intraday done for %s slot=%s", today, slot)
    except Exception:  # noqa: BLE001
        logger.exception("market-sync daemon intraday tick failed")


def _maybe_run_fund_premium_sync(store: MarketStore) -> None:
    """Refresh the fund-premium snapshot every ~5 min during trading.

    Runs on its OWN cadence/meta_key (not bundled into ``_INTRADAY_DATASETS``)
    because ``_sync_fund_premium_snapshot`` does a serial ~40-80s mootdx scan;
    sharing the intraday tick's 240s budget would risk aborting the cheaper
    pool/capital datasets mid-way. The fund-premium page only reads this one
    table, so an isolated tick is both safer and independently observable.
    """
    from src.data.trade_calendar import cn_market_phase, is_trading_day

    now = _now_cst()
    today = now.strftime("%Y-%m-%d")
    if not is_trading_day(today):
        return
    phase = cn_market_phase(now)
    if phase not in {"in_session", "lunch_break"}:
        return
    minute_of_day = now.hour * 60 + now.minute
    slot = minute_of_day // _INTRADAY_SYNC_INTERVAL_MINUTES
    meta_key = f"daemon:fund_premium:{today}:{slot}"
    if store.get_meta(meta_key):
        return
    try:
        logger.info("market-sync daemon: starting fund-premium sync for %s slot=%s", today, slot)
        run_daily_sync(today, store=store, datasets={"premium"}, deadline_seconds=120)
        store.set_meta(meta_key, _now_cst().isoformat())
        logger.info("market-sync daemon: fund-premium done for %s slot=%s", today, slot)
    except Exception:  # noqa: BLE001
        logger.exception("market-sync daemon fund-premium tick failed")


def _loop() -> None:
    from src.data.rate_limiter import mark_background, reset_background

    # Mark this daemon thread as background so the limiter reserves slots for
    # foreground requests and reclaims permits if a sync stalls.
    token = mark_background(True)
    store = MarketStore()
    while True:
        try:
            _maybe_run_premarket_sync(store)
            _maybe_run_intraday_sync(store)
            _maybe_run_fund_premium_sync(store)
            _maybe_run_daily_sync(store)
        except Exception:  # noqa: BLE001
            logger.exception("market-sync loop error")
        time.sleep(_SYNC_TICK_SECONDS)


def start_market_sync_daemon() -> None:
    """Start the background market-sync daemon thread (idempotent)."""
    if os.getenv("MARKET_SYNC_DAEMON_ENABLED", "0").strip().lower() not in {"1", "true", "yes"}:
        logger.info("market-sync daemon disabled; use vibe-trading-sync worker")
        return
    global _daemon_started
    with _daemon_lock:
        if _daemon_started:
            return
        _daemon_started = True
    thread = threading.Thread(target=_loop, name="market-sync", daemon=True)
    thread.start()
    logger.info("market-sync daemon started")
