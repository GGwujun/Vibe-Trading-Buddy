"""Daily recommendation routes.

This module adds a recommendation workflow without changing the existing
market-intelligence pages. A generated pick is persisted as a timestamped
record, then later enriched with T+0/T+1/T+3/T+5 performance from OHLCV data.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

_CST = timezone(timedelta(hours=8))
_STORE_PATH = Path.home() / ".vibe-trading" / "daily_recommendations.json"
_DB_PATH = Path.home() / ".vibe-trading" / "daily_recommendations.db"
_STORE_LOCK = threading.Lock()
_MAX_GENERATED = 5
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecommendationPhase:
    id: str
    label: str
    analysis_slot: str
    target_offset_days: int
    status: str
    hour: int
    minute: int
    final_for: str | None = None


_PHASES: dict[str, RecommendationPhase] = {
    "post_close_base": RecommendationPhase("post_close_base", "收盘基线", "afternoon", 1, "draft", 15, 45),
    "evening_review": RecommendationPhase("evening_review", "晚间复核", "afternoon", 1, "draft", 21, 0),
    "premarket_review": RecommendationPhase("premarket_review", "盘前复核", "morning", 0, "draft", 8, 15),
    "morning_final": RecommendationPhase("morning_final", "早盘最终", "morning", 0, "final", 9, 24, "morning"),
    "afternoon_final": RecommendationPhase("afternoon_final", "午盘最终", "afternoon", 0, "final", 14, 20, "afternoon"),
    "manual": RecommendationPhase("manual", "手动生成", "manual", 0, "final", 0, 0),
}
_SLOT_ALIASES = {
    "morning": "morning_final",
    "am": "morning_final",
    "0927": "morning_final",
    "09:27": "morning_final",
    "afternoon": "afternoon_final",
    "pm": "afternoon_final",
    "1430": "afternoon_final",
    "14:30": "afternoon_final",
    "manual": "manual",
    "now": "manual",
}
_FINAL_PHASES = {phase.id for phase in _PHASES.values() if phase.status == "final"}


class GenerateRecommendationsRequest(BaseModel):
    slot: str = Field(..., description="recommendation phase or legacy slot")
    limit: int = Field(default=5, ge=1, le=10)


def _now_cst() -> datetime:
    return datetime.now(_CST)


def _today_cst() -> str:
    return _now_cst().strftime("%Y-%m-%d")


def _normalize_phase(value: str) -> str:
    phase_id = _SLOT_ALIASES.get(value.strip().lower(), value.strip().lower())
    if phase_id in _PHASES:
        return phase_id
    raise HTTPException(status_code=400, detail=f"unknown recommendation phase: {value}")


def _normalize_slot(slot: str) -> str:
    return _PHASES[_normalize_phase(slot)].analysis_slot


def _slot_label(slot: str) -> str:
    return {"morning": "9:27", "afternoon": "14:30", "manual": "手动生成"}.get(slot, slot)


def _slot_label(slot: str) -> str:
    if slot in _PHASES:
        return _PHASES[slot].label
    return {"morning": "早盘", "afternoon": "午盘", "manual": "手动生成"}.get(slot, slot)


def _phase_label(phase_id: str) -> str:
    return _PHASES.get(phase_id, _PHASES["manual"]).label


def _next_weekday(date_value: datetime) -> datetime:
    cur = date_value
    while cur.weekday() >= 5:
        cur += timedelta(days=1)
    return cur


def _target_date_for_phase(phase_id: str, now: datetime | None = None) -> str:
    phase = _PHASES[_normalize_phase(phase_id)]
    base = now or _now_cst()
    target = base + timedelta(days=phase.target_offset_days)
    try:
        from src.data.trade_calendar import is_trading_day

        while not is_trading_day(target.strftime("%Y-%m-%d")):
            target += timedelta(days=1)
    except Exception:
        target = _next_weekday(target)
    return target.strftime("%Y-%m-%d")


def _load_records() -> list[dict[str, Any]]:
    _ensure_db()
    try:
        with sqlite3.connect(_DB_PATH) as conn:
            rows = conn.execute(
                """
                select payload from recommendations
                order by target_date desc, created_at desc, generation_phase asc, rank asc
                """
            ).fetchall()
        return [json.loads(row[0]) for row in rows]
    except Exception:
        logger.exception("failed to load daily recommendations from sqlite")
        return []


def _load_legacy_json_records() -> list[dict[str, Any]]:
    if not _STORE_PATH.exists():
        return []
    try:
        data = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_records(records: list[dict[str, Any]]) -> None:
    _ensure_db()
    with sqlite3.connect(_DB_PATH) as conn:
        conn.executemany(
            """
            insert into recommendations (
                id, date, slot, symbol, rank, name, price_at_pick,
                score, strategy, created_at, target_date, generation_phase,
                status, version, supersedes_id, final_for, payload
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set
                date=excluded.date,
                slot=excluded.slot,
                symbol=excluded.symbol,
                rank=excluded.rank,
                name=excluded.name,
                price_at_pick=excluded.price_at_pick,
                score=excluded.score,
                strategy=excluded.strategy,
                created_at=excluded.created_at,
                target_date=excluded.target_date,
                generation_phase=excluded.generation_phase,
                status=excluded.status,
                version=excluded.version,
                supersedes_id=excluded.supersedes_id,
                final_for=excluded.final_for,
                payload=excluded.payload
            """,
            [
                (
                    r.get("id"),
                    r.get("date"),
                    r.get("slot"),
                    r.get("symbol"),
                    int(r.get("rank", 0) or 0),
                    r.get("name"),
                    float(r.get("price_at_pick", 0) or 0),
                    float(r.get("score", 0) or 0),
                    r.get("strategy"),
                    r.get("created_at"),
                    r.get("target_date") or r.get("date"),
                    r.get("generation_phase") or r.get("slot"),
                    r.get("status") or "final",
                    int(r.get("version", 1) or 1),
                    r.get("supersedes_id"),
                    r.get("final_for"),
                    json.dumps(r, ensure_ascii=False),
                )
                for r in records
            ],
        )


def _ensure_db() -> None:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(_DB_PATH) as conn:
        conn.execute(
            """
            create table if not exists recommendations (
                id text primary key,
                date text not null,
                slot text not null,
                symbol text not null,
                rank integer not null,
                name text not null,
                price_at_pick real not null,
                score real not null,
                strategy text not null,
                created_at text not null,
                target_date text not null,
                generation_phase text not null,
                status text not null,
                version integer not null default 1,
                supersedes_id text,
                final_for text,
                payload text not null
            )
            """
        )
        columns = {row[1] for row in conn.execute("pragma table_info(recommendations)").fetchall()}
        migrations = {
            "target_date": "ALTER TABLE recommendations ADD COLUMN target_date text",
            "generation_phase": "ALTER TABLE recommendations ADD COLUMN generation_phase text",
            "status": "ALTER TABLE recommendations ADD COLUMN status text",
            "version": "ALTER TABLE recommendations ADD COLUMN version integer NOT NULL DEFAULT 1",
            "supersedes_id": "ALTER TABLE recommendations ADD COLUMN supersedes_id text",
            "final_for": "ALTER TABLE recommendations ADD COLUMN final_for text",
        }
        for column, sql in migrations.items():
            if column not in columns:
                conn.execute(sql)
        conn.execute("UPDATE recommendations SET target_date = COALESCE(target_date, date)")
        conn.execute("UPDATE recommendations SET generation_phase = COALESCE(generation_phase, slot)")
        conn.execute("UPDATE recommendations SET status = COALESCE(status, 'final')")
        conn.execute("create index if not exists idx_recs_date_slot on recommendations(date, slot)")
        conn.execute("create index if not exists idx_recs_target_phase on recommendations(target_date, generation_phase, status)")
        conn.execute("create index if not exists idx_recs_phase_symbol on recommendations(target_date, generation_phase, symbol)")
        conn.execute("create index if not exists idx_recs_symbol on recommendations(symbol)")
        count = conn.execute("select count(*) from recommendations").fetchone()[0]
    if count == 0:
        legacy = _load_legacy_json_records()
        if legacy:
            with sqlite3.connect(_DB_PATH) as conn:
                conn.executemany(
                    """
                    insert or replace into recommendations (
                        id, date, slot, symbol, rank, name, price_at_pick,
                        score, strategy, created_at, target_date, generation_phase,
                        status, version, supersedes_id, final_for, payload
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            r.get("id"),
                            r.get("date"),
                            r.get("slot"),
                            r.get("symbol"),
                            int(r.get("rank", 0) or 0),
                            r.get("name"),
                            float(r.get("price_at_pick", 0) or 0),
                            float(r.get("score", 0) or 0),
                            r.get("strategy"),
                            r.get("created_at"),
                            r.get("target_date") or r.get("date"),
                            r.get("generation_phase") or r.get("slot"),
                            r.get("status") or "final",
                            int(r.get("version", 1) or 1),
                            r.get("supersedes_id"),
                            r.get("final_for"),
                            json.dumps(r, ensure_ascii=False),
                        )
                        for r in legacy
                    ],
                )


