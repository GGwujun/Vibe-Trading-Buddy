"""Concurrency limiter for outbound market-data calls (tpdog / akshare / mootdx).

A dual-layer semaphore with zombie-permit reclamation, adapted from
TradingAgents-AShare's ``_AkshareLock``:

- **total**: hard cap on concurrent outbound calls (anti-rate-limit / anti-ban).
- **scheduled_max**: an extra cap for background/daemon work so the hot
  foreground path (user requests) always keeps a few slots and is never
  starved by a full-market backfill.
- **zombie reclamation**: a permit held longer than ``stale_timeout`` is
  reclaimed automatically — a daemon thread killed mid-call (no cancel of its
  internal work) would otherwise leak a permit forever.
- **double-release safe**: when a zombie thread finally exits its ``with``
  block, the holder record is already gone, so ``__exit__`` releases nothing.

Usage::

    from src.data.rate_limiter import market_limiter, mark_scheduled

    mark_scheduled(True)   # set in the daemon / backfill entry point
    with market_limiter:
        rows = tpdog_client.call(...)
    # mark_scheduled(False) when the background work is done

A caller that does not set the context var is treated as foreground and only
counts against ``total``.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Context var propagated across asyncio.to_thread / ThreadPoolExecutor calls:
# True inside a background (daemon/backfill) task, False/unset for foreground.
_is_background_task: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "market_is_background_task", default=False
)


def mark_background(value: bool) -> contextvars.Token[bool]:
    """Mark the current context as background (daemon/backfill) or foreground.

    Returns a token the caller can pass to :func:`reset_background` to restore.
    """
    return _is_background_task.set(value)


def reset_background(token: contextvars.Token[bool]) -> None:
    """Restore the prior background/foreground context flag."""
    _is_background_task.reset(token)


def is_background() -> bool:
    return _is_background_task.get()


class MarketLimiter:
    """Dual-layer semaphore limiter with zombie reclamation."""

    def __init__(self, total: int = 5, background_max: int = 3,
                 acquire_timeout: float = 60.0,
                 stale_timeout: float = 120.0) -> None:
        self._total = threading.Semaphore(total)
        self._background = threading.Semaphore(background_max)
        self._holders: dict[int, tuple[float, bool]] = {}  # tid -> (mono, is_bg)
        self._mu = threading.Lock()
        self.acquire_timeout = acquire_timeout
        self.stale_timeout = stale_timeout

    # ── zombie reclamation ──

    def _reclaim_stale(self) -> int:
        """Reclaim permits held past ``stale_timeout``; return count reclaimed."""
        now = time.monotonic()
        reclaimed = 0
        with self._mu:
            stale = [
                (tid, is_bg)
                for tid, (t, is_bg) in self._holders.items()
                if now - t > self.stale_timeout
            ]
            for tid, is_bg in stale:
                del self._holders[tid]
                self._total.release()
                if is_bg:
                    self._background.release()
                reclaimed += 1
        if reclaimed:
            logger.warning(
                "MarketLimiter reclaimed %d stale permits from zombie threads",
                reclaimed,
            )
        return reclaimed

    def _acquire_or_reclaim(self, sem: threading.Semaphore, label: str) -> None:
        if sem.acquire(timeout=self.acquire_timeout):
            return
        self._reclaim_stale()
        if sem.acquire(timeout=10):
            return
        raise TimeoutError(f"market {label} slot acquire timeout after reclaim")

    # ── context manager ──

    def __enter__(self) -> "MarketLimiter":
        is_bg = _is_background_task.get()
        try:
            if is_bg:
                self._acquire_or_reclaim(self._background, "background")
                try:
                    self._acquire_or_reclaim(self._total, "total")
                except BaseException:
                    self._background.release()
                    raise
            else:
                self._acquire_or_reclaim(self._total, "total")
        except TimeoutError:
            logger.error("MarketLimiter acquire timeout (background=%s)", is_bg)
            raise
        with self._mu:
            self._holders[threading.get_ident()] = (time.monotonic(), is_bg)
        return self

    def __exit__(self, *exc_info: object) -> None:
        tid = threading.get_ident()
        with self._mu:
            info = self._holders.pop(tid, None)
        if info is None:
            return  # already reclaimed as a zombie — don't double-release
        _, is_bg = info
        self._total.release()
        if is_bg:
            self._background.release()


# Process-wide limiter for all outbound market-data calls.
market_limiter = MarketLimiter(total=5, background_max=3)
