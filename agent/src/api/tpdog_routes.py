"""TPDog data routes — thin REST surface over the tpdog client.

Mounted under ``/tpdog`` and proxied by the Vite dev server (see
``frontend/vite.config.ts`` API_ONLY_PATHS). These endpoints let the frontend
verify the configured token and pull a few high-value datasets (trading day,
daily K-line, call auction, dragon-tiger list) without each page hardcoding the
tpdog URL/token.

Token comes from ``TPDOG_TOKEN`` in agent/.env (set via the Settings UI). All
endpoints return ``{ok, detail, ...}``-style payloads and never leak the token.
Admin-gated like the other data-source surfaces.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, Query
from pydantic import BaseModel

from src.api.auth_routes import require_admin
from src.data.tpdog_client import (
    TpdogError,
    TpdogNotConfiguredError,
    call,
    is_configured,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tpdog", tags=["tpdog"])


class TpdogStatus(BaseModel):
    configured: bool
    ok: bool
    detail: str


class TpdogEnvelope(BaseModel):
    """Standard wrapper so callers get a consistent shape even on error."""

    ok: bool
    detail: str
    content: List[Dict[str, Any]] = []


def _error_envelope(exc: Exception) -> TpdogEnvelope:
    return TpdogEnvelope(ok=False, detail=str(exc)[:300], content=[])


@router.get("/status", response_model=TpdogStatus, dependencies=[Depends(require_admin)])
async def tpdog_status() -> TpdogStatus:
    """Report whether TPDOG_TOKEN is configured and the API actually answers."""
    if not is_configured():
        return TpdogStatus(configured=False, ok=False, detail="TPDOG_TOKEN 未配置")
    try:
        # Cheapest live call: pull current-year trading days (1 积分).
        call("trading_day/year", year="2026")
        return TpdogStatus(configured=True, ok=True, detail="token 有效")
    except Exception as exc:  # noqa: BLE001 — status must never 500
        return TpdogStatus(configured=True, ok=False, detail=str(exc)[:300])


@router.get("/trading-days", response_model=TpdogEnvelope, dependencies=[Depends(require_admin)])
async def trading_days(year: str = Query(..., description="格式 yyyy，如 2026")) -> TpdogEnvelope:
    try:
        return TpdogEnvelope(ok=True, detail="成功", content=call("trading_day/year", year=year))
    except Exception as exc:  # noqa: BLE001
        return _error_envelope(exc)


def _project_code_from_tpdog(tpdog_code: str) -> str | None:
    """Convert tpdog code (sh.600206) → project form (600206.SH) for DB lookup."""
    if "." not in tpdog_code:
        return None
    prefix, digits = tpdog_code.split(".", 1)
    if len(digits) != 6 or not digits.isdigit():
        return None
    return digits + "." + prefix.upper()


@router.get("/daily", response_model=TpdogEnvelope, dependencies=[Depends(require_admin)])
async def daily_history(
    code: str = Query(..., description="如 sh.600206 / sz.000001"),
    start: str = Query(..., description="yyyy-MM-dd"),
    end: str = Query(..., description="yyyy-MM-dd"),
) -> TpdogEnvelope:
    """日K历史 — OHLCV + 涨跌幅 + 换手。1 积分/次，30 次/秒。

    Reads from the market DB first (when populated); falls back to tpdog and
    persists what it fetched.
    """
    try:
        from src.data.market_store import db_read_enabled, get_market_store
        store = get_market_store() if db_read_enabled() else None
        proj = _project_code_from_tpdog(code)
        if store is not None and proj is not None:
            try:
                df = store.get_daily_bars(proj, start=start, end=end)
            except Exception:  # noqa: BLE001
                df = None
            if df is not None and not df.empty:
                content = [
                    {"date": str(ts)[:10], "open": r["open"], "high": r["high"],
                     "low": r["low"], "close": r["close"], "volume": r["volume"]}
                    for ts, r in df.iterrows()
                ]
                return TpdogEnvelope(ok=True, detail=f"{len(content)} 条 (DB)", content=content)
        content = call("stock_his/daily", code=code, start=start, end=end)
        # Persist settled rows (date < today) for next time.
        if store is not None and proj is not None and content:
            try:
                from src.data.market_sync import _today_cst_str
                today = _today_cst_str()
                settled = [r for r in content if r.get("date") and r["date"] < today]
                if settled:
                    store.upsert_daily_bars(proj, settled)
            except Exception:  # noqa: BLE001
                pass
        return TpdogEnvelope(ok=True, detail=f"{len(content)} 条", content=content)
    except Exception as exc:  # noqa: BLE001
        return _error_envelope(exc)


@router.get("/call-auction", response_model=TpdogEnvelope, dependencies=[Depends(require_admin)])
async def call_auction(
    code: str = Query(..., description="如 sh.600206"),
    sort: int = Query(2, description="1 正序 / 2 倒序"),
    test: bool = Query(False, description="t=1 样例数据（非交易时段可用）"),
) -> TpdogEnvelope:
    """集合竞价。仅在交易日 09:15-09:30 有效；test=True 返回样例。5 积分/次。"""
    try:
        content = call("current/call_auction", code=code, sort=sort, t=1 if test else None)
        return TpdogEnvelope(ok=True, detail=f"{len(content)} 条", content=content)
    except Exception as exc:  # noqa: BLE001
        return _error_envelope(exc)


@router.get("/dragon-tiger", response_model=TpdogEnvelope, dependencies=[Depends(require_admin)])
async def dragon_tiger(date: str = Query(..., description="yyyy-MM-dd")) -> TpdogEnvelope:
    """龙虎榜。1 积分/次，30 次/秒。

    DB 命中直接返回；否则调 tpdog 并落库。
    """
    try:
        from src.data.market_store import db_read_enabled, get_market_store
        store = get_market_store() if db_read_enabled() else None
        if store is not None and store.has_dragon_tiger(date):
            rows = store.get_dragon_tiger(date)
            return TpdogEnvelope(ok=True, detail=f"{len(rows)} 条 (DB)", content=rows)
        content = call("board/bill", date=date)
        if store is not None and content:
            try:
                store.upsert_dragon_tiger(date, content)
            except Exception:  # noqa: BLE001
                pass
        return TpdogEnvelope(ok=True, detail=f"{len(content)} 条", content=content)
    except Exception as exc:  # noqa: BLE001
        return _error_envelope(exc)


def register_tpdog_routes(app: FastAPI) -> None:
    """Mount all /tpdog/* routes onto the app."""
    app.include_router(router)
