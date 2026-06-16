"""Scheduled-analysis task + history store (~/.vibe-trading/tracking_*.json).

Two JSON files, one logical store:

- ``tracking_tasks.json``   — list of per-stock scheduled tasks.
- ``tracking_history.json`` — ``{symbol: [history_entry, ...]}`` (newest last).

A module-level ``threading.Lock`` guards read-modify-write sequences because
the scheduler runs analysis in a thread executor while HTTP handlers may also
mutate state.

Task schema::

    { task_id, symbol, name, horizon("短线"|"中线"|"长线"),
      time("HH:MM"), enabled(bool), created_at,
      last_run_at(iso|null), last_status("ok"|"error"|null) }

History entry schema::

    { run_id, task_id, symbol, name, horizon, run_at(iso北京),
      status("ok"|"error"), result(trimmed dict|null), error(str|null) }

Atomic write via temp-file + rename, mirroring ``watchlist_store.py``.
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# Beijing time (UTC+8) — matches the convention in ohlcv_cache.py
_CST = timezone(timedelta(hours=8))

# Keep at most this many history entries per symbol (newest kept).
HISTORY_CAP_PER_SYMBOL = 30

# Keys preserved in the trimmed analysis result stored to history.
_RESULT_KEYS = (
    "symbol", "name", "price", "change_pct", "overall_score", "decision",
    "decision_label", "confidence", "stop_loss", "take_profit", "risk_reward",
    "dimensions",
)

_LOCK = threading.Lock()


def _tasks_path() -> Path:
    root = Path.home() / ".vibe-trading"
    root.mkdir(parents=True, exist_ok=True)
    return root / "tracking_tasks.json"


def _history_path() -> Path:
    root = Path.home() / ".vibe-trading"
    root.mkdir(parents=True, exist_ok=True)
    return root / "tracking_history.json"


def _atomic_write(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _new_task_id() -> str:
    return f"ts-{int(datetime.now(_CST).timestamp())}-{secrets.token_hex(3)}"


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def load_tasks() -> list[dict[str, Any]]:
    """Return all scheduled tasks, or an empty list if the file is absent."""
    path = _tasks_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [t for t in data if isinstance(t, dict) and "task_id" in t]
    except (json.JSONDecodeError, OSError):
        pass
    return []


def save_tasks(tasks: list[dict[str, Any]]) -> None:
    _atomic_write(_tasks_path(), tasks)


def upsert_task(task: dict[str, Any]) -> dict[str, Any]:
    """Insert or update a task by ``task_id`` (generating one if missing).

    Returns the stored task dict.
    """
    with _LOCK:
        tasks = load_tasks()
        task_id = task.get("task_id")
        is_new = not task_id
        if is_new:
            task_id = _new_task_id()
            task["task_id"] = task_id
            task.setdefault("created_at", datetime.now(_CST).isoformat())
            task.setdefault("last_run_at", None)
            task.setdefault("last_status", None)
            tasks.append(task)
        else:
            updated = False
            for i, t in enumerate(tasks):
                if t.get("task_id") == task_id:
                    # Merge so callers can pass partial updates.
                    merged = {**t, **{k: v for k, v in task.items() if v is not None}}
                    tasks[i] = merged
                    task = merged
                    updated = True
                    break
            if not updated:
                task.setdefault("created_at", datetime.now(_CST).isoformat())
                task.setdefault("last_run_at", None)
                task.setdefault("last_status", None)
                tasks.append(task)
        save_tasks(tasks)
        return task


def delete_task(task_id: str) -> bool:
    with _LOCK:
        tasks = load_tasks()
        before = len(tasks)
        tasks = [t for t in tasks if t.get("task_id") != task_id]
        if len(tasks) < before:
            save_tasks(tasks)
            return True
        return False


def get_task(task_id: str) -> dict[str, Any] | None:
    for t in load_tasks():
        if t.get("task_id") == task_id:
            return t
    return None


def get_task_for_symbol(symbol: str) -> dict[str, Any] | None:
    for t in load_tasks():
        if t.get("symbol") == symbol:
            return t
    return None


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def load_history() -> dict[str, list[dict[str, Any]]]:
    path = _history_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, list)}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_history(history: dict[str, list[dict[str, Any]]]) -> None:
    _atomic_write(_history_path(), history)


def get_history(symbol: str) -> list[dict[str, Any]]:
    """Return a symbol's history sorted newest-first."""
    entries = load_history().get(symbol, [])
    return sorted(entries, key=lambda e: e.get("run_at", ""), reverse=True)


def append_history(symbol: str, entry: dict[str, Any]) -> None:
    """Append a history entry, trimming to ``HISTORY_CAP_PER_SYMBOL`` newest."""
    with _LOCK:
        history = load_history()
        entries = history.get(symbol, [])
        entries.append(entry)
        # Keep most recent by run_at.
        entries = sorted(entries, key=lambda e: e.get("run_at", ""))
        if len(entries) > HISTORY_CAP_PER_SYMBOL:
            entries = entries[-HISTORY_CAP_PER_SYMBOL:]
        history[symbol] = entries
        save_history(history)


def delete_history_for_symbol(symbol: str) -> int:
    """Clear a symbol's history. Returns the number of entries removed."""
    with _LOCK:
        history = load_history()
        removed = len(history.get(symbol, []))
        if symbol in history:
            del history[symbol]
            save_history(history)
        return removed


def trim_result(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """Reduce a full ``_analyze_symbol`` dict to the trimmed subset stored in history."""
    if not isinstance(result, dict):
        return None
    return {k: result.get(k) for k in _RESULT_KEYS}
