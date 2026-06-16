"""Tests for the dual-layer market-call rate limiter."""

from __future__ import annotations

import threading
import time

import pytest

from src.data import rate_limiter as rl
from src.data.rate_limiter import MarketLimiter


def test_foreground_uses_only_total_slot() -> None:
    """A foreground (non-background) caller takes only the total semaphore."""
    lim = MarketLimiter(total=2, background_max=1, stale_timeout=999)
    with lim:
        # total has 1 left, background still full (1).
        assert lim._total._value == 1
        assert lim._background._value == 1


def test_background_takes_both_slots() -> None:
    lim = MarketLimiter(total=2, background_max=1, stale_timeout=999)
    rl.mark_background(True)
    try:
        with lim:
            assert lim._total._value == 1
            assert lim._background._value == 0  # background slot consumed
    finally:
        rl._is_background_task.set(False)
    # released
    assert lim._total._value == 2
    assert lim._background._value == 1


def test_double_release_is_safe_after_reclaim() -> None:
    """A zombie thread whose permit was reclaimed must not release on exit."""
    lim = MarketLimiter(total=1, background_max=1, stale_timeout=0.05)
    holder_done = threading.Event()

    def hold() -> None:
        with lim:
            holder_done.set()
            time.sleep(0.3)  # exceed stale_timeout → permit reclaimed

    t = threading.Thread(target=hold)
    t.start()
    holder_done.wait()
    time.sleep(0.15)  # let reclamation window pass

    # A second caller triggers reclaim internally and gets the slot.
    lim2_ok = []

    def grab() -> None:
        with lim:
            lim2_ok.append(True)

    t2 = threading.Thread(target=grab)
    t2.start()
    t2.join(timeout=20)
    assert lim2_ok  # second caller acquired despite the zombie still "inside"

    t.join()
    # After the zombie finally exits, no double-release: total back to 1.
    assert lim._total._value == 1


def test_permit_released_on_exception() -> None:
    lim = MarketLimiter(total=1, background_max=1, stale_timeout=999)
    with pytest.raises(RuntimeError):
        with lim:
            raise RuntimeError("boom")
    assert lim._total._value == 1  # released despite the exception
