"""Background notification scheduler.

The settings page already stores per-platform push windows. This module turns
those stored settings into an opt-in background loop that sends the same real
market digest used by ``/notify/test``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Iterator

from src.notify.models import NotifyConfig, PlatformConfig
from src.notify.sender import send
from src.notify.store import load_config
from src.notify.summary import build_summary

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))
_TICK_SECONDS = 30
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_FIRED_KEYS: set[str] = set()

_SLOTS: tuple[tuple[str, str, str], ...] = (
    ("pre_market", "pre_market_enabled", "pre_market_time"),
    ("after_close", "after_close_enabled", "after_close_time"),
    ("custom", "custom_enabled", "custom_time"),
)


def _now_cst() -> datetime:
    return datetime.now(_CST)


def _normalize_time(value: str) -> str | None:
    text = (value or "").strip()
    return text if _TIME_RE.match(text) else None


def _iter_due_pushes(
    cfg: NotifyConfig,
    now: datetime,
) -> Iterator[tuple[str, str, PlatformConfig]]:
    """Yield ``(platform, slot, platform_config)`` entries due at ``now``."""

    hhmm = now.astimezone(_CST).strftime("%H:%M")
    for platform in ("feishu", "dingtalk", "wechat"):
        platform_cfg = getattr(cfg, platform)
        if not platform_cfg.enabled or not platform_cfg.webhook_url:
            continue
        for slot, enabled_attr, time_attr in _SLOTS:
            if not bool(getattr(platform_cfg, enabled_attr, False)):
                continue
            configured_time = _normalize_time(str(getattr(platform_cfg, time_attr, "")))
            if configured_time == hhmm:
                yield platform, slot, platform_cfg


def _push_key(now: datetime, platform: str, slot: str) -> str:
    day = now.astimezone(_CST).strftime("%Y-%m-%d")
    return f"{day}|{platform}|{slot}"


def _gc_fired_keys(today: str) -> None:
    if any(not key.startswith(today) for key in _FIRED_KEYS):
        _FIRED_KEYS.difference_update(key for key in list(_FIRED_KEYS) if not key.startswith(today))


def send_due_notifications(
    cfg: NotifyConfig | None = None,
    now: datetime | None = None,
) -> list[dict[str, str | bool]]:
    """Send all configured notifications due at ``now``.

    Returns a small result list for logging/tests. The function is synchronous
    because the underlying webhook sender uses ``requests``; callers should run
    it in a thread executor from async contexts.
    """

    current = now or _now_cst()
    today = current.astimezone(_CST).strftime("%Y-%m-%d")
    _gc_fired_keys(today)
    cfg = cfg or load_config()

    results: list[dict[str, str | bool]] = []
    due = list(_iter_due_pushes(cfg, current))
    if not due:
        return results

    title, markdown = build_summary()
    for platform, slot, platform_cfg in due:
        key = _push_key(current, platform, slot)
        if key in _FIRED_KEYS:
            continue
        _FIRED_KEYS.add(key)
        push_title = f"{title} [{slot}]"
        ok, message = send(platform, platform_cfg, push_title, markdown)
        logger.info("notify scheduled push platform=%s slot=%s ok=%s message=%s", platform, slot, ok, message)
        results.append({"platform": platform, "slot": slot, "ok": ok, "message": message})
    return results


async def run_notify_scheduler_loop() -> None:
    """Background loop for configured webhook pushes."""

    logger.info("notify scheduler loop started")
    while True:
        try:
            await asyncio.sleep(_TICK_SECONDS)
            await asyncio.get_running_loop().run_in_executor(None, send_due_notifications)
        except asyncio.CancelledError:
            logger.info("notify scheduler loop cancelled")
            break
        except Exception:
            logger.exception("notify scheduler loop iteration failed")
            await asyncio.sleep(300)


def notify_scheduler_enabled() -> bool:
    return os.getenv("NOTIFY_AUTORUN", "1").strip().lower() not in {"0", "false", "no"}
