"""Market dashboard aggregate route.

This route is intentionally a thin composition layer over existing API builders.
It gives the frontend one stable "AI dashboard" payload while preserving
per-source degradation when news, events, recommendations, or scans fail.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable

import pandas as pd
from fastapi import Depends, FastAPI, Request

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))

AuthDep = Callable[..., Awaitable[Any] | Any]


def _now_cst() -> datetime:
    return datetime.now(_CST)


def _today_cst() -> str:
    return _now_cst().strftime("%Y-%m-%d")


def _previous_weekday(date_str: str) -> str:
    cur = datetime.strptime(date_str, "%Y-%m-%d").date() - timedelta(days=1)
    while cur.weekday() >= 5:
        cur -= timedelta(days=1)
    return cur.strftime("%Y-%m-%d")


def _close_review_visible_trade_date() -> str:
    """Latest trading date whose close review is allowed to be shown."""
    today = _today_cst()
    now = _now_cst()
    try:
        from src.data.trade_calendar import cn_market_phase, is_trading_day, previous_trading_day

        if is_trading_day(today) and cn_market_phase(now) == "post_close":
            return today
        return previous_trading_day(today)
    except Exception:  # noqa: BLE001
        if now.weekday() < 5 and now.time() >= time(15, 0):
            return today
        return _previous_weekday(today)


def _stage_payload_with_freshness(stage: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    payload = dict(snapshot.get("payload") or {})
    payload.setdefault("trade_date", snapshot.get("trade_date"))
    payload.setdefault("source_policy", "db_only")
    payload["snapshot_updated_at"] = snapshot.get("updated_at")
    if stage != "morning-brief":
        return payload

    today = _today_cst()
    is_today_trading = False
    try:
        from src.data.trade_calendar import is_trading_day

        is_today_trading = is_trading_day(today)
    except Exception:  # noqa: BLE001
        is_today_trading = _now_cst().weekday() < 5
    payload["expected_trade_date"] = today if is_today_trading else snapshot.get("trade_date")
    payload["freshness_policy"] = "trading-day premarket snapshot; generated after 07:30 CST"
    payload["is_stale"] = bool(is_today_trading and snapshot.get("trade_date") != today)
    if payload["is_stale"]:
        missing = set(payload.get("missing_tables") or [])
        missing.add("market_stage_snapshot:today")
        payload["missing_tables"] = sorted(missing)
        payload["data_status"] = "stale"
        payload["stale_reason"] = f"早盘内参快照仍是 {snapshot.get('trade_date')}，今天 {today} 的盘前快照尚未同步。"
    return payload


def _load_recommendations(limit: int = 30) -> dict[str, Any]:
    from src.api.daily_recommendation_routes import _load_records, _with_performance

    today = _today_cst()
    records = [r for r in _load_records() if r.get("date") == today][:limit]
    return {"items": _with_performance(records), "date": today}


def _load_opportunities() -> dict[str, Any]:
    from src.api.opportunity_routes import _build_opportunities

    return _build_opportunities()


def _load_news(limit: int = 12) -> dict[str, Any]:
    from src.api.news_routes import _build_news_list

    payload = _build_news_list("")
    payload["articles"] = payload.get("articles", [])[:limit]
    return payload


def _load_tracking() -> dict[str, Any]:
    from src.data import schedule_store
    from src.data.tracking_watchlist_store import load_tracking_watchlist

    return {
        "watchlist": load_tracking_watchlist(),
        "tasks": schedule_store.load_tasks(),
    }


async def _load_events() -> dict[str, Any]:
    from src.api.events_routes import _build_events_payload

    return await _build_events_payload()


def _fetch_a_share_spot() -> pd.DataFrame:
    import akshare as ak

    if hasattr(ak, "stock_zh_a_spot_em"):
        return ak.stock_zh_a_spot_em()
    return ak.stock_zh_a_spot()


def _fetch_industry_spot() -> pd.DataFrame:
    import akshare as ak

    return ak.stock_board_industry_name_em()


def _fetch_index_spot() -> pd.DataFrame:
    import akshare as ak

    if hasattr(ak, "stock_zh_index_spot_em"):
        return ak.stock_zh_index_spot_em(symbol="沪深重要指数")
    return ak.stock_zh_index_spot()


def _column(df: pd.DataFrame, candidates: tuple[str, ...], fallback_index: int | None = None) -> str | None:
    for name in candidates:
        if name in df.columns:
            return name
    if fallback_index is not None and 0 <= fallback_index < len(df.columns):
        return str(df.columns[fallback_index])
    return None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str):
            value = value.replace("%", "").replace(",", "").strip()
            if not value or value in {"-", "--"}:
                return default
        return float(value)
    except Exception:
        return default


def _format_stock_rows(df: pd.DataFrame, limit: int = 8) -> list[dict[str, Any]]:
    code_col = _column(df, ("代码", "code", "symbol"), 1)
    name_col = _column(df, ("名称", "name"), 2)
    price_col = _column(df, ("最新价", "最新", "price", "trade"), 3)
    change_col = _column(df, ("涨跌幅", "change_pct", "changepercent"), 4)
    if not code_col or not name_col:
        return []
    rows: list[dict[str, Any]] = []
    for _, row in df.head(limit).iterrows():
        rows.append({
            "symbol": str(row.get(code_col, "")),
            "name": str(row.get(name_col, "")),
            "price": round(_number(row.get(price_col)) if price_col else 0, 2),
            "change_pct": round(_number(row.get(change_col)) if change_col else 0, 2),
        })
    return rows


def _build_breadth_from_spot(df: pd.DataFrame) -> dict[str, Any]:
    code_col = _column(df, ("代码", "code", "symbol"), 1)
    change_col = _column(df, ("涨跌幅", "change_pct", "changepercent"), 4)
    amount_col = _column(df, ("成交额", "amount", "turnover"), 6)
    price_col = _column(df, ("最新价", "最新", "price", "trade"), 3)

    working = df.copy()
    if code_col:
        working = working[working[code_col].astype(str).str.extract(r"(\d{6})", expand=False).notna()]
    if price_col:
        working = working[working[price_col].map(_number) > 0]

    changes = working[change_col].map(_number) if change_col else pd.Series(dtype=float)
    amount = working[amount_col].map(_number).sum() if amount_col else 0.0
    total = int(len(working))
    return {
        "total": total,
        "advancers": int((changes > 0).sum()),
        "decliners": int((changes < 0).sum()),
        "flat": int((changes == 0).sum()),
        "limit_up": int((changes >= 9.8).sum()),
        "limit_down": int((changes <= -9.8).sum()),
        "turnover_billion": round(amount / 100_000_000, 2),
    }


def _build_index_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    code_col = _column(df, ("代码", "code", "symbol"), 1)
    name_col = _column(df, ("名称", "name"), 2)
    price_col = _column(df, ("最新价", "最新", "price", "trade"), 3)
    change_col = _column(df, ("涨跌幅", "change_pct", "changepercent"), 4)
    if not code_col or not name_col:
        return []

    preferred_codes = ("000001", "399001", "399006", "000300", "000905", "000852", "000688", "899050")
    preferred_names = ("上证", "深证", "创业板", "沪深300", "中证500", "北证50")
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        code = str(row.get(code_col, ""))
        name = str(row.get(name_col, ""))
        if not any(key in code for key in preferred_codes) and not any(key in name for key in preferred_names):
            continue
        rows.append({
            "symbol": code,
            "name": name,
            "price": round(_number(row.get(price_col)) if price_col else 0, 2),
            "change_pct": round(_number(row.get(change_col)) if change_col else 0, 2),
        })
        if len(rows) >= 6:
            break
    return rows


def _build_sector_rows(df: pd.DataFrame, limit: int = 10) -> list[dict[str, Any]]:
    name_col = _column(df, ("板块名称", "名称", "name"), 1)
    change_col = _column(df, ("涨跌幅", "change_pct"), 5)
    up_col = _column(df, ("上涨家数", "advancers"), 8)
    down_col = _column(df, ("下跌家数", "decliners"), 9)
    leader_col = _column(df, ("领涨股票", "leader"), 10)
    if not name_col:
        return []
    working = df.copy()
    if change_col:
        working["_change"] = working[change_col].map(_number)
        working = working.sort_values("_change", ascending=False)
    rows: list[dict[str, Any]] = []
    for _, row in working.head(limit).iterrows():
        rows.append({
            "name": str(row.get(name_col, "")),
            "change_pct": round(_number(row.get(change_col)) if change_col else 0, 2),
            "advancers": int(_number(row.get(up_col))) if up_col else 0,
            "decliners": int(_number(row.get(down_col))) if down_col else 0,
            "leader": str(row.get(leader_col, "")) if leader_col else "",
        })
    return rows


def _load_market_overview() -> dict[str, Any]:
    spot = _fetch_a_share_spot()
    if spot is None or spot.empty:
        raise RuntimeError("A-share spot data unavailable")
    change_col = _column(spot, ("涨跌幅", "change_pct", "changepercent"), 4)
    if change_col:
        sorted_spot = spot.assign(_change=spot[change_col].map(_number)).sort_values("_change", ascending=False)
    else:
        sorted_spot = spot

    industry_rows: list[dict[str, Any]] = []
    try:
        industry = _fetch_industry_spot()
        if industry is not None and not industry.empty:
            industry_rows = _build_sector_rows(industry)
    except Exception as exc:  # noqa: BLE001 - overview should degrade by section
        logger.info("market overview: industry fetch failed: %s", exc)

    index_rows: list[dict[str, Any]] = []
    try:
        index_df = _fetch_index_spot()
        if index_df is not None and not index_df.empty:
            index_rows = _build_index_rows(index_df)
    except Exception as exc:  # noqa: BLE001
        logger.info("market overview: index fetch failed: %s", exc)

    return {
        "as_of": _now_cst().isoformat(),
        "breadth": _build_breadth_from_spot(spot),
        "indices": index_rows,
        "hot_sectors": industry_rows,
        "top_gainers": _format_stock_rows(sorted_spot, limit=8),
        "top_losers": _format_stock_rows(sorted_spot.sort_values("_change", ascending=True) if "_change" in sorted_spot else spot, limit=8),
    }


def _market_store():
    """Lazy accessor for the singleton MarketStore (SQLite pool/bars/capital)."""
    from src.data.market_store import get_market_store
    return get_market_store()


# Consecutive limit-up field names across tpdog/akshare variants.
_LIANBAN_KEYS = ("连板数", "连板", "连续涨停天数", "连续涨停", "lianban", "lbsd", "lbc")


_LIANBAN_KEYS = ("c_times", "连板数", "连板", "连续涨停天数", "连续涨停", "lianban", "lbsd", "lbc")


def _row_lianban(row: dict[str, Any]) -> int:
    """Best-effort extract of consecutive limit-up days (default 1 = first board)."""
    for key in _LIANBAN_KEYS:
        v = row.get(key)
        if v not in (None, "", "-"):
            try:
                n = int(float(str(v).replace("连板", "").strip()))
                return max(1, n)
            except (ValueError, TypeError):
                continue
    return 1


def _load_pools(trade_date: str) -> dict[str, Any] | None:
    """Real limit-up/down pools + limit-up ladder from the synced DB.

    Returns None when no pool has been synced for the day (pre-market / holiday /
    sync not yet run). Caller surfaces '盘后同步' hint instead of fake estimates.
    """
    store = _market_store()
    try:
        limit_up = store.get_pool("limitup", trade_date)
        limit_down = store.get_pool("limitdown", trade_date)
    except Exception as exc:  # noqa: BLE001
        logger.info("pools load failed: %s", exc)
        return None
    if not limit_up and not limit_down:
        return None

    # Ladder: group limit-up stocks by consecutive-board count, highest first.
    ladder_buckets: dict[int, list[dict[str, Any]]] = {}
    for row in limit_up:
        days = _row_lianban(row)
        ladder_buckets.setdefault(days, []).append({
            "symbol": str(row.get("code") or row.get("symbol") or ""),
            "name": str(row.get("name") or ""),
            "days": days,
        })
    ladder = [
        {"days": days, "count": len(stocks), "stocks": stocks}
        for days, stocks in sorted(ladder_buckets.items(), reverse=True)
    ]
    max_height = ladder[0]["days"] if ladder else 0

    def _fmt(rows: list[dict[str, Any]], limit: int = 30, *, with_days: bool = False) -> list[dict[str, Any]]:
        out = []
        for r in rows[:limit]:
            out.append({
                "symbol": str(r.get("code") or r.get("symbol") or ""),
                "name": str(r.get("name") or ""),
                "days": (_row_lianban(r) if with_days else 0),
            })
        return out

    return {
        "as_of": _now_cst().isoformat(),
        "trade_date": trade_date,
        "limit_up_count": len(limit_up),
        "limit_down_count": len(limit_down),
        "max_limit_up_height": max_height,
        "limit_up_list": _fmt(limit_up, with_days=True),
        "limit_down_list": _fmt(limit_down),
        "limitup_ladder": ladder,
    }


def _apply_real_limits(breadth: dict[str, Any], pools: dict[str, Any] | None) -> dict[str, Any]:
    """Override estimated ±9.8% limit counts with real pool counts when synced."""
    if not pools:
        return breadth
    breadth = dict(breadth)
    breadth["limit_up"] = pools.get("limit_up_count", breadth.get("limit_up", 0))
    breadth["limit_down"] = pools.get("limit_down_count", breadth.get("limit_down", 0))
    breadth["limit_up_real"] = True  # source = pool, not estimate
    return breadth


def _load_capital() -> dict[str, Any]:
    """Capital-flow evidence: sector TOP5 + stock in/out flow + north-bound."""
    from src.data.capital_flow import get_sector_capital, get_north_capital

    sector_top5 = []
    try:
        sector_top5 = [
            {"sector": r.get("sector", ""), "main_net": r.get("main_net", 0), "change_pct": r.get("change_pct", 0)}
            for r in (get_sector_capital() or [])[:5]
        ]
    except Exception as exc:  # noqa: BLE001
        logger.info("capital sector failed: %s", exc)

    north_recent = []
    try:
        north_recent = [
            {"date": r.get("date", ""), "net": r.get("net", 0)}
            for r in (get_north_capital(10) or [])[:10]
        ]
    except Exception as exc:  # noqa: BLE001
        logger.info("capital north failed: %s", exc)

    stock_inflow, stock_outflow = _stock_capital_rank()

    return {
        "as_of": _now_cst().isoformat(),
        "sector_top5": sector_top5,
        "stock_inflow_top": stock_inflow,
        "stock_outflow_top": stock_outflow,
        "north_recent": north_recent,
    }


def _stock_capital_rank(limit: int = 8) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Whole-market individual-stock fund-flow rank via akshare (one call)."""
    try:
        import akshare as ak
        df = ak.stock_individual_fund_flow_rank(indicator="今日")
    except Exception as exc:  # noqa: BLE001
        logger.info("stock capital rank failed: %s", exc)
        return [], []
    if df is None or df.empty:
        return [], []
    code_col = _column(df, ("代码", "code"), 1)
    name_col = _column(df, ("名称", "name"), 2)
    net_col = _column(df, ("今日主力净流入-净额", "主力净流入-净额", "main_net"), 3)
    pct_col = _column(df, ("今日涨跌幅", "涨跌幅", "change_pct"), 4)
    if not code_col or not net_col:
        return [], []
    df = df.copy()
    df["_net"] = df[net_col].map(_number)
    df["_pct"] = df[pct_col].map(_number) if pct_col else 0.0

    def _pick(row):
        return {
            "symbol": str(row.get(code_col, "")),
            "name": str(row.get(name_col, "")),
            "main_net": round(float(row.get("_net", 0.0)), 0),
            "change_pct": round(float(row.get("_pct", 0.0)), 2),
        }

    top_in = [_pick(r) for _, r in df.sort_values("_net", ascending=False).head(limit).iterrows()]
    top_out = [_pick(r) for _, r in df.sort_values("_net", ascending=True).head(limit).iterrows()]
    return top_in, top_out