def _record_key(target_date: str, phase_id: str, symbol: str, version: int = 1) -> str:
    return f"{target_date}:{phase_id}:v{version}:{symbol}"


def _strategy_label(category_id: str) -> str:
    return {
        "breakout": "突破",
        "trend": "趋势",
        "oversold": "低吸",
        "event": "事件催化",
    }.get(category_id, category_id)


def _slot_adjusted_score(item: dict[str, Any], slot: str) -> float:
    score = float(item.get("confidence", 0) or 0)
    cat = item.get("category_id", "")
    change = float(item.get("change_pct", 0) or 0)
    if slot == "morning":
        if cat == "trend":
            score += 0.03
        if cat in {"breakout", "event"}:
            score -= 0.04
        if change >= 6:
            score -= 0.12
    elif slot == "afternoon":
        if cat == "trend":
            score += 0.07
        elif cat == "breakout":
            score += 0.02
        if change < 0:
            score -= 0.05
        if change >= 6:
            score -= 0.08
    return max(0.01, min(0.99, score))


def _date_gap_days(older: str | None, newer: str | None) -> int | None:
    if not older or not newer:
        return None
    try:
        return (
            datetime.strptime(newer[:10], "%Y-%m-%d")
            - datetime.strptime(older[:10], "%Y-%m-%d")
        ).days
    except ValueError:
        return None


def _assert_candidate_market_data_fresh() -> None:
    try:
        from src.data.market_store import get_market_store

        store = get_market_store()
        if store is None:
            return
        _, latest_daily = store.date_range("bars_daily")
        _, latest_realtime = store.date_range("realtime_quote_snapshot")
    except Exception:
        logger.debug("daily recommendation market freshness check failed", exc_info=True)
        return

    gap = _date_gap_days(latest_daily, latest_realtime)
    if gap is not None and gap > 3:
        raise HTTPException(
            status_code=503,
            detail=(
                "Daily market data is stale "
                f"(latest daily={latest_daily}, latest realtime={latest_realtime}); "
                "wait for post-close sync before generating recommendations"
            ),
        )


def _refresh_candidate_quote(item: dict[str, Any]) -> dict[str, Any]:
    symbol = str(item.get("symbol", "")).upper()
    if not symbol:
        return item
    try:
        from src.data.market_data_service import normalize_code
        from src.data.market_store import get_market_store

        store = get_market_store()
        quote = None if store is None else store.get_latest_realtime_quote(normalize_code(symbol), _today_cst())
    except Exception:
        logger.debug("daily recommendation quote refresh failed for %s", symbol, exc_info=True)
        return item
    if not quote:
        return item
    price = float(quote.get("price") or 0)
    if price <= 0:
        return item
    item["price"] = price
    if quote.get("rise_rate") is not None:
        item["change_pct"] = float(quote.get("rise_rate") or 0)
    item["quote_date"] = str(quote.get("trade_date") or "")[:10]
    item["quote_source"] = quote.get("source")
    return item


