"""Fund premium / discount data layer for the fund arbitrage feature.

Computes the premium/discount rate of exchange-traded funds (LOF/ETF).

Data source strategy — a fallback chain (none rely on tushare, whose fund
endpoints need higher credits than the configured token has):

  1. **akshare fund_etf_spot_em / fund_lof_spot_em** (eastmoney) — PRIMARY.
     Returns the full market with IOPV + 折价率 column directly. Empirically
     flaky (RemoteDisconnected under rate-limiting), so wrapped in retry.
  2. **akshare fund_etf_spot_ths / fund_lof_spot_ths** (同花顺) — stable NAV
     source, but no exchange price. Paired with mootdx quotes (serial, top-N
     active funds only) to compute premium.
  3. Empty + error note if all sources fail.

Premium rate = (exchange_price - nav) / nav × 100%
  - premium > 0  → 溢价 → 申购套利（申购→场内卖）
  - premium < 0  → 折价 → 赎回套利（场内买→赎回）

All public functions return plain dicts/lists and never raise.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_CACHE_TTL = 300  # 5 minutes
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> Any | None:
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (time.time() - hit[0]) < _CACHE_TTL:
            return hit[1]
    return None


def _cache_set(key: str, val: Any) -> None:
    with _cache_lock:
        _cache[key] = (time.time(), val)


# ---------------------------------------------------------------------------
# Fund code ranges
# ---------------------------------------------------------------------------

_SH_FUND_RE = re.compile(r"^(50|51|56|58)\d{4}$")
_SZ_ETF_RE = re.compile(r"^15\d{4}$")
_SZ_LOF_RE = re.compile(r"^16\d{4}$")


def _fund_market(code: str) -> int | None:
    if _SH_FUND_RE.match(code):
        return 1
    if _SZ_ETF_RE.match(code) or _SZ_LOF_RE.match(code):
        return 0
    return None


def _fund_type(code: str) -> str:
    if _SH_FUND_RE.match(code) or _SZ_ETF_RE.match(code):
        return "ETF"
    if _SZ_LOF_RE.match(code):
        return "LOF"
    return "未知"


def _safe_float(v: Any) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Source 1: akshare eastmoney spot (PRIMARY — has 折价率 directly)
# ---------------------------------------------------------------------------

# em spot columns (Chinese) → our field. The em endpoint returns 折价率 directly.
_EM_COL_MAP = {
    "代码": "code",
    "名称": "name",
    "最新价": "price",
    "涨跌幅": "change_pct",
    "成交额": "amount",
    "IOPV实时估值": "iopv",
    "基金净值": "nav",
    "折价率": "discount_rate",  # em gives this directly
}


def _fetch_em_spot(fund_kind: str) -> list[dict[str, Any]] | None:
    """Pull eastmoney spot (ETF/LOF). Returns list of dicts or None on failure.

    fund_kind: "ETF" or "LOF". Uses a short socket timeout so a hung em
    connection fails fast (≤8s) instead of blocking the whole scan for minutes.
    Retries once.
    """
    import socket
    import akshare as ak

    # Force a hard connect/read timeout so em's frequent RemoteDisconnected /
    # hang doesn't stall the scan. Save & restore the prior default.
    old_default = socket.getdefaulttimeout()
    socket.setdefaulttimeout(8)
    fn = ak.fund_lof_spot_em if fund_kind == "LOF" else ak.fund_etf_spot_em
    last_exc = None
    try:
        for attempt in range(2):
            try:
                df = fn()
                if df is None or df.empty:
                    return None
                rename = {k: v for k, v in _EM_COL_MAP.items() if k in df.columns}
                df = df.rename(columns=rename)
                rows: list[dict[str, Any]] = []
                for _, row in df.iterrows():
                    code = str(row.get("code", "")).strip()
                    if not code or _fund_market(code) is None:
                        continue
                    price = _safe_float(row.get("price"))
                    nav = _safe_float(row.get("nav")) or _safe_float(row.get("iopv"))
                    if price > 0 and nav > 0:
                        premium = (price - nav) / nav * 100.0
                    else:
                        premium = _safe_float(row.get("discount_rate"))
                    rows.append({
                        "code": code,
                        "name": str(row.get("name", "")).strip(),
                        "type": _fund_type(code),
                        "price": round(price, 4),
                        "nav": round(nav, 4),
                        "premium_rate": round(premium, 3),
                        "amount": _safe_float(row.get("amount")),
                        "change_pct": _safe_float(row.get("change_pct")),
                        "redeem_status": "",
                        "subscribe_status": "",
                        "trade_date": "",
                        "signal": "溢价" if premium > 0 else ("折价" if premium < 0 else "—"),
                    })
                return rows
            except (TimeoutError, OSError, Exception) as exc:
                last_exc = exc
                logger.info("fund_premium: em %s attempt %d failed: %s", fund_kind, attempt, exc)
                time.sleep(0.5)
    finally:
        socket.setdefaulttimeout(old_default)
    logger.warning("fund_premium: em %s unavailable: %s", fund_kind, last_exc)
    return None


# ---------------------------------------------------------------------------
# Source 2: ths NAV + mootdx serial quotes (FALLBACK — top-N active only)
# ---------------------------------------------------------------------------

_THS_COL_MAP = {
    "基金代码": "code",
    "基金名称": "name",
    "当前-单位净值": "nav_now",
    "最新-单位净值": "nav_latest",    "赎回状态": "redeem_status",
    "申购状态": "subscribe_status",
    "基金类型": "fund_type_label",
    "查询日期": "trade_date",
}


def _fetch_lof_nav_em() -> dict[str, dict[str, Any]] | None:
    """Fetch LOF NAV via eastmoney open-fund daily (only source for LOF NAV).

    em is flaky, so wrapped in a short-socket retry (8s connect timeout, 2
    attempts). Returns {code: {nav, name, ...}} for codes matching the LOF
    range (16xxxx). Returns None on failure.
    """
    import socket
    import akshare as ak

    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(8)
    try:
        df = None
        for _ in range(2):
            try:
                df = ak.fund_open_fund_daily_em()
                break
            except Exception as exc:
                logger.info("fund_premium: lof em attempt failed: %s", exc)
                import time as _t
                _t.sleep(0.8)
    finally:
        socket.setdefaulttimeout(old)

    if df is None or df.empty:
        return None

    out: dict[str, dict[str, Any]] = {}
    # Columns vary by akshare version; tolerate both common names.
    code_col = next((c for c in df.columns if "代码" in str(c)), None)
    name_col = next((c for c in df.columns if "名称" in str(c)), None)
    nav_col = next((c for c in df.columns if "单位净值" in str(c)), None)
    date_col = next((c for c in df.columns if "日期" in str(c) or "时间" in str(c)), None)
    if not code_col or not nav_col:
        return None
    for _, row in df.iterrows():
        code = str(row.get(code_col, "")).strip()
        # Only keep LOF codes (16xxxx); the open-fund list includes OF too.
        if not _SZ_LOF_RE.match(code):
            continue
        nav = _safe_float(row.get(nav_col))
        if nav <= 0:
            continue
        out[code] = {
            "nav": nav,
            "name": str(row.get(name_col, "")).strip() if name_col else "",
            "redeem_status": "",
            "subscribe_status": "",
            "trade_date": str(row.get(date_col, "")) if date_col else "",
        }
    return out


def _fetch_ths_nav(fund_kind: str) -> dict[str, dict[str, Any]] | None:
    import akshare as ak

    # akshare has no fund_lof_spot_ths (only the ETF variant exists), and the
    # ETF ths endpoint does NOT include LOF codes (16xxxx). So:
    #  - ETF → fund_etf_spot_ths (stable)
    #  - LOF → fund_open_fund_daily_em (only source for LOF NAV; em is flaky,
    #          wrapped in a short-socket retry). LOF is a smaller universe.
    if fund_kind == "LOF":
        return _fetch_lof_nav_em()

    fn = ak.fund_etf_spot_ths
    try:
        df = fn()
    except Exception as exc:
        logger.info("fund_premium: ths %s failed: %s", fund_kind, exc)
        return None
    if df is None or df.empty:
        return None
    rename = {k: v for k, v in _THS_COL_MAP.items() if k in df.columns}
    df = df.rename(columns=rename)
    out: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        code = str(row.get("code", "")).strip()
        if not code:
            continue
        out[code] = {
            "nav": _safe_float(row.get("nav_now") or row.get("nav_latest")),
            "name": str(row.get("name", "")).strip(),
            "redeem_status": str(row.get("redeem_status", "")),
            "subscribe_status": str(row.get("subscribe_status", "")),
            "trade_date": str(row.get("trade_date", "")),
        }
    return out


def _mootdx_quotes_serial(codes: list[str], limit_codes: int = 120) -> dict[str, dict[str, Any]]:
    """Serially fetch live prices via mootdx quotes (not thread-safe).

    Capped at ``limit_codes`` — scanning the full 1600-fund universe serially
    would take minutes. Caller should pre-sort codes by likely activity.
    """
    try:
        from src.data.mootdx_helper import get_quotes
        client = get_quotes(timeout=10)
    except Exception as exc:
        logger.warning("fund_premium: mootdx init failed: %s", exc)
        return {}

    out: dict[str, dict[str, Any]] = {}
    for code in codes[:limit_codes]:
        market = _fund_market(code)
        if market is None:
            continue
        try:
            df = client.quotes(symbol=code, market=market)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        try:
            r = df.iloc[0]
            price = _safe_float(r.get("price"))
            if price <= 0:
                continue
            out[code] = {
                "price": price,
                "pre_close": _safe_float(r.get("last_close")),
                "amount": _safe_float(r.get("amount")),
            }
        except (ValueError, TypeError, KeyError):
            continue
    return out


def _akshare_sina_prices() -> dict[str, dict[str, Any]]:
    """Akshare sina spot prices (works on Aliyun when mootdx port blocked).

    Returns {code: {price, amount}} for exchange-traded assets (stocks + funds).
    ~27s full scan, but covers the whole market.
    """
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot()  # sina backend
    except Exception as exc:
        logger.info("fund_premium: akshare sina spot failed: %s", exc)
        return {}
    if df is None or df.empty:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        code = str(row.get("代码", "")).strip()
        if not code or _fund_market(code) is None:
            continue
        try:
            price = float(row.get("最新价", 0) or 0)
            if price <= 0:
                continue
            out[code] = {
                "price": price,
                "pre_close": float(row.get("昨收", 0) or 0),
                "amount": float(row.get("成交额", 0) or 0),
            }
        except (ValueError, TypeError):
            continue
    return out


def _tpdog_etf_prices(codes: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch latest-day ETF close + turnover from tpdog (mootdx/sina fallback).

    tpdog ``etf/daily`` returns the latest trading day's K-line per fund (1 积分
    each). Used when mootdx TDX port is blocked and before falling back to the
    ~27s sina full-market scan. Only ETF/LOF codes are sent (6-digit, 沪/深).
    Returns {code: {price, pre_close, amount}}.
    """
    try:
        from src.data.tpdog_client import call
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for code in codes:
        if _fund_market(code) is None:
            continue
        try:
            content = call("etf/daily", code=f"etf.{code}")
        except Exception as exc:  # noqa: BLE001 — best-effort, never raise
            logger.debug("fund_premium: tpdog etf/daily %s failed: %s", code, exc)
            continue
        # tpdog returns a single object (not a list) for etf/daily.
        row = content if isinstance(content, dict) else (content[0] if content else None)
        if not row:
            continue
        close = _safe_float(row.get("close"))
        if close <= 0:
            continue
        rise = _safe_float(row.get("rise"))
        out[code] = {
            "price": close,
            "pre_close": round(close - rise, 4) if rise else 0.0,
            "amount": _safe_float(row.get("total_amt")),
        }
    return out


