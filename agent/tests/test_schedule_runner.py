from __future__ import annotations

from datetime import datetime

from src.data import schedule_runner as runner


def _cst(when: str) -> datetime:
    return datetime.fromisoformat(when).replace(tzinfo=runner._CST)


def test_task_due_catches_up_after_scheduled_time() -> None:
    task = {
        "task_id": "t1",
        "symbol": "600000.SH",
        "time": "15:05",
        "enabled": True,
        "created_at": "2026-07-01T10:00:00+08:00",
        "last_run_at": "2026-07-01T15:05:10+08:00",
    }

    assert runner._task_due_for_now(task, _cst("2026-07-02T15:20:00")) is True


def test_task_due_skips_before_scheduled_time() -> None:
    task = {
        "task_id": "t1",
        "symbol": "600000.SH",
        "time": "15:05",
        "enabled": True,
        "created_at": "2026-07-01T10:00:00+08:00",
    }

    assert runner._task_due_for_now(task, _cst("2026-07-02T15:04:59")) is False


def test_task_due_skips_when_already_ran_today() -> None:
    task = {
        "task_id": "t1",
        "symbol": "600000.SH",
        "time": "15:05",
        "enabled": True,
        "created_at": "2026-07-01T10:00:00+08:00",
        "last_run_at": "2026-07-02T15:10:00+08:00",
    }

    assert runner._task_due_for_now(task, _cst("2026-07-02T16:00:00")) is False


def test_task_due_skips_same_day_task_created_after_scheduled_time() -> None:
    task = {
        "task_id": "t1",
        "symbol": "600000.SH",
        "time": "15:05",
        "enabled": True,
        "created_at": "2026-07-02T16:00:00+08:00",
    }

    assert runner._task_due_for_now(task, _cst("2026-07-02T16:01:00")) is False