def _load_themes() -> dict[str, Any]:
    """Concept sectors (treemap heatmap source) + derived main/observe lines."""
    try:
        import akshare as ak
        concept = ak.stock_board_concept_name_em()
    except Exception as exc:  # noqa: BLE001
        logger.info("themes concept failed: %s", exc)
        return {"as_of": _now_cst().isoformat(), "concept_sectors": [], "main_lines": [], "observe": []}
    rows = _build_sector_rows(concept, limit=40)
    # Main lines: top 3 by change_pct (clearly leading themes today).
    by_change = sorted(rows, key=lambda r: r.get("change_pct", 0), reverse=True)
    main_lines = [{"name": r["name"], "change_pct": r["change_pct"], "leader": r.get("leader", "")} for r in by_change[:3]]
    # Observe: middling change but active (advancers-decliners spread) — crude proxy.
    observe = []
    for r in by_change[3:13]:
        spread = r.get("advancers", 0) - r.get("decliners", 0)
        if -3 < r.get("change_pct", 0) < 3 and spread > 0:
            observe.append({"name": r["name"], "change_pct": r["change_pct"], "leader": r.get("leader", "")})
        if len(observe) >= 5:
            break
    return {
        "as_of": _now_cst().isoformat(),
        "concept_sectors": rows,
        "main_lines": main_lines,
        "observe": observe,
    }