def _fetch_via_ths_mootdx(fund_kind: str, limit: int) -> list[dict[str, Any]]:
    """Fallback path: ths NAV list + mootdx serial quotes for top-N codes.

    Fallback chain for prices: mootdx (TDX port) → tpdog ETF 日K (HTTPS) →
    akshare sina spot (full market, ~27s).
    """
    nav_map = _fetch_ths_nav(fund_kind)
    if not nav_map:
        return []
    codes = list(nav_map.keys())[:limit * 3]
    prices = _mootdx_quotes_serial(codes, limit_codes=limit * 2)
    if not prices:
        # mootdx unavailable → tpdog ETF 日K (HTTPS, per-fund)
        prices = _tpdog_etf_prices(codes)
    if not prices:
        # tpdog also unavailable → akshare sina full-market scan
        prices = _akshare_sina_prices()
    rows: list[dict[str, Any]] = []
    for code, pinfo in prices.items():
        ninfo = nav_map.get(code)
        if not ninfo:
            continue
        nav = ninfo["nav"]
        price = pinfo["price"]
        if nav <= 0 or price <= 0:
            continue
        premium = (price - nav) / nav * 100.0
        pre_close = pinfo.get("pre_close", 0.0)
        rows.append({
            "code": code,
            "name": ninfo.get("name", ""),
            "type": _fund_type(code),
            "price": round(price, 4),
            "nav": round(nav, 4),
            "premium_rate": round(premium, 3),
            "amount": pinfo.get("amount", 0.0),
            "change_pct": round(((price - pre_close) / pre_close * 100.0) if pre_close > 0 else 0.0, 3),
            "redeem_status": ninfo.get("redeem_status", ""),
            "subscribe_status": ninfo.get("subscribe_status", ""),
            "trade_date": ninfo.get("trade_date", ""),
            "signal": "溢价" if premium > 0 else ("折价" if premium < 0 else "—"),
        })
    return rows