def _candidate_pool(slot: str) -> list[dict[str, Any]]:
    from src.api.opportunity_routes import _build_opportunities

    _assert_candidate_market_data_fresh()
    payload = _build_opportunities()
    if payload.get("error"):
        raise HTTPException(status_code=503, detail=str(payload["error"]))

    items: list[dict[str, Any]] = []
    for category in payload.get("categories", []):
        category_id = category.get("id", "")
        category_label = category.get("label", category_id)
        for raw in category.get("opportunities", []):
            item = dict(raw)
            item["category_id"] = category_id
            item["category_label"] = category_label
            item = _refresh_candidate_quote(item)
            item["score"] = round(_slot_adjusted_score(item, slot), 3)
            items.append(item)

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda x: x.get("score", 0), reverse=True):
        symbol = str(item.get("symbol", "")).upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        unique.append(item)
    return unique


def _extract_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("empty AI response")
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("AI response did not contain JSON")
    data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("AI response JSON is not an object")
    return data


def _factor_review(item: dict[str, Any]) -> dict[str, Any]:
    symbol = str(item.get("symbol", "")).upper()
    review: dict[str, Any] = {
        "score": 0.5,
        "status": "unavailable",
        "summary": "Alpha因子不可用",
        "top_bullish": [],
        "top_bearish": [],
        "peer_count": 0,
    }
    if not symbol:
        return review
    try:
        from src.data.alpha_signals import compute_alpha_signals

        raw = compute_alpha_signals(symbol)
        top_bullish = [
            {
                "label": s.get("label", ""),
                "theme": s.get("theme", ""),
                "rank_pct": s.get("rank_pct", 0.5),
                "direction": s.get("direction", "neutral"),
            }
            for s in raw.get("top_bullish", [])[:3]
        ]
        top_bearish = [
            {
                "label": s.get("label", ""),
                "theme": s.get("theme", ""),
                "rank_pct": s.get("rank_pct", 0.5),
                "direction": s.get("direction", "neutral"),
            }
            for s in raw.get("top_bearish", [])[:3]
        ]
        score = float(raw.get("score", 0.5) or 0.5)
        if raw.get("error"):
            summary = str(raw["error"])
            status = "limited"
        elif score >= 0.60:
            summary = "Alpha因子偏多"
            status = "ok"
        elif score <= 0.40:
            summary = "Alpha因子偏空"
            status = "ok"
        else:
            summary = "Alpha因子中性"
            status = "ok"
        return {
            "score": round(max(0.01, min(0.99, score)), 3),
            "status": status,
            "summary": summary,
            "top_bullish": top_bullish,
            "top_bearish": top_bearish,
            "peer_count": int(raw.get("peer_count", 0) or 0),
        }
    except Exception as exc:
        logger.info("factor review failed for %s: %s", symbol, exc)
        return review


def _market_regime_from_closes(closes: list[float]) -> dict[str, Any]:
    clean = [float(value) for value in closes if float(value) > 0]
    if len(clean) < 20:
        return {"regime": "unknown", "score": 0.0, "reason": "insufficient_index_history"}
    latest = clean[-1]
    ma20 = sum(clean[-20:]) / 20
    ma60 = sum(clean[-60:]) / 60 if len(clean) >= 60 else ma20
    ret20 = (latest - clean[-20]) / clean[-20] * 100 if clean[-20] > 0 else 0.0
    if latest < ma20 and (ma20 < ma60 or ret20 < -3):
        return {
            "regime": "risk_off",
            "score": -1.0,
            "reason": f"index below MA20, 20d={ret20:.2f}%",
        }
    if latest > ma20 and ma20 > ma60 and ret20 > 0:
        return {
            "regime": "risk_on",
            "score": 1.0,
            "reason": f"index above MA20/MA60, 20d={ret20:.2f}%",
        }
    return {
        "regime": "neutral",
        "score": 0.0,
        "reason": f"mixed index trend, 20d={ret20:.2f}%",
    }


def _current_market_regime() -> dict[str, Any]:
    try:
        from src.data.market_store import get_market_store

        store = get_market_store()
        conn = getattr(store, "_conn", None)
        if conn is None:
            return {"regime": "unknown", "score": 0.0, "reason": "market_store_unavailable"}
        for code in ("000300.SH", "000001.SH", "399001.SZ", "399006.SZ"):
            rows = conn.execute(
                "SELECT close FROM index_daily WHERE code = ? ORDER BY trade_date DESC LIMIT 60",
                (code,),
            ).fetchall()
            closes = [float(row["close"]) for row in reversed(rows) if row["close"] is not None]
            if len(closes) >= 20:
                regime = _market_regime_from_closes(closes)
                regime["index_code"] = code
                return regime
    except Exception:
        logger.debug("daily recommendation market regime unavailable", exc_info=True)
    return {"regime": "unknown", "score": 0.0, "reason": "index_data_unavailable"}