def _load_multi_period(top_gainers: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    """Multi-period (1d/5d/20d/60d) cumulative return for today's top gainers.

    Uses DB daily bars to extend intraday gainers backward. Marked approx when
    a symbol's bars are missing/cold. Returns at most `limit` rows.
    """
    if not top_gainers:
        return []
    store = _market_store()
    out: list[dict[str, Any]] = []
    for g in top_gainers[:limit]:
        code = str(g.get("symbol", "")).split(".")[0]
        if not code or len(code) != 6:
            continue
        row = {
            "symbol": code,
            "name": g.get("name", ""),
            "d1": round(g.get("change_pct", 0.0), 2),
            "d5": None, "d20": None, "d60": None,
            "approx": True,
        }
        try:
            df = store.get_daily_bars(code)
        except Exception as exc:  # noqa: BLE001
            logger.debug("multi_period bars %s failed: %s", code, exc)
            df = None
        if df is not None and not df.empty and "close" in df.columns:
            closes = df["close"].astype(float).dropna().sort_index()
            n = len(closes)
            if n >= 2:
                last = float(closes.iloc[-1])
                def _pct(back: int) -> float | None:
                    if n <= back:
                        return None
                    base = float(closes.iloc[-1 - back])
                    return round((last - base) / base * 100, 2) if base else None
                row["d5"], row["d20"], row["d60"] = _pct(5), _pct(20), _pct(60)
                row["approx"] = False
        out.append(row)
    return out


def _compute_sentiment(
    market_overview: dict[str, Any],
    capital: dict[str, Any] | None,
    pools: dict[str, Any] | None,
) -> dict[str, Any]:
    """Explainable synthetic sentiment gauge (NOT a pro fear/greed index).

    Combines breadth (advancers ratio), limit-up heat (count + max height),
    and main-force capital sign into a 0-100 temperature, a -1..+1 directional
    breadth strength, and a qualitative stage. Every metric carries a one-line
    ``formula`` so the UI can explain how it was derived.
    """
    breadth = market_overview.get("breadth") or {}
    total = int(breadth.get("total") or 0)
    adv = int(breadth.get("advancers") or 0)
    dec = int(breadth.get("decliners") or 0)
    limit_up = int((pools or {}).get("limit_up_count") or breadth.get("limit_up") or 0)
    max_height = int((pools or {}).get("max_limit_up_height") or 0)

    adv_ratio = (adv / total) if total else 0.5
    breadth_strength = ((adv - dec) / total) if total else 0.0  # -1..+1

    # Temperature components (each 0..100):
    #   breadth heat: adv_ratio scaled; limit-up heat: sqrt-scaled count capped;
    #   capital heat: sign + magnitude of sector TOP5 main-force net flow.
    breadth_heat = adv_ratio * 100.0
    limit_heat = min(100.0, (limit_up ** 0.6) * 18.0)  # 5 涨停≈50, 20 涨停≈80
    sector_net = 0.0
    if capital and capital.get("sector_top5"):
        sector_net = sum(s.get("main_net", 0) for s in capital["sector_top5"]) / 1e8  # →亿
    # squash sector net (in 亿) into 0..100 around 0
    capital_heat = max(0.0, min(100.0, 50.0 + sector_net * 4.0))

    temperature = round(0.4 * breadth_heat + 0.3 * limit_heat + 0.3 * capital_heat, 1)
    temperature = max(0.0, min(100.0, temperature))

    if temperature >= 75:
        label = "过热"
    elif temperature >= 60:
        label = "热"
    elif temperature >= 40:
        label = "温和"
    elif temperature >= 25:
        label = "冷"
    else:
        label = "冰冷"

    # Stage: combines temperature + limit-up height + breadth direction.
    if temperature >= 70 and max_height >= 4:
        stage, stage_reason = "高潮", "情绪高温且连板高度≥4，投机情绪强烈"
    elif temperature >= 60 and breadth_strength > 0.2:
        stage, stage_reason = "升温", "涨多跌少且温度上行，赚钱效应扩散"
    elif temperature <= 25 and breadth_strength < -0.2:
        stage, stage_reason = "冰点", "普跌且温度极低，恐慌释放中"
    elif breadth_strength < 0 and temperature < 50:
        stage, stage_reason = "退潮", "涨跌转为跌多，高位情绪回落"
    elif 35 <= temperature <= 55 and abs(breadth_strength) < 0.15:
        stage, stage_reason = "回暖", "温度中性、多空均衡，观察方向选择"
    else:
        stage, stage_reason = "震荡", "信号混杂，无明显趋势"

    summary = (
        f"情绪 {temperature:.0f}/{label} · 方向广度 {breadth_strength:+.2f} · "
        f"涨停 {limit_up} · 连板高度 {max_height} 板"
    )

    return {
        "as_of": _now_cst().isoformat(),
        "temperature": temperature,
        "label": label,
        "breadth_strength": round(breadth_strength, 3),
        "adv_dec_diff": adv - dec,
        "stage": stage,
        "stage_reason": stage_reason,
        "summary": summary,
        "formulas": {
            "temperature": "0.4×涨跌家数比 + 0.3×涨停热度 + 0.3×行业主力资金（合成，非专业恐贪指数）",
            "breadth_strength": "(上涨家数-下跌家数)/总数，-1..+1",
            "stage": "温度+连板高度+涨跌方向组合判定",
        },
    }


def _market_environment(market_overview: dict[str, Any], sentiment: dict[str, Any] | None) -> dict[str, Any]:
    """One-line market regime label (放量普涨/缩量分化/恐慌杀跌...) + turnover."""
    breadth = market_overview.get("breadth") or {}
    total = int(breadth.get("total") or 0)
    adv = int(breadth.get("advancers") or 0)
    dec = int(breadth.get("decliners") or 0)
    turnover = float(breadth.get("turnover_billion") or 0)
    adv_ratio = (adv / total) if total else 0.5
    strength = (sentiment or {}).get("breadth_strength", 0.0)

    if adv_ratio > 0.75 and turnover > 8000:
        regime = "放量普涨"
    elif adv_ratio > 0.7:
        regime = "普涨"
    elif adv_ratio < 0.25 and turnover > 8000:
        regime = "恐慌杀跌"
    elif adv_ratio < 0.3:
        regime = "普跌"
    elif abs(strength) < 0.15 and adv_ratio > 0.45:
        regime = "缩量分化"
    else:
        regime = "震荡"
    return {
        "regime": regime,
        "turnover_billion": round(turnover, 1),
        "adv_ratio": round(adv_ratio, 3),
    }


async def _run_source(name: str, loader: Callable[[], Any]) -> tuple[str, Any, str | None]:
    try:
        result = loader()
        if asyncio.iscoroutine(result):
            result = await result
        return name, result, None
    except Exception as exc:  # noqa: BLE001 - dashboard must degrade per source
        logger.warning("market dashboard source failed: %s: %s", name, exc)
        return name, None, str(exc)[:200]


def _market_mood(recommendations: list[dict[str, Any]], opportunities: list[dict[str, Any]]) -> dict[str, str]:
    if not recommendations and not opportunities:
        return {
            "label": "等待数据",
            "tone": "neutral",
            "detail": "推荐和机会池尚未返回，先检查数据源状态。",
        }

    scores = []
    for item in recommendations:
        base = float(item.get("score", 0.5) or 0.5)
        ai = float((item.get("ai_review") or {}).get("score", base) or base)
        factor = float((item.get("factor_review") or {}).get("score", base) or base)
        scores.append(base * 0.5 + ai * 0.3 + factor * 0.2)
    avg_score = sum(scores) / len(scores) if scores else 0
    hot_count = sum(1 for item in opportunities if float(item.get("change_pct", 0) or 0) >= 5)
    weak_count = sum(1 for item in opportunities if float(item.get("change_pct", 0) or 0) < 0)

    if avg_score >= 0.72 or hot_count >= 4:
        return {"label": "进攻观察", "tone": "good", "detail": "高分候选较多，盘中适合重点跟踪量价确认。"}
    if weak_count > hot_count and len(recommendations) < 3:
        return {"label": "防守优先", "tone": "bad", "detail": "有效候选偏少，优先控制仓位和等待确认。"}
    return {"label": "均衡观察", "tone": "warn", "detail": "有候选但强度未压倒性，适合分层跟踪。"}


def _flatten_opportunities(categories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for category in categories:
        for raw in category.get("opportunities", []) or []:
            item = dict(raw)
            item["category_id"] = category.get("id", "")
            item["category_label"] = category.get("label", "")
            items.append(item)
    return items


def _score_recommendation(item: dict[str, Any]) -> float:
    base = float(item.get("score", 0.5) or 0.5)
    ai = float((item.get("ai_review") or {}).get("score", base) or base)
    factor = float((item.get("factor_review") or {}).get("score", base) or base)
    return max(0.01, min(0.99, base * 0.5 + ai * 0.3 + factor * 0.2))


def _tail_action(score: float, change_pct: float, risk_text: str = "") -> tuple[str, str]:
    risk_lower = risk_text.lower()
    if change_pct >= 7:
        return "等回落", "涨幅偏高，尾盘不追价，等待回踩或次日确认。"
    if score >= 0.72 and change_pct >= 0:
        return "重点跟踪", "强度和当日表现较好，尾盘重点观察承接与收盘位置。"
    if score >= 0.58:
        return "轻仓观察", "信号有效但强度一般，适合小仓位观察或加入明日清单。"
    if "risk" in risk_lower or "风险" in risk_text:
        return "风险规避", "风险提示较强，尾盘先规避，等待新证据。"
    return "等待确认", "信号不足以支持尾盘动作，继续观察量价和事件变化。"


def _build_tail_decisions(
    recommendations: list[dict[str, Any]],
    opportunities: list[dict[str, Any]],
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Build a dedicated tail-session decision list from existing evidence."""

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    tail_recs = [item for item in recommendations if item.get("slot") == "afternoon"]
    if not tail_recs:
        tail_recs = [item for item in recommendations if item.get("slot") == "morning"]

    for item in sorted(tail_recs, key=_score_recommendation, reverse=True):
        symbol = str(item.get("symbol", "")).upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        score = _score_recommendation(item)
        change_pct = float(item.get("change_pct_at_pick", 0) or 0)
        action, rationale = _tail_action(score, change_pct, str(item.get("risk_note", "")))
        rows.append({
            "symbol": symbol,
            "name": item.get("name", symbol),
            "action": action,
            "score": round(score, 3),
            "price": item.get("price_at_pick"),
            "change_pct": change_pct,
            "reason": item.get("reason") or (item.get("ai_review") or {}).get("summary") or rationale,
            "risk_note": item.get("risk_note") or (item.get("ai_review") or {}).get("risk") or rationale,
            "source": "daily_recommendation",
            "source_label": item.get("slot_label") or item.get("slot") or "recommendation",
        })
        if len(rows) >= limit:
            return rows

    for item in sorted(opportunities, key=lambda raw: float(raw.get("confidence", 0) or 0), reverse=True):
        symbol = str(item.get("symbol", "")).upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        score = float(item.get("confidence", 0.5) or 0.5)
        change_pct = float(item.get("change_pct", 0) or 0)
        action, rationale = _tail_action(score, change_pct, str(item.get("reason", "")))
        rows.append({
            "symbol": symbol,
            "name": item.get("name", symbol),
            "action": action,
            "score": round(max(0.01, min(0.99, score)), 3),
            "price": item.get("price"),
            "change_pct": change_pct,
            "reason": item.get("reason") or rationale,
            "risk_note": rationale,
            "source": "opportunity",
            "source_label": item.get("category_label") or item.get("category_id") or "opportunity",
        })
        if len(rows) >= limit:
            break

    return rows


def _top_events(events: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for category in events:
        for event in category.get("events", []) or []:
            item = dict(event)
            item["category_label"] = category.get("label", "")
            rows.append(item)
    return rows[:limit]


def _morning_brief(
    market_overview: dict[str, Any],
    recommendations: list[dict[str, Any]],
    news: list[dict[str, Any]],
    events: list[dict[str, Any]],
    opportunities: list[dict[str, Any]],
) -> dict[str, Any]:
    breadth = market_overview.get("breadth") or {}
    morning_recs = [item for item in recommendations if item.get("slot") == "morning"]
    focus = morning_recs[:5] or sorted(opportunities, key=lambda item: float(item.get("confidence", 0) or 0), reverse=True)[:5]
    advancers = int(breadth.get("advancers", 0) or 0)
    decliners = int(breadth.get("decliners", 0) or 0)
    risk_note = "市场广度偏强，可重点观察高置信候选。"
    if decliners > advancers:
        risk_note = "下跌家数多于上涨家数，早盘先控制仓位，等待确认。"

    return {
        "title": "早盘内参",
        "as_of": market_overview.get("as_of") or _now_cst().isoformat(),
        "market_breadth": breadth,
        "indices": market_overview.get("indices", []),
        "top_news": news[:5],
        "key_events": _top_events(events, limit=5),
        "focus_symbols": [
            {
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "reason": item.get("reason") or item.get("category_label", ""),
                "score": round(_score_recommendation(item), 3) if "slot" in item else round(float(item.get("confidence", 0.5) or 0.5), 3),
            }
            for item in focus
        ],
        "risk_note": risk_note,
    }


def _intraday_monitor(
    market_overview: dict[str, Any],
    opportunities: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    breadth = market_overview.get("breadth") or {}
    hot_sectors = market_overview.get("hot_sectors", [])[:8]
    alerts: list[dict[str, Any]] = []
    for item in opportunities[:8]:
        change = float(item.get("change_pct", 0) or 0)
        confidence = float(item.get("confidence", 0) or 0)
        if change >= 6:
            level = "hot"
            message = "涨幅偏高，关注冲高回落风险。"
        elif confidence >= 0.75:
            level = "focus"
            message = "信号强度较高，等待量价确认。"
        else:
            level = "watch"
            message = "纳入观察池。"
        alerts.append({
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "level": level,
            "change_pct": round(change, 2),
            "message": message,
        })

    return {
        "title": "盘中监控",
        "breadth": breadth,
        "hot_sectors": hot_sectors,
        "alerts": alerts,
        "scheduled_tasks": [
            {
                "symbol": task.get("symbol"),
                "name": task.get("name"),
                "time": task.get("time"),
                "enabled": bool(task.get("enabled")),
                "last_status": task.get("last_status"),
            }
            for task in tasks[:20]
        ],
    }


def _tail_strategy(tail_decisions: list[dict[str, Any]], market_overview: dict[str, Any]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in tail_decisions:
        grouped.setdefault(str(item.get("action", "等待确认")), []).append(item)
    return {
        "title": "尾盘策略",
        "decisions": tail_decisions,
        "groups": grouped,
        "rules": [
            "涨幅过高的候选不追价，优先等回落或次日确认。",
            "重点跟踪标的只在收盘承接良好时保留到明日观察。",
            "市场广度走弱时降低尾盘动作优先级。",
        ],
        "breadth": market_overview.get("breadth") or {},
    }


def _close_review(
    recommendations: list[dict[str, Any]],
    market_overview: dict[str, Any],
    tail_decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    returns = [
        float((item.get("performance") or {}).get("latest_return_pct"))
        for item in recommendations
        if (item.get("performance") or {}).get("latest_return_pct") is not None
    ]
    avg_return = round(sum(returns) / len(returns), 2) if returns else None
    win_rate = round(sum(1 for value in returns if value > 0) / len(returns) * 100, 1) if returns else None
    tomorrow_watch = [
        {
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "action": item.get("action"),
            "reason": item.get("reason"),
        }
        for item in tail_decisions[:8]
    ]
    return {
        "title": "收盘复盘",
        "summary": {
            "recommendation_count": len(recommendations),
            "tracked_return_count": len(returns),
            "avg_latest_return_pct": avg_return,
            "win_rate": win_rate,
        },
        "breadth": market_overview.get("breadth") or {},
        "tomorrow_watch": tomorrow_watch,
        "review_questions": [
            "今日推荐是否跑赢市场广度？",
            "尾盘强势股是否有真实承接，而不是单纯拉高？",
            "明日优先观察哪些事件和板块扩散？",
        ],
    }


def _project_suffix(code: str) -> str:
    digits = str(code or "").split(".", 1)[0]
    if str(code).upper().endswith(".SH") or digits.startswith(("5", "6", "9")):
        return ".SH"
    if str(code).upper().endswith(".BJ") or digits.startswith(("4", "8")):
        return ".BJ"
    return ".SZ"


def _clean_stock_rows(df: pd.DataFrame, limit: int = 8) -> list[dict[str, Any]]:
    code_col = _column(df, ("代码", "code", "symbol"), 1)
    name_col = _column(df, ("名称", "name"), 2)
    price_col = _column(df, ("最新价", "price", "trade"), 3)
    change_col = _column(df, ("涨跌幅", "change_pct", "changepercent"), 4)
    if not code_col or not name_col:
        return []
    rows: list[dict[str, Any]] = []
    for _, row in df.head(limit).iterrows():
        code = str(row.get(code_col, "")).strip()
        rows.append({
            "symbol": code,
            "name": str(row.get(name_col, "")).strip() or code,
            "price": round(_number(row.get(price_col)) if price_col else 0, 2),
            "change_pct": round(_number(row.get(change_col)) if change_col else 0, 2),
        })
    return rows


def _clean_breadth_from_spot(df: pd.DataFrame) -> dict[str, Any]:
    code_col = _column(df, ("代码", "code", "symbol"), 1)
    change_col = _column(df, ("涨跌幅", "change_pct", "changepercent"), 4)
    amount_col = _column(df, ("成交额", "amount", "turnover"), 6)
    price_col = _column(df, ("最新价", "price", "trade"), 3)
    working = df.copy()
    if code_col:
        working = working[working[code_col].astype(str).str.extract(r"(\d{6})", expand=False).notna()]
    if price_col:
        working = working[working[price_col].map(_number) > 0]
    changes = working[change_col].map(_number) if change_col else pd.Series(dtype=float)
    amount = working[amount_col].map(_number).sum() if amount_col else 0.0
    return {
        "total": int(len(working)),
        "advancers": int((changes > 0).sum()),
        "decliners": int((changes < 0).sum()),
        "flat": int((changes == 0).sum()),
        "limit_up": int((changes >= 9.8).sum()),
        "limit_down": int((changes <= -9.8).sum()),
        "turnover_billion": round(amount / 100_000_000, 2),
    }


def _clean_index_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    code_col = _column(df, ("代码", "code", "symbol"), 1)
    name_col = _column(df, ("名称", "name"), 2)
    price_col = _column(df, ("最新价", "price", "trade"), 3)
    change_col = _column(df, ("涨跌幅", "change_pct", "changepercent"), 4)
    if not code_col or not name_col:
        return []
    preferred_codes = ("000001", "399001", "399006", "000300", "000905", "000852", "000688")
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        code = str(row.get(code_col, "")).strip()
        name = str(row.get(name_col, "")).strip()
        if not any(key in code for key in preferred_codes):
            continue
        rows.append({
            "symbol": code,
            "name": name or code,
            "price": round(_number(row.get(price_col)) if price_col else 0, 2),
            "change_pct": round(_number(row.get(change_col)) if change_col else 0, 2),
        })
        if len(rows) >= 7:
            break
    return rows


def _clean_sector_rows(df: pd.DataFrame, limit: int = 10) -> list[dict[str, Any]]:
    name_col = _column(df, ("板块名称", "名称", "name"), 1)
    change_col = _column(df, ("涨跌幅", "change_pct"), 5)
    up_col = _column(df, ("上涨家数", "advancers"), 8)
    down_col = _column(df, ("下跌家数", "decliners"), 9)
    leader_col = _column(df, ("领涨股票", "leader"), 10)
    if not name_col:
        return []
    working = df.copy()
    if change_col:
        working["_change"] = working[change_col].map(_number)
        working = working.sort_values("_change", ascending=False)
    rows: list[dict[str, Any]] = []
    for _, row in working.head(limit).iterrows():
        rows.append({
            "name": str(row.get(name_col, "")).strip(),
            "change_pct": round(_number(row.get(change_col)) if change_col else 0, 2),
            "advancers": int(_number(row.get(up_col))) if up_col else 0,
            "decliners": int(_number(row.get(down_col))) if down_col else 0,
            "leader": str(row.get(leader_col, "")).strip() if leader_col else "",
        })
    return rows


def _db_market_overview() -> dict[str, Any]:
    store = _market_store()
    if store is None:
        raise RuntimeError("market store unavailable")
    conn = store._conn

    trade_row = conn.execute("SELECT MAX(trade_date) AS d FROM bars_daily").fetchone()
    trade_date = trade_row["d"] if trade_row and trade_row["d"] else None
    if not trade_date:
        raise RuntimeError("local bars_daily is empty")

    rows = conn.execute(
        """
        SELECT b.code, b.close, b.rise_rate, b.total_amt, COALESCE(s.name, b.name, b.code) AS name
        FROM bars_daily b
        LEFT JOIN security_master s ON s.code = b.code
        WHERE b.trade_date = ?
        """,
        (trade_date,),
    ).fetchall()
    changes = [float(r["rise_rate"] or 0) for r in rows]
    turnover = sum(float(r["total_amt"] or 0) for r in rows)
    top = sorted(rows, key=lambda r: float(r["rise_rate"] or 0), reverse=True)
    bottom = sorted(rows, key=lambda r: float(r["rise_rate"] or 0))

    def _stock(row: Any) -> dict[str, Any]:
        return {
            "symbol": row["code"],
            "name": row["name"] or row["code"],
            "price": round(float(row["close"] or 0), 2),
            "change_pct": round(float(row["rise_rate"] or 0), 2),
        }

    index_rows: list[dict[str, Any]] = []
    name_map = {
        "000001.SH": "上证指数",
        "399001.SZ": "深证成指",
        "399006.SZ": "创业板指",
        "000300.SH": "沪深300",
        "000905.SH": "中证500",
        "000852.SH": "中证1000",
        "000688.SH": "科创50",
        "899050.BJ": "北证50",
    }
    for code in name_map:
        r = conn.execute(
            """
            SELECT code, close, pct_chg, trade_date
            FROM index_daily
            WHERE code = ?
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            (code,),
        ).fetchone()
        if not r:
            continue
        index_rows.append({
            "symbol": r["code"],
            "name": name_map.get(r["code"], r["code"]),
            "price": round(float(r["close"] or 0), 2),
            "change_pct": round(float(r["pct_chg"] or 0), 2),
            "trade_date": r["trade_date"],
        })

    overview = {
        "as_of": _now_cst().isoformat(),
        "source": "market_db",
        "trade_date": trade_date,
        "breadth": {
            "total": len(rows),
            "advancers": sum(1 for v in changes if v > 0),
            "decliners": sum(1 for v in changes if v < 0),
            "flat": sum(1 for v in changes if v == 0),
            "limit_up": sum(1 for v in changes if v >= 9.8),
            "limit_down": sum(1 for v in changes if v <= -9.8),
            "turnover_billion": round(turnover / 100_000_000, 2),
        },
        "indices": index_rows,
        "hot_sectors": [],
        "top_gainers": [_stock(r) for r in top[:8]],
        "top_losers": [_stock(r) for r in bottom[:8]],
    }
    try:
        snapshot = _latest_breadth_snapshot(store, trade_date)
        if snapshot:
            overview = _apply_breadth_snapshot(overview, snapshot)
    except Exception as exc:  # noqa: BLE001 - dashboard should keep the bars fallback
        logger.info("market overview: breadth snapshot unavailable: %s", exc)
    return overview


def _load_market_overview() -> dict[str, Any]:
    overview = _db_market_overview()
    live_indices = _live_index_rows()
    if live_indices:
        overview = dict(overview)
        overview["indices"] = live_indices
        overview["index_source"] = "akshare.index_spot"
        overview["as_of"] = _now_cst().isoformat()
    return overview


def _latest_breadth_snapshot(store: Any, fallback_trade_date: str | None) -> dict[str, Any] | None:
    dates: list[str] = []
    for trade_date in (_today_cst(), fallback_trade_date):
        if trade_date and trade_date not in dates:
            dates.append(trade_date)
    for trade_date in dates:
        snapshot = store.get_market_breadth_snapshot(trade_date)
        if snapshot:
            return snapshot
    return None


def _apply_breadth_snapshot(overview: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    breadth = dict(overview.get("breadth") or {})
    for key in ("total", "advancers", "decliners", "limit_up", "limit_down", "max_limit_up_height"):
        if snapshot.get(key) is not None:
            breadth[key] = int(snapshot.get(key) or 0)
    if snapshot.get("unchanged") is not None:
        breadth["flat"] = int(snapshot.get("unchanged") or 0)
    if snapshot.get("turnover_billion") is not None:
        breadth["turnover_billion"] = round(float(snapshot.get("turnover_billion") or 0), 2)

    updated = dict(overview)
    updated["breadth"] = breadth
    updated["trade_date"] = snapshot.get("trade_date") or overview.get("trade_date")
    updated["breadth_source"] = snapshot.get("source") or "market_breadth_snapshot"
    updated["breadth_updated_at"] = snapshot.get("updated_at")
    return updated


_INDEX_SYMBOL_MAP = {
    "000001": "000001.SH",
    "399001": "399001.SZ",
    "399006": "399006.SZ",
    "000300": "000300.SH",
    "000905": "000905.SH",
    "000852": "000852.SH",
    "000688": "000688.SH",
    "899050": "899050.BJ",
}


def _normalize_index_symbol(symbol: Any) -> str:
    raw = str(symbol or "").strip().upper()
    if raw in _INDEX_SYMBOL_MAP.values():
        return raw
    digits = "".join(ch for ch in raw if ch.isdigit())
    code = digits[-6:] if len(digits) >= 6 else raw
    return _INDEX_SYMBOL_MAP.get(code, raw)


def _live_index_rows() -> list[dict[str, Any]]:
    try:
        index_df = _fetch_index_spot()
        if index_df is None or index_df.empty:
            return []
        rows = _build_index_rows(index_df)
    except Exception as exc:  # noqa: BLE001
        logger.info("market overview: live index fetch failed: %s", exc)
        return []

    trade_date = _today_cst()
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        symbol = _normalize_index_symbol(row.get("symbol"))
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        normalized.append({
            **row,
            "symbol": symbol,
            "trade_date": trade_date,
            "source": "akshare.index_spot",
        })
    return normalized


def _compute_sentiment(
    market_overview: dict[str, Any],
    capital: dict[str, Any] | None,
    pools: dict[str, Any] | None,
) -> dict[str, Any]:
    breadth = market_overview.get("breadth") or {}
    total = int(breadth.get("total") or 0)
    adv = int(breadth.get("advancers") or 0)
    dec = int(breadth.get("decliners") or 0)
    limit_up = int((pools or {}).get("limit_up_count") or breadth.get("limit_up") or 0)
    max_height = int((pools or {}).get("max_limit_up_height") or 0)
    adv_ratio = adv / total if total else 0.5
    breadth_strength = (adv - dec) / total if total else 0.0
    breadth_heat = adv_ratio * 100.0
    limit_heat = min(100.0, (limit_up ** 0.6) * 18.0)
    sector_net = 0.0
    if capital and capital.get("sector_top5"):
        sector_net = sum(s.get("main_net", 0) for s in capital["sector_top5"]) / 1e8
    capital_heat = max(0.0, min(100.0, 50.0 + sector_net * 4.0))
    temperature = round(max(0.0, min(100.0, 0.4 * breadth_heat + 0.3 * limit_heat + 0.3 * capital_heat)), 1)
    if temperature >= 75:
        label = "过热"
    elif temperature >= 60:
        label = "偏热"
    elif temperature >= 40:
        label = "温和"
    elif temperature >= 25:
        label = "偏冷"
    else:
        label = "冰冷"
    if temperature >= 70 and max_height >= 4:
        stage, stage_reason = "高潮", "情绪高温且连板高度较高，投机情绪强"
    elif temperature >= 60 and breadth_strength > 0.2:
        stage, stage_reason = "升温", "涨多跌少且温度上行，赚钱效应扩散"
    elif temperature <= 25 and breadth_strength < -0.2:
        stage, stage_reason = "冰点", "普跌且温度极低，恐慌释放中"
    elif breadth_strength < 0 and temperature < 50:
        stage, stage_reason = "退潮", "跌多涨少，高位情绪回落"
    elif 35 <= temperature <= 55 and abs(breadth_strength) < 0.15:
        stage, stage_reason = "震荡", "温度中性、多空均衡，等待方向选择"
    else:
        stage, stage_reason = "回暖", "信号边际改善，适合观察主线承接"
    return {
        "as_of": _now_cst().isoformat(),
        "temperature": temperature,
        "label": label,
        "breadth_strength": round(breadth_strength, 3),
        "adv_dec_diff": adv - dec,
        "stage": stage,
        "stage_reason": stage_reason,
        "summary": f"情绪 {temperature:.0f}/{label}，涨跌广度 {breadth_strength:+.2f}，涨停 {limit_up}，连板高度 {max_height}",
        "formulas": {
            "temperature": "0.4*涨跌家数比 + 0.3*涨停热度 + 0.3*行业主力资金",
            "breadth_strength": "(上涨家数-下跌家数)/总数，范围 -1 到 +1",
            "stage": "温度、连板高度、涨跌方向组合判定",
        },
    }


def _market_environment(market_overview: dict[str, Any], sentiment: dict[str, Any] | None) -> dict[str, Any]:
    breadth = market_overview.get("breadth") or {}
    total = int(breadth.get("total") or 0)
    adv = int(breadth.get("advancers") or 0)
    turnover = float(breadth.get("turnover_billion") or 0)
    adv_ratio = adv / total if total else 0.5
    strength = float((sentiment or {}).get("breadth_strength", 0.0) or 0.0)
    if adv_ratio > 0.75 and turnover > 8000:
        regime = "放量普涨"
    elif adv_ratio > 0.7:
        regime = "普涨"
    elif adv_ratio < 0.25 and turnover > 8000:
        regime = "放量普跌"
    elif adv_ratio < 0.3:
        regime = "普跌"
    elif abs(strength) < 0.15:
        regime = "震荡分化"
    else:
        regime = "结构行情"
    return {"regime": regime, "turnover_billion": round(turnover, 1), "adv_ratio": round(adv_ratio, 3)}


def _market_mood(recommendations: list[dict[str, Any]], opportunities: list[dict[str, Any]]) -> dict[str, str]:
    if not recommendations and not opportunities:
        return {"label": "等待数据", "tone": "neutral", "detail": "推荐、机会池暂未返回，先检查数据源状态。"}
    scores = []
    for item in recommendations:
        base = float(item.get("score", 0.5) or 0.5)
        ai = float((item.get("ai_review") or {}).get("score", base) or base)
        factor = float((item.get("factor_review") or {}).get("score", base) or base)
        scores.append(base * 0.5 + ai * 0.3 + factor * 0.2)
    avg_score = sum(scores) / len(scores) if scores else 0
    hot_count = sum(1 for item in opportunities if float(item.get("change_pct", 0) or 0) >= 5)
    weak_count = sum(1 for item in opportunities if float(item.get("change_pct", 0) or 0) < 0)
    if avg_score >= 0.72 or hot_count >= 4:
        return {"label": "进攻观察", "tone": "good", "detail": "高分候选较多，适合重点跟踪量价确认。"}
    if weak_count > hot_count and len(recommendations) < 3:
        return {"label": "防守优先", "tone": "bad", "detail": "有效候选偏少，优先控制仓位。"}
    return {"label": "均衡观察", "tone": "warn", "detail": "有候选但强度未压倒性，适合分层跟踪。"}


def _compute_sentiment(
    market_overview: dict[str, Any],
    capital: dict[str, Any] | None,
    pools: dict[str, Any] | None,
) -> dict[str, Any]:
    breadth = market_overview.get("breadth") or {}
    total = int(breadth.get("total") or 0)
    adv = int(breadth.get("advancers") or 0)
    dec = int(breadth.get("decliners") or 0)
    limit_up = int((pools or {}).get("limit_up_count") or breadth.get("limit_up") or 0)
    max_height = int((pools or {}).get("max_limit_up_height") or 0)
    adv_ratio = adv / total if total else 0.5
    breadth_strength = (adv - dec) / total if total else 0.0
    breadth_heat = adv_ratio * 100.0
    limit_heat = min(100.0, (limit_up ** 0.6) * 18.0)
    sector_net = 0.0
    if capital and capital.get("sector_top5"):
        sector_net = sum(s.get("main_net", 0) for s in capital["sector_top5"]) / 1e8
    capital_heat = max(0.0, min(100.0, 50.0 + sector_net * 4.0))
    temperature = round(max(0.0, min(100.0, 0.4 * breadth_heat + 0.3 * limit_heat + 0.3 * capital_heat)), 1)

    if temperature >= 75:
        label = "过热"
    elif temperature >= 60:
        label = "偏热"
    elif temperature >= 40:
        label = "温和"
    elif temperature >= 25:
        label = "偏冷"
    else:
        label = "冰冷"

    if temperature >= 70 and max_height >= 4:
        stage, stage_reason = "高潮", "情绪温度较高且连板高度抬升，投机情绪偏强。"
    elif temperature >= 60 and breadth_strength > 0.2:
        stage, stage_reason = "升温", "上涨家数占优，赚钱效应正在扩散。"
    elif temperature <= 25 and breadth_strength < -0.2:
        stage, stage_reason = "冰点", "普跌且情绪温度很低，优先等待恐慌释放。"
    elif breadth_strength < 0 and temperature < 50:
        stage, stage_reason = "退潮", "下跌家数占优，高位情绪存在回落压力。"
    elif 35 <= temperature <= 55 and abs(breadth_strength) < 0.15:
        stage, stage_reason = "震荡", "温度中性、多空均衡，等待方向选择。"
    else:
        stage, stage_reason = "回暖", "边际信号改善，适合观察主线承接。"

    return {
        "as_of": _now_cst().isoformat(),
        "temperature": temperature,
        "label": label,
        "breadth_strength": round(breadth_strength, 3),
        "adv_dec_diff": adv - dec,
        "stage": stage,
        "stage_reason": stage_reason,
        "summary": f"情绪 {temperature:.0f}/{label}，涨跌广度 {breadth_strength:+.2f}，涨停 {limit_up}，连板高度 {max_height}",
        "formulas": {
            "temperature": "0.4*涨跌家数热度 + 0.3*涨停热度 + 0.3*行业主力资金",
            "breadth_strength": "(上涨家数-下跌家数)/总数，范围 -1 到 +1",
            "stage": "温度、连板高度、涨跌方向组合判定",
        },
    }


def _market_environment(market_overview: dict[str, Any], sentiment: dict[str, Any] | None) -> dict[str, Any]:
    breadth = market_overview.get("breadth") or {}
    total = int(breadth.get("total") or 0)
    adv = int(breadth.get("advancers") or 0)
    turnover = float(breadth.get("turnover_billion") or 0)
    adv_ratio = adv / total if total else 0.5
    strength = float((sentiment or {}).get("breadth_strength", 0.0) or 0.0)
    if adv_ratio > 0.75 and turnover > 8000:
        regime = "放量普涨"
    elif adv_ratio > 0.7:
        regime = "普涨"
    elif adv_ratio < 0.25 and turnover > 8000:
        regime = "放量普跌"
    elif adv_ratio < 0.3:
        regime = "普跌"
    elif abs(strength) < 0.15:
        regime = "震荡分化"
    else:
        regime = "结构行情"
    return {"regime": regime, "turnover_billion": round(turnover, 1), "adv_ratio": round(adv_ratio, 3)}


def _market_mood(recommendations: list[dict[str, Any]], opportunities: list[dict[str, Any]]) -> dict[str, str]:
    if not recommendations and not opportunities:
        return {"label": "等待数据", "tone": "neutral", "detail": "推荐和机会池暂未返回，先检查数据源状态。"}

    scores = []
    for item in recommendations:
        base = float(item.get("score", 0.5) or 0.5)
        ai = float((item.get("ai_review") or {}).get("score", base) or base)
        factor = float((item.get("factor_review") or {}).get("score", base) or base)
        scores.append(base * 0.5 + ai * 0.3 + factor * 0.2)
    avg_score = sum(scores) / len(scores) if scores else 0
    hot_count = sum(1 for item in opportunities if float(item.get("change_pct", 0) or 0) >= 5)
    weak_count = sum(1 for item in opportunities if float(item.get("change_pct", 0) or 0) < 0)

    if avg_score >= 0.72 or hot_count >= 4:
        return {"label": "进攻观察", "tone": "good", "detail": "高分候选较多，盘中适合重点跟踪量价确认。"}
    if weak_count > hot_count and len(recommendations) < 3:
        return {"label": "防守优先", "tone": "bad", "detail": "有效候选偏少，优先控制仓位并等待确认。"}
    return {"label": "均衡观察", "tone": "warn", "detail": "有候选但强度未压倒性，适合分层跟踪。"}


def _tail_action(score: float, change_pct: float, risk_text: str = "") -> tuple[str, str]:
    risk_lower = risk_text.lower()
    if change_pct >= 7:
        return "等回落", "涨幅偏高，尾盘不追价，等待回踩或次日确认。"
    if score >= 0.72 and change_pct >= 0:
        return "重点跟踪", "强度和当日表现较好，尾盘重点观察承接与收盘位置。"
    if score >= 0.58:
        return "轻仓观察", "信号有效但强度一般，适合小仓位观察或加入明日清单。"
    if "risk" in risk_lower or "风险" in risk_text:
        return "风险规避", "风险提示较强，尾盘先规避，等待新证据。"
    return "等待确认", "信号不足以支持尾盘动作，继续观察量价和事件变化。"


def _morning_brief(
    market_overview: dict[str, Any],
    recommendations: list[dict[str, Any]],
    news: list[dict[str, Any]],
    events: list[dict[str, Any]],
    opportunities: list[dict[str, Any]],
) -> dict[str, Any]:
    breadth = market_overview.get("breadth") or {}
    morning_recs = [item for item in recommendations if item.get("slot") == "morning"]
    focus = morning_recs[:5] or sorted(opportunities, key=lambda item: float(item.get("confidence", 0) or 0), reverse=True)[:5]
    advancers = int(breadth.get("advancers", 0) or 0)
    decliners = int(breadth.get("decliners", 0) or 0)
    risk_note = "市场广度偏强，可重点观察高置信候选和主线承接。"
    if decliners > advancers:
        risk_note = "下跌家数多于上涨家数，早盘先控仓，等待指数与主线确认。"

    return {
        "title": "早盘内参",
        "as_of": market_overview.get("as_of") or _now_cst().isoformat(),
        "market_breadth": breadth,
        "indices": market_overview.get("indices", []),
        "top_news": news[:5],
        "key_events": _top_events(events, limit=5),
        "focus_symbols": [
            {
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "reason": item.get("reason") or item.get("summary") or item.get("category_label", ""),
                "score": round(_score_recommendation(item), 3) if "slot" in item else round(float(item.get("confidence", 0.5) or 0.5), 3),
            }
            for item in focus
        ],
        "risk_note": risk_note,
    }


def _intraday_monitor(
    market_overview: dict[str, Any],
    opportunities: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    alerts: list[dict[str, Any]] = []
    for item in opportunities[:8]:
        change = float(item.get("change_pct", 0) or 0)
        confidence = float(item.get("confidence", 0) or 0)
        if change >= 6:
            level = "hot"
            message = "涨幅偏高，关注冲高回落风险。"
        elif confidence >= 0.75:
            level = "focus"
            message = "信号强度较高，等待量价确认。"
        else:
            level = "watch"
            message = "纳入观察池。"
        alerts.append({
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "level": level,
            "change_pct": round(change, 2),
            "message": message,
        })

    return {
        "title": "盘中监控",
        "breadth": market_overview.get("breadth") or {},
        "hot_sectors": market_overview.get("hot_sectors", [])[:8],
        "alerts": alerts,
        "scheduled_tasks": [
            {
                "symbol": task.get("symbol"),
                "name": task.get("name"),
                "time": task.get("time"),
                "enabled": bool(task.get("enabled")),
                "last_status": task.get("last_status"),
            }
            for task in tasks[:20]
        ],
    }


def _tail_strategy(tail_decisions: list[dict[str, Any]], market_overview: dict[str, Any]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in tail_decisions:
        grouped.setdefault(str(item.get("action", "等待确认")), []).append(item)
    return {
        "title": "尾盘策略",
        "decisions": tail_decisions,
        "groups": grouped,
        "rules": [
            "涨幅过高的候选不追价，优先等回落或次日确认。",
            "重点跟踪标的只在收盘承接良好时保留到明日观察。",
            "市场广度走弱时降低尾盘动作优先级。",
        ],
        "breadth": market_overview.get("breadth") or {},
    }


def _close_review(
    recommendations: list[dict[str, Any]],
    market_overview: dict[str, Any],
    tail_decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    returns = [
        float((item.get("performance") or {}).get("latest_return_pct"))
        for item in recommendations
        if (item.get("performance") or {}).get("latest_return_pct") is not None
    ]
    avg_return = round(sum(returns) / len(returns), 2) if returns else None
    win_rate = round(sum(1 for value in returns if value > 0) / len(returns) * 100, 1) if returns else None
    tomorrow_watch = [
        {
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "action": item.get("action"),
            "reason": item.get("reason"),
        }
        for item in tail_decisions[:8]
    ]
    return {
        "title": "收盘复盘",
        "summary": {
            "recommendation_count": len(recommendations),
            "tracked_return_count": len(returns),
            "avg_latest_return_pct": avg_return,
            "win_rate": win_rate,
        },
        "breadth": market_overview.get("breadth") or {},
        "tomorrow_watch": tomorrow_watch,
        "review_questions": [
            "今日推荐是否跑赢市场广度？",
            "尾盘强势股是否有真实承接，而不是单纯拉高？",
            "明日优先观察哪些事件和板块扩散？",
        ],
    }


def _load_pools(trade_date: str) -> dict[str, Any] | None:
    store = _market_store()
    pool_date = store.latest_date("stock_pool") or trade_date
    try:
        limit_up = store.get_pool("limitup", pool_date)
        limit_down = store.get_pool("limitdown", pool_date)
        fire = store.get_pool("fire", pool_date)
        previous = store.get_pool("previous", pool_date)
    except Exception as exc:  # noqa: BLE001
        logger.info("pools load failed: %s", exc)
        return None
    if not limit_up and not limit_down:
        return None

    name_map = store.security_names([
        str(row.get("code") or row.get("symbol") or "")
            for row in [*limit_up, *limit_down, *fire]
    ])

    def _pool_symbol(row: dict[str, Any]) -> str:
        return str(row.get("code") or row.get("symbol") or "")

    def _pool_name(row: dict[str, Any]) -> str:
        symbol = _pool_symbol(row)
        return name_map.get(symbol.upper()) or name_map.get(symbol.split(".", 1)[0]) or str(row.get("name") or symbol)

    ladder_buckets: dict[int, list[dict[str, Any]]] = {}
    for row in limit_up:
        days = _row_lianban(row)
        ladder_buckets.setdefault(days, []).append({
            "symbol": _pool_symbol(row),
            "name": _pool_name(row),
            "days": days,
        })
    ladder = [
        {"days": days, "count": len(stocks), "stocks": stocks}
        for days, stocks in sorted(ladder_buckets.items(), reverse=True)
    ]

    def _fmt(rows: list[dict[str, Any]], *, with_days: bool = False) -> list[dict[str, Any]]:
        out = []
        for r in rows[:30]:
            out.append({
                "symbol": _pool_symbol(r),
                "name": _pool_name(r),
                "days": (_row_lianban(r) if with_days else 0),
            })
        return out

    non_st_limit_up = sum(1 for row in limit_up if "ST" not in _pool_name(row).upper())
    st_limit_up = max(0, len(limit_up) - non_st_limit_up)
    touched_limit_up_count = len(limit_up) + len(fire)
    fail_rate = round(len(fire) / touched_limit_up_count * 100, 2) if touched_limit_up_count else None
    promoted_count = sum(1 for row in limit_up if _row_lianban(row) >= 2)
    promotion_rate = round(promoted_count / len(previous) * 100, 2) if previous else None
    sealed_amount_billion = round(sum(_number(row.get("seal_amount")) for row in limit_up) / 100_000_000, 2)

    return {
        "as_of": _now_cst().isoformat(),
        "trade_date": pool_date,
        "limit_up_count": len(limit_up),
        "limit_down_count": len(limit_down),
        "touched_limit_up_count": touched_limit_up_count,
        "failed_limit_up_count": len(fire),
        "non_st_limit_up_count": non_st_limit_up,
        "st_limit_up_count": st_limit_up,
        "fail_rate": fail_rate,
        "promotion_rate": promotion_rate,
        "sealed_amount_billion": sealed_amount_billion,
        "max_limit_up_height": ladder[0]["days"] if ladder else 0,
        "limit_up_list": _fmt(limit_up, with_days=True),
        "limit_down_list": _fmt(limit_down),
        "failed_limit_up_list": _fmt(fire),
        "limitup_ladder": ladder,
    }


def _load_capital() -> dict[str, Any] | None:
    store = _market_store()
    capital_date = (
        store.latest_date("sector_capital_flow")
        or store.latest_date("stock_capital_rank")
        or store.latest_date("stock_capital_flow")
    )
    if not capital_date:
        return None
    sector_top5 = store.get_sector_capital(capital_date, limit=5)
    stock_inflow = store.get_stock_capital_rank(capital_date, "inflow", limit=8)
    stock_outflow = store.get_stock_capital_rank(capital_date, "outflow", limit=8)
    return {
        "as_of": _now_cst().isoformat(),
        "trade_date": capital_date,
        "sector_top5": sector_top5,
        "stock_inflow_top": stock_inflow,
        "stock_outflow_top": stock_outflow,
        "north_recent": [],
    }


def _load_themes() -> dict[str, Any] | None:
    store = _market_store()
    theme_date = store.latest_date("sector_snapshot")
    if not theme_date:
        return None
    concepts = store.get_sector_snapshot(theme_date, "concept", limit=40)
    # 热力图取全部行业、按|涨跌幅|排序(大涨大跌都靠前),反映真实涨跌分布;不能只取涨幅TOP否则满屏红。
    industries = store.get_sector_snapshot(theme_date, "industry", limit=200, order_by="abs")
    source_rows = concepts or industries
    if not source_rows:
        return None
    by_change = sorted(source_rows, key=lambda r: float(r.get("change_pct") or 0), reverse=True)
    main_lines = [
        {"name": r.get("name", ""), "change_pct": r.get("change_pct", 0), "leader": r.get("leader", "")}
        for r in by_change[:3]
    ]
    observe = []
    for r in by_change[3:15]:
        spread = int(r.get("advancers") or 0) - int(r.get("decliners") or 0)
        change = float(r.get("change_pct") or 0)
        if -3 < change < 3 and spread >= 0:
            observe.append({"name": r.get("name", ""), "change_pct": change, "leader": r.get("leader", "")})
        if len(observe) >= 5:
            break
    return {
        "as_of": _now_cst().isoformat(),
        "trade_date": theme_date,
        "concept_sectors": concepts,
        "industry_sectors": industries,
        "main_lines": main_lines,
        "observe": observe,
    }


def register_market_dashboard_routes(
    app: FastAPI,
    require_auth: AuthDep | None = None,
    require_event_stream_auth: AuthDep | None = None,
) -> None:
    if require_auth is None or require_event_stream_auth is None:
        import sys as _sys

        host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        if host is None:
            raise RuntimeError("register_market_dashboard_routes: api_server not in sys.modules")
        if require_auth is None:
            require_auth = host.require_auth
        if require_event_stream_auth is None:
            require_event_stream_auth = host.require_event_stream_auth

    @app.get("/market-dashboard", dependencies=[Depends(require_auth)])
    async def get_market_dashboard(request: Request) -> dict[str, Any]:
        today = _today_cst()
        results = await asyncio.gather(
            _run_source("market_overview", _load_market_overview),
            _run_source("capital", _load_capital),
            _run_source("themes", _load_themes),
            _run_source("pools", lambda: _load_pools(today)),
            _run_source("tracking", _load_tracking),
        )
        data = {name: payload for name, payload, _error in results}
        errors = [{"source": name, "message": error} for name, _payload, error in results if error]

        recommendations: list[dict[str, Any]] = []
        opportunity_categories: list[dict[str, Any]] = []
        opportunity_items = _flatten_opportunities(opportunity_categories)
        tail_decisions: list[dict[str, Any]] = []
        market_overview = data.get("market_overview") or {}
        # Override estimated limit counts with real pool counts (no fake fallback).
        pools = data.get("pools")
        if market_overview.get("breadth"):
            market_overview["breadth"] = _apply_real_limits(market_overview["breadth"], pools)
        news_items: list[dict[str, Any]] = []
        event_categories: list[dict[str, Any]] = []
        tracking = data.get("tracking") or {}
        capital = data.get("capital")
        themes = data.get("themes")
        sentiment = _compute_sentiment(market_overview, capital, pools)
        environment = _market_environment(market_overview, sentiment)
        multi_period = _load_multi_period(market_overview.get("top_gainers") or [])
        morning_brief = _morning_brief(market_overview, recommendations, news_items, event_categories, opportunity_items)
        intraday_monitor = _intraday_monitor(market_overview, opportunity_items, tracking.get("tasks", []))
        tail_strategy = _tail_strategy(tail_decisions, market_overview)
        close_review = _close_review(recommendations, market_overview, tail_decisions)

        return {
            "date": today,
            "updated_at": _now_cst().isoformat(),
            "mood": _market_mood(recommendations, opportunity_items),
            "market_overview": market_overview,
            "capital": capital,
            "pools": pools,
            "sentiment": sentiment,
            "environment": environment,
            "themes": themes,
            "multi_period": multi_period,
            "morning_brief": morning_brief,
            "intraday_monitor": intraday_monitor,
            "tail_strategy": tail_strategy,
            "close_review": close_review,
            "tail_decisions": tail_decisions,
            "recommendations": recommendations,
            "opportunities": opportunity_categories,
            "news": news_items,
            "events": event_categories,
            "watchlist": tracking.get("watchlist", []),
            "tasks": tracking.get("tasks", []),
            "errors": errors,
            "counts": {
                "recommendations": len(recommendations),
                "opportunities": len(opportunity_items),
                "indices": len(market_overview.get("indices", [])),
                "hot_sectors": len(market_overview.get("hot_sectors", [])),
                "news": len((data.get("news") or {}).get("articles", [])),
                "events": sum(len(category.get("events", []) or []) for category in (data.get("events") or {}).get("categories", [])),
                "watchlist": len(tracking.get("watchlist", [])),
                "tasks": len(tracking.get("tasks", [])),
                "tail_decisions": len(tail_decisions),
            },
        }

    @app.get("/market-dashboard/stages/{stage}", dependencies=[Depends(require_auth)])
    async def get_market_dashboard_stage(stage: str, request: Request) -> dict[str, Any]:
        allowed = {"morning-brief", "intraday-monitor", "tail-strategy", "close-review"}
        stage_key = stage.strip().lower()
        if stage_key not in allowed:
            return {
                "status": "error",
                "error": "unknown stage",
                "allowed": sorted(allowed),
            }
        expected_trade_date = _close_review_visible_trade_date() if stage_key == "close-review" else None
        snapshot = _market_store().get_market_stage_snapshot_fast(stage_key, expected_trade_date)
        if not snapshot:
            return {
                "status": "ok",
                "stage": stage_key,
                "date": expected_trade_date,
                "updated_at": None,
                "data": {
                    "data_status": "missing",
                    "missing_tables": ["market_stage_snapshot"],
                    "source_policy": "db_only",
                    "expected_trade_date": expected_trade_date,
                },
                "errors": [{"source": "market_stage_snapshot", "message": "stage snapshot not synced"}],
            }
        return {
            "status": "ok",
            "stage": stage_key,
            "date": snapshot.get("trade_date"),
            "updated_at": snapshot.get("updated_at"),
            "data": _stage_payload_with_freshness(stage_key, snapshot),
            "errors": [],
        }

    @app.get("/market-dashboard/bars/{code}", dependencies=[Depends(require_auth)])
    async def get_market_bars(code: str, days: int = 60) -> dict[str, Any]:
        """Daily OHLCV bars for the K-line chart (DB-backed, no intraday)."""
        raw = (code or "").strip()
        # Normalize sh/sz prefixes and .SH/.SZ suffix to a project code.
        bare = (
            raw.lower()
            .replace("sh", "")
            .replace("sz", "")
            .replace(".sh", "")
            .replace(".sz", "")
            .replace(".bj", "")
            .replace(".", "")
            .upper()
        )
        suffix = _project_suffix(raw or bare)
        project_code = raw.upper() if raw.upper().endswith((".SH", ".SZ", ".BJ")) else bare + suffix
        limit = min(max(int(days), 10), 250)
        store = _market_store()

        # Core index K-line comes from index_daily, which is the long-term index store.
        index_candidates = {
            bare,
            project_code,
            f"{bare}.SH",
            f"{bare}.SZ",
        }
        try:
            placeholders = ",".join("?" for _ in index_candidates)
            rows = store._conn.execute(
                f"""
                SELECT code, trade_date, open, high, low, close, volume
                FROM index_daily
                WHERE code IN ({placeholders})
                ORDER BY trade_date DESC
                LIMIT ?
                """,
                (*index_candidates, limit),
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.warning("index bars %s failed: %s", raw, exc)
            rows = []
        if rows:
            out = [
                {
                    "date": r["trade_date"],
                    "open": round(float(r["open"] or 0), 3),
                    "close": round(float(r["close"] or 0), 3),
                    "high": round(float(r["high"] or 0), 3),
                    "low": round(float(r["low"] or 0), 3),
                    "volume": float(r["volume"] or 0),
                }
                for r in reversed(rows)
            ]
            return {"status": "ok", "code": rows[0]["code"], "bars": out}

        # Stock K-line falls back to bars_daily. Try both suffix-normalized and bare
        # forms for older local data.
        for candidate in (project_code, bare):
            try:
                df = store.get_daily_bars(candidate, days=limit)
            except Exception as exc:  # noqa: BLE001
                logger.warning("bars %s failed: %s", candidate, exc)
                continue
            if df is not None and not df.empty:
                cols = {c.lower(): c for c in df.columns}
                out = []
                for ts, row in df.iterrows():
                    out.append({
                        "date": pd.Timestamp(ts).strftime("%Y-%m-%d"),
                        "open": round(float(row[cols.get("open", "open")]), 3) if "open" in cols else None,
                        "close": round(float(row[cols.get("close", "close")]), 3) if "close" in cols else None,
                        "high": round(float(row[cols.get("high", "high")]), 3) if "high" in cols else None,
                        "low": round(float(row[cols.get("low", "low")]), 3) if "low" in cols else None,
                        "volume": float(row[cols.get("volume", "volume")]) if "volume" in cols else 0.0,
                    })
                return {"status": "ok", "code": candidate, "bars": out}
        return {"status": "ok", "code": project_code, "bars": []}
