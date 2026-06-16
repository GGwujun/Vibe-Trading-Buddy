"""Alpha factor cross-sectional signals for position analysis.

Computes a curated set of ~15 alpha factors on a peer universe and
extracts the target stock's cross-sectional percentile ranking.

Cache: peer panel + alpha results cached in memory for 5 minutes.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Curated alpha list — 15 factors across 7 themes
# ---------------------------------------------------------------------------

CURATED_ALPHAS: list[dict[str, str]] = [
    # momentum
    {"id": "alpha101_006",  "theme": "momentum",   "label": "动量-开盘价动量"},
    {"id": "alpha101_013",  "theme": "momentum",   "label": "动量-量价相关性"},
    {"id": "gtja191_010",   "theme": "momentum",   "label": "动量-收益加速度"},
    # reversal
    {"id": "alpha101_043",  "theme": "reversal",    "label": "反转-短期反转"},
    {"id": "qlib158_roc20", "theme": "reversal",    "label": "反转-20日变动率"},
    # volatility
    {"id": "alpha101_061",  "theme": "volatility",  "label": "波动-波动率偏度"},
    {"id": "gtja191_078",   "theme": "volatility",  "label": "波动-振幅异常"},
    # volume / liquidity
    {"id": "alpha101_050",  "theme": "volume",      "label": "量价-成交量比率"},
    {"id": "gtja191_140",   "theme": "volume",      "label": "量价-换手率信号"},
    {"id": "qlib158_std60", "theme": "volume",      "label": "量价-60日波动"},
    # quality
    {"id": "academic_rmw",  "theme": "quality",     "label": "质量-盈利因子"},
    {"id": "alpha101_044",  "theme": "quality",     "label": "质量-盈利稳定性"},
    # size
    {"id": "academic_smb",  "theme": "size",        "label": "规模-SMB因子"},
    # value
    {"id": "academic_hml",  "theme": "value",       "label": "价值-HML因子"},
    # trend / MA
    {"id": "qlib158_ma5",   "theme": "trend",       "label": "趋势-5日均线"},
]


def _theme_emoji(theme: str) -> str:
    return {
        "momentum": "🚀", "reversal": "🔄", "volatility": "🌊",
        "volume": "📊", "quality": "💎", "size": "📏",
        "value": "💰", "trend": "📈",
    }.get(theme, "📌")


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_SIGNALS_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 300  # 5 min


# ---------------------------------------------------------------------------
# Peer universe
# ---------------------------------------------------------------------------

def _get_peer_codes(code: str, min_peers: int = 10, max_peers: int = 50) -> list[str]:
    """Return peer stock codes (same industry) for cross-sectional comparison.

    Uses mootdx block/industry data to find peer stocks. Falls back to
    a broad CSI 300 blue-chip list if industry peers are too few.
    """
    raw = code.replace(".SZ", "").replace(".SH", "")

    peers: list[str] = []
    try:
        from src.data.mootdx_helper import get_quotes
        client = get_quotes(timeout=10)
        blocks = client.block()
        if blocks and isinstance(blocks, dict):
            for bk_name, bk_data in blocks.items():
                if isinstance(bk_data, dict) and "code" in bk_data:
                    codes = str(bk_data["code"]).split(",")
                    if raw in codes:
                        # Found industry block — collect peers
                        for c in codes:
                            c = c.strip()
                            if c and len(c) == 6 and c != raw:
                                prefix = c[0]
                                suffix = ".SH" if prefix in ("6", "5", "9") else ".SZ"
                                peers.append(c + suffix)
                        break
            # If exact block not found, collect from closely related blocks
            if not peers:
                raw_prefix = raw[:3]
                for bk_name, bk_data in blocks.items():
                    if isinstance(bk_data, dict) and "code" in bk_data:
                        codes = str(bk_data["code"]).split(",")
                        if raw in codes:
                            continue  # already tried
                        # Collect from blocks that share similar stocks
                        for c in codes:
                            c = c.strip()
                            if c and len(c) == 6 and c[:3] == raw_prefix and c != raw:
                                prefix = c[0]
                                suffix = ".SH" if prefix in ("6", "5", "9") else ".SZ"
                                peers.append(c + suffix)
    except Exception:
        logger.debug("Peer lookup via mootdx failed", exc_info=True)

    # Deduplicate and limit
    peers = list(dict.fromkeys(peers))[:max_peers]

    # Fallback: use hardcoded broad market peers if too few found
    if len(peers) < min_peers:
        fallback = [
            "000001.SZ", "000002.SZ", "000063.SZ", "000333.SZ", "000651.SZ",
            "000858.SZ", "002415.SZ", "002475.SZ", "002594.SZ", "300059.SZ",
            "300124.SZ", "300750.SZ", "600000.SH", "600036.SH", "600276.SH",
            "600519.SH", "600585.SH", "600887.SH", "601012.SH", "601088.SH",
            "601166.SH", "601318.SH", "601398.SH", "601857.SH", "603259.SH",
            "000568.SZ", "000725.SZ", "002142.SZ", "600030.SH", "600050.SH",
            "600104.SH", "600309.SH", "600406.SH", "600438.SH", "600690.SH",
            "600809.SH", "600900.SH", "601166.SH", "601328.SH", "601668.SH",
            "601688.SH", "601818.SH", "603288.SH", "688981.SH",
        ]
        for c in fallback:
            if c != code and c not in peers:
                peers.append(c)
        peers = peers[:max_peers]

    return peers


# ---------------------------------------------------------------------------
# Panel loading
# ---------------------------------------------------------------------------

def _load_peer_panel(codes: list[str], days: int = 90) -> dict[str, pd.DataFrame]:
    """Load wide OHLCV panel for a list of stock codes.

    Returns panel dict: {col_name: DataFrame(index=date, columns=code)}.
    """
    from src.data.ohlcv_cache import fetch_batch

    data = fetch_batch(codes, days=days)
    if not data:
        return {}

    # Pivot per-stock DataFrames into wide panel per column
    columns = ["open", "high", "low", "close", "volume"]
    panel: dict[str, pd.DataFrame] = {}
    for col in columns:
        frames = []
        for code, df in data.items():
            if col in df.columns:
                s = df[col].copy()
                s.name = code
                frames.append(s)
        if frames:
            panel[col] = pd.concat(frames, axis=1).sort_index()

    return panel


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def compute_alpha_signals(code: str) -> dict[str, Any]:
    """Compute curated alpha factor signals for a single stock.

    Returns:
        dict with keys:
        - peer_count: number of peer stocks used
        - signals: list of {id, theme, label, emoji, rank_pct, z_score, direction, status}
        - top_bullish: list of top 3 strongest alpha signals
        - top_bearish: list of top 3 weakest alpha signals
        - score: composite alpha score (0-1)
        - error: error message if computation failed (str or None)

    Signals where the alpha couldn't be computed have status='skipped'.
    """
    cache_key = code
    now = time.time()
    with _CACHE_LOCK:
        cached = _SIGNALS_CACHE.get(cache_key)
        if cached and (now - cached.get("_ts", 0)) < _CACHE_TTL:
            return {k: v for k, v in cached.items() if k != "_ts"}

    result: dict[str, Any] = {"signals": [], "top_bullish": [], "top_bearish": [],
                                "peer_count": 0, "score": 0.50, "error": None}

    # 1. Get peer codes
    peers = _get_peer_codes(code)
    if not peers:
        result["error"] = "无 peer 可比股票"
        return result

    # 2. Load panel
    all_codes = [code] + peers
    panel = _load_peer_panel(all_codes, days=90)
    if not panel or "close" not in panel or panel["close"].empty:
        result["error"] = "无法加载行情面板数据"
        return result

    # Verify target stock is in the panel
    if code not in panel["close"].columns:
        result["error"] = f"{code} 不在面板中"
        return result
    result["peer_count"] = len(peers)

    # 3. Compute each alpha
    from src.factors.registry import Registry
    registry = Registry()
    registry._scan()

    signals: list[dict] = []
    successes = 0

    for spec in CURATED_ALPHAS:
        alpha_id = spec["id"]
        entry: dict = {
            "id": alpha_id, "theme": spec["theme"], "label": spec["label"],
            "emoji": _theme_emoji(spec["theme"]),
            "rank_pct": 0.5, "z_score": 0.0, "direction": "neutral", "status": "skipped",
        }
        try:
            raw = registry.compute(alpha_id, panel)
            if raw is None or raw.empty:
                signals.append(entry)
                continue

            # Extract target stock's most recent value
            if code not in raw.columns:
                signals.append(entry)
                continue

            series = raw[code].dropna()
            if len(series) < 2:
                signals.append(entry)
                continue

            # Cross-sectional ranking: where does this stock rank vs peers?
            last_row = raw.iloc[-1].dropna()
            if len(last_row) < 3:
                signals.append(entry)
                continue

            stock_val = float(last_row.get(code, np.nan))
            if np.isnan(stock_val) or np.isinf(stock_val):
                signals.append(entry)
                continue

            all_vals = last_row.values.astype(float)
            all_vals = all_vals[np.isfinite(all_vals)]

            if len(all_vals) < 3:
                signals.append(entry)
                continue

            rank_pct = float((all_vals < stock_val).sum()) / max(1, len(all_vals) - 1)
            rank_pct = max(0.01, min(0.99, rank_pct))

            # Z-score within peers
            mean = float(np.mean(all_vals))
            std = float(np.std(all_vals))
            z_score = float((stock_val - mean) / std) if std > 0 else 0.0
            z_score = max(-3.0, min(3.0, z_score))

            # Direction
            if entry["theme"] == "reversal":
                # For reversal factors, high rank = bearish (reversal is fading)
                direction = "bearish" if rank_pct > 0.7 else "bullish" if rank_pct < 0.3 else "neutral"
            elif entry["theme"] == "volatility":
                # High volatility rank = bearish (more risk)
                direction = "bearish" if rank_pct > 0.7 else "bullish" if rank_pct < 0.3 else "neutral"
            else:
                # Most factors: high rank = bullish (strong signal)
                direction = "bullish" if rank_pct > 0.7 else "bearish" if rank_pct < 0.3 else "neutral"

            entry.update({
                "rank_pct": round(rank_pct, 3), "z_score": round(z_score, 2),
                "direction": direction, "status": "ok",
            })
            successes += 1
        except Exception:
            pass  # Factor couldn't be computed — keep status='skipped'
        signals.append(entry)

    result["signals"] = signals

    # 4. Composite alpha score
    ok_signals = [s for s in signals if s["status"] == "ok"]
    if ok_signals:
        # Convert rank_pct to a score: bullish factors (>0.5) increase score
        scores = []
        for s in ok_signals:
            if s["direction"] == "bullish":
                scores.append(s["rank_pct"])
            elif s["direction"] == "bearish":
                scores.append(1.0 - s["rank_pct"])
            else:
                scores.append(0.5)
        composite = float(np.mean(scores)) if scores else 0.5
        result["score"] = round(composite, 3)

    # 5. Top signals
    sorted_signals = sorted(ok_signals, key=lambda s: s["rank_pct"], reverse=True)
    result["top_bullish"] = sorted_signals[:3]
    result["top_bearish"] = sorted(sorted_signals, key=lambda s: s["rank_pct"])[:3]

    with _CACHE_LOCK:
        _SIGNALS_CACHE[cache_key] = {**result, "_ts": time.time()}

    return result
