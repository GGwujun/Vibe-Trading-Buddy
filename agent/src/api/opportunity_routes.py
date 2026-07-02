"""Trading opportunity scanner — scans A-shares for actionable signals.

Mounted by ``agent/api_server.py`` via ``register_opportunity_routes(app, ...)``.

Route:
- ``GET /opportunity/list`` — four categories of opportunities, each top 10

Scanning flow:
1. Get a broad active A-share universe from the local market DB
2. Run four detectors per stock and score setup quality
3. Return top 10 per category after full-pool ranking, 10-min cache
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import numpy as np
import pandas as pd
from fastapi import Depends, FastAPI, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_CACHE: dict[str, Any] | None = None
_CACHE_TS: float = 0.0
_CACHE_LOCK = threading.Lock()
_DEFAULT_SCAN_LIMIT = 5000
_MIN_BARS_FOR_SIGNAL = 60
_CACHE_TTL = 600  # 10 min — scanning 200 stocks is expensive

# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


def _fetch_stocks_akshare(limit: int = 200) -> list[dict[str, Any]]:
    """Top A-share stocks by volume via akshare (sina backend, ~27s)."""
    try:
        import akshare as ak
        import re
        df = ak.stock_zh_a_spot()  # sina backend, works on Aliyun
    except Exception:
        return []
    if df is None or df.empty:
        return []
    out: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        code = str(row.get("代码", "")).strip()
        if len(code) != 6:
            continue
        suffix = "SZ" if code.startswith(("0", "3")) else "SH" if code.startswith("6") else None
        if suffix is None:
            continue
        out.append({
            "symbol": f"{code}.{suffix}",
            "name": str(row.get("名称", "")).strip(),
            "price": float(row.get("最新价", 0) or 0),
            "volume": float(row.get("成交量", 0) or 0),
            "change_pct": float(row.get("涨跌幅", 0) or 0),
        })
    out.sort(key=lambda x: x.get("volume", 0), reverse=True)
    return out[:limit]


def _scan_limit(default: int = _DEFAULT_SCAN_LIMIT) -> int:
    raw = os.getenv("OPPORTUNITY_SCAN_LIMIT", "").strip()
    if not raw:
        return default
    try:
        return max(200, min(int(raw), 6000))
    except ValueError:
        return default


def _stock_name_map(rows: list[dict[str, Any]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for row in rows:
        code = str(row.get("code") or "").upper()
        name = str(row.get("name") or row.get("symbol") or code).strip()
        if code:
            names[code] = name
    return names


def _valid_signal_stock(code: str, name: str, close: float, avg_turnover_proxy: float) -> bool:
    if close <= 2 or close >= 200:
        return False
    if avg_turnover_proxy <= 0:
        return False
    upper_name = name.upper()
    if "ST" in upper_name or "退" in name:
        return False
    return bool(re.match(r"^(000|001|002|003|004|300|301)\d{3}\.SZ$|^60[0-5]\d{3}\.SH$|^688\d{3}\.SH$", code))


def _fetch_stocks_from_local_db(limit: int = _DEFAULT_SCAN_LIMIT) -> list[dict[str, Any]]:
    """Build a broad A-share candidate universe from local settled daily bars."""
    try:
        from src.data.market_data_service import daily_bars_batch, default_strategy_codes, security_master
    except Exception as exc:
        logger.debug("local opportunity universe imports failed: %s", exc)
        return []

    codes = default_strategy_codes()
    if not codes:
        return []
    names = _stock_name_map(security_master(default_only=True))
    bar_data = daily_bars_batch(codes[:limit], days=90)
    stock_data: list[dict[str, Any]] = []
    for code, df in bar_data.items():
        if df is None or df.empty or len(df) < _MIN_BARS_FOR_SIGNAL:
            continue
        close = df["close"].astype(float)
        volume = df["volume"].astype(float)
        if float(close.iloc[-1]) <= 0 or float(close.iloc[-2]) <= 0:
            continue
        avg_turnover_proxy = float((close.tail(5) * volume.tail(5)).mean())
        name = names.get(code, code)
        if not _valid_signal_stock(code, name, float(close.iloc[-1]), avg_turnover_proxy):
            continue
        stock_data.append({
            "symbol": code,
            "name": name,
            "close": round(float(close.iloc[-1]), 2),
            "volume_avg_5": float(volume.tail(5).mean()),
            "turnover_proxy_avg_5": avg_turnover_proxy,
            "change_pct": round((float(close.iloc[-1]) - float(close.iloc[-2])) / float(close.iloc[-2]) * 100, 2),
            "df": df,
        })

    stock_data.sort(key=lambda x: x.get("turnover_proxy_avg_5", 0), reverse=True)
    logger.info("Opportunity scan: local DB produced %d/%d candidates", len(stock_data), len(codes))
    return stock_data[:limit]


def _fetch_top_stocks(limit: int = 200) -> list[dict[str, Any]]:
    """Get top active A-share stocks by recent volume.

    Primary: akshare stock_zh_a_spot (sina backend — works from Aliyun/Docker).
    Fallback: mootdx (native TDX TCP, may not work on all cloud hosts).
    """
    local = _fetch_stocks_from_local_db(limit)
    if local:
        return local
    stocks = _fetch_stocks_akshare(limit)
    if stocks:
        return stocks
    # Fallback to mootdx
    try:
        from src.data.mootdx_helper import get_quotes
        client = get_quotes(timeout=15)

        # Valid A-share individual stock code ranges (excludes indices, ETFs, etc.)
        # SZ main: 000001-004999, ChiNext: 300000-301999
        # SH main: 600000-605999, STAR: 688000-689999
        def _is_sz_stock(code: str) -> bool:
            return bool(re.match(r"^(000|001|002|003|004)\d{3}$|^(300|301)\d{3}$", code))

        def _is_sh_stock(code: str) -> bool:
            return bool(re.match(r"^60[0-5]\d{3}$|^688\d{3}$", code))

        sz_stocks: list[dict[str, Any]] = []
        sh_stocks: list[dict[str, Any]] = []
        # Load stock info from both markets
        for market_id, suffix, bucket in [(0, "SZ", sz_stocks), (1, "SH", sh_stocks)]:
            try:
                df = client.stocks(market=market_id)
                if df is not None and not df.empty:
                    for _, row in df.iterrows():
                        code = str(row.get("code", "")).strip()
                        if len(code) != 6:
                            continue
                        # Filter out indices, ETFs, and other non-stock instruments
                        if suffix == "SZ" and not _is_sz_stock(code):
                            continue
                        if suffix == "SH" and not _is_sh_stock(code):
                            continue
                        name = str(row.get("name", "")).strip().replace("\x00", "")
                        bucket.append({
                            "symbol": code + "." + suffix,
                            "name": name,
                            "pre_close": float(row.get("pre_close", 0) or 0),
                        })
            except Exception:
                logger.warning("mootdx stocks failed for market %d (SZ=0, SH=1)", market_id)

        logger.info("Opportunity scan: loaded %d SZ + %d SH stocks", len(sz_stocks), len(sh_stocks))

        if not sz_stocks and not sh_stocks:
            return []

        # Shuffle each market independently so every stock (including ChiNext /
        # STAR) gets a fair chance, then interleave to balance SZ vs SH in the
        # volume-sorted top-N selection.
        import random
        rng = random.Random(42)
        rng.shuffle(sz_stocks)
        rng.shuffle(sh_stocks)
        all_stocks = []
        max_len = max(len(sz_stocks), len(sh_stocks))
        for i in range(max_len):
            if i < len(sz_stocks):
                all_stocks.append(sz_stocks[i])
            if i < len(sh_stocks):
                all_stocks.append(sh_stocks[i])

        # Use local DB bar data. Scheduled sync/backfill keeps this warm.
        from src.data.market_data_service import daily_bars_batch

        top_n = min(len(all_stocks), limit * 3)
        sampled = all_stocks[:top_n]
        symbols = [s["symbol"] for s in sampled]

        bar_data = daily_bars_batch(symbols, days=30)
        logger.info("Opportunity scan: %d/%d symbols available in local DB",
                     len(bar_data), len(symbols))

        stock_data: list[dict[str, Any]] = []
        for s in sampled:
            if len(stock_data) >= limit:
                break
            df = bar_data.get(s["symbol"])
            if df is None or df.empty or len(df) < 10:
                continue
            close = df["close"].astype(float)
            volume = df["volume"].astype(float)
            s["close"] = round(float(close.iloc[-1]), 2)
            s["volume_avg_5"] = float(volume.iloc[-5:].mean())
            # Use bar data for change (pre_close from stocks() has different units)
            prev_close = float(close.iloc[-2])
            s["change_pct"] = round((s["close"] - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0
            s["df"] = df
            stock_data.append(s)

        stock_data.sort(key=lambda x: x.get("volume_avg_5", 0), reverse=True)
        return stock_data[:limit]
    except Exception as exc:
        logger.warning("Failed to fetch top stocks: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Opportunity detectors
# ---------------------------------------------------------------------------


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).mean()


def _rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi_val = 100 - (100 / (1 + rs))
    return round(float(rsi_val.iloc[-1]), 1) if not pd.isna(rsi_val.iloc[-1]) else 50.0


def _detect_breakout(s: dict[str, Any]) -> dict[str, Any] | None:
    """技术突破: volume breakout + price above MA20 + recent gains."""
    df = s.get("df")
    if df is None:
        return None
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)

    ma20 = _sma(close, 20)
    vol_ma5 = _sma(volume, 5)
    cur_vol = float(volume.iloc[-1])
    cur_close = float(close.iloc[-1])
    cur_ma20 = float(ma20.iloc[-1])

    gain_5d = (cur_close - float(close.iloc[-6])) / float(close.iloc[-6]) if len(close) > 6 else 0
    vol_ratio = cur_vol / float(vol_ma5.iloc[-1]) if float(vol_ma5.iloc[-1]) > 0 else 0

    if cur_close > cur_ma20 and gain_5d > 0.03 and vol_ratio > 1.5:
        return {
            "reason": f"放量突破MA20，5日涨幅{gain_5d:.1%}，量比{vol_ratio:.1f}",
            "confidence": min(0.95, 0.5 + gain_5d * 5 + (vol_ratio - 1) * 0.15),
        }
    return None


def _detect_trend(s: dict[str, Any]) -> dict[str, Any] | None:
    """趋势延续: MA bullish alignment + MACD golden cross + healthy RSI."""
    df = s.get("df")
    if df is None:
        return None
    close = df["close"].astype(float)

    ma5 = _sma(close, 5)
    ma10 = _sma(close, 10)
    ma20 = _sma(close, 20)

    cur5, cur10, cur20 = float(ma5.iloc[-1]), float(ma10.iloc[-1]), float(ma20.iloc[-1])
    if not (cur5 > cur10 > cur20):
        return None

    ema12 = _ema(close, 12)
    ema26 = _ema(close, 26)
    dif = ema12 - ema26
    dea = _ema(dif, 9)
    cur_dif = float(dif.iloc[-1])
    cur_dea = float(dea.iloc[-1])
    prev_dif = float(dif.iloc[-2])
    prev_dea = float(dea.iloc[-2])

    if not (cur_dif > cur_dea and prev_dif <= prev_dea):  # golden cross recently
        # also accept already bullish
        if not (cur_dif > cur_dea):
            return None

    rsi_val = _rsi(close)
    if 50 <= rsi_val <= 75:
        return {
            "reason": f"多头排列，RSI {rsi_val}，{'MACD金叉' if prev_dif <= prev_dea else 'MACD多头'}",
            "confidence": min(0.90, 0.55 + (rsi_val - 50) * 0.01),
        }
    return None


def _detect_oversold(s: dict[str, Any]) -> dict[str, Any] | None:
    """超跌反弹: RSI oversold + below MA60 + recent stabilization."""
    df = s.get("df")
    if df is None:
        return None
    close = df["close"].astype(float)

    rsi_val = _rsi(close)
    if rsi_val >= 32:
        return None

    ma60 = _sma(close, 60)
    cur = float(close.iloc[-1])
    cur_ma60 = float(ma60.iloc[-1]) if len(ma60) > 60 and not pd.isna(ma60.iloc[-1]) else cur * 2
    if cur > cur_ma60:
        return None

    # Recent stabilization: last 3 days not making new lows
    low3 = float(close.iloc[-3:].min())
    low5 = float(close.iloc[-8:-3].min()) if len(close) > 8 else low3
    if low3 > low5:
        return {
            "reason": f"RSI {rsi_val} 超卖，低于MA60，近3日止跌企稳",
            "confidence": min(0.85, 0.4 + (32 - rsi_val) * 0.03),
        }
    return None


def _detect_event_catalyst(s: dict[str, Any]) -> dict[str, Any] | None:
    """事件催化: check prediction market events for China-related big moves."""
    import sys
    events_mod = sys.modules.get("src.api.events_routes")
    if events_mod is None:
        return None

    try:
        with events_mod._CACHE_LOCK:
            cache = events_mod._EVENTS_CACHE
    except Exception:
        return None
    if not cache:
        return None

    hit: dict[str, Any] | None = None
    for cat in cache.get("categories", []):
        for e in cat.get("events", []):
            if abs(e.get("prob_change_24h", 0)) >= 0.10:
                if hit is None or abs(e["prob_change_24h"]) > abs(hit["prob_change_24h"]):
                    hit = e

    if hit:
        direction = "利好" if hit["prob_change_24h"] < 0 else "关注"
        return {
            "reason": f"{hit['title'][:40]} · 概率异动{hit['prob_change_24h']:+.0%}",
            "confidence": min(0.80, abs(hit["prob_change_24h"]) * 2 + 0.3),
            "event_title": hit["title"],
            "direction": direction,
        }
    return None


def _potential_metrics(s: dict[str, Any]) -> dict[str, float]:
    df = s.get("df")
    if df is None or df.empty or len(df) < 30:
        return {}
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    latest = float(close.iloc[-1])
    if latest <= 0:
        return {}
    ma20 = float(_sma(close, 20).iloc[-1])
    ma60_raw = _sma(close, 60).iloc[-1] if len(close) >= 60 else np.nan
    ma60 = float(ma60_raw) if not pd.isna(ma60_raw) else ma20
    ret20 = (latest - float(close.iloc[-20])) / float(close.iloc[-20]) if float(close.iloc[-20]) > 0 else 0.0
    ret60 = (latest - float(close.iloc[-60])) / float(close.iloc[-60]) if len(close) >= 60 and float(close.iloc[-60]) > 0 else ret20
    dist_ma20 = (latest - ma20) / ma20 if ma20 > 0 else 0.0
    dist_ma60 = (latest - ma60) / ma60 if ma60 > 0 else 0.0
    vol5 = float(volume.tail(5).mean())
    vol20 = float(volume.tail(20).mean())
    vol_ratio = vol5 / vol20 if vol20 > 0 else 1.0
    daily_ret = close.pct_change().dropna()
    volatility20 = float(daily_ret.tail(20).std()) if len(daily_ret) >= 20 else 0.0
    high60 = float(close.tail(60).max()) if len(close) >= 60 else float(close.max())
    drawdown60 = (latest - high60) / high60 if high60 > 0 else 0.0
    return {
        "ret20": ret20,
        "ret60": ret60,
        "dist_ma20": dist_ma20,
        "dist_ma60": dist_ma60,
        "vol_ratio": vol_ratio,
        "volatility20": volatility20,
        "drawdown60": drawdown60,
    }


def _opportunity_quality_score(s: dict[str, Any], signal: dict[str, Any], category: str) -> tuple[float, dict[str, float]]:
    metrics = _potential_metrics(s)
    score = float(signal.get("confidence", 0.5) or 0.5)
    change = float(s.get("change_pct", 0) or 0)
    if not metrics:
        return round(max(0.01, min(0.99, score)), 3), metrics

    ret20 = metrics["ret20"]
    dist_ma20 = metrics["dist_ma20"]
    vol_ratio = metrics["vol_ratio"]
    volatility20 = metrics["volatility20"]
    drawdown60 = metrics["drawdown60"]

    # Potential should reward stable structure, not today's heat.
    if 0.02 <= ret20 <= 0.18:
        score += 0.08
    elif ret20 > 0.30:
        score -= 0.08
    elif ret20 < -0.10 and category != "oversold":
        score -= 0.06

    if category == "trend":
        if 0 <= dist_ma20 <= 0.08:
            score += 0.08
        elif dist_ma20 > 0.15:
            score -= 0.10
        if -0.12 <= drawdown60 <= -0.02:
            score += 0.04
    elif category == "breakout":
        if 0.03 <= dist_ma20 <= 0.12:
            score += 0.04
        if change >= 6:
            score -= 0.14
        elif 0 <= change <= 3:
            score += 0.04
    elif category == "oversold":
        if drawdown60 <= -0.15 and change > -4:
            score += 0.06

    if 0.8 <= vol_ratio <= 1.8:
        score += 0.04
    elif vol_ratio > 3:
        score -= 0.08
    if volatility20 > 0.055:
        score -= 0.08
    if change >= 8:
        score -= 0.10

    return round(max(0.01, min(0.99, score)), 3), {k: round(v, 4) for k, v in metrics.items()}


# ---------------------------------------------------------------------------
# Build payload
# ---------------------------------------------------------------------------


def _build_opportunities() -> dict[str, Any]:
    stocks = _fetch_top_stocks(_scan_limit())
    if not stocks:
        return {"categories": [], "updated_at": datetime.now(timezone.utc).isoformat(), "error": "无法获取股票数据"}

    cats = [
        {"id": "breakout", "label": "技术突破", "icon": "flame", "color": "red"},
        {"id": "trend", "label": "趋势延续", "icon": "trending-up", "color": "green"},
        {"id": "oversold", "label": "超跌反弹", "icon": "sparkles", "color": "blue"},
        {"id": "event", "label": "事件催化", "icon": "zap", "color": "amber"},
    ]

    detectors = {
        "breakout": _detect_breakout,
        "trend": _detect_trend,
        "oversold": _detect_oversold,
        "event": _detect_event_catalyst,
    }

    results: dict[str, list[dict[str, Any]]] = {c["id"]: [] for c in cats}
    for s in stocks:
        for cat_id, detector in detectors.items():
            try:
                signal = detector(s)
                if signal:
                    quality_score, metrics = _opportunity_quality_score(s, signal, cat_id)
                    results[cat_id].append({
                        "symbol": s["symbol"],
                        "name": s["name"],
                        "price": s.get("close", 0),
                        "change_pct": s.get("change_pct", 0),
                        "reason": signal["reason"],
                        "confidence": quality_score,
                        "raw_confidence": round(float(signal["confidence"]), 3),
                        "quality_metrics": metrics,
                        "category": cat_id,
                    })
            except Exception:
                continue

    categories = []
    for cat in cats:
        cat_id = cat["id"]
        categories.append({**cat, "opportunities": sorted(results[cat_id], key=lambda x: x["confidence"], reverse=True)[:10]})

    return {"categories": categories, "updated_at": datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Opportunity(BaseModel):
    symbol: str
    name: str
    price: float
    change_pct: float = 0
    reason: str
    confidence: float
    category: str


class OpportunityCategory(BaseModel):
    id: str
    label: str
    icon: str
    color: str
    opportunities: list[dict[str, Any]] = Field(default_factory=list)


class OpportunityResponse(BaseModel):
    categories: list[dict[str, Any]]
    updated_at: str
    error: str | None = None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

AuthDep = Callable[..., Awaitable[Any] | Any]


def register_opportunity_routes(
    app: FastAPI,
    require_auth: AuthDep | None = None,
    require_event_stream_auth: AuthDep | None = None,
) -> None:
    if require_auth is None or require_event_stream_auth is None:
        import sys as _sys
        host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        if host is None:
            raise RuntimeError("register_opportunity_routes: api_server not in sys.modules")
        if require_auth is None:
            require_auth = host.require_auth
        if require_event_stream_auth is None:
            require_event_stream_auth = host.require_event_stream_auth

    @app.get("/opportunity", response_model=OpportunityResponse, dependencies=[Depends(require_auth)])
    async def list_opportunities(request: Request) -> dict[str, Any]:
        global _CACHE, _CACHE_TS

        now = time.time()
        with _CACHE_LOCK:
            if _CACHE is not None and (now - _CACHE_TS) < _CACHE_TTL:
                return _CACHE

        import asyncio
        loop = asyncio.get_event_loop()
        payload = await loop.run_in_executor(None, _build_opportunities)

        with _CACHE_LOCK:
            _CACHE = payload
            _CACHE_TS = time.time()

        return payload