# ---------------------------------------------------------------------------
# Public: scan + detail
# ---------------------------------------------------------------------------

def scan_fund_premium(
    fund_type: str = "ETF",
    min_abs_premium: float = 0.0,
    limit: int = 50,
    use_cache: bool = True,
    try_em: bool = False,
) -> list[dict[str, Any]]:
    """Scan funds for premium/discount arbitrage opportunities.

    Args:
        fund_type: "ETF" / "LOF" / "ALL".
        min_abs_premium: min |premium_rate| (%) to include.
        limit: max rows returned (sorted by |premium| desc). Also caps how many
            funds are quoted via mootdx in the fallback path (each quote ~0.5s
            serially, so 50 ≈ 25s worst case).
        try_em: if True, try eastmoney first (full market + 折价率 directly).
            Disabled by default because em's connection hangs unpredictably
            under rate-limiting and can't be reliably timed out on Windows.

    Note: |premium| > 50% is treated as stale-NAV noise (new funds / NAV not
    yet updated) and filtered out.
    """
    cache_key = f"scan:{fund_type}:{min_abs_premium}:{limit}:{try_em}"
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    kinds = ["ETF", "LOF"] if fund_type.upper() in ("ALL", "全部") else [fund_type.upper()]
    all_rows: list[dict[str, Any]] = []

    for kind in kinds:
        rows = None
        if try_em:
            rows = _fetch_em_spot(kind)
        if rows is None:
            # mootdx serial quotes are ~0.5s each; cap at `limit` (+headroom) to
            # keep the scan responsive. 40 funds ≈ 20s.
            quote_cap = min(max(limit, 40), 80)
            rows = _fetch_via_ths_mootdx(kind, limit=quote_cap)
            logger.info("fund_premium: %s via ths+mootdx (%d rows)", kind, len(rows))
        all_rows.extend(rows)

    # Filter: min premium + drop absurd values (stale NAV noise)
    out = [
        r for r in all_rows
        if abs(r["premium_rate"]) >= min_abs_premium and abs(r["premium_rate"]) <= 50.0
    ]
    out.sort(key=lambda r: abs(r["premium_rate"]), reverse=True)
    out = out[:limit]
    _cache_set(cache_key, out)
    return out


