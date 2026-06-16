"""Manual market-sync API — admin-gated endpoints over :mod:`market_sync`.

Exposes ``run_daily_sync`` for ad-hoc triggers (a past trade date, a single
code, a backfill run) and a status/health surface so the operator can see what
the daemon has done. Long-running backfill runs in a daemon thread; status is
polled via ``GET /market-sync/status``.

Mounted under ``/market-sync`` and proxied by Vite (``API_ONLY_PATHS``).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel

from src.api.auth_routes import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/market-sync", tags=["market-sync"])

# Single-flight guard for the background backfill thread.
_backfill_lock = threading.Lock()
_backfill_running = {"active": False, "started_at": None, "last_result": None}


# ----------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------


class StatusResponse(BaseModel):
    daemon_enabled: bool
    backfill_running: bool
    last_synced: dict[str, str]  # date → iso ts, from sync_meta daemon:* keys
    tables: dict[str, int]
    date_ranges: dict[str, list[str | None]]


class DailySyncRequest(BaseModel):
    trade_date: Optional[str] = None  # default: latest settled trading day
    codes: Optional[list[str]] = None
    datasets: Optional[list[str]] = None


class SyncResultResponse(BaseModel):
    ok: bool
    trade_date: str
    detail: str
    rows: dict[str, int] = {}


class BackfillRequest(BaseModel):
    years: int = 2
    datasets: list[str] = ["daily", "dragon", "pool", "etf"]
    universe: str = "default"  # "default" | "all"
    etf_codes: Optional[list[str]] = None
    codes: Optional[list[str]] = None


class CodeSyncRequest(BaseModel):
    code: str
    datasets: list[str] = ["daily"]
    start: Optional[str] = None
    end: Optional[str] = None


class HealthResponse(BaseModel):
    tpdog_configured: bool
    tpdog_ok: bool
    trading_today: bool
    detail: str


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _store():
    from src.data.market_store import get_market_store
    return get_market_store()


def _latest_synced() -> dict[str, str]:
    """Return {date: iso_ts} for every daemon:<date> sync_meta key."""
    store = _store()
    if store is None:
        return {}
    out: dict[str, str] = {}
    try:
        rows = store._conn.execute(
            "SELECT key, value FROM sync_meta WHERE key LIKE 'daemon:%' ORDER BY key DESC LIMIT 30"
        ).fetchall()
        for r in rows:
            out[r["key"].split(":", 1)[1]] = r["value"]
    except Exception:  # noqa: BLE001
        pass
    return out


def _resolve_trade_date(trade_date: Optional[str]) -> str:
    """Default to today (CST); callers that need a settled date pass one."""
    from src.data.market_sync import _today_cst_str
    return trade_date or _today_cst_str()


# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------


@router.get("/status", response_model=StatusResponse, dependencies=[Depends(require_admin)])
async def status() -> StatusResponse:
    store = _store()
    if store is None:
        raise HTTPException(503, "market store unavailable")
    from src.data.market_sync import _daemon_started
    counts = store.table_counts()
    ranges = {t: list(store.date_range(t)) for t in counts}
    return StatusResponse(
        daemon_enabled=_daemon_started,
        backfill_running=_backfill_running["active"],
        last_synced=_latest_synced(),
        tables=counts,
        date_ranges=ranges,
    )


@router.post("/daily", response_model=SyncResultResponse, dependencies=[Depends(require_admin)])
async def sync_daily(body: DailySyncRequest) -> SyncResultResponse:
    """Run one trade date's sync synchronously (foreground)."""
    from src.data.market_sync import run_daily_sync

    store = _store()
    if store is None:
        raise HTTPException(503, "market store unavailable")
    trade_date = _resolve_trade_date(body.trade_date)
    datasets = set(body.datasets) if body.datasets else None
    try:
        rows = run_daily_sync(
            trade_date, store=store, codes=body.codes, datasets=datasets,
        )
    except Exception as exc:  # noqa: BLE001
        return SyncResultResponse(ok=False, trade_date=trade_date, detail=str(exc)[:300])
    return SyncResultResponse(
        ok=True, trade_date=trade_date, detail=f"synced {sum(rows.values())} rows", rows=rows,
    )