def _apply_attribution_guardrails(
    item: dict[str, Any],
    slot: str,
    market_regime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    score = float(item.get("score", item.get("confidence", 0.5)) or 0.5)
    category = str(item.get("category_id") or item.get("category") or "")
    change = float(item.get("change_pct", 0) or 0)
    factor_score = float((item.get("factor_review") or {}).get("score", 0.5) or 0.5)
    ai_score = float((item.get("ai_review") or {}).get("score", 0.5) or 0.5)
    adjustments: list[str] = []

    if category == "trend":
        score += 0.04
        adjustments.append("trend_prior")
    elif category == "breakout":
        score -= 0.10
        adjustments.append("breakout_drawdown_prior")
    elif category == "event":
        score -= 0.08
        adjustments.append("event_low_sample_prior")

    if slot == "morning":
        score -= 0.04
        adjustments.append("morning_prior")
        if category in {"breakout", "event"}:
            score -= 0.05
            adjustments.append("morning_hot_signal_penalty")

    if change >= 6:
        score -= 0.12
        adjustments.append("chase_high_penalty")
    elif 0 <= change < 3:
        score += 0.03
        adjustments.append("moderate_intraday_move_prior")

    if category in {"breakout", "event"} and factor_score < 0.70:
        score -= 0.05
        adjustments.append("weak_factor_confirmation")

    if ai_score >= 0.75 and category in {"breakout", "event"}:
        score -= 0.04
        adjustments.append("ai_hot_signal_overconfidence_penalty")

    regime = str((market_regime or {}).get("regime") or "unknown")
    if regime == "risk_off":
        if category in {"breakout", "event"}:
            score -= 0.08
            adjustments.append("risk_off_hot_signal_penalty")
        elif category == "trend":
            score -= 0.03
            adjustments.append("risk_off_trend_penalty")
        elif category == "oversold":
            score += 0.02
            adjustments.append("risk_off_oversold_prior")
        if change >= 6:
            score -= 0.04
            adjustments.append("risk_off_chase_high_penalty")
    elif regime == "risk_on":
        if category == "trend":
            score += 0.03
            adjustments.append("risk_on_trend_bonus")
        elif category == "breakout" and change < 6:
            score += 0.02
            adjustments.append("risk_on_moderate_breakout_bonus")

    item["score"] = round(max(0.01, min(0.99, score)), 3)
    item["attribution_adjustments"] = adjustments
    item["market_regime"] = market_regime or {"regime": "unknown"}
    return item


def _ai_review_candidates(candidates: list[dict[str, Any]], slot: str, limit: int) -> dict[str, dict[str, Any]]:
    if not candidates:
        return {}
    payload = []
    for item in candidates:
        factor = item.get("factor_review", {})
        payload.append({
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "price": item.get("price"),
            "change_pct": item.get("change_pct"),
            "slot": slot,
            "category": item.get("category_label") or item.get("category_id"),
            "scanner_reason": item.get("reason"),
            "scanner_score": item.get("score"),
            "factor_score": factor.get("score"),
            "factor_summary": factor.get("summary"),
            "top_bullish_factors": [s.get("label") for s in factor.get("top_bullish", [])],
            "top_bearish_factors": [s.get("label") for s in factor.get("top_bearish", [])],
        })

    prompt = (
        "你是A股每日推荐复核员。必须结合候选池信号、Alpha因子和风险，筛选适合今日推荐的股票。\n"
        "要求：\n"
        "1. 不要编造新股票，只能评价输入候选。\n"
        "2. ai_score 取 0-1，越高越值得进入今日推荐。\n"
        "3. decision 只能是 recommend/watch/reject。\n"
        "4. summary 用一句中文说明为什么推荐或观察。\n"
        "5. risk 用一句中文写主要失效条件。\n"
        f"6. 最多 recommend {limit} 只。\n\n"
        f"候选JSON：\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        "只输出JSON，格式："
        '{"reviews":[{"symbol":"300000.SZ","ai_score":0.72,"decision":"recommend","summary":"...","risk":"...","factor_note":"..."}]}'
    )
    try:
        from src.providers.chat import ChatLLM

        llm = ChatLLM()
        resp = llm.chat([
            {"role": "system", "content": "你只输出可解析JSON，不输出Markdown。"},
            {"role": "user", "content": prompt},
        ], timeout=25)
        data = _extract_json_object(resp.content or "")
    except Exception as exc:
        logger.warning("AI recommendation review failed: %s", exc)
        raise HTTPException(status_code=503, detail=f"AI推荐复核不可用: {exc}")

    reviews: dict[str, dict[str, Any]] = {}
    for raw in data.get("reviews", []):
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("symbol", "")).upper()
        if not symbol:
            continue
        decision = str(raw.get("decision", "watch")).lower()
        if decision not in {"recommend", "watch", "reject"}:
            decision = "watch"
        try:
            ai_score = float(raw.get("ai_score", 0.5))
        except (TypeError, ValueError):
            ai_score = 0.5
        reviews[symbol] = {
            "score": round(max(0.01, min(0.99, ai_score)), 3),
            "decision": decision,
            "summary": str(raw.get("summary", "")).strip(),
            "risk": str(raw.get("risk", "")).strip(),
            "factor_note": str(raw.get("factor_note", "")).strip(),
            "status": "ok",
        }
    return reviews


def _reviewed_candidates(slot: str, limit: int) -> list[dict[str, Any]]:
    candidates = _candidate_pool(slot)
    if not candidates:
        return []

    shortlist = candidates[: max(limit * 3, 8)]
    for item in shortlist:
        factor = _factor_review(item)
        item["factor_review"] = factor
        base = float(item.get("score", item.get("confidence", 0.5)) or 0.5)
        factor_score = float(factor.get("score", 0.5) or 0.5)
        item["pre_ai_score"] = round(max(0.01, min(0.99, base * 0.70 + factor_score * 0.30)), 3)

    ai_reviews = _ai_review_candidates(shortlist, slot, limit)
    market_regime = _current_market_regime()
    reviewed: list[dict[str, Any]] = []
    for item in shortlist:
        symbol = str(item.get("symbol", "")).upper()
        ai = ai_reviews.get(symbol)
        if not ai:
            continue
        item["ai_review"] = ai
        item["score"] = round(
            max(0.01, min(0.99, float(item.get("pre_ai_score", 0.5)) * 0.45 + float(ai.get("score", 0.5)) * 0.55)),
            3,
        )
        item = _apply_attribution_guardrails(item, slot, market_regime)
        if ai.get("decision") == "recommend" and float(item.get("score", 0) or 0) >= 0.58:
            reviewed.append(item)

    if not reviewed:
        raise HTTPException(status_code=503, detail="AI复核后没有可推荐标的")
    reviewed.sort(key=lambda x: x.get("score", 0), reverse=True)
    return reviewed