def scan_source_status() -> dict[str, Any]:
    """Quick health probe of each data source (for the UI to show which path is live)."""
    import akshare as ak
    status = {"em": False, "ths": False, "mootdx": False}
    try:
        df = ak.fund_etf_spot_em()
        status["em"] = df is not None and not df.empty
    except Exception:
        status["em"] = False
    try:
        df = ak.fund_etf_spot_ths()
        status["ths"] = df is not None and not df.empty
    except Exception:
        status["ths"] = False
    try:
        from src.data.mootdx_helper import get_quotes
        c = get_quotes(timeout=8)
        status["mootdx"] = c is not None
    except Exception:
        status["mootdx"] = False
    return status


def get_fund_detail(code: str) -> dict[str, Any]:
    """Real-time premium snapshot for a single fund.

    mootdx single quote for price + ths batch (cached) for NAV. Does NOT call
    scan_fund_premium (which quotes many funds serially).
    """
    code = code.strip()
    if _fund_market(code) is None:
        return {"status": "error", "error": f"{code} 不是有效的场内基金代码"}
    ftype = _fund_type(code)

    # Price: single mootdx quote (fast), fall back to tpdog ETF 日K (HTTPS)
    prices = _mootdx_quotes_serial([code], limit_codes=1)
    pinfo = prices.get(code)
    if not pinfo:
        prices = _tpdog_etf_prices([code])
        pinfo = prices.get(code)
    if not pinfo:
        return {"status": "error", "error": f"未取到 {code} 的场内实时价"}
    price = pinfo["price"]
    pre_close = pinfo.get("pre_close", 0.0)

    # NAV: ths batch (cached under a detail key to avoid re-pulling each call)
    nav_map = _cache_get(f"thsnav:{ftype}")
    if nav_map is None:
        nav_map = _fetch_ths_nav(ftype) or {}
        _cache_set(f"thsnav:{ftype}", nav_map)
    ninfo = nav_map.get(code, {})
    nav = ninfo.get("nav", 0.0)
    premium = ((price - nav) / nav * 100.0) if nav > 0 else 0.0

    return {
        "status": "ok",
        "code": code,
        "name": ninfo.get("name", ""),
        "type": ftype,
        "price": round(price, 4),
        "nav": round(nav, 4),
        "premium_rate": round(premium, 3),
        "pre_close": round(pre_close, 4),
        "change_pct": round(((price - pre_close) / pre_close * 100.0) if pre_close > 0 else 0.0, 3),
        "amount": pinfo.get("amount", 0.0),
        "redeem_status": ninfo.get("redeem_status", ""),
        "subscribe_status": ninfo.get("subscribe_status", ""),
        "trade_date": ninfo.get("trade_date", ""),
        "signal": "溢价" if premium > 0 else ("折价" if premium < 0 else "—"),
    }


def get_etf_holdings(code: str) -> dict[str, Any]:
    """Fetch ETF holdings (停牌股套利分析需要). Best-effort via akshare."""
    import akshare as ak
    try:
        df = ak.fund_portfolio_hold_em(symbol=code, date="2025")
        if df is None or df.empty:
            return {"status": "ok", "code": code, "holdings": []}
        holdings = []
        for _, row in df.head(20).iterrows():
            holdings.append({
                "stock_code": str(row.get("股票代码", "")),
                "stock_name": str(row.get("股票名称", "")),
                "weight": _safe_float(row.get("占净值比例", row.get("持仓比例", 0))),
            })
        return {"status": "ok", "code": code, "holdings": holdings}
    except Exception as exc:
        logger.info("fund_premium: holdings failed for %s: %s", code, exc)
        return {"status": "ok", "code": code, "holdings": [], "note": "持仓数据获取失败"}
