"""Scheduled-analysis HTTP routes for the "自选 & 定时分析" page.

Mounted by ``agent/api_server.py`` via ``register_scheduled_analysis_routes``.

Routes:
- Independent watchlist: GET/PUT/DELETE ``/tracking/watchlist[/{symbol}]``
- Tasks:                 GET/POST ``/tracking/tasks``, PUT/DELETE ``/tracking/tasks/{task_id}``,
                         POST ``/tracking/tasks/{task_id}/run`` (manual trigger)
- History:               GET/DELETE ``/tracking/history/{symbol}``

All endpoints require auth. The scheduled task store and runner live in
``src.data.schedule_store`` / ``src.data.schedule_runner``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException, Request

logger = logging.getLogger(__name__)

AuthDep = Callable[..., Awaitable[Any] | Any]

_SYMBOL_RE = re.compile(r"^\d{6}(\.(SZ|SH))?$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_HORIZONS = ("短线", "中线", "长线")

# Beijing time (UTC+8) — matches the convention in ohlcv_cache.py
_CST = timezone(timedelta(hours=8))


def _normalize_symbol(raw: str) -> str:
    """Accept '000001' or '000001.SZ', resolving exchange via prefix heuristic.

    A lightweight copy of position_routes' ``_normalize_symbol`` (that one is
    closure-bound). No mootdx round-trip — fast enough for the scheduler path.
    """
    s = (raw or "").strip().upper()
    m = _SYMBOL_RE.match(s)
    if not m:
        return ""
    if m.group(2):  # already has suffix
        return s
    prefix = s[:3]
    if prefix in {"000", "001", "002", "003", "004", "159", "300", "301"}:
        return s + ".SZ"
    if prefix in {"600", "601", "603", "605", "688", "689"}:
        return s + ".SH"
    return s + ".SH"  # default guess for unknown prefixes (ETFs etc.)


def register_scheduled_analysis_routes(
    app: FastAPI,
    require_auth: AuthDep | None = None,
    require_event_stream_auth: AuthDep | None = None,
) -> None:
    if require_auth is None or require_event_stream_auth is None:
        import sys as _sys

        host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        if host is None:
            raise RuntimeError(
                "register_scheduled_analysis_routes: api_server module not in sys.modules"
            )
        if require_auth is None:
            require_auth = host.require_auth
        if require_event_stream_auth is None:
            require_event_stream_auth = host.require_event_stream_auth

    # ------------------------------------------------------------------
    # Independent watchlist
    # ------------------------------------------------------------------
    from src.data.tracking_watchlist_store import (
        add_to_tracking_watchlist,
        load_tracking_watchlist,
        remove_from_tracking_watchlist,
        save_tracking_watchlist,
    )

    @app.get("/tracking/watchlist", dependencies=[Depends(require_auth)])
    async def get_tracking_watchlist(request: Request) -> dict[str, Any]:
        items = load_tracking_watchlist()
        return {"items": items, "count": len(items)}

    @app.put("/tracking/watchlist", dependencies=[Depends(require_auth)])
    async def put_tracking_watchlist(request: Request) -> dict[str, Any]:
        body = await request.json()
        items = body.get("items", []) if isinstance(body, dict) else []
        if not isinstance(items, list):
            raise HTTPException(status_code=400, detail="'items' must be a list")
        if len(items) > 100:
            raise HTTPException(status_code=400, detail="Max 100 items")
        save_tracking_watchlist([i for i in items if isinstance(i, dict)])
        return {"ok": True, "count": len(items)}

    @app.post("/tracking/watchlist/{symbol}", dependencies=[Depends(require_auth)])
    async def add_tracking_watchlist_item(symbol: str, request: Request) -> dict[str, Any]:
        code = _normalize_symbol(symbol)
        if not code:
            raise HTTPException(status_code=400, detail="Invalid code (e.g. 000001 or 000001.SZ)")
        name = ""
        try:
            from src.api.position_routes import _get_stock_name
            name = _get_stock_name(code)
        except Exception:  # noqa: BLE001
            logger.debug("stock name lookup failed for %s", code, exc_info=True)
        item = add_to_tracking_watchlist(code, name)
        return {"ok": True, "item": item}

    @app.delete("/tracking/watchlist/{symbol}", dependencies=[Depends(require_auth)])
    async def delete_tracking_watchlist_item(symbol: str, request: Request) -> dict[str, Any]:
        removed = remove_from_tracking_watchlist(symbol)
        return {"ok": removed, "symbol": symbol}

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------
    from src.data import schedule_store

    @app.get("/tracking/tasks", dependencies=[Depends(require_auth)])
    async def list_tracking_tasks(request: Request) -> dict[str, Any]:
        items = schedule_store.load_tasks()
        return {"items": items, "count": len(items)}

    @app.post("/tracking/tasks", dependencies=[Depends(require_auth)])
    async def create_tracking_task(request: Request) -> dict[str, Any]:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Invalid body")
        code = _normalize_symbol(str(body.get("symbol", "")))
        if not code:
            raise HTTPException(status_code=400, detail="Invalid symbol (e.g. 000001 or 000001.SZ)")
        horizon = body.get("horizon", "短线")
        if horizon not in _HORIZONS:
            raise HTTPException(status_code=400, detail=f"horizon must be one of {_HORIZONS}")
        t = str(body.get("time", ""))
        if not _TIME_RE.match(t):
            raise HTTPException(status_code=400, detail="time must be HH:MM (Beijing)")
        enabled = bool(body.get("enabled", True))

        # One task per symbol: if one exists, update it in place.
        existing = schedule_store.get_task_for_symbol(code)
        name = ""
        try:
            from src.api.position_routes import _get_stock_name
            name = _get_stock_name(code)
        except Exception:  # noqa: BLE001
            pass
        task = {
            "task_id": existing["task_id"] if existing else "",
            "symbol": code,
            "name": name,
            "horizon": horizon,
            "time": t,
            "enabled": enabled,
        }
        saved = schedule_store.upsert_task(task)
        return {"ok": True, "task": saved}

    @app.put("/tracking/tasks/{task_id}", dependencies=[Depends(require_auth)])
    async def update_tracking_task(task_id: str, request: Request) -> dict[str, Any]:
        existing = schedule_store.get_task(task_id)
        if not existing:
            raise HTTPException(status_code=404, detail="task not found")
        body = await request.json()
        patch: dict[str, Any] = {"task_id": task_id}
        if "horizon" in body:
            if body["horizon"] not in _HORIZONS:
                raise HTTPException(status_code=400, detail=f"horizon must be one of {_HORIZONS}")
            patch["horizon"] = body["horizon"]
        if "time" in body:
            if not _TIME_RE.match(str(body["time"])):
                raise HTTPException(status_code=400, detail="time must be HH:MM")
            patch["time"] = body["time"]
        if "enabled" in body:
            patch["enabled"] = bool(body["enabled"])
        saved = schedule_store.upsert_task(patch)
        return {"ok": True, "task": saved}

    @app.delete("/tracking/tasks/{task_id}", dependencies=[Depends(require_auth)])
    async def delete_tracking_task(task_id: str, request: Request) -> dict[str, Any]:
        removed = schedule_store.delete_task(task_id)
        return {"ok": removed, "task_id": task_id}

    @app.delete("/tracking/tasks/by-symbol/{symbol}", dependencies=[Depends(require_auth)])
    async def delete_tracking_task_by_symbol(symbol: str, request: Request) -> dict[str, Any]:
        """Delete a task by symbol (used when syncing watchlist removals)."""
        task = schedule_store.get_task_for_symbol(symbol)
        removed = False
        if task:
            removed = schedule_store.delete_task(task["task_id"])
        return {"ok": removed, "symbol": symbol}

    @app.post("/tracking/tasks/{task_id}/run", dependencies=[Depends(require_auth)])
    async def run_tracking_task_now(task_id: str, request: Request) -> dict[str, Any]:
        from src.data.schedule_runner import _run_analysis_for_task

        task = schedule_store.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        loop = asyncio.get_event_loop()
        entry = await loop.run_in_executor(None, _run_analysis_for_task, task)
        return {"ok": True, "run": entry}

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------
    @app.get("/tracking/history/{symbol}", dependencies=[Depends(require_auth)])
    async def get_tracking_history(symbol: str, request: Request) -> dict[str, Any]:
        items = schedule_store.get_history(symbol)
        return {"symbol": symbol, "items": items, "count": len(items)}

    @app.delete("/tracking/history/{symbol}", dependencies=[Depends(require_auth)])
    async def clear_tracking_history(symbol: str, request: Request) -> dict[str, Any]:
        removed = schedule_store.delete_history_for_symbol(symbol)
        return {"ok": True, "symbol": symbol, "removed": removed}