def _make_record(
    item: dict[str, Any],
    phase_id: str,
    target_date: str,
    rank: int,
    version: int,
) -> dict[str, Any]:
    now = _now_cst()
    symbol = str(item["symbol"]).upper()
    date = now.strftime("%Y-%m-%d")
    phase = _PHASES[_normalize_phase(phase_id)]
    slot = phase.analysis_slot
    ai_review = item.get("ai_review") or {}
    factor_review = item.get("factor_review") or {}
    ai_summary = str(ai_review.get("summary") or "").strip()
    ai_risk = str(ai_review.get("risk") or "").strip()
    factor_note = str(ai_review.get("factor_note") or factor_review.get("summary") or "").strip()
    reason_parts = [part for part in [ai_summary, factor_note, item.get("reason", "")] if part]
    reason = "；".join(reason_parts[:3]) or item.get("reason", "")
    risk_note = ai_risk or _risk_note(item)
    return {
        "id": _record_key(target_date, phase.id, symbol, version),
        "date": date,
        "target_date": target_date,
        "slot": slot,
        "slot_label": _slot_label(slot),
        "generation_phase": phase.id,
        "phase_label": phase.label,
        "status": phase.status,
        "version": version,
        "supersedes_id": None,
        "final_for": phase.final_for,
        "rank": rank,
        "symbol": symbol,
        "name": item.get("name", symbol),
        "price_at_pick": float(item.get("price", 0) or 0),
        "change_pct_at_pick": float(item.get("change_pct", 0) or 0),
        "score": float(item.get("score", item.get("confidence", 0)) or 0),
        "strategy": _strategy_label(str(item.get("category_id", ""))),
        "category": item.get("category_id", ""),
        "reason": reason,
        "risk_note": risk_note,
        "ai_review": ai_review,
        "factor_review": factor_review,
        "attribution_adjustments": item.get("attribution_adjustments", []),
        "market_regime": item.get("market_regime", {"regime": "unknown"}),
        "evidence_snapshot": _evidence_snapshot(item, slot, reason, risk_note, now),
        "recommendation_method": "ai_factor_review",
        "created_at": now.isoformat(),
        "source": "ai_factor_reviewer",
    }


def _evidence_snapshot(
    item: dict[str, Any],
    slot: str,
    reason: str,
    risk_note: str,
    now: datetime,
) -> dict[str, Any]:
    ai_review = item.get("ai_review") or {}
    factor_review = item.get("factor_review") or {}
    bullish = [
        str(entry.get("label", "")).strip()
        for entry in factor_review.get("top_bullish", [])[:3]
        if str(entry.get("label", "")).strip()
    ]
    bearish = [
        str(entry.get("label", "")).strip()
        for entry in factor_review.get("top_bearish", [])[:3]
        if str(entry.get("label", "")).strip()
    ]
    market_line = (
        f"推荐时价格 {float(item.get('price', 0) or 0):.2f}，"
        f"日内涨跌幅 {float(item.get('change_pct', 0) or 0):+.2f}%"
    )
    return {
        "as_of": now.isoformat(),
        "slot": slot,
        "source": "推荐生成时固化，后续复盘不重新查询",
        "market": market_line,
        "scanner": str(item.get("reason", "")).strip(),
        "ai": str(ai_review.get("summary") or "").strip(),
        "factor": str(factor_review.get("summary") or ai_review.get("factor_note") or "").strip(),
        "bullish_factors": bullish,
        "bearish_factors": bearish,
        "recommendation": reason,
        "risk": risk_note,
    }


def _risk_note(item: dict[str, Any]) -> str:
    cat = item.get("category_id", "")
    if cat == "breakout":
        return "放量突破信号需要关注次日是否继续放量，缩量回落则信号失效。"
    if cat == "trend":
        return "趋势延续信号需要关注均线结构是否保持，跌破短期均线需降级。"
    if cat == "oversold":
        return "超跌反弹信号反转确认较弱，若继续创新低应快速剔除。"
    if cat == "event":
        return "事件催化信号对新闻和外部概率变化敏感，需要防止高开低走。"
    return "仅作为候选标的，需要结合流动性、仓位和止损条件复核。"


def _bar_date(index_value: Any) -> str:
    if hasattr(index_value, "strftime"):
        return index_value.strftime("%Y-%m-%d")
    return str(index_value)[:10]


def _realtime_performance(symbol: str, price_at_pick: float, pick_date: str) -> dict[str, Any] | None:
    try:
        from src.data.market_data_service import normalize_code
        from src.data.market_store import get_market_store

        store = get_market_store()
        if store is None:
            return None
        quote = store.get_latest_realtime_quote(normalize_code(symbol))
    except Exception:
        logger.debug("daily recommendation realtime quote lookup failed for %s", symbol, exc_info=True)
        return None

    if not quote:
        return None
    quote_date = str(quote.get("trade_date") or "")[:10]
    if pick_date and quote_date and quote_date < pick_date:
        return None
    current = float(quote.get("price") or 0)
    if current <= 0 or price_at_pick <= 0:
        return None

    high = float(quote.get("high") or current)
    low = float(quote.get("low") or current)
    return {
        "status": "realtime",
        "latest_date": quote_date or None,
        "latest_price": round(current, 3),
        "latest_return_pct": round((current - price_at_pick) / price_at_pick * 100, 2),
        "max_gain_pct": round((max(high, current) - price_at_pick) / price_at_pick * 100, 2),
        "max_drawdown_pct": round((min(low, current) - price_at_pick) / price_at_pick * 100, 2),
        "source": quote.get("source") or "realtime_quote_snapshot",
        "snapshot_at": quote.get("snapshot_at") or quote.get("updated_at"),
        "t0": {
            "date": quote_date or pick_date,
            "close": round(current, 3),
            "return_pct": round((current - price_at_pick) / price_at_pick * 100, 2),
        },
        "t1": None,
        "t3": None,
        "t5": None,
    }