@router.post("/code", response_model=SyncResultResponse, dependencies=[Depends(require_admin)])
async def sync_code(body: CodeSyncRequest) -> SyncResultResponse:
    """Sync a single code's daily-K over [start, end] (defaults: last 90 days)."""
    from src.data.market_sync import _fetch_daily_range_rows, _to_tpdog_code

    store = _store()
    if store is None:
        raise HTTPException(503, "market store unavailable")
    from src.data.market_sync import _today_cst_str
    end = body.end or _today_cst_str()
    # Default start: the 1st of end's month (tpdog caps windows at ~1 month).
    start = body.start or (end[:8] + "01")
    tpdog_code = _to_tpdog_code(body.code)
    if tpdog_code is None:
        raise HTTPException(400, f"unsupported code: {body.code}")
    try:
        rows = _fetch_daily_range_rows(tpdog_code, start, end)
        today_str = _today_cst_str()
        settled = [r for r in rows if r.get("date") and r["date"] < today_str]
        n = store.upsert_daily_bars(body.code, settled) if settled else 0
    except Exception as exc:  # noqa: BLE001
        return SyncResultResponse(ok=False, trade_date=end, detail=str(exc)[:300])
    return SyncResultResponse(ok=True, trade_date=end, detail=f"{n} rows", rows={"daily": n})


@router.post("/backfill", dependencies=[Depends(require_admin)])
async def backfill(body: BackfillRequest) -> dict[str, Any]:
    """Kick off a background backfill (returns immediately; poll /status)."""
    if not _backfill_lock.acquire(blocking=False):
        return {"ok": False, "detail": "a backfill is already running", "running": True}
    _backfill_running.update(active=True, started_at=datetime.now(timezone.utc).isoformat())

    def _run() -> None:
        from src.data.market_sync import run_daily_sync, _today_cst_str
        from src.data.rate_limiter import mark_background
        mark_background(True)  # reserve foreground slots during the backfill
        try:
            today = _today_cst_str()
            rows = run_daily_sync(
                today, store=_store(), codes=body.codes,
                datasets=set(body.datasets), universe=body.universe,
                etf_codes=body.etf_codes, deadline_seconds=3600,
            )
            _backfill_running["last_result"] = {"ok": True, "rows": rows}
        except Exception as exc:  # noqa: BLE001
            logger.exception("backfill failed")
            _backfill_running["last_result"] = {"ok": False, "detail": str(exc)[:300]}
        finally:
            _backfill_running["active"] = False
            _backfill_lock.release()

    threading.Thread(target=_run, name="market-backfill", daemon=True).start()
    return {"ok": True, "detail": "backfill started; poll GET /market-sync/status", "running": True}


@router.post("/snapshot", response_model=SyncResultResponse, dependencies=[Depends(require_admin)])
async def snapshot(body: DailySyncRequest = DailySyncRequest()) -> SyncResultResponse:
    """Run just the snapshot datasets (dragon/pool/premium) for one date."""
    body.datasets = ["dragon", "pool", "premium"]
    return await sync_daily(body)


@router.get("/health", response_model=HealthResponse, dependencies=[Depends(require_admin)])
async def health() -> HealthResponse:
    from src.data.market_sync import _is_trading_day, _today_cst_str
    from src.data.tpdog_client import is_configured

    configured = is_configured()
    tpdog_ok = False
    detail = ""
    if configured:
        try:
            from src.data.tpdog_client import call
            call("trading_day/year", year="2026")
            tpdog_ok = True
            detail = "token ok"
        except Exception as exc:  # noqa: BLE001
            detail = str(exc)[:200]
    else:
        detail = "TPDOG_TOKEN not configured"
    return HealthResponse(
        tpdog_configured=configured, tpdog_ok=tpdog_ok,
        trading_today=_is_trading_day(_today_cst_str()), detail=detail,
    )


def register_market_sync_routes(app: FastAPI) -> None:
    """Mount all /market-sync/* routes onto the app."""
    app.include_router(router)
