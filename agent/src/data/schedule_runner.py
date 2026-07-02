"""Scheduled-analysis runner + asyncio loop.

``_run_analysis_for_task`` is the shared runner used by BOTH the manual
"run now" endpoint and the automatic scheduler. It is synchronous (does I/O
via mootdx) and MUST be invoked via ``loop.run_in_executor`` to avoid
blocking the event loop.

``run_scheduler_loop`` ticks every 30 seconds, finds enabled tasks whose
Beijing-time ``HH:MM`` matches the current minute (and not already fired
this minute), and dispatches each to the thread executor. Missed runs while
the server was down are NOT caught up — the loop simply waits for the next
matching minute.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, time, timedelta, timezone

from src.data import schedule_store

logger = logging.getLogger(__name__)

# Beijing time (UTC+8) — matches the market-data scheduler convention.
_CST = timezone(timedelta(hours=8))

_TICK_SECONDS = 30
try:
    _MAX_RUNS_PER_TICK = max(1, int(os.getenv("SCHEDULED_ANALYSIS_MAX_RUNS_PER_TICK", "1")))
except ValueError:
    _MAX_RUNS_PER_TICK = 1
# In-memory dedupe: keys like "2026-06-15 14:30|000001.SZ". Reset each day.
_FIRED_KEYS: set[str] = set()


def _parse_hhmm(value: str | None) -> time | None:
    try:
        hh, mm = str(value or "").split(":", 1)
        return time(int(hh), int(mm))
    except Exception:
        return None


def _iso_date(value: str | None) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(_CST).strftime("%Y-%m-%d")
    except Exception:
        return raw[:10] if len(raw) >= 10 else None


def _iso_time(value: str | None) -> time | None:
    if not value:
        return None
    raw = str(value).strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(_CST).time()
    except Exception:
        return None


def _task_due_for_now(task: dict, now_cst: datetime) -> bool:
    """Return whether a task should fire once for today's scheduled time.

    The old scheduler only fired during the exact HH:MM minute. That made
    daily analysis easy to miss after restarts or short outages. This predicate
    catches up once after the scheduled time has passed, while avoiding an
    immediate same-day run for a task created after its scheduled time.
    """
    if not task.get("enabled", False):
        return False
    scheduled_time = _parse_hhmm(task.get("time"))
    if scheduled_time is None:
        return False
    today = now_cst.strftime("%Y-%m-%d")
    if now_cst.time().replace(second=0, microsecond=0) < scheduled_time:
        return False
    if _iso_date(task.get("last_run_at")) == today:
        return False

    created_date = _iso_date(task.get("created_at"))
    created_time = _iso_time(task.get("created_at"))
    if created_date == today and created_time and created_time > scheduled_time:
        return False
    return True


def _run_analysis_for_task(task: dict) -> dict:
    """Run rule-engine analysis for one task; write history; update task.

    Synchronous — call inside ``loop.run_in_executor``. Returns the history
    entry that was written.
    """
    from src.api.position_routes import _analyze_symbol, _fetch_a_share_data

    symbol = task["symbol"]
    now_cst = datetime.now(_CST)
    run_id = f"run-{int(now_cst.timestamp())}-{symbol.replace('.', '')}"
    entry = {
        "run_id": run_id,
        "task_id": task.get("task_id", ""),
        "symbol": symbol,
        "name": task.get("name", ""),
        "horizon": task.get("horizon", "短线"),
        "run_at": now_cst.isoformat(),
        "status": "ok",
        "result": None,
        "error": None,
    }
    try:
        data = _fetch_a_share_data([symbol], days=90)
        df = data.get(symbol)
        if df is None or df.empty:
            raise RuntimeError("no market data available")
        result = _analyze_symbol(symbol, df)
        entry["result"] = schedule_store.trim_result(result)
    except Exception as exc:  # noqa: BLE001 — scheduler must never raise
        logger.warning("scheduled analysis failed for %s: %s", symbol, exc)
        entry["status"] = "error"
        entry["error"] = str(exc)[:200]

    schedule_store.append_history(symbol, entry)
    # Update the task's last-run bookkeeping (merge so we don't clobber fields).
    schedule_store.upsert_task({
        "task_id": task.get("task_id"),
        "symbol": symbol,
        "name": entry["name"],
        "last_run_at": now_cst.isoformat(),
        "last_status": entry["status"],
    })
    logger.info(
        "scheduled analysis %s for %s (%s)", entry["status"], symbol, entry["run_at"],
    )
    return entry


async def run_scheduler_loop() -> None:
    """Background loop: fire enabled tasks whose Beijing HH:MM is due."""
    logger.info("scheduled-analysis loop started")
    while True:
        try:
            await asyncio.sleep(_TICK_SECONDS)
            now_cst = datetime.now(_CST)
            today = now_cst.strftime("%Y-%m-%d")

            # GC fired keys from prior days.
            if any(not k.startswith(today) for k in _FIRED_KEYS):
                _FIRED_KEYS.difference_update(k for k in list(_FIRED_KEYS) if not k.startswith(today))

            loop = asyncio.get_running_loop()
            dispatched = 0
            for task in schedule_store.load_tasks():
                if dispatched >= _MAX_RUNS_PER_TICK:
                    break
                if not _task_due_for_now(task, now_cst):
                    continue
                symbol = task.get("symbol", "")
                key = f"{today}|{task.get('task_id') or symbol}"
                if key in _FIRED_KEYS:
                    continue
                _FIRED_KEYS.add(key)
                logger.info("scheduled-analysis dispatching %s for %s", task.get("time"), symbol)
                await loop.run_in_executor(None, _run_analysis_for_task, task)
                dispatched += 1
        except asyncio.CancelledError:
            logger.info("scheduled-analysis loop cancelled")
            break
        except Exception:  # noqa: BLE001 — never let the loop die
            logger.exception("scheduled-analysis loop iteration failed")