def _baseline_today_performance(price_at_pick: float, pick_date: str) -> dict[str, Any] | None:
    if pick_date != _today_cst() or price_at_pick <= 0:
        return None
    return {
        "status": "baseline",
        "latest_date": pick_date,
        "latest_price": round(price_at_pick, 3),
        "latest_return_pct": 0.0,
        "max_gain_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "source": "price_at_pick",
        "snapshot_at": None,
        "t0": {"date": pick_date, "close": round(price_at_pick, 3), "return_pct": 0.0},
        "t1": None,
        "t3": None,
        "t5": None,
    }


def _performance_for(record: dict[str, Any]) -> dict[str, Any]:
    from src.data.market_data_service import latest_daily_bars

    price = float(record.get("price_at_pick", 0) or 0)
    symbol = str(record.get("symbol", ""))
    if not symbol or price <= 0:
        return {"status": "missing_price"}

    pick_date = str(record.get("date", ""))
    df = latest_daily_bars(symbol, days=40)
    if df is None or df.empty or "close" not in df.columns:
        return (
            _realtime_performance(symbol, price, pick_date)
            or _baseline_today_performance(price, pick_date)
            or {"status": "no_market_data"}
        )

    rows = []
    for idx, row in df.sort_index().iterrows():
        date = _bar_date(idx)
        if date >= pick_date:
            close = float(row.get("close", 0) or 0)
            high = float(row.get("high", close) or close)
            low = float(row.get("low", close) or close)
            rows.append({"date": date, "close": close, "high": high, "low": low})
    if not rows:
        return (
            _realtime_performance(symbol, price, pick_date)
            or _baseline_today_performance(price, pick_date)
            or {"status": "pending"}
        )

    out: dict[str, Any] = {
        "status": "ok",
        "latest_date": rows[-1]["date"],
        "latest_return_pct": round((rows[-1]["close"] - price) / price * 100, 2),
        "max_gain_pct": round((max(r["high"] for r in rows) - price) / price * 100, 2),
        "max_drawdown_pct": round((min(r["low"] for r in rows) - price) / price * 100, 2),
    }
    horizons = {"t0": 0, "t1": 1, "t3": 3, "t5": 5}
    for key, offset in horizons.items():
        if len(rows) > offset:
            close = rows[offset]["close"]
            out[key] = {
                "date": rows[offset]["date"],
                "close": round(close, 3),
                "return_pct": round((close - price) / price * 100, 2),
            }
        else:
            out[key] = None
    return out


def _with_performance(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for record in records:
        enriched.append({**record, "performance": _performance_for(record)})
    return enriched


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [r for r in records if r.get("performance", {}).get("t1")]
    if not completed:
        return {"count": len(records), "t1_count": 0, "t1_win_rate": None, "t1_avg_return": None}
    t1_returns = [float(r["performance"]["t1"]["return_pct"]) for r in completed]
    wins = [x for x in t1_returns if x > 0]
    return {
        "count": len(records),
        "t1_count": len(t1_returns),
        "t1_win_rate": round(len(wins) / len(t1_returns) * 100, 1),
        "t1_avg_return": round(sum(t1_returns) / len(t1_returns), 2),
    }


def _horizon_return(record: dict[str, Any], horizon: str) -> float | None:
    perf = record.get("performance") or {}
    point = perf.get(horizon)
    if not isinstance(point, dict) or point.get("return_pct") is None:
        return None
    try:
        return float(point["return_pct"])
    except (TypeError, ValueError):
        return None


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[mid], 2)
    return round((ordered[mid - 1] + ordered[mid]) / 2, 2)


def _performance_summary(records: list[dict[str, Any]], horizon: str = "t1") -> dict[str, Any]:
    returns = [value for record in records if (value := _horizon_return(record, horizon)) is not None]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value <= 0]
    avg_win = _mean(wins)
    avg_loss = _mean(losses)
    payoff = None
    if avg_win is not None and avg_loss is not None and avg_loss != 0:
        payoff = round(avg_win / abs(avg_loss), 2)
    return {
        "count": len(records),
        "completed_count": len(returns),
        "win_rate": round(len(wins) / len(returns) * 100, 1) if returns else None,
        "avg_return": _mean(returns),
        "median_return": _median(returns),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff_ratio": payoff,
        "best_return": round(max(returns), 2) if returns else None,
        "worst_return": round(min(returns), 2) if returns else None,
    }


def _score_bucket(value: Any) -> str:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if score < 0.55:
        return "<0.55"
    if score < 0.65:
        return "0.55-0.65"
    if score < 0.75:
        return "0.65-0.75"
    return ">=0.75"


def _change_bucket(value: Any) -> str:
    try:
        change = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if change <= -2:
        return "<=-2%"
    if change < 0:
        return "-2%-0%"
    if change < 3:
        return "0%-3%"
    if change < 6:
        return "3%-6%"
    return ">=6%"


def _dimension_value(record: dict[str, Any], dimension: str) -> str:
    if dimension == "slot":
        return str(record.get("slot") or "unknown")
    if dimension == "generation_phase":
        return str(record.get("generation_phase") or record.get("slot") or "unknown")
    if dimension == "status":
        return str(record.get("status") or "unknown")
    if dimension == "category":
        return str(record.get("category") or "unknown")
    if dimension == "strategy":
        return str(record.get("strategy") or "unknown")
    if dimension == "rank":
        try:
            return f"rank_{int(record.get('rank') or 0)}"
        except (TypeError, ValueError):
            return "unknown"
    if dimension == "score_bucket":
        return _score_bucket(record.get("score"))
    if dimension == "change_bucket":
        return _change_bucket(record.get("change_pct_at_pick"))
    if dimension == "ai_score_bucket":
        return _score_bucket((record.get("ai_review") or {}).get("score"))
    if dimension == "factor_score_bucket":
        return _score_bucket((record.get("factor_review") or {}).get("score"))
    return "unknown"


