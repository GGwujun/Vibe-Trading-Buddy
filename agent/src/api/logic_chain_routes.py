"""Logic Chain — comprehensive single-stock analysis across 8 dimensions.

Mounted by ``agent/api_server.py`` via ``register_logic_chain_routes(app, ...)``.

Route:
- ``GET /logic-chain/{code}`` — eight-layer stock analysis

Layers (order reflects top-down reasoning):
1. Macro environment     (index trend, volume, event sentiment)
2. Industry analysis     (sector performance, position, ranking)
3. Fundamentals          (F10: financial health, ROE, PE, institutional)
4. Technicals            (trend, momentum, patterns, support/resistance)
5. Capital flows         (F10: top trader, north-bound, shareholder changes)
6. Sentiment             (news, analyst ratings, event catalysts)
7. Risk assessment       (volatility, max drawdown, VaR)
8. Composite decision    (weighted score, action, position size, SL/TP)

Weights: Technical 35% + Capital 20% + Industry 15% + Fundamental 15% + Macro 10% + Sentiment 5%
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import numpy as np
import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 600  # 10 min

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signal_emoji(score: float) -> str:
    if score >= 0.60:
        return "✅"
    if score >= 0.40:
        return "⚠️"
    return "🔴"


def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).mean()


# ---------------------------------------------------------------------------
# F10 data helper — fixes the bug where code called F10C(code, N)
# F10C() only accepts symbol= and returns a table-of-contents.
# F10() returns the actual F10 document sections as GBK-encoded text.
# ---------------------------------------------------------------------------

# Cache F10 results per code to avoid redundant API calls within one request
_F10_CACHE: dict[str, dict[str, str]] = {}  # {code: {section_name_cn: text}}


def _f10_section_text(code: str, section_kw: str) -> str | None:
    """Return the text of a F10 section matching *section_kw*.

    Args:
        code: Plain 6-digit code (no .SZ/.SH suffix).
        section_kw: Chinese keyword to match against section names (e.g. '财务').

    Returns the section text (mootdx already returns correctly-decoded
    Chinese strings), or None if unavailable.
    """
    raw_code = code.replace(".SZ", "").replace(".SH", "")
    if raw_code in _F10_CACHE:
        cached = _F10_CACHE[raw_code]
        for name, text in cached.items():
            if section_kw in name:
                return text
        return None

    try:
        from src.data.mootdx_helper import get_quotes
        client = get_quotes(timeout=12)
        data = client.F10(raw_code)
        if not isinstance(data, dict) or not data:
            return None

        # mootdx returns keys as correctly-decoded Chinese and values as
        # plain str — use them as-is (no re-encoding needed).
        decoded: dict[str, str] = {}
        for k, v in data.items():
            decoded[k] = str(v)

        _F10_CACHE[raw_code] = decoded

        for name, text in decoded.items():
            if section_kw in name:
                return text
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Layer 1: Macro Environment
# ---------------------------------------------------------------------------


def _layer_macro() -> dict[str, Any]:
    """Analyze macro environment: index trend, volume, events."""
    items: list[dict[str, Any]] = []
    score = 0.50

    # 1a. Index trend (disk-cached)
    try:
        from src.data.ohlcv_cache import fetch_with_cache
        df = fetch_with_cache("999999.SH", days=30)
        if df is not None and not df.empty:
            close = df["close"].astype(float)
            volume = df["volume"].astype(float)
            cur = float(close.iloc[-1])
            ma20 = float(_sma(close, 20).iloc[-1])
            ma5 = float(_sma(close, 5).iloc[-1])
            vol_5 = float(volume.iloc[-5:].mean())
            vol_20 = float(volume.iloc[-20:].mean())
            vol_ratio = vol_5 / vol_20 if vol_20 > 0 else 1.0

            if ma5 > ma20:
                direction = "多头排列"
                items.append({"label": "上证趋势", "value": f"MA5>{'MA20' if cur > ma20 else 'MA20附近'}", "signal": "✅" if cur > ma20 else "⚠️"})
                score += 0.10
            else:
                direction = "空头排列"
                items.append({"label": "上证趋势", "value": "MA5<MA20 偏弱", "signal": "🔴"})
                score -= 0.08

            vol_signal = "✅" if vol_ratio > 1.1 else "⚠️" if vol_ratio > 0.8 else "🔴"
            items.append({"label": "量能", "value": f"量比 {vol_ratio:.1f}x", "signal": vol_signal})
            if vol_ratio > 1.1:
                score += 0.05
            elif vol_ratio < 0.8:
                score -= 0.05
    except Exception:
        items.append({"label": "上证趋势", "value": "数据获取失败", "signal": "⚠️"})

    # 1b. Event sentiment
    try:
        import sys
        em = sys.modules.get("src.api.events_routes")
        if em and em._EVENTS_CACHE:
            china_hits = []
            for cat in em._EVENTS_CACHE.get("categories", []):
                for e in cat.get("events", []):
                    if any(kw in e.get("title", "").lower() for kw in ["china", "taiwan", "tariff", "trade", "beijing"]):
                        china_hits.append(e)
            if china_hits:
                big = [e for e in china_hits if abs(e.get("prob_change_24h", 0)) > 0.05]
                items.append({"label": "事件情绪", "value": f"{len(big)}件概率异动" if big else "无重大异动", "signal": "⚠️" if big else "✅"})
                if big:
                    score -= 0.03 * min(len(big), 3)
            else:
                items.append({"label": "事件情绪", "value": "无相关事件", "signal": "✅"})
    except Exception:
        items.append({"label": "事件情绪", "value": "—", "signal": "⚠️"})

    score = max(0.05, min(0.95, score))
    return {"score": round(score, 3), "items": items, "signal": _signal_emoji(score)}


# ---------------------------------------------------------------------------
# Layer 2: Industry Analysis
# ---------------------------------------------------------------------------


def _layer_industry(code: str) -> dict[str, Any]:
    """Analyze industry/sector: via mootdx sector data and F10."""
    items: list[dict[str, Any]] = []
    score = 0.50
    raw_code = code.replace(".SZ", "").replace(".SH", "")

    # 2a. Industry name — try 公司概况 first, then 行业分析
    company_text = _f10_section_text(raw_code, "公司")
    industry_text = _f10_section_text(raw_code, "行业")
    ind_name = ""

    # Try to find 所属行业 in the company overview
    if company_text:
        m = re.search(r'所属行业[：:\s]*([^\n\r]{2,20})', company_text)
        if m:
            ind_name = m.group(1).strip()
    # Fallback: extract sector from 行业分析 section heading like "--股份制行--"
    if not ind_name and industry_text:
        m = re.search(r'--(\S{2,12}?)--', industry_text)
        if m:
            ind_name = m.group(1).strip()
    # Last fallback: look for industry keywords
    if not ind_name and industry_text:
        m = re.search(r'(?:行业|板块)[：:\s]*([^\n\r]{2,20})', industry_text)
        if m:
            ind_name = m.group(1).strip()[:20]

    if ind_name:
        items.append({"label": "所属行业", "value": ind_name, "signal": "✅"})

    # 2b. Industry position/ranking
    rank_text = _f10_section_text(raw_code, "业内")
    all_ind_text = (industry_text or "") + (rank_text or "")
    if "龙头" in all_ind_text or "前列" in all_ind_text or "领先" in all_ind_text:
        items.append({"label": "业内地位", "value": "行业领先", "signal": "✅"})
        score += 0.12
    elif "中游" in all_ind_text:
        items.append({"label": "业内地位", "value": "行业中游", "signal": "⚠️"})
    elif ind_name:
        # Have industry name but no explicit ranking — still provide a position item
        items.append({"label": "业内地位", "value": "未披露排名", "signal": "⚠️"})

    # 2b. Sector trend via stock block
    try:
        from src.data.mootdx_helper import get_quotes
        client = get_quotes(timeout=10)
        blocks = client.block()
        found_blocks = []
        if blocks is not None and isinstance(blocks, dict):
            for bk_name, bk_data in blocks.items():
                if isinstance(bk_data, dict) and "code" in bk_data:
                    codes = str(bk_data["code"]).split(",")
                    if raw_code in codes:
                        found_blocks.append(bk_name)
        if found_blocks:
            items.append({"label": "概念板块", "value": "、".join(found_blocks[:3]), "signal": "⚠️"})
            if len(found_blocks) >= 2:
                score += 0.05
    except Exception:
        pass

    if len(items) < 2:
        items.append({"label": "行业信息", "value": "数据有限", "signal": "⚠️"})

    score = max(0.05, min(0.95, score))
    return {"score": round(score, 3), "items": items, "signal": _signal_emoji(score)}


# ---------------------------------------------------------------------------
# Layer 3: Fundamentals
# ---------------------------------------------------------------------------


def _parse_f10_number(text: str) -> float | None:
    """Extract a number from F10 formatted text."""
    nums = re.findall(r'[-+]?\d+\.?\d*', str(text).replace(",", "").replace("%", ""))
    return float(nums[0]) if nums else None


def _layer_fundamental(code: str) -> dict[str, Any]:
    """Analyze fundamentals from F10 data (财务分析 + 股东研究 sections)."""
    items: list[dict[str, Any]] = []
    score = 0.50
    raw_code = code.replace(".SZ", "").replace(".SH", "")

    # Financial data from 财务分析 section
    fin_text = _f10_section_text(raw_code, "财务")
    if fin_text:
        # F10 table format uses ｜ (fullwidth pipe) as column separator.
        # Each row: 指标名  (spaces)  ｜  value1  ｜  value2  ｜ ...
        # The FIRST value after ｜ is the most recent quarter.

        def _first_val(pattern: str) -> str | None:
            """Match pattern and return the first numeric value after ｜."""
            m = re.search(pattern + r'[｜\|\s]*([\d.-]+)', fin_text)
            return m.group(1) if m else None

        # ROE — 净资产收益率
        roe_str = _first_val(r'净资产收益率[^｜\|\n]*')
        if roe_str:
            roe = float(roe_str)
            items.append({"label": "ROE", "value": f"{roe:.1f}%", "signal": "✅" if roe > 10 else "⚠️" if roe > 5 else "🔴"})
            score += 0.06 if roe > 10 else -0.05 if roe < 5 else 0

        # Revenue growth — 营业收入增长率
        rev_str = _first_val(r'(?:营业[总收]入|主营)[^｜\|\n]*?(?:增长|同比|增长率)')
        if not rev_str:
            rev_str = _first_val(r'营业[总收]入[^｜\|\n]*?(?:增长|同比)')
        if rev_str:
            rev_g = float(rev_str)
            items.append({"label": "营收增速", "value": f"{rev_g:+.1f}%", "signal": "✅" if rev_g > 10 else "⚠️" if rev_g > 0 else "🔴"})
            score += 0.05 if rev_g > 10 else -0.04 if rev_g < 0 else 0

        # EPS — 每股收益
        eps_str = _first_val(r'(?:基本|稀释)?每股收益[^｜\|\n]*?(?:扣除|.)')
        if not eps_str:
            eps_str = _first_val(r'每股收益[^｜\|\n]*')
        if eps_str:
            eps = float(eps_str)
            items.append({"label": "每股收益", "value": f"¥{eps:.2f}", "signal": "✅" if eps > 0.5 else "⚠️" if eps > 0 else "🔴"})
            score += 0.03 if eps > 0.5 else -0.03 if eps < 0 else 0

        # Debt ratio — 资产负债率
        debt_str = _first_val(r'资产负债率[^｜\|\n]*')
        if debt_str:
            debt = float(debt_str)
            items.append({"label": "资产负债率", "value": f"{debt:.1f}%", "signal": "✅" if debt < 60 else "⚠️" if debt < 80 else "🔴"})
            if debt < 60:
                score += 0.04
            elif debt > 80:
                score -= 0.04

        # Net profit — 净利润
        profit_match = re.search(r'净利润[^｜\|\n]*?[｜\|]\s*([\d.]+)\s*[亿万千]', fin_text)
        if profit_match:
            items.append({"label": "净利润", "value": profit_match.group(0).strip()[:40], "signal": "✅"})

    # Shareholder data from 股东研究 section
    holder_text = _f10_section_text(raw_code, "股东")
    if holder_text:
        # Institutional holdings
        inst_match = re.search(r'(?:机构|基金).*?持股[^}]*?([\d.]+)\s*%', holder_text)
        if not inst_match:
            inst_match = re.search(r'持股[比列][^}]*?([\d.]+)\s*%', holder_text)
        if inst_match:
            inst_pct = float(inst_match.group(1))
            items.append({"label": "机构持股", "value": f"{inst_pct:.1f}%", "signal": "✅" if inst_pct > 5 else "⚠️"})
            if inst_pct > 5:
                score += 0.04

        # Shareholder count trend
        if "减少" in holder_text:
            items.append({"label": "股东人数", "value": "减少（筹码集中）", "signal": "✅"})
            score += 0.03
        elif "增加" in holder_text:
            items.append({"label": "股东人数", "value": "增加（筹码分散）", "signal": "⚠️"})
            score -= 0.03

    if len(items) < 2:
        items.append({"label": "基本面数据", "value": "F10数据有限", "signal": "⚠️"})

    score = max(0.05, min(0.95, score))
    return {"score": round(score, 3), "items": items, "signal": _signal_emoji(score)}


# ---------------------------------------------------------------------------
# Layer 4: Technicals (reuse position_routes)
# ---------------------------------------------------------------------------


def _layer_technical(df: pd.DataFrame) -> dict[str, Any]:
    """Technical analysis: trend, momentum, patterns."""
    items: list[dict[str, Any]] = []
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    cur = float(close.iloc[-1])

    # Trend
    ma5 = float(_sma(close, 5).iloc[-1])
    ma10 = float(_sma(close, 10).iloc[-1])
    ma20 = float(_sma(close, 20).iloc[-1])
    ma60 = float(_sma(close, 60).iloc[-1]) if len(close) >= 60 else ma20

    if ma5 > ma10 > ma20:
        trend_label = "多头排列"
        trend_signal = "✅"
        trend_score = 0.85
    elif ma5 > ma10:
        trend_label = "短期偏多"
        trend_signal = "⚠️"
        trend_score = 0.60
    elif ma5 < ma10 < ma20:
        trend_label = "空头排列"
        trend_signal = "🔴"
        trend_score = 0.15
    else:
        trend_label = "震荡整理"
        trend_signal = "⚠️"
        trend_score = 0.35

    items.append({"label": "趋势", "value": trend_label, "signal": trend_signal})

    # Support / Resistance
    high20 = float(close.iloc[-20:].max())
    low20 = float(close.iloc[-20:].min())
    items.append({"label": "支撑位", "value": f"¥{low20:.2f}", "signal": "✅" if cur > low20 * 1.03 else "⚠️"})
    items.append({"label": "阻力位", "value": f"¥{high20:.2f}", "signal": "⚠️" if cur > high20 * 0.95 else "✅"})

    # RSI
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi_val = 100 - (100 / (1 + rs))
    rsi = round(float(rsi_val.iloc[-1]), 1) if not pd.isna(rsi_val.iloc[-1]) else 50
    items.append({"label": "RSI(14)", "value": f"{rsi}", "signal": "✅" if 40 < rsi < 70 else "⚠️" if rsi < 30 else "🔴" if rsi > 85 else "⚠️"})

    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    cur_dif = float(dif.iloc[-1])
    prev_dif = float(dif.iloc[-2])

    if cur_dif > 0:
        macd_label = "金叉" if prev_dif <= 0 else "多头"
        macd_signal = "✅"
    else:
        macd_label = "死叉" if prev_dif >= 0 else "空头"
        macd_signal = "🔴" if macd_label == "死叉" else "⚠️"
    items.append({"label": "MACD", "value": macd_label, "signal": macd_signal})

    # Volume
    vol_5 = float(volume.iloc[-5:].mean())
    vol_20 = float(volume.iloc[-20:].mean())
    vol_ratio = vol_5 / vol_20 if vol_20 > 0 else 1
    items.append({"label": "量比", "value": f"{vol_ratio:.1f}x", "signal": "✅" if 0.8 < vol_ratio < 2.5 else "⚠️"})

    # Patterns
    try:
        from src.tools.pattern_tool import find_peaks_valleys
        pv = find_peaks_valleys(close, window=5)
        peaks = pv["peaks"]
        valleys = pv["valleys"]
        patterns = []
        # Double bottom check
        if len(valleys) >= 2:
            v1, v2 = valleys[-2], valleys[-1]
            l1, l2 = float(close.iloc[v1]), float(close.iloc[v2])
            if abs(l1 / l2 - 1) < 0.03 and (v2 - v1) > 10:
                patterns.append("双底")
        if len(peaks) >= 2:
            p1, p2 = peaks[-2], peaks[-1]
            h1, h2 = float(close.iloc[p1]), float(close.iloc[p2])
            if abs(h1 / h2 - 1) < 0.03 and (p2 - p1) > 10:
                patterns.append("双顶")
        if patterns:
            items.append({"label": "技术形态", "value": "、".join(patterns), "signal": "✅" if "双底" in patterns else "⚠️"})
        else:
            items.append({"label": "技术形态", "value": "无明显形态", "signal": "⚠️"})
    except Exception:
        items.append({"label": "技术形态", "value": "—", "signal": "⚠️"})

    # Composite technical score
    tech_score = trend_score * 0.4 + 0.6
    tech_score += 0.05 if rsi_val.iloc[-1] and 40 < float(rsi_val.iloc[-1]) < 70 else -0.05 if float(rsi_val.iloc[-1]) < 30 else 0
    tech_score += 0.05 if cur_dif > 0 else -0.05
    tech_score = max(0.05, min(0.95, tech_score))

    return {"score": round(tech_score, 3), "items": items, "signal": _signal_emoji(tech_score)}


# ---------------------------------------------------------------------------
# Layer 5: Capital Flows
# ---------------------------------------------------------------------------


def _layer_capital(code: str) -> dict[str, Any]:
    """Capital flow analysis from F10 data (主力追踪 + 股东研究 sections)."""
    items: list[dict[str, Any]] = []
    score = 0.50
    raw_code = code.replace(".SZ", "").replace(".SH", "")

    # Dragon-Tiger board / major player tracking from 主力追踪 section
    lhb_text = _f10_section_text(raw_code, "主力")
    if lhb_text:
        if "买入" in lhb_text or "增持" in lhb_text:
            items.append({"label": "龙虎榜", "value": "近期上榜", "signal": "⚠️"})
            net_buy = lhb_text.count("买入") + lhb_text.count("增持") - lhb_text.count("卖出") - lhb_text.count("减持")
            items.append({"label": "净买卖", "value": f"买{lhb_text.count('买入')}卖{lhb_text.count('卖出')}", "signal": "✅" if net_buy > 0 else "🔴"})
            score += 0.06 if net_buy > 0 else -0.06
        else:
            items.append({"label": "龙虎榜", "value": "近期未上榜", "signal": "✅"})

    # Shareholder changes from 股东研究 section
    holder_text = _f10_section_text(raw_code, "股东")
    if holder_text:
        # Shareholder count trend
        if "减少" in holder_text:
            items.append({"label": "股东户数", "value": "减少（筹码集中）", "signal": "✅"})
            score += 0.04
        elif "增加" in holder_text:
            items.append({"label": "股东户数", "value": "增加（筹码分散）", "signal": "⚠️"})
            score -= 0.03

        # Major shareholder changes
        if "增持" in holder_text:
            items.append({"label": "大股东动向", "value": "增持", "signal": "✅"})
            score += 0.04
        elif "减持" in holder_text:
            items.append({"label": "大股东动向", "value": "减持", "signal": "🔴"})
            score -= 0.04

    if len(items) < 2:
        items.append({"label": "资金数据", "value": "数据有限", "signal": "⚠️"})

    score = max(0.05, min(0.95, score))
    return {"score": round(score, 3), "items": items, "signal": _signal_emoji(score)}


# ---------------------------------------------------------------------------
# Layer 6: Sentiment
# ---------------------------------------------------------------------------


def _layer_sentiment(code: str, name: str) -> dict[str, Any]:
    """Sentiment analysis: news + analyst ratings + events."""
    items: list[dict[str, Any]] = []
    score = 0.50

    # 6a. Analyst rating from F10 研究评级 section
    rating_text = _f10_section_text(raw_code, "研究") or _f10_section_text(raw_code, "评级")
    if rating_text:
        if "买入" in rating_text or "增持" in rating_text:
            items.append({"label": "研报评级", "value": "买入/增持", "signal": "✅"})
            score += 0.08
        elif "卖出" in rating_text or "减持" in rating_text:
            items.append({"label": "研报评级", "value": "卖出/减持", "signal": "🔴"})
            score -= 0.08
        else:
            items.append({"label": "研报评级", "value": "中性或无", "signal": "⚠️"})
    else:
        items.append({"label": "研报评级", "value": "—", "signal": "⚠️"})

    # 6b. News count via DDG
    try:
        from src.api.news_routes import _fetch_ddg_news
        news = _fetch_ddg_news(f"{name} 股票", max_results=5)
        items.append({"label": "近期新闻", "value": f"{len(news)}条", "signal": "✅" if len(news) >= 3 else "⚠️" if len(news) > 0 else "🔴"})
        if len(news) >= 3:
            score += 0.03
    except Exception:
        items.append({"label": "近期新闻", "value": "—", "signal": "⚠️"})

    # 6c. Event catalyst
    try:
        import sys
        em = sys.modules.get("src.api.events_routes")
        if em and em._EVENTS_CACHE:
            hits = []
            for cat in em._EVENTS_CACHE.get("categories", []):
                for e in cat.get("events", []):
                    if abs(e.get("prob_change_24h", 0)) >= 0.05:
                        hits.append(e)
            items.append({"label": "事件催化", "value": f"{len(hits)}件异动" if hits else "无", "signal": "⚠️" if hits else "✅"})
    except Exception:
        pass

    score = max(0.05, min(0.95, score))
    return {"score": round(score, 3), "items": items, "signal": _signal_emoji(score)}


# ---------------------------------------------------------------------------
# Layer 7: Risk Assessment
# ---------------------------------------------------------------------------


def _layer_risk(df: pd.DataFrame) -> dict[str, Any]:
    """Risk metrics: volatility, max drawdown, VaR."""
    items: list[dict[str, Any]] = []
    score = 0.50
    close = df["close"].astype(float)

    # Daily returns
    rets = close.pct_change().dropna()

    # Volatility (annualized)
    vol_daily = float(rets.std())
    vol_annual = vol_daily * np.sqrt(252)
    items.append({"label": "年化波动率", "value": f"{vol_annual:.1%}", "signal": "✅" if vol_annual < 0.40 else "⚠️" if vol_annual < 0.60 else "🔴"})
    if vol_annual < 0.40:
        score += 0.05
    elif vol_annual > 0.60:
        score -= 0.05

    # Max drawdown (recent)
    cummax = close.cummax()
    dd = (close - cummax) / cummax
    max_dd = float(dd.min())
    items.append({"label": "最大回撤(期)", "value": f"{max_dd:.1%}", "signal": "✅" if max_dd > -0.15 else "⚠️" if max_dd > -0.25 else "🔴"})
    if max_dd > -0.15:
        score += 0.05
    elif max_dd < -0.25:
        score -= 0.04

    # VaR (95%)
    var95 = float(np.percentile(rets, 5))
    items.append({"label": "VaR(95%)", "value": f"{var95:.2%}", "signal": "✅" if var95 > -0.04 else "⚠️" if var95 > -0.06 else "🔴"})
    if var95 > -0.04:
        score += 0.04
    elif var95 < -0.06:
        score -= 0.03

    # Sharpe (approximate, risk-free = 2%)
    rf_daily = 0.02 / 252
    sharpe = (float(rets.mean()) - rf_daily) / vol_daily * np.sqrt(252) if vol_daily > 0 else 0
    items.append({"label": "夏普比率", "value": f"{sharpe:.2f}", "signal": "✅" if sharpe > 0.5 else "⚠️" if sharpe > 0 else "🔴"})
    if sharpe > 0.5:
        score += 0.05
    elif sharpe < 0:
        score -= 0.04

    score = max(0.05, min(0.95, score))
    return {"score": round(score, 3), "items": items, "signal": _signal_emoji(score)}


# ---------------------------------------------------------------------------
# Build the full chain
# ---------------------------------------------------------------------------


def _build_chain(code: str) -> dict[str, Any]:
    """Execute all 8 layers and return the full logic chain."""
    from src.api.position_routes import _get_stock_name, _fetch_a_share_data

    name = _get_stock_name(code)
    data = _fetch_a_share_data([code], days=90)
    df = data.get(code)

    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"No market data for {code}")

    close = df["close"].astype(float)
    cur_price = round(float(close.iloc[-1]), 2)
    prev_price = round(float(close.iloc[-2]), 2)
    change_pct = round((cur_price - prev_price) / prev_price * 100, 2)

    # Run all layers
    macro = _layer_macro()
    industry = _layer_industry(code)
    fundamental = _layer_fundamental(code)
    technical = _layer_technical(df)
    capital = _layer_capital(code)
    sentiment = _layer_sentiment(code, name)
    risk = _layer_risk(df)

    # Weighted composite
    weights = {
        "technical": 0.35, "capital": 0.20, "industry": 0.15,
        "fundamental": 0.15, "macro": 0.10, "sentiment": 0.05,
    }
    composite = (
        technical["score"] * weights["technical"]
        + capital["score"] * weights["capital"]
        + industry["score"] * weights["industry"]
        + fundamental["score"] * weights["fundamental"]
        + macro["score"] * weights["macro"]
        + sentiment["score"] * weights["sentiment"]
    )
    # Risk acts as a modifier: good risk profile adds, bad subtracts
    risk_mod = (risk["score"] - 0.50) * 0.2
    composite = max(0.01, min(0.99, composite + risk_mod))

    if composite >= 0.60:
        action = "买入"
        position_pct = int(min(30, max(10, composite * 40)))
    elif composite >= 0.40:
        action = "持有/观望"
        position_pct = int(max(5, (composite - 0.35) * 30))
    else:
        action = "卖出/回避"
        position_pct = 0

    # Stop loss / Take profit
    atr = float((df["high"].astype(float) - df["low"].astype(float)).iloc[-14:].mean())
    stop_loss = round(cur_price - 2 * atr, 2)
    take_profit = round(cur_price + 3 * atr, 2)

    layers = [
        {"id": "macro", "label": "宏观环境", "icon": "globe", **macro},
        {"id": "industry", "label": "行业分析", "icon": "building", **industry},
        {"id": "fundamental", "label": "基本面", "icon": "file-text", **fundamental},
        {"id": "technical", "label": "技术面", "icon": "trending-up", **technical},
        {"id": "capital", "label": "资金面", "icon": "banknote", **capital},
        {"id": "sentiment", "label": "情绪面", "icon": "newspaper", **sentiment},
        {"id": "risk", "label": "风险评估", "icon": "shield", **risk},
    ]

    return {
        "code": code,
        "name": name,
        "price": cur_price,
        "change_pct": change_pct,
        "layers": layers,
        "decision": {
            "score": round(composite, 3),
            "signal": _signal_emoji(composite),
            "action": action,
            "position_pct": position_pct,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "risk_reward": round((take_profit - cur_price) / (cur_price - stop_loss), 1) if stop_loss < cur_price else 0,
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class LayerItem(BaseModel):
    label: str
    value: str
    signal: str


class Layer(BaseModel):
    id: str
    label: str
    icon: str
    score: float
    signal: str
    items: list[dict[str, Any]] = Field(default_factory=list)


class Decision(BaseModel):
    score: float
    signal: str
    action: str
    position_pct: int
    stop_loss: float
    take_profit: float
    risk_reward: float


class LogicChainResponse(BaseModel):
    code: str
    name: str
    price: float
    change_pct: float
    layers: list[dict[str, Any]]
    decision: dict[str, Any]
    updated_at: str


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

AuthDep = Callable[..., Awaitable[Any] | Any]


def register_logic_chain_routes(
    app: FastAPI,
    require_auth: AuthDep | None = None,
    require_event_stream_auth: AuthDep | None = None,
) -> None:
    if require_auth is None or require_event_stream_auth is None:
        import sys as _sys
        host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        if host is None:
            raise RuntimeError("register_logic_chain_routes: api_server not in sys.modules")
        if require_auth is None:
            require_auth = host.require_auth
        if require_event_stream_auth is None:
            require_event_stream_auth = host.require_event_stream_auth

    _CODE_RE = __import__("re").compile(r"^\d{6}\.(SZ|SH)$")

    @app.get("/logic-chain/{code}", response_model=LogicChainResponse, dependencies=[Depends(require_auth)])
    async def logic_chain(code: str, request: Request) -> dict[str, Any]:
        code = code.strip().upper()
        if not _CODE_RE.match(code):
            raise HTTPException(status_code=400, detail="Invalid code (e.g. 000001.SZ)")

        cache_key = f"lc:{code}"
        now = time.time()
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            if cached and (now - cached.get("_ts", 0)) < _CACHE_TTL:
                return {k: v for k, v in cached.items() if not k.startswith("_")}

        import asyncio
        loop = asyncio.get_event_loop()
        payload = await loop.run_in_executor(None, _build_chain, code)

        with _CACHE_LOCK:
            _CACHE[cache_key] = {**payload, "_ts": time.time()}

        return payload
