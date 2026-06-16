"""Position decision HTTP routes for the Web UI.

Mounted by ``agent/api_server.py`` via ``register_position_routes(app, ...)``.

Routes:
- ``POST /position/analyze`` — multi-factor analysis for A-share watchlist
- ``GET  /position/snapshot/{code}`` — quick price snapshot + basic signal

Data sources: Tushare (primary, A-share) with AKShare fallback.
All analysis runs server-side with 5-min caching.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy as np
import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_ANALYSIS_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 600  # 10 min (F10 data quasi-static intraday)

# AI analysis cache — independent from rule-engine cache
_AI_CACHE: dict[str, dict[str, Any]] = {}
_AI_CACHE_LOCK = threading.Lock()
_AI_CACHE_TTL = 600  # 10 min

# ---------------------------------------------------------------------------
# Market data helpers
# ---------------------------------------------------------------------------


def _fetch_a_share_data(codes: list[str], days: int = 90) -> dict[str, pd.DataFrame]:
    """Fetch A-share daily OHLCV with disk cache. mootdx → Tushare → AKShare.

    Returns: ``{code: DataFrame(cols=open,high,low,close,volume)}``
    """
    from src.data.ohlcv_cache import fetch_batch

    # -- Primary: mootdx with Parquet disk cache --
    results = fetch_batch(codes, days=days)
    if results:
        return results

    # -- Fallback: Tushare --
    token = os.getenv("TUSHARE_TOKEN", "").strip()
    if token and token not in {"", "your-tushare-token"}:
        try:
            import tushare as ts
            api = ts.pro_api(token)
            end = datetime.now().strftime("%Y%m%d")
            start = (datetime.now() - timedelta(days=days + 30)).strftime("%Y%m%d")
            for code in codes:
                try:
                    df = api.daily(ts_code=code, start_date=start, end_date=end)
                    if df is not None and not df.empty:
                        df = df.rename(columns={
                            "open": "open", "high": "high", "low": "low",
                            "close": "close", "vol": "volume", "trade_date": "trade_date",
                        })
                        df["trade_date"] = pd.to_datetime(df["trade_date"])
                        df = df.sort_values("trade_date").set_index("trade_date")
                        results[code] = df[["open", "high", "low", "close", "volume"]]
                except Exception:
                    logger.debug("Tushare fetch failed for %s", code)
            if results:
                return results
        except Exception as exc:
            logger.warning("Tushare unavailable: %s", exc)

    # -- Last resort: AKShare --
    try:
        import akshare as ak
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=days + 30)).strftime("%Y%m%d")
        for code in codes:
            symbol = code.replace(".SZ", "").replace(".SH", "")
            try:
                df = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start, end_date=end, adjust="qfq")
                if df is not None and not df.empty:
                    df = df.rename(columns={
                        "开盘": "open", "最高": "high", "最低": "low",
                        "收盘": "close", "成交量": "volume", "日期": "trade_date",
                    })
                    df["trade_date"] = pd.to_datetime(df["trade_date"])
                    df = df.sort_values("trade_date").set_index("trade_date")
                    results[code] = df[["open", "high", "low", "close", "volume"]]
            except Exception:
                logger.debug("AKShare fetch failed for %s", code)
    except Exception as exc:
        logger.error("AKShare unavailable: %s", exc)

    return results


def _fetch_index_data() -> dict[str, Any] | None:
    """Fetch 上证指数 via disk-cached mootdx."""
    try:
        from src.data.ohlcv_cache import fetch_with_cache
        df = fetch_with_cache("999999.SH", days=10)
        if df is not None and not df.empty:
            close = df["close"].astype(float)
            price = round(float(close.iloc[-1]), 2)
            prev = round(float(close.iloc[-2]), 2)
            change = round((price - prev) / prev * 100, 2)
            return {"name": "上证指数", "price": price, "change_pct": change}
    except Exception:
        logger.debug("Index fetch failed", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# Technical analysis
# ---------------------------------------------------------------------------


def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).mean()


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi_val = 100 - (100 / (1 + rs))
    return round(float(rsi_val.iloc[-1]), 1) if not pd.isna(rsi_val.iloc[-1]) else 50.0


def _macd(close: pd.Series) -> dict[str, Any]:
    ema12 = _ema(close, 12)
    ema26 = _ema(close, 26)
    dif = ema12 - ema26
    dea = _ema(dif, 9)
    macd_bar = 2 * (dif - dea)

    cur_dif = float(dif.iloc[-1])
    cur_dea = float(dea.iloc[-1])
    prev_dif = float(dif.iloc[-2])
    prev_dea = float(dea.iloc[-2])

    if cur_dif > cur_dea and prev_dif <= prev_dea:
        signal = "金叉"
    elif cur_dif < cur_dea and prev_dif >= prev_dea:
        signal = "死叉"
    elif cur_dif > cur_dea:
        signal = "多头"
    else:
        signal = "空头"

    return {"dif": round(cur_dif, 4), "dea": round(cur_dea, 4), "signal": signal}


def _bollinger(close: pd.Series, period: int = 20) -> dict[str, float]:
    ma = _sma(close, period)
    std = close.rolling(period).std()
    upper = ma + 2 * std
    lower = ma - 2 * std
    return {
        "upper": round(float(upper.iloc[-1]), 2),
        "mid": round(float(ma.iloc[-1]), 2),
        "lower": round(float(lower.iloc[-1]), 2),
    }


def _trend_analysis(close: pd.Series) -> dict[str, Any]:
    """Analyze price trend via MA alignment and slope."""
    ma5 = _sma(close, 5)
    ma10 = _sma(close, 10)
    ma20 = _sma(close, 20)
    ma60 = _sma(close, 60)

    cur5, cur10, cur20, cur60 = float(ma5.iloc[-1]), float(ma10.iloc[-1]), float(ma20.iloc[-1]), float(ma60.iloc[-1])

    if cur5 > cur10 > cur20 > cur60:
        pattern = "多头排列"
        direction = "up"
        score = 0.85
    elif cur5 < cur10 < cur20 < cur60:
        pattern = "空头排列"
        direction = "down"
        score = 0.15
    elif cur5 > cur10:
        # Short-term MAs above, long-term below → recovering
        pattern = "短期多头"
        direction = "up"
        score = 0.65
    elif cur5 < cur10:
        pattern = "短期空头"
        direction = "down"
        score = 0.35
    else:
        pattern = "震荡"
        direction = "neutral"
        score = 0.50

    # Adjust by slope — steeper slope = stronger signal
    slope_5d = (cur5 - float(ma5.iloc[-6])) / float(ma5.iloc[-6]) if len(ma5) > 6 else 0
    if direction == "up" and slope_5d > 0:
        score = min(0.95, score + abs(slope_5d) * 2)
    elif direction == "down" and slope_5d < 0:
        score = max(0.05, score - abs(slope_5d) * 2)

    return {"direction": direction, "ma_pattern": pattern, "score": round(score, 3)}


def _momentum_analysis(df: pd.DataFrame) -> dict[str, Any]:
    """RSI + MACD + volume momentum."""
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)

    rsi_val = _rsi(close)
    macd_info = _macd(close)
    bb = _bollinger(close)

    # Volume: compare recent 5-day avg to 20-day avg
    vol_5 = float(volume.iloc[-5:].mean())
    vol_20 = float(volume.iloc[-20:].mean()) if len(volume) >= 20 else vol_5
    vol_ratio = vol_5 / vol_20 if vol_20 > 0 else 1.0

    # Composite momentum score
    score = 0.50

    # RSI contribution
    if rsi_val > 70:
        score -= 0.10
    elif rsi_val > 60:
        score += 0.10
    elif rsi_val < 30:
        score += 0.10  # oversold bounce
    elif rsi_val < 40:
        score -= 0.10
    else:
        score += 0.05

    # MACD contribution
    if macd_info["signal"] == "金叉":
        score += 0.15
    elif macd_info["signal"] == "死叉":
        score -= 0.15
    elif macd_info["signal"] == "多头":
        score += 0.08
    else:
        score -= 0.08

    # Volume contribution
    if vol_ratio > 1.5 and rsi_val > 50:
        score += 0.08  # strong volume with uptrend
    elif vol_ratio > 1.5 and rsi_val < 50:
        score -= 0.08  # strong volume with downtrend

    # Bollinger position
    cur_price = float(close.iloc[-1])
    if cur_price > bb["upper"]:
        score -= 0.05  # overbought
    elif cur_price < bb["lower"]:
        score += 0.05  # oversold

    score = max(0.05, min(0.95, score))
    return {"rsi": rsi_val, "macd_signal": macd_info["signal"], "vol_ratio": round(vol_ratio, 2), "score": round(score, 3)}


def _technical_patterns(df: pd.DataFrame) -> dict[str, Any]:
    """Detect chart patterns using the existing pattern tool functions."""
    from src.tools.pattern_tool import find_peaks_valleys, candlestick_patterns

    close = df["close"].astype(float)
    open_ = df["open"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    pv = find_peaks_valleys(close, window=5)
    peaks = pv["peaks"]
    valleys = pv["valleys"]

    detected: list[str] = []
    score = 0.50

    # Head-and-shoulders: 3 peaks, middle highest
    if len(peaks) >= 3:
        for i in range(len(peaks) - 2):
            p1, p2, p3 = peaks[i], peaks[i + 1], peaks[i + 2]
            h1, h2, h3 = float(close.iloc[p1]), float(close.iloc[p2]), float(close.iloc[p3])
            if h2 > h1 and h2 > h3 and abs(h1 / h3 - 1) < 0.08:
                if p3 > len(close) - 5:
                    detected.append("头肩顶形态")
                    score -= 0.25

    # Inverse head-and-shoulders
    if len(valleys) >= 3:
        for i in range(len(valleys) - 2):
            v1, v2, v3 = valleys[i], valleys[i + 1], valleys[i + 2]
            l1, l2, l3 = float(close.iloc[v1]), float(close.iloc[v2]), float(close.iloc[v3])
            if l2 < l1 and l2 < l3 and abs(l1 / l3 - 1) < 0.08:
                if v3 > len(close) - 5:
                    detected.append("头肩底形态")
                    score += 0.25

    # Double bottom
    if len(valleys) >= 2:
        v1, v2 = valleys[-2], valleys[-1]
        l1, l2 = float(close.iloc[v1]), float(close.iloc[v2])
        if abs(l1 / l2 - 1) < 0.03 and (v2 - v1) > 10:
            detected.append("双底形态")
            score += 0.20

    # Double top
    if len(peaks) >= 2:
        p1, p2 = peaks[-2], peaks[-1]
        h1, h2 = float(close.iloc[p1]), float(close.iloc[p2])
        if abs(h1 / h2 - 1) < 0.03 and (p2 - p1) > 10:
            detected.append("双顶形态")
            score -= 0.20

    # Support/resistance from recent high/low
    recent_high = float(high.iloc[-20:].max())
    recent_low = float(low.iloc[-20:].min())
    cur = float(close.iloc[-1])
    if recent_low > 0 and cur < recent_low * 1.03:
        detected.append("接近支撑位")
        score += 0.10
    if recent_high > 0 and cur > recent_high * 0.97:
        detected.append("接近阻力位")
        score -= 0.10

    # Candlestick
    try:
        candle = candlestick_patterns(open_[-40:], high[-40:], low[-40:], close[-40:])
        last = candle.iloc[-1] if len(candle) > 0 else ""
        if "hammer" in str(last).lower():
            detected.append("锤子线")
            score += 0.10
        elif "engulfing" in str(last).lower():
            detected.append("吞没形态")
            score += 0.15
        elif "doji" in str(last).lower():
            detected.append("十字星")
    except Exception:
        pass

    score = max(0.05, min(0.95, score))
    if not detected:
        detected.append("无明显形态")

    return {"patterns": detected, "score": round(score, 3)}


# Event tag → A-share sector keywords for industry-aware matching
_EVENT_SECTOR_MAP: dict[str, list[str]] = {
    "ai": ["人工智能", "AI", "科技", "半导体", "芯片", "软件", "互联网"],
    "tech": ["科技", "AI", "半导体", "芯片", "软件", "电子"],
    "semiconductor": ["半导体", "芯片", "集成电路", "电子"],
    "crypto": ["区块链", "数字货币", "金融科技"],
    "bitcoin": ["区块链", "数字货币"],
    "space": ["航天", "卫星", "军工"],
    "finance": ["银行", "保险", "券商", "金融"],
    "stock": ["券商", "金融"],
    "economy": ["银行", "保险", "金融", "地产", "消费"],
    "inflation": ["银行", "金融", "消费", "农业", "食品"],
    "interest": ["银行", "地产", "金融"],
    "fed": ["银行", "金融", "地产", "有色"],
    "cpi": ["消费", "农业", "食品", "零售"],
    "gdp": ["银行", "金融", "基建", "制造"],
    "energy": ["能源", "电力", "新能源", "石油", "煤炭", "化工"],
    "oil": ["石油", "化工", "能源", "航运"],
    "climate": ["新能源", "光伏", "风电", "环保", "电力"],
    "war": ["军工", "国防", "航天"],
    "military": ["军工", "国防", "航天"],
    "taiwan": ["军工", "半导体", "电子", "航运"],
    "china": ["银行", "消费", "军工", "地产", "制造", "新能源", "电子"],
    "tariff": ["制造", "纺织", "家电", "电子", "汽车", "航运"],
    "trade": ["制造", "家电", "电子", "航运", "港口", "纺织"],
    "sanctions": ["半导体", "军工", "电子", "通信"],
    "russia": ["能源", "军工", "农业", "航运"],
    "health": ["医药", "医疗", "生物", "医疗器械"],
    "disease": ["医药", "医疗", "生物"],
    "auto": ["汽车", "新能源车", "零部件", "电池"],
    "ev": ["汽车", "新能源车", "电池", "充电桩"],
    "real estate": ["地产", "建材", "家电"],
    "infrastructure": ["基建", "建材", "钢铁", "工程机械"],
}

# Sentiment direction keywords in event titles
_SENTIMENT_KW: dict[str, list[str]] = {
    "negative": [
        "invasion", "war", "crash", "collapse", "recession", "sanctions",
        "tariff", "ban", "crisis", "default", "prosecution", "ban",
        "下跌", "崩盘", "衰退", "制裁", "危机", "违约", "禁止", "封锁",
        "调查", "诉讼", "罚款", "暴雷", "退市",
    ],
    "positive": [
        "growth", "boom", "breakthrough", "approval", "soft landing",
        "rally", "recovery", "stimulus", "innovation", "easing",
        "增长", "繁荣", "突破", "复苏", "刺激", "创新", "宽松",
        "降息", "利好", "获批", "量产", "商业化",
    ],
}


def _get_event_cache() -> dict | None:
    """Return the events cache dict, or None if unavailable."""
    import sys
    events_mod = sys.modules.get("src.api.events_routes")
    if events_mod is None:
        return None
    try:
        with events_mod._CACHE_LOCK:
            return events_mod._EVENTS_CACHE
    except Exception:
        return None


def _event_title_sentiment(title: str) -> int:
    """Return +1 (positive-leaning), -1 (negative-leaning), or 0 (neutral)."""
    t = title.lower()
    pos = sum(1 for kw in _SENTIMENT_KW["positive"] if kw in t)
    neg = sum(1 for kw in _SENTIMENT_KW["negative"] if kw in t)
    if pos > neg: return 1
    if neg > pos: return -1
    return 0


def _parse_volume_usd(vol_str: str) -> float:
    """Parse human-readable volume like '$1.2M' or '$234K' to float USD."""
    try:
        s = vol_str.replace("$", "").replace(",", "").strip().upper()
        if s.endswith("M"): return float(s[:-1]) * 1_000_000
        if s.endswith("K"): return float(s[:-1]) * 1_000
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _event_sentiment_for_stock(industry_names: list[str] | None = None) -> dict[str, Any]:
    """Analyze prediction market events relevant to a stock's industry.

    Returns structured sentiment with volume-weighted scoring,
    directional parsing, and top relevant event titles.
    """
    cache = _get_event_cache()
    if not cache:
        return {"relevant_count": 0, "high_impact_count": 0,
                "sentiment": "neutral", "sentiment_score": 0.0,
                "top_events": [], "score": 0.50}

    ind_kw = set()
    if industry_names:
        for name in industry_names:
            name_lower = name.lower()
            for tag, sectors in _EVENT_SECTOR_MAP.items():
                if any(s in name_lower for s in sectors) or tag in name_lower:
                    ind_kw.add(tag)

    # Always match China/Taiwan macro events for all A-shares
    macro_kw = {"china", "taiwan", "tariff", "trade", "xi", "ccp", "beijing"}

    scored_events: list[dict] = []
    for cat in cache.get("categories", []):
        for e in cat.get("events", []):
            title = str(e.get("title", "")).lower()
            cat_tags = [cat.get("id", "")]
            # Check category-level relevance
            category_match = bool(ind_kw & set(cat_tags))
            # Check title keyword relevance
            all_kw = ind_kw | macro_kw
            title_match = any(kw in title for kw in all_kw)
            if not (category_match or title_match):
                continue

            prob = float(e.get("probability", 0.5))
            change = float(e.get("prob_change_24h", 0))
            vol = _parse_volume_usd(str(e.get("volume", "0")))
            direction = _event_title_sentiment(str(e.get("title", "")))

            # If probability dropped on a negative event → bullish signal
            if direction < 0 and change < -0.02:
                direction = 1
            elif direction > 0 and change < -0.02:
                direction = -1

            weight = max(0.1, min(5.0, (vol / 50000) ** 0.3))
            if category_match and title_match:
                weight *= 2.5  # Strong: both category and title match
            elif title_match:
                weight *= 1.8  # Title keyword match
            elif category_match:
                weight *= 0.5  # Weak: category-only match
            else:
                continue  # No match at all

            scored_events.append({
                "title": str(e.get("title", "")),
                "probability": prob,
                "change_24h": change,
                "volume": str(e.get("volume", "")),
                "direction": direction,
                "weight": weight,
            })

    if not scored_events:
        return {"relevant_count": 0, "high_impact_count": 0,
                "sentiment": "neutral", "sentiment_score": 0.0,
                "top_events": [], "score": 0.50}

    # Volume-weighted directional score: -1 to +1
    total_weight = sum(se["weight"] for se in scored_events)
    if total_weight > 0:
        weighted_dir = sum(se["direction"] * se["weight"] for se in scored_events) / total_weight
    else:
        weighted_dir = 0.0

    # Sort by weight descending, take top 3
    scored_events.sort(key=lambda x: x["weight"], reverse=True)
    top_events = [
        {"title": se["title"], "probability": se["probability"],
         "change_24h": se["change_24h"], "volume": se["volume"]}
        for se in scored_events[:3]
    ]

    high_impact = sum(1 for se in scored_events if se["weight"] > 1.5)

    # Map weighted_dir (-1..+1) to score (0.05..0.95)
    score = 0.50 + weighted_dir * 0.30
    score = max(0.05, min(0.95, score))

    if weighted_dir > 0.15:
        sentiment = "bullish"
    elif weighted_dir < -0.15:
        sentiment = "bearish"
    else:
        sentiment = "neutral"

    return {
        "relevant_count": len(scored_events),
        "high_impact_count": high_impact,
        "sentiment": sentiment,
        "sentiment_score": round(weighted_dir, 3),
        "top_events": top_events,
        "score": round(score, 3),
    }


# ---------------------------------------------------------------------------
# Dimension helpers — wrap logic_chain layer functions
# ---------------------------------------------------------------------------


def _signal_emoji(score: float) -> str:
    if score >= 0.65: return "✅"
    if score >= 0.40: return "⚠️"
    return "🔴"


def _safe_dimension(label: str, fn, *args) -> dict[str, Any]:
    """Call a layer function safely, returning a fallback on failure."""
    try:
        result = fn(*args)
        if isinstance(result, dict) and "score" in result:
            return result
    except Exception:
        logger.debug("Dimension %s failed", label, exc_info=True)
    return {
        "score": 0.50,
        "signal": "⚠️",
        "summary": "数据获取失败",
        "items": [{"label": "状态", "value": "数据获取失败", "signal": "⚠️"}],
    }


# ---------------------------------------------------------------------------
# Composite analysis
# ---------------------------------------------------------------------------


_DIMENSION_WEIGHTS = {
    "technical": 0.26, "fundamental": 0.16, "capital": 0.14,
    "industry": 0.12, "macro": 0.10, "risk": 0.08, "events": 0.06,
    "alphas": 0.08,
}

_DIMENSION_META = {
    "macro":       {"label": "宏观环境", "icon": "globe"},
    "industry":    {"label": "行业分析", "icon": "building2"},
    "fundamental": {"label": "基本面",   "icon": "file-text"},
    "technical":   {"label": "技术面",   "icon": "trending-up"},
    "capital":     {"label": "资金面",   "icon": "banknote"},
    "risk":        {"label": "风险评估", "icon": "shield"},
    "events":      {"label": "消息面",   "icon": "newspaper"},
    "alphas":      {"label": "Alpha因子","icon": "sigma"},
}


def _build_alpha_dimension(alpha_raw: dict[str, Any]) -> dict[str, Any]:
    """Convert raw alpha_signals output into a dimension dict (items + score)."""
    items: list[dict[str, Any]] = []
    score = alpha_raw.get("score", 0.50)
    peer_count = alpha_raw.get("peer_count", 0)

    ok_signals = [s for s in alpha_raw.get("signals", []) if s.get("status") == "ok"]
    if ok_signals:
        items.append({"label": "可比样本", "value": f"{peer_count}只同行业股票", "signal": "✅"})

        # Show top 3 strongest
        for s in alpha_raw.get("top_bullish", [])[:3]:
            pct = int(s["rank_pct"] * 100)
            items.append({
                "label": f"  {s['emoji']} {s['label']}",
                "value": f"前{pct}% → 偏强",
                "signal": "✅",
            })

        # Show top 3 weakest
        for s in alpha_raw.get("top_bearish", [])[:3]:
            pct = int(s["rank_pct"] * 100)
            items.append({
                "label": f"  {s['emoji']} {s['label']}",
                "value": f"后{100-pct}% → 偏弱",
                "signal": "🔴" if pct < 30 else "⚠️",
            })

        # Summary stats
        bullish_count = sum(1 for s in ok_signals if s.get("direction") == "bullish")
        bearish_count = sum(1 for s in ok_signals if s.get("direction") == "bearish")
        items.append({"label": "信号统计", "value": f"{len(ok_signals)}个有效 · {bullish_count}多{bearish_count}空", "signal": "✅" if bullish_count > bearish_count else "⚠️"})

        if score >= 0.60:
            summary = "Alpha因子偏多"
        elif score >= 0.40:
            summary = "Alpha因子中性"
        else:
            summary = "Alpha因子偏空"
    elif alpha_raw.get("error"):
        items.append({"label": "计算状态", "value": alpha_raw["error"], "signal": "⚠️"})
        summary = "Alpha因子不可用"
    else:
        items.append({"label": "计算状态", "value": f"{peer_count}只可比 · 结果有限", "signal": "⚠️"})
        summary = "Alpha结果有限"

    return {"score": round(score, 3), "signal": _signal_emoji(score), "summary": summary, "items": items}


def _analyze_symbol(code: str, df: pd.DataFrame) -> dict[str, Any]:
    """Run 8-dimension multi-factor analysis on one symbol.

    Reuses logic_chain _layer_* functions for fundamental, capital,
    industry, macro, and risk dimensions.
    """
    from src.api.logic_chain_routes import (
        _layer_macro, _layer_industry, _layer_fundamental,
        _layer_capital, _layer_risk,
    )
    from src.tools.pattern_tool import (
        support_resistance, trend_line_slope,
        triangle, broadening, head_and_shoulders, double_top_bottom,
    )

    close = df["close"].astype(float)
    volume = df["volume"].astype(float)

    cur_price = round(float(close.iloc[-1]), 2)
    prev_price = round(float(close.iloc[-2]), 2)
    change_pct = round((cur_price - prev_price) / prev_price * 100, 2)

    name = _get_stock_name(code)

    # ---- Market basics (latest bar: 今开/昨收/日高/日低/成交量) ----
    try:
        open_today = round(float(df["open"].astype(float).iloc[-1]), 2)
        day_high = round(float(df["high"].astype(float).iloc[-1]), 2)
        day_low = round(float(df["low"].astype(float).iloc[-1]), 2)
        day_volume = int(float(df["volume"].astype(float).iloc[-1]))
    except Exception:
        open_today = day_high = day_low = 0
        day_volume = 0
    market_basics = {
        "open": open_today,            # 今开
        "prev_close": prev_price,      # 昨收
        "high": day_high,              # 日高
        "low": day_low,                # 日低
        "volume": day_volume,          # 成交量（手）
    }

    # ---- 1. Macro (no I/O -- uses cached index data) ----
    macro = _safe_dimension("macro", _layer_macro)

    # ---- 2-4. I/O-bound dimensions run in parallel ----
    with ThreadPoolExecutor(max_workers=4) as pool:
        fut_ind = pool.submit(_safe_dimension, "industry", _layer_industry, code)
        fut_fund = pool.submit(_safe_dimension, "fundamental", _layer_fundamental, code)
        fut_cap = pool.submit(_safe_dimension, "capital", _layer_capital, code)

        industry = fut_ind.result()
        fundamental = fut_fund.result()
        capital = fut_cap.result()

    # ---- 5. Technical (enriched with pattern_tool) ----
    technical = _analyze_technical_enriched(df, close, volume, cur_price)

    # ---- 6. Risk ----
    risk = _safe_dimension("risk", _layer_risk, df)

    # ---- 7. Events (prediction market + stock news) ----
    events = _analyze_events_stock(code, name)

    # ---- 8. Alpha factor signals (cross-sectional, run in this thread-pool worker) ----
    alpha_sig: dict[str, Any] = {"score": 0.50, "signal": "⚠️", "summary": "", "items": []}
    try:
        from src.data.alpha_signals import compute_alpha_signals as _compute_alpha
        alpha_raw = _compute_alpha(code)
        alpha_sig = _build_alpha_dimension(alpha_raw)
    except Exception:
        logger.debug("Alpha signals failed for %s", code, exc_info=True)

    # ---- Weighted composite ----
    dimensions_map = {
        "macro": macro, "industry": industry, "fundamental": fundamental,
        "technical": technical, "capital": capital, "risk": risk, "events": events,
        "alphas": alpha_sig,
    }
    overall = sum(
        dimensions_map[d_id]["score"] * _DIMENSION_WEIGHTS.get(d_id, 0.10)
        for d_id in _DIMENSION_WEIGHTS
    )
    # Risk modifier
    risk_mod = (risk.get("score", 0.50) - 0.50) * 0.15
    overall = max(0.01, min(0.99, overall + risk_mod))

    # ---- Decision (5-tier) ----
    if overall >= 0.70:
        decision, decision_label = "strong_buy", "强烈买入"
    elif overall >= 0.58:
        decision, decision_label = "buy", "买入"
    elif overall >= 0.42:
        decision, decision_label = "hold", "持有"
    elif overall >= 0.30:
        decision, decision_label = "sell", "卖出"
    else:
        decision, decision_label = "strong_sell", "强烈卖出"

    # Confidence
    dist = abs(overall - 0.50) * 2
    confidence = "high" if dist > 0.40 else "medium" if dist > 0.20 else "low"

    # Count failed dimensions
    failed_count = sum(
        1 for d in dimensions_map.values()
        if d.get("summary") == "数据获取失败"
    )
    if failed_count >= 3:
        confidence = "low"

    # Stop-loss / Take-profit from risk
    sl, tp, rr = _compute_sl_tp(cur_price, close)

    # Build dimensions array (8 dimensions: 7 rule-engine + alphas)
    dimensions = []
    for d_id in ["technical", "fundamental", "capital", "industry", "macro", "risk", "events", "alphas"]:
        d = dimensions_map[d_id]
        meta = _DIMENSION_META.get(d_id, {"label": d_id, "icon": "help-circle"})
        dim_entry: dict[str, Any] = {
            "id": d_id,
            "label": meta["label"],
            "icon": meta["icon"],
            "score": d.get("score", 0.50),
            "signal": d.get("signal", "⚠️"),
            "summary": d.get("summary", ""),
            "items": d.get("items", []),
        }
        # Attach top_events/top_news to the events dimension
        if d_id == "events":
            top_evt = d.get("top_events")
            if top_evt:
                dim_entry["top_events"] = top_evt
            top_nws = d.get("top_news")
            if top_nws:
                dim_entry["top_news"] = top_nws
        # Attach raw alpha signals for AI prompt
        if d_id == "alphas":
            dim_entry["alpha_raw"] = alpha_raw if isinstance(alpha_raw, dict) else None
        dimensions.append(dim_entry)

    # Legacy flat fields for backward compat
    trend_legacy = _trend_analysis(close)
    patterns_legacy = _technical_patterns(df)
    momentum_legacy = _momentum_analysis(df)
    events_legacy = _event_sentiment_for_stock()

    return {
        "symbol": code,
        "name": name,
        "price": cur_price,
        "change_pct": change_pct,
        "market_basics": market_basics,
        # Legacy
        "trend": {"direction": trend_legacy["direction"], "ma_pattern": trend_legacy["ma_pattern"], "score": trend_legacy["score"]},
        "technical": {"patterns": patterns_legacy["patterns"], "score": patterns_legacy["score"]},
        "momentum": {"rsi": momentum_legacy["rsi"], "macd_signal": momentum_legacy["macd_signal"], "vol_ratio": momentum_legacy["vol_ratio"], "score": momentum_legacy["score"]},
        "events": events_legacy,
        "overall_score": round(overall, 3),
        "decision": decision,
        "decision_label": decision_label,
        "confidence": confidence,
        # V2
        "dimensions": dimensions,
        "stop_loss": sl,
        "take_profit": tp,
        "risk_reward": rr,
        "api_version": 2,
    }


def _analyze_technical_enriched(
    df: pd.DataFrame, close: pd.Series, volume: pd.Series, cur_price: float,
) -> dict[str, Any]:
    """Enriched technical analysis reusing pattern_tool functions."""
    from src.tools.pattern_tool import (
        support_resistance, trend_line_slope, head_and_shoulders,
        double_top_bottom,
    )
    items: list[dict[str, Any]] = []
    score = 0.50

    # 1. Trend
    trend = _trend_analysis(close)
    items.append({"label": "趋势方向", "value": trend["ma_pattern"], "signal": "✅" if trend["direction"] == "up" else "⚠️" if trend["direction"] == "neutral" else "🔴"})
    score += (trend["score"] - 0.50) * 0.3

    # 2. RSI
    rsi = _rsi(close)
    rsi_signal = "✅" if 40 < rsi < 70 else "⚠️" if 30 <= rsi <= 40 or 70 <= rsi <= 80 else "🔴"
    items.append({"label": "RSI(14)", "value": f"{rsi:.1f}", "signal": rsi_signal})
    if 40 < rsi < 70:
        score += 0.05
    elif rsi > 80 or rsi < 30:
        score -= 0.05

    # 3. MACD
    macd_info = _macd(close)
    macd_signal = "✅" if macd_info["signal"] in ("金叉", "多头") else "⚠️" if macd_info["signal"] == "空头" else "🔴"
    items.append({"label": "MACD", "value": macd_info["signal"], "signal": macd_signal})
    if macd_info["signal"] == "金叉":
        score += 0.06
    elif macd_info["signal"] == "死叉":
        score -= 0.06

    # 4. Support / Resistance (from pattern_tool)
    try:
        sr = support_resistance(close, window=20, num_levels=3)
        supports = sr.get("support", [])
        resistances = sr.get("resistance", [])
        if supports:
            items.append({"label": "支撑位", "value": "、".join(f"¥{s:.2f}" for s in supports[:2]), "signal": "✅"})
        if resistances:
            items.append({"label": "阻力位", "value": "、".join(f"¥{r:.2f}" for r in resistances[:2]), "signal": "⚠️"})
    except Exception:
        # Fallback: simple high/low
        low20 = round(float(close.iloc[-20:].min()), 2)
        high20 = round(float(close.iloc[-20:].max()), 2)
        items.append({"label": "支撑位", "value": f"¥{low20:.2f}", "signal": "✅" if cur_price > low20 * 1.03 else "⚠️"})
        items.append({"label": "阻力位", "value": f"¥{high20:.2f}", "signal": "⚠️" if cur_price > high20 * 0.95 else "✅"})

    # 5. Trend slope
    try:
        slope_series = trend_line_slope(close, window=20)
        slope_val = float(slope_series.iloc[-1]) if len(slope_series) > 0 else 0
        slope_pct = slope_val / cur_price * 100 if cur_price > 0 else 0
        items.append({"label": "趋势斜率", "value": f"{slope_pct:+.2f}%/日", "signal": "✅" if slope_pct > 0 else "⚠️"})
        score += 0.03 if slope_pct > 0 else -0.03
    except Exception:
        pass

    # 6. Patterns
    patterns_detected: list[str] = []
    try:
        hs = head_and_shoulders(close, window=10)
        if hs is not None and not hs.empty:
            last_hs = int(hs.iloc[-1]) if len(hs) > 0 else 0
            if last_hs == 1:
                patterns_detected.append("头肩顶")
                score -= 0.08
            elif last_hs == -1:
                patterns_detected.append("头肩底")
                score += 0.08

        dtb = double_top_bottom(close, window=10)
        if dtb is not None and not dtb.empty:
            last_dtb = int(dtb.iloc[-1]) if len(dtb) > 0 else 0
            if last_dtb == 1:
                patterns_detected.append("双顶")
                score -= 0.06
            elif last_dtb == -1:
                patterns_detected.append("双底")
                score += 0.06
    except Exception:
        pass

    if patterns_detected:
        items.append({"label": "技术形态", "value": "、".join(patterns_detected), "signal": "✅" if "底" in "".join(patterns_detected) else "⚠️"})
    else:
        items.append({"label": "技术形态", "value": "无明显形态", "signal": "⚠️"})

    # 7. Volume ratio
    vol_5 = float(volume.iloc[-5:].mean())
    vol_20 = float(volume.iloc[-20:].mean()) if len(volume) >= 20 else vol_5
    vol_ratio = vol_5 / vol_20 if vol_20 > 0 else 1.0
    items.append({"label": "量比", "value": f"{vol_ratio:.1f}x", "signal": "✅" if 0.8 < vol_ratio < 2.5 else "⚠️"})

    # 8. ATR (14-day, for volatility context)
    try:
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr = round(float(tr.rolling(14).mean().iloc[-1]), 2)
        items.append({"label": "ATR(14)", "value": f"¥{atr:.2f}", "signal": "✅"})
    except Exception:
        atr = cur_price * 0.02
        items.append({"label": "ATR(14)", "value": f"¥{atr:.2f}", "signal": "⚠️"})

    score = max(0.05, min(0.95, score))
    summary = "技术面偏多" if score >= 0.60 else "技术面偏空" if score < 0.40 else "技术面中性"
    return {"score": round(score, 3), "signal": _signal_emoji(score), "summary": summary, "items": items}


def _analyze_events_stock(code: str, name: str) -> dict[str, Any]:
    """Events & sentiment: prediction market + stock-specific news, combined."""
    items: list[dict[str, Any]] = []
    score = 0.50
    top_events: list[dict] = []
    top_news: list[dict] = []

    # 1. Get industry info for event matching
    industry_names: list[str] = []
    try:
        from src.data.mootdx_helper import get_quotes
        client = get_quotes(timeout=8)
        raw_code = code.replace(".SZ", "").replace(".SH", "")
        # F10 category 5 = industry info
        ind_data = client.F10C(raw_code, 5)
        if ind_data and isinstance(ind_data, dict):
            for v in ind_data.values():
                text = str(v)[:100]
                industry_names.append(text)
    except Exception:
        pass

    # 2. Prediction market events — industry-aware + volume-weighted
    pm = _event_sentiment_for_stock(industry_names if industry_names else None)
    top_events = pm.get("top_events", [])
    evt_count = pm.get("relevant_count", 0)

    if evt_count > 0:
        hi = pm.get("high_impact_count", 0)
        evt_signal = "✅" if pm["sentiment"] == "bullish" else "🔴" if pm["sentiment"] == "bearish" else "⚠️"
        value = f"{evt_count}条相关"
        if hi > 0:
            value += f"（{hi}条高影响）"
        items.append({"label": "预测市场事件", "value": value, "signal": evt_signal})
        # Event sub-items (top event titles)
        for i, evt in enumerate(top_events[:2]):
            prob_pct = int(evt["probability"] * 100)
            chg = evt.get("change_24h", 0)
            chg_str = f" ↑{chg*100:+.1f}pt" if chg > 0.01 else f" ↓{abs(chg)*100:.1f}pt" if chg < -0.01 else ""
            items.append({"label": f"  {evt['title'][:45]}", "value": f"{prob_pct}%{chg_str} {evt.get('volume','')}", "signal": "📌"})
        score += (pm["score"] - 0.50) * 0.25  # Events contribute up to ±0.125
    else:
        items.append({"label": "预测市场事件", "value": "暂无相关事件", "signal": "✅"})

    # 3. Stock-specific news from news_routes
    news_sentiment = 0.0
    try:
        from src.api.news_routes import _fetch_sina_news, _fetch_ddg_news
        sina_news = _fetch_sina_news(keyword=name, limit=5)
        ddg_news = _fetch_ddg_news(f"{name} 股票", max_results=5)
        all_news = sina_news + ddg_news
        # Deduplicate by title
        seen = set()
        unique_news = []
        for n in all_news:
            t = n.get("title", "")
            if t and t not in seen:
                seen.add(t)
                unique_news.append(n)
        unique_news = unique_news[:5]

        if unique_news:
            pos_kw = ["增长", "盈利", "利好", "上涨", "突破", "业绩", "分红", "回购",
                      "中标", "获批", "扭亏", "签约", "量产", "商业化"]
            neg_kw = ["下跌", "亏损", "减持", "诉讼", "处罚", "调查", "下滑",
                      "暴雷", "退市", "问询", "警示", "冻结", "违约"]
            pos_count = sum(1 for n in unique_news if any(kw in str(n.get("title", "")) for kw in pos_kw))
            neg_count = sum(1 for n in unique_news if any(kw in str(n.get("title", "")) for kw in neg_kw))
            total = max(1, len(unique_news))
            news_sentiment = (pos_count - neg_count) / total

            sentiment_label = "偏正面" if news_sentiment > 0.15 else "偏负面" if news_sentiment < -0.15 else "中性"
            sentiment_signal = "✅" if news_sentiment > 0.15 else "🔴" if news_sentiment < -0.15 else "⚠️"
            items.append({"label": "个股新闻", "value": f"{len(unique_news)}条 · {sentiment_label} ({pos_count}利好/{neg_count}利空)", "signal": sentiment_signal})
            # News sub-items (headlines)
            for n in unique_news[:3]:
                pub = str(n.get("published", "") or "")[:5]
                items.append({"label": f"  {n['title'][:40]}", "value": pub, "signal": "📰"})
            top_news = [{"title": n.get("title", ""), "published": n.get("published", ""),
                         "source": n.get("source", "")} for n in unique_news[:3]]
            score += news_sentiment * 0.15  # News contributes up to ±0.15
        else:
            items.append({"label": "个股新闻", "value": "暂无", "signal": "⚠️"})
    except Exception:
        items.append({"label": "个股新闻", "value": "获取失败", "signal": "⚠️"})

    # 4. Anomaly detection: event probability swings >10% → flag
    big_swings = [e for e in top_events if abs(e.get("change_24h", 0)) > 0.10]
    if big_swings:
        score += 0.05 if news_sentiment >= 0 else -0.05
        items.append({"label": "舆情异动", "value": f"{len(big_swings)}条事件大幅异动", "signal": "⚠️"})

    # 5. News volume spike: >8 articles → abnormal attention
    if len(top_news) >= 4:
        items.append({"label": "舆情异动", "value": "新闻量异常增多", "signal": "⚠️"})

    score = max(0.05, min(0.95, score))
    if score >= 0.65:
        summary = "消息面偏正面"
    elif score >= 0.40:
        summary = "消息面中性"
    else:
        summary = "消息面偏负面"

    return {
        "score": round(score, 3), "signal": _signal_emoji(score),
        "summary": summary, "items": items,
        "top_events": top_events, "top_news": top_news,
    }


def _compute_sl_tp(cur_price: float, close: pd.Series) -> tuple[float | None, float | None, float | None]:
    """Compute stop-loss (2x ATR below) and take-profit (3x ATR above)."""
    try:
        high = close.copy()
        low = close.copy()
        tr = pd.concat([high.diff().abs(), low.diff().abs()], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1]) if len(tr) >= 14 else cur_price * 0.02
        sl = round(cur_price - 2 * atr, 2)
        tp = round(cur_price + 3 * atr, 2)
        rr = round((tp - cur_price) / (cur_price - sl), 2) if (cur_price - sl) > 0 else None
        return sl, tp, rr
    except Exception:
        return None, None, None


_STOCK_NAMES: dict[str, str] = {}
_STOCK_NAMES_LOADED = False

# Disk cache to avoid hitting Tushare rate limits (1 call/min for stock_basic).
_STOCK_NAMES_CACHE_DIR = Path.home() / ".vibe-trading" / "cache"
_STOCK_NAMES_CACHE_FILE = _STOCK_NAMES_CACHE_DIR / "stock_names.json"
_STOCK_NAMES_CACHE_MAX_AGE = 86400  # 24 hours


def _load_stock_names_from_cache() -> dict[str, str] | None:
    """Load stock names from disk cache if fresh."""
    try:
        if not _STOCK_NAMES_CACHE_FILE.exists():
            return None
        mtime = _STOCK_NAMES_CACHE_FILE.stat().st_mtime
        if time.time() - mtime > _STOCK_NAMES_CACHE_MAX_AGE:
            logger.info("Stock name cache is stale (>24h), will refresh")
            return None
        data = json.loads(_STOCK_NAMES_CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and len(data) > 500:
            logger.info("Loaded %d stock names from disk cache", len(data))
            return data
    except Exception:
        logger.debug("Failed to load stock names cache", exc_info=True)
    return None


def _save_stock_names_to_cache() -> None:
    """Persist stock names to disk cache."""
    try:
        _STOCK_NAMES_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _STOCK_NAMES_CACHE_FILE.write_text(
            json.dumps(_STOCK_NAMES, ensure_ascii=False), encoding="utf-8",
        )
    except Exception:
        logger.debug("Failed to save stock names cache", exc_info=True)


def _load_stock_names_batch() -> None:
    """Load all A-share stock names, preferring disk cache over API calls.

    Tries: disk cache → mootdx → Tushare → AKShare → hardcoded fallback.
    """
    global _STOCK_NAMES, _STOCK_NAMES_LOADED
    if _STOCK_NAMES_LOADED:
        return

    # 1. Try disk cache first (instant, no API calls)
    cached = _load_stock_names_from_cache()
    if cached:
        _STOCK_NAMES = cached
        _STOCK_NAMES_LOADED = True
        return

    loaded_count = 0

    # 2. mootdx (通达信协议 — no rate limits, no token needed)
    try:
        from src.data.mootdx_helper import get_quotes
        client = get_quotes(timeout=15)
        for market_id, suffix in [(0, ".SZ"), (1, ".SH")]:
            try:
                df = client.stocks(market=market_id)
                if df is not None and not df.empty:
                    for _, row in df.iterrows():
                        code = str(row.get("code", "")).strip()
                        name = str(row.get("name", "")).strip().replace("\x00", "")
                        if code and name and len(code) == 6:
                            _STOCK_NAMES.setdefault(code + suffix, name)
                            loaded_count += 1
            except Exception:
                logger.debug("mootdx stocks() failed for market %d", market_id)
        if loaded_count > 500:
            logger.info("Loaded %d stock names via mootdx", loaded_count)
    except Exception:
        logger.warning("mootdx stock names unavailable", exc_info=True)

    # 3. Tushare — single call with exchange="" covers all exchanges
    if loaded_count < 500:
        token = os.getenv("TUSHARE_TOKEN", "").strip()
        if token and token not in {"", "your-tushare-token"}:
            try:
                import tushare as ts
                pro = ts.pro_api(token)
                df = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name")
                if df is not None and not df.empty:
                    for _, row in df.iterrows():
                        ts_code = str(row.get("ts_code", "")).strip()
                        name = str(row.get("name", "")).strip().replace("\x00", "")
                        if ts_code and name:
                            _STOCK_NAMES.setdefault(ts_code, name)
                            loaded_count += 1
                    logger.info("Loaded %d stock names via Tushare", loaded_count)
            except Exception:
                logger.warning("Tushare stock_basic unavailable", exc_info=True)

    # 4. AKShare fallback
    if loaded_count < 500:
        try:
            import akshare as ak
            df = ak.stock_zh_a_spot_em()
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    raw_code = str(row.get("代码", "")).strip()
                    name = str(row.get("名称", "")).strip()
                    if raw_code and name and len(raw_code) == 6:
                        prefix = raw_code[:1]
                        suffix = ".SH" if prefix in ("6", "5", "9") else ".SZ"
                        _STOCK_NAMES.setdefault(raw_code + suffix, name)
                loaded_count = len(_STOCK_NAMES)
                logger.info("Loaded %d stock names via AKShare", loaded_count)
        except Exception:
            logger.debug("AKShare stock names unavailable", exc_info=True)

    # 4. Last resort: hardcoded common stocks
    if loaded_count < 50:
        KNOWN: dict[str, str] = {
            "000001.SZ": "平安银行", "000002.SZ": "万科A", "000063.SZ": "中兴通讯",
            "000333.SZ": "美的集团", "000651.SZ": "格力电器", "000858.SZ": "五粮液",
            "002415.SZ": "海康威视", "002475.SZ": "立讯精密", "002594.SZ": "比亚迪",
            "300059.SZ": "东方财富", "300124.SZ": "汇川技术", "300750.SZ": "宁德时代",
            "600000.SH": "浦发银行", "600036.SH": "招商银行", "600276.SH": "恒瑞医药",
            "600519.SH": "贵州茅台", "600585.SH": "海螺水泥", "600809.SH": "山西汾酒",
            "600887.SH": "伊利股份", "601012.SH": "隆基绿能", "601088.SH": "中国神华",
            "601166.SH": "兴业银行", "601318.SH": "中国平安", "601398.SH": "工商银行",
            "601857.SH": "中国石油", "601939.SH": "建设银行", "603259.SH": "药明康德",
            "603288.SH": "海天味业",
            # ETFs
            "510050.SH": "上证50ETF", "510300.SH": "沪深300ETF", "510500.SH": "中证500ETF",
            "512010.SH": "医药ETF", "512880.SH": "证券ETF", "513100.SH": "纳指ETF",
            "159915.SZ": "创业板ETF", "159919.SZ": "沪深300ETF", "159949.SZ": "创业板50",
        }
        for k, v in KNOWN.items():
            _STOCK_NAMES.setdefault(k, v)

    # Persist to disk cache for next cold start
    if loaded_count > 500:
        _save_stock_names_to_cache()

    _STOCK_NAMES_LOADED = True


def _get_stock_name(code: str) -> str:
    """Look up A-share stock name via batch cache; no per-stock API calls.

    Per-stock Tushare calls also hit the same rate limit as batch calls,
    so we avoid them and just return the code as-is for unknown symbols.
    """
    if not _STOCK_NAMES_LOADED:
        _load_stock_names_batch()
    return _STOCK_NAMES.get(code, code)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class DimensionItem(BaseModel):
    label: str = Field(..., description="Metric label")
    value: str = Field(..., description="Metric value")
    signal: str = Field(..., description="Signal emoji: ✅/⚠️/🔴")


class AnalysisDimension(BaseModel):
    id: str = Field(..., description="Dimension id")
    label: str = Field(..., description="Dimension display name")
    icon: str = Field("bar-chart-2", description="Lucide icon name")
    score: float = Field(..., description="Dimension score 0-1")
    signal: str = Field(..., description="Overall signal emoji")
    summary: str = Field("", description="One-line summary")
    items: list[DimensionItem] = Field(default_factory=list)


class AnalyzeRequest(BaseModel):
    symbols: list[str] = Field(..., min_length=1, max_length=20)
    cache_bust: float = Field(default=0, validation_alias="_cache_bust")


class AiAnalysisRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=12)
    signal: dict[str, Any]
    position: dict[str, Any] | None = None
    cache_bust: float = Field(default=0, validation_alias="_cache_bust")


# -- Backward-compat flat fields (kept for old clients) --
class TrendInfo(BaseModel):
    direction: str
    ma_pattern: str
    score: float


class TechnicalInfo(BaseModel):
    patterns: list[str]
    score: float


class MomentumInfo(BaseModel):
    rsi: float
    macd_signal: str
    vol_ratio: float
    score: float


class EventsInfo(BaseModel):
    relevant_count: int = 0
    sentiment: str = "neutral"
    score: float = 0.50


class SignalResult(BaseModel):
    symbol: str
    name: str
    price: float
    change_pct: float
    # Legacy flat fields (kept for backward compat)
    trend: dict[str, Any] = Field(default_factory=dict)
    technical: dict[str, Any] = Field(default_factory=dict)
    momentum: dict[str, Any] = Field(default_factory=dict)
    events: dict[str, Any] = Field(default_factory=dict)
    overall_score: float
    decision: str
    decision_label: str
    confidence: str
    # V2 fields
    dimensions: list[dict[str, Any]] = Field(default_factory=list)
    stop_loss: float | None = None
    take_profit: float | None = None
    risk_reward: float | None = None
    api_version: int = 2


class AnalyzeResponse(BaseModel):
    signals: list[dict[str, Any]]
    index: dict[str, Any] | None = None
    updated_at: str


class SnapshotResponse(BaseModel):
    symbol: str
    name: str
    price: float
    change_pct: float
    updated_at: str


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

AuthDep = Callable[..., Awaitable[Any] | Any]


def register_position_routes(
    app: FastAPI,
    require_auth: AuthDep | None = None,
    require_event_stream_auth: AuthDep | None = None,
) -> None:
    if require_auth is None or require_event_stream_auth is None:
        import sys as _sys

        host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        if host is None:
            raise RuntimeError("register_position_routes: api_server module not in sys.modules")
        if require_auth is None:
            require_auth = host.require_auth
        if require_event_stream_auth is None:
            require_event_stream_auth = host.require_event_stream_auth

    _SYMBOL_RE = __import__("re").compile(r"^\d{6}(\.(SZ|SH))?$")


    def _resolve_exchange(code: str) -> str:
        """Determine whether a 6-digit code is .SZ or .SH by querying mootdx.

        Returns 'SZ', 'SH', or '' (unknown).
        """
        try:
            from src.data.mootdx_helper import get_quotes
            client = get_quotes(timeout=5)
            for market_id, suffix in [(0, "SZ"), (1, "SH")]:
                try:
                    df = client.stocks(market=market_id)
                    if df is not None and not df.empty:
                        codes = set(str(c).strip() for c in df.get("code", []))
                        if code in codes:
                            return suffix
                except Exception:
                    continue
        except Exception:
            logger.debug("mootdx exchange lookup failed", exc_info=True)
        return ""


    def _normalize_symbol(raw: str) -> str:
        """Accept both '000001' and '000001.SZ', auto-detect exchange via mootdx."""
        s = raw.strip().upper()
        m = _SYMBOL_RE.match(s)
        if not m:
            return ""
        if m.group(2):  # already has suffix
            return s
        # Try mootdx resolution
        suffix = _resolve_exchange(s)
        if suffix:
            return s + "." + suffix
        # Fallback heuristic
        prefix = s[:3]
        if prefix in {"000", "001", "002", "003", "004", "159", "300", "301"}:
            return s + ".SZ"
        if prefix in {"600", "601", "603", "605", "688", "689"}:
            return s + ".SH"
        # Default guess for unknown prefixes (ETFs: 51x, 56x, 58x etc.)
        return s + ".SH"


    @app.post("/position/analyze", response_model=AnalyzeResponse, dependencies=[Depends(require_auth)])
    async def analyze_positions(request: Request, body: AnalyzeRequest) -> dict[str, Any]:
        global _ANALYSIS_CACHE

        symbols = [_normalize_symbol(s) for s in body.symbols]
        symbols = list(dict.fromkeys([s for s in symbols if s]))
        if not symbols:
            raise HTTPException(status_code=400, detail="No valid A-share symbols (e.g. 000001 or 000001.SZ)")

        cache_key = ",".join(sorted(symbols))
        # Manual refresh (non-zero _cache_bust) bypasses cache
        if body.cache_bust and body.cache_bust > 0:
            cache_key += f"|{int(body.cache_bust)}"
        now = time.time()
        with _CACHE_LOCK:
            cached = _ANALYSIS_CACHE.get(cache_key)
            if cached and not body.cache_bust and (now - cached.get("_ts", 0)) < _CACHE_TTL:
                return {k: v for k, v in cached.items() if not k.startswith("_")}

        # Fetch market data
        data = _fetch_a_share_data(symbols, days=90)
        unresolved = [s for s in symbols if s not in data]
        if unresolved:
            logger.warning("No data for: %s", unresolved)

        # Analyze each symbol concurrently
        loop = asyncio.get_event_loop()
        results = []
        for code in symbols:
            df = data.get(code)
            if df is None or df.empty:
                results.append({
                    "symbol": code, "name": _get_stock_name(code), "price": 0,
                    "change_pct": 0, "error": "无数据",
                    "trend": {"direction": "unknown", "ma_pattern": "—", "score": 0.50},
                    "technical": {"patterns": ["数据不足"], "score": 0.50},
                    "momentum": {"rsi": 0, "macd_signal": "—", "vol_ratio": 0, "score": 0.50},
                    "events": {"relevant_count": 0, "sentiment": "unknown", "score": 0.50},
                    "overall_score": 0.50, "decision": "hold", "decision_label": "数据不足",
                    "confidence": "low",
                    "dimensions": [], "stop_loss": None, "take_profit": None,
                    "risk_reward": None, "api_version": 2,
                })
                continue

            result = await loop.run_in_executor(None, _analyze_symbol, code, df)
            results.append(result)

        # Sort: strong_buy → buy → hold → sell → strong_sell, then by score
        order = {"strong_buy": 0, "buy": 1, "hold": 2, "sell": 3, "strong_sell": 4}
        results.sort(key=lambda r: (order.get(r["decision"], 99), -r["overall_score"]))

        # Fetch index
        index_data = _fetch_index_data()

        payload: dict[str, Any] = {
            "signals": results,
            "index": index_data,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        with _CACHE_LOCK:
            _ANALYSIS_CACHE[cache_key] = {**payload, "_ts": time.time()}

        return payload

    @app.get("/position/snapshot/{code}", response_model=SnapshotResponse, dependencies=[Depends(require_auth)])
    async def position_snapshot(code: str, request: Request) -> dict[str, Any]:
        code = code.strip().upper()
        if not _SYMBOL_RE.match(code):
            raise HTTPException(status_code=400, detail="Invalid code (e.g. 000001 or 000001.SZ)")

        data = _fetch_a_share_data([code], days=5)
        df = data.get(code)
        if df is None or df.empty:
            raise HTTPException(status_code=404, detail=f"No data for {code}")

        close = df["close"].astype(float)
        cur = round(float(close.iloc[-1]), 2)
        prev = round(float(close.iloc[-2]), 2)
        change = round((cur - prev) / prev * 100, 2)

        return {
            "symbol": code,
            "name": _get_stock_name(code),
            "price": cur,
            "change_pct": change,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    # ---- Watchlist persistence (server-side JSON store) ----

    from src.data.watchlist_store import load_watchlist, save_watchlist, remove_from_watchlist

    @app.get("/position/watchlist", dependencies=[Depends(require_auth)])
    async def get_watchlist(request: Request) -> dict[str, Any]:
        items = load_watchlist()
        return {"items": items, "count": len(items)}

    @app.put("/position/watchlist", dependencies=[Depends(require_auth)])
    async def put_watchlist(request: Request) -> dict[str, Any]:
        body = await request.json()
        items = body.get("items", []) if isinstance(body, dict) else []
        if not isinstance(items, list):
            raise HTTPException(status_code=400, detail="'items' must be a list")
        if len(items) > 100:
            raise HTTPException(status_code=400, detail="Max 100 items")
        save_watchlist(items)
        return {"ok": True, "count": len(items)}

    @app.delete("/position/watchlist/{symbol}", dependencies=[Depends(require_auth)])
    async def delete_watchlist_item(symbol: str, request: Request) -> dict[str, Any]:
        removed = remove_from_watchlist(symbol)
        return {"ok": removed, "symbol": symbol}

    # ---- AI Analysis endpoint (AgentLoop-driven) ----

    _AI_SYSTEM_PROMPT = (
        "你是 Vibe-Trading 的持仓分析 AI。你可以调用工具获取实时行情、新闻、研报、龙虎榜等数据。\n"
        "请按以下结构输出分析（每段用 ## 标题分隔，总共 400-600 字）：\n\n"
        "## 消息面解读\n"
        "用户 prompt 中已给出该股的预测市场事件和近期新闻标题。\n"
        "请逐一解读每条事件/新闻对该股的含义：\n"
        "- 事件A：概率多少、方向如何、24h 变动说明什么\n"
        "- 事件B：与该股行业的关系、利好还是利空\n"
        "- 新闻标题中隐含了什么信号（业绩拐点？政策风险？行业拐点？资金动向？）\n"
        "- 综合：消息面整体偏正面还是偏负面？最关键的 1 条消息是什么？\n"
        "如果没有任何事件或新闻，也请明确说明\"当前消息面真空，市场缺乏方向性催化\"。\n\n"
        "## 核心矛盾\n"
        "综合消息面 + 技术面 + 基本面 + Alpha因子，用 1-2 句话指出最关键的矛盾或共振。\n"
        "用户 prompt 中提供了该股在 20 个精选 Alpha 因子上的横截面排名（vs 同行业）。\n"
        "排名百分位越高代表该股在该因子维度上相对同行业越强。请在分析中至少引用 2-3 个最显著的因子信号。\n\n"
        "## 关键价位\n"
        "列出 3-5 个关键技术位，每个带具体价格：\n"
        "- 强支撑 / 弱支撑 / 当前价 / 弱阻力 / 强阻力\n\n"
        "## 买卖策略\n"
        "根据消息面 + 技术面给出分情景操作方案，明确触发条件和仓位比例。\n\n"
        "## 补仓/加仓策略（如有持仓）\n"
        "- 触发价、比例、止损调整、总仓位上限\n\n"
        "## 风险提示\n"
        "最重要的 1-2 个风险点。\n\n"
        "要求：\n"
        "- 所有价位必须是具体数字\n"
        "- 仓位建议用百分比\n"
        "- 像交易员给交易员写备注，不客套"
    )

    # Keywords that trigger LLM content filters — check event titles against these
    _SENSITIVE_PATTERNS = [
        "taiwan", "invasion", "sanctions", "coup", "assassination",
        "ccp", "xi jinping", "xJP",
    ]

    def _build_ai_prompt(signal: dict, position: dict | None) -> str:
        """Build a structured prompt for AI analysis from rule-engine results."""
        lines = ["请分析以下持仓：\n"]
        name = signal.get("name", signal.get("symbol", ""))
        code = signal.get("symbol", "")
        price = signal.get("price", 0)
        change = signal.get("change_pct", 0)
        lines.append(f"股票：{name}（{code}）")
        chg_sign = "+" if change >= 0 else ""
        lines.append(f"现价：¥{_fmt_price_val(price)}（{chg_sign}{change:.2f}%）")

        if position:
            cost = position.get("cost", 0)
            shares = position.get("shares", 0)
            mkt_val = price * shares if shares > 0 else 0
            pnl = (price - cost) * shares if shares > 0 else 0
            pnl_pct = ((price - cost) / cost * 100) if cost > 0 else 0
            lines.append(f"持仓：成本 ¥{_fmt_price_val(cost)}，{int(shares)}股，市值 ¥{_fmt_price_val(mkt_val)}，浮盈 {pnl:+.0f}元（{pnl_pct:+.1f}%）")

        sl = signal.get("stop_loss")
        tp = signal.get("take_profit")
        rr = signal.get("risk_reward")
        if sl and tp:
            lines.append(f"止损 ¥{_fmt_price_val(sl)} / 止盈 ¥{_fmt_price_val(tp)}" + (f" / 盈亏比 1:{rr}" if rr else ""))

        dimension_labels = {
            "technical": "技术面", "fundamental": "基本面", "capital": "资金面",
            "industry": "行业分析", "macro": "宏观环境", "risk": "风险评估", "events": "事件情绪",
        }
        lines.append("\n规则引擎 7 维度分析：")
        dims = signal.get("dimensions", [])
        if dims:
            for d in dims:
                d_id = d.get("id", "")
                label = dimension_labels.get(d_id, d.get("label", d_id))
                score_pct = int((d.get("score", 0.50)) * 100)
                summary = d.get("summary", "")
                # Collect key items
                key_items = []
                for item in d.get("items", [])[:3]:
                    key_items.append(f"{item['label']}:{item['value']}")
                detail = "，".join(key_items) if key_items else ""
                lines.append(f"- {label} {score_pct}分 {d.get('signal','')} {summary}  [{detail}]")

        overall_pct = int(signal.get("overall_score", 0.50) * 100)
        decision = signal.get("decision_label", "")
        confidence = signal.get("confidence", "")
        conf_cn = {"high": "高置信", "medium": "中置信", "low": "低置信"}.get(confidence, confidence)
        lines.append(f"\n综合评分：{overall_pct}/100 · 决策：{decision} · {conf_cn}")

        # Feed real event data into AI prompt
        events_dim = next((d for d in dims if d.get("id") == "events"), None)
        if events_dim:
            evt_items = events_dim.get("items", [])
            top_events = events_dim.get("top_events", [])
            top_news = events_dim.get("top_news", [])
            if top_events:
                lines.append("\n📊 相关预测市场事件（Polymarket/Kalshi）：")
                for evt in top_events:
                    title = evt["title"]
                    # Skip events with content that may trigger LLM content filters
                    if any(p in title.lower() for p in _SENSITIVE_PATTERNS):
                        prob_pct = int(evt.get("probability", 0) * 100)
                        chg = evt.get("change_24h", 0) or 0
                        chg_str = f"↑{chg*100:+.1f}pt" if chg > 0.005 else f"↓{abs(chg)*100:.1f}pt" if chg < -0.005 else "—"
                        lines.append(f"  · [地缘政治相关事件] 概率{prob_pct}% {chg_str} {evt.get('volume','')}")
                    else:
                        prob_pct = int(evt.get("probability", 0) * 100)
                        chg = evt.get("change_24h", 0) or 0
                        chg_str = f"↑{chg*100:+.1f}pt" if chg > 0.005 else f"↓{abs(chg)*100:.1f}pt" if chg < -0.005 else "—"
                        lines.append(f'  · "{title}"  概率{prob_pct}% {chg_str}  {evt.get("volume","")}')
            if top_news:
                lines.append("\n📰 个股近期新闻：")
                for n in top_news:
                    pub = str(n.get("published", ""))[:10]
                    title = n["title"]
                    if any(p in title.lower() for p in _SENSITIVE_PATTERNS):
                        lines.append(f"  · [时政相关新闻]  [{pub}]")
                    else:
                        lines.append(f"  · {title}  [{pub}]")

        # Feed alpha factor signals into AI prompt
        alphas_dim = next((d for d in dims if d.get("id") == "alphas"), None)
        alpha_raw_data = alphas_dim.get("alpha_raw") if alphas_dim else None
        if alpha_raw_data and isinstance(alpha_raw_data, dict):
            ok_signals = [s for s in alpha_raw_data.get("signals", []) if s.get("status") == "ok"]
            if ok_signals:
                peer_count = alpha_raw_data.get("peer_count", 0)
                lines.append(f"\n📈 Alpha 因子信号（{peer_count}只沪深300同行业横截面排名，排名%越高相对越强）：")
                for s in ok_signals:
                    pct = int(s["rank_pct"] * 100)
                    tag = "↑强" if pct >= 70 else "↓弱" if pct <= 30 else "→中"
                    lines.append(f"  - {s['emoji']} {s['label']}（{s['theme']}）：排名 {pct}%（前{s['rank_pct']*100:.0f}%）{tag}")
                lines.append("解读提示：排名>70%表明显著强于同行业，<30%表明显著弱于同行业。请结合量化因子信号做综合判断。")

        lines.append("")
        lines.append("请调用工具获取最新行情数据（包含 MA5/MA10/MA20/MA60/MA120 均线、支撑阻力位），")
        lines.append("然后按系统指令中的 5 段结构（核心矛盾、关键价位、买卖策略、补仓策略、风险提示）")
        lines.append("输出完整分析。结合上方预测市场事件、个股新闻和 Alpha 因子信号进行综合判断。")
        lines.append("所有价位必须是具体数字，仓位建议用百分比。")
        if position and position.get("shares", 0) > 0:
            lines.append("该股有持仓，请重点给出补仓/加仓/减仓的具体触发价位和比例。")

        return "\n".join(lines)

    def _fmt_price_val(p: float) -> str:
        """Format a price value for the AI prompt."""
        return f"{p:.0f}" if p >= 1000 else f"{p:.2f}"

    @app.post("/position/ai-analysis", dependencies=[Depends(require_auth)])
    async def position_ai_analysis(request: Request, body: AiAnalysisRequest):
        code = body.code.strip().upper()
        if not _SYMBOL_RE.match(code):
            raise HTTPException(status_code=400, detail="Invalid code format")

        # Check cache
        cache_key = code
        if body.cache_bust and body.cache_bust > 0:
            cache_key += f"|{int(body.cache_bust)}"
        now = time.time()
        with _AI_CACHE_LOCK:
            cached = _AI_CACHE.get(cache_key)
            if cached and (now - cached.get("_ts", 0)) < _AI_CACHE_TTL:
                content = cached.get("content", "")
                async def cached_stream():
                    yield f"event: text_delta\ndata: {json.dumps({'delta': content})}\n\n"
                    yield f"event: analysis_complete\ndata: {json.dumps({'content': content, 'cached': True})}\n\n"
                return StreamingResponse(cached_stream(), media_type="text/event-stream")

        prompt = _build_ai_prompt(body.signal, body.position)

        async def event_stream():
            from src.tools import build_registry
            from src.providers.chat import ChatLLM
            from src.agent.loop import AgentLoop
            from src.memory.persistent import PersistentMemory

            event_queue: asyncio.Queue = asyncio.Queue()
            full_content = ""
            run_result: dict[str, Any] = {}

            def on_event(event_type: str, data: dict) -> None:
                # Forward events to the async queue (thread-safe)
                event_queue.put_nowait({"type": event_type, "data": data})

            def run_agent() -> dict[str, Any]:
                pm = PersistentMemory()
                llm = ChatLLM()
                registry = build_registry(persistent_memory=pm, include_shell_tools=False)
                agent = AgentLoop(
                    registry=registry,
                    llm=llm,
                    max_iterations=3,
                    event_callback=on_event,
                    persistent_memory=pm,
                )
                return agent.run(
                    user_message=_AI_SYSTEM_PROMPT + "\n\n" + prompt,
                    session_id="",
                )

            loop = asyncio.get_running_loop()
            # Start AgentLoop in thread pool — events come through the queue
            agent_future = loop.run_in_executor(None, run_agent)

            # Stream events as they arrive until AgentLoop completes
            while not agent_future.done() or not event_queue.empty():
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                    etype = event["type"]
                    data = event["data"]
                    if etype == "text_delta":
                        delta = data.get("delta", "")
                        full_content += delta
                        yield f"event: text_delta\ndata: {json.dumps({'delta': delta})}\n\n"
                    elif etype in ("tool_call", "tool_result"):
                        yield f"event: {etype}\ndata: {json.dumps(data)}\n\n"
                    elif etype == "llm_usage":
                        yield f"event: llm_usage\ndata: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    pass

            # Collect result
            error_msg = ""
            run_id = ""
            try:
                run_result = await agent_future
                full_content = run_result.get("content", "") or full_content
                run_id = run_result.get("run_id", "")
                # AgentLoop may return failed status with empty content
                if run_result.get("status") == "failed" and not full_content:
                    reason = run_result.get("reason", "")
                    if "1301" in reason or "contentFilter" in reason or "敏感" in reason:
                        error_msg = "AI 分析被内容安全拦截。请稍后重试或更换股票。"
                    elif "BadRequestError" in reason or "400" in reason:
                        error_msg = "AI 服务请求失败，请稍后重试。"
                    else:
                        error_msg = f"AI 分析失败: {reason[:100]}" if reason else "AI 分析失败，请稍后重试。"
            except Exception as exc:
                err_str = str(exc)
                if "1301" in err_str or "contentFilter" in err_str or "敏感" in err_str:
                    error_msg = "AI 分析被内容安全拦截。预测市场事件标题含政治敏感词（如 Taiwan/sanctions），请稍后重试或更换股票。"
                elif "BadRequestError" in err_str or "400" in err_str:
                    error_msg = "AI 服务请求失败（参数错误），请稍后重试。"
                elif "timeout" in err_str.lower() or "timed out" in err_str.lower():
                    error_msg = "AI 分析超时，请稍后重试。"
                else:
                    error_msg = f"AI 分析出错: {err_str[:120]}"
                logger.warning("AI analysis failed for %s: %s", code, exc)

            if error_msg:
                full_content = error_msg

            # Cache successful results
            if full_content and not error_msg:
                with _AI_CACHE_LOCK:
                    _AI_CACHE[cache_key] = {"content": full_content, "_ts": time.time()}

            logger.info("AI analysis SSE done: content_len=%d, error=%s, run_id=%s", len(full_content), error_msg[:50] if error_msg else "", run_id)
            yield "event: debug_done\ndata: {\"ok\":true}\n\n"
            yield f"event: analysis_complete\ndata: {json.dumps({'content': full_content, 'error': bool(error_msg), 'run_id': run_id})}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")