def _attribution_rows(
    records: list[dict[str, Any]],
    dimension: str,
    horizon: str = "t1",
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        groups.setdefault(_dimension_value(record, dimension), []).append(record)
    rows = []
    for key, grouped in groups.items():
        rows.append({"dimension": dimension, "key": key, **_performance_summary(grouped, horizon)})
    return sorted(
        rows,
        key=lambda row: (
            -(row.get("completed_count") or 0),
            row.get("avg_return") if row.get("avg_return") is not None else -999,
        ),
        reverse=False,
    )


def _recommendation_attribution(records: list[dict[str, Any]], horizon: str = "t1") -> dict[str, Any]:
    if horizon not in {"t0", "t1", "t3", "t5"}:
        raise HTTPException(status_code=400, detail="horizon must be one of t0, t1, t3, or t5")
    dimensions = [
        "slot",
        "generation_phase",
        "category",
        "strategy",
        "rank",
        "score_bucket",
        "change_bucket",
        "ai_score_bucket",
        "factor_score_bucket",
    ]
    by_dimension = {dimension: _attribution_rows(records, dimension, horizon) for dimension in dimensions}
    completed_floor = max(3, int(len(records) * 0.05))
    all_rows = [
        row
        for rows in by_dimension.values()
        for row in rows
        if (row.get("completed_count") or 0) >= completed_floor and row.get("avg_return") is not None
    ]
    weak_spots = sorted(all_rows, key=lambda row: (row["avg_return"], row["win_rate"] or 0))[:5]
    strong_spots = sorted(all_rows, key=lambda row: (row["avg_return"], row["win_rate"] or 0), reverse=True)[:5]
    return {
        "horizon": horizon,
        "summary": _performance_summary(records, horizon),
        "by_dimension": by_dimension,
        "weak_spots": weak_spots,
        "strong_spots": strong_spots,
        "min_completed_for_spots": completed_floor,
    }


def _has_slot_record(records: list[dict[str, Any]], date: str, slot: str) -> bool:
    return any(r.get("date") == date and r.get("slot") == slot for r in records)


def _has_phase_record(records: list[dict[str, Any]], target_date: str, phase_id: str) -> bool:
    return any(
        r.get("target_date", r.get("date")) == target_date
        and r.get("generation_phase", r.get("slot")) == phase_id
        and r.get("status") != "superseded"
        for r in records
    )


def _next_phase_version(records: list[dict[str, Any]], target_date: str, phase_id: str) -> int:
    versions = [
        int(r.get("version", 1) or 1)
        for r in records
        if r.get("target_date", r.get("date")) == target_date
        and r.get("generation_phase", r.get("slot")) == phase_id
    ]
    return (max(versions) + 1) if versions else 1


def _mark_superseded(records: list[dict[str, Any]], new_records: list[dict[str, Any]], phase: RecommendationPhase) -> None:
    new_symbols = {str(r.get("symbol", "")).upper() for r in new_records}
    new_ids = {str(r.get("id")) for r in new_records}
    new_target = str(new_records[0].get("target_date")) if new_records else ""
    for record in records:
        if str(record.get("id")) in new_ids:
            continue
        same_target = record.get("target_date", record.get("date")) == new_target
        same_phase = record.get("generation_phase", record.get("slot")) == phase.id
        same_symbol = str(record.get("symbol", "")).upper() in new_symbols
        if same_target and same_phase:
            record["status"] = "superseded"
            record["superseded_by"] = ",".join(sorted(new_ids))
        elif phase.status == "final" and same_target and same_symbol and record.get("status") == "draft":
            record["status"] = "superseded"
            record["superseded_by"] = next((r["id"] for r in new_records if r.get("symbol") == record.get("symbol")), None)


def _generate_for_phase(phase_id: str, limit: int, *, target_date: str | None = None) -> list[dict[str, Any]]:
    phase = _PHASES[_normalize_phase(phase_id)]
    target = target_date or _target_date_for_phase(phase.id)
    slot = phase.analysis_slot
    candidates = _reviewed_candidates(slot, limit)
    if not candidates:
        return []

    selected = candidates[: min(limit, _MAX_GENERATED)]
    with _STORE_LOCK:
        records = _load_records()
        version = _next_phase_version(records, target, phase.id)
        new_records = [
            _make_record(item, phase.id, target, rank + 1, version)
            for rank, item in enumerate(selected)
        ]
        _mark_superseded(records, new_records, phase)
        existing = {str(r.get("id")): r for r in records}
        for record in new_records:
            existing[record["id"]] = record
        records = sorted(existing.values(), key=lambda r: str(r.get("created_at", "")), reverse=True)
        _save_records(records)
    return new_records


def _generate_for_slot(slot: str, limit: int) -> list[dict[str, Any]]:
    return _generate_for_phase(_normalize_phase(slot), limit)


def _is_trading_day_today() -> bool:
    try:
        from src.data.trade_calendar import is_trading_day

        return bool(is_trading_day(_today_cst()))
    except Exception:
        return _now_cst().weekday() < 5


AuthDep = Callable[..., Awaitable[Any] | Any]


def register_daily_recommendation_routes(
    app: FastAPI,
    require_auth: AuthDep | None = None,
    require_event_stream_auth: AuthDep | None = None,
) -> None:
    if require_auth is None or require_event_stream_auth is None:
        import sys as _sys

        host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        if host is None:
            raise RuntimeError("register_daily_recommendation_routes: api_server not in sys.modules")
        if require_auth is None:
            require_auth = host.require_auth
        if require_event_stream_auth is None:
            require_event_stream_auth = host.require_event_stream_auth

    @app.post("/daily-recommendations/generate", dependencies=[Depends(require_auth)])
    async def generate_recommendations(body: GenerateRecommendationsRequest, request: Request) -> dict[str, Any]:
        phase_id = _normalize_phase(body.slot)
        phase = _PHASES[phase_id]
        target_date = _target_date_for_phase(phase_id)
        new_records = _generate_for_phase(phase_id, body.limit, target_date=target_date)
        if not new_records:
            raise HTTPException(status_code=503, detail="No recommendation candidates are available")

        return {
            "date": _today_cst(),
            "target_date": target_date,
            "slot": phase.analysis_slot,
            "slot_label": _slot_label(phase.analysis_slot),
            "generation_phase": phase.id,
            "phase_label": phase.label,
            "status": phase.status,
            "items": _with_performance(new_records),
            "updated_at": _now_cst().isoformat(),
        }

    @app.get("/daily-recommendations", dependencies=[Depends(require_auth)])
    async def list_recommendations(
        request: Request,
        date: str = Query("", max_length=10),
        slot: str = Query("", max_length=16),
        target_date: str = Query("", max_length=10),
        phase: str = Query("", max_length=32),
        status: str = Query("final", max_length=16),
        limit: int = Query(80, ge=1, le=300),
    ) -> dict[str, Any]:
        with _STORE_LOCK:
            records = _load_records()

        if date:
            records = [r for r in records if r.get("date") == date]
        if target_date:
            records = [r for r in records if r.get("target_date", r.get("date")) == target_date]
        if slot:
            normalized = _normalize_slot(slot)
            records = [r for r in records if r.get("slot") == normalized]
        if phase:
            normalized_phase = _normalize_phase(phase)
            records = [r for r in records if r.get("generation_phase", r.get("slot")) == normalized_phase]
        if status and status != "all":
            records = [r for r in records if r.get("status", "final") == status]
        records = records[:limit]
        enriched = _with_performance(records)
        cutoff = (_now_cst() - timedelta(days=30)).strftime("%Y-%m-%d")
        with _STORE_LOCK:
            rolling_records = [
                r for r in _load_records()
                if str(r.get("date", "")) >= cutoff and r.get("status", "final") == "final"
            ]
        rolling_enriched = _with_performance(rolling_records)
        return {
            "items": enriched,
            "summary": _summary(rolling_enriched),
            "list_summary": _summary(enriched),
            "updated_at": _now_cst().isoformat(),
        }

    @app.get("/daily-recommendations/backtest", dependencies=[Depends(require_auth)])
    async def recommendation_backtest(
        request: Request,
        days: int = Query(30, ge=1, le=365),
    ) -> dict[str, Any]:
        cutoff = (_now_cst() - timedelta(days=days)).strftime("%Y-%m-%d")
        with _STORE_LOCK:
            records = [
                r for r in _load_records()
                if str(r.get("date", "")) >= cutoff and r.get("status", "final") == "final"
            ]
        enriched = _with_performance(records)

        by_slot: dict[str, list[dict[str, Any]]] = {}
        by_phase: dict[str, list[dict[str, Any]]] = {}
        for record in enriched:
            by_slot.setdefault(str(record.get("slot", "manual")), []).append(record)
            by_phase.setdefault(str(record.get("generation_phase", record.get("slot", "manual"))), []).append(record)

        slot_rows = []
        for slot, rows in sorted(by_slot.items()):
            slot_rows.append({"slot": slot, "slot_label": _slot_label(slot), **_summary(rows)})
        phase_rows = []
        for phase_id, rows in sorted(by_phase.items()):
            phase_rows.append({"generation_phase": phase_id, "phase_label": _phase_label(phase_id), **_summary(rows)})

        return {
            "days": days,
            "summary": _summary(enriched),
            "by_slot": slot_rows,
            "by_phase": phase_rows,
            "items": enriched,
            "updated_at": _now_cst().isoformat(),
        }

    @app.get("/daily-recommendations/attribution", dependencies=[Depends(require_auth)])
    async def recommendation_attribution(
        request: Request,
        days: int = Query(30, ge=1, le=365),
        horizon: str = Query("t1", max_length=2),
    ) -> dict[str, Any]:
        cutoff = (_now_cst() - timedelta(days=days)).strftime("%Y-%m-%d")
        with _STORE_LOCK:
            records = [
                r for r in _load_records()
                if str(r.get("date", "")) >= cutoff and r.get("status", "final") == "final"
            ]
        enriched = _with_performance(records)
        return {
            "days": days,
            **_recommendation_attribution(enriched, horizon),
            "updated_at": _now_cst().isoformat(),
        }

    @app.on_event("startup")
    async def start_daily_recommendation_scheduler() -> None:
        if os.getenv("DAILY_RECOMMENDATIONS_AUTORUN", "1").strip().lower() in {"0", "false", "no"}:
            return

        import asyncio

        async def _loop() -> None:
            while True:
                try:
                    now = _now_cst()
                    checks = [
                        _PHASES["post_close_base"],
                        _PHASES["evening_review"],
                        _PHASES["premarket_review"],
                        _PHASES["morning_final"],
                        _PHASES["afternoon_final"],
                    ]
                    with _STORE_LOCK:
                        records = _load_records()
                    for phase in checks:
                        target_date = _target_date_for_phase(phase.id, now)
                        if phase.target_offset_days == 0 and not _is_trading_day_today():
                            continue
                        if phase.target_offset_days > 0 and not _is_trading_day_today():
                            continue
                        due = now.hour > phase.hour or (now.hour == phase.hour and now.minute >= phase.minute)
                        if not due or _has_phase_record(records, target_date, phase.id):
                            continue
                        await asyncio.get_running_loop().run_in_executor(
                            None,
                            _generate_for_phase,
                            phase.id,
                            5,
                        )
                        logger.info(
                            "daily recommendations generated phase=%s target_date=%s",
                            phase.id,
                            target_date,
                        )
                    await asyncio.sleep(60)
                except Exception:
                    logger.exception("daily recommendation scheduler tick failed")
                    await asyncio.sleep(300)

        asyncio.create_task(_loop())
