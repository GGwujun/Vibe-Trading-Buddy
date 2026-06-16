"""Mootdx helper: provide a working TDX server (the built-in list has stale IPs).

We pin known-good servers so every caller (opportunity scanner, fund premium,
OHLCV cache, backtest loader) works without per-callsite changes. The built-in
mootdx server list contains many IPs that are no longer reachable from cloud
hosts (Aliyun ECS in particular), so we maintain our own list and pick the first
server that actually responds — falling through on connection failure instead of
hard-failing on a single dead IP.

Callers should use :func:`get_quotes`, which returns a client pinned to a
verified-reachable server. :func:`pick_server` is exposed for the health check.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Known-working TDX servers verified from Aliyun ECS (2026-06-14).
# The mootdx built-in list contains IPs that are no longer reachable. Ordered by
# observed reliability from CN cloud hosts; callers try them in order and use the
# first that connects.
_WORKING_SERVERS: list[tuple[str, int]] = [
    ("180.153.18.170", 7709),
    ("180.153.18.171", 7709),
    ("180.153.39.51", 7709),
    ("221.231.141.60", 7709),
    ("115.238.56.198", 7709),
    ("115.238.90.165", 7709),
    ("117.184.140.156", 7709),
    ("59.173.18.77", 7709),
]

# Cache the first server that worked so we don't re-probe every call. Stored as
# (server_tuple, expiry_ts); we re-probe at most once every few minutes.
_good_server: tuple[tuple[str, int] | None, float] = (None, 0.0)
_SERVER_TTL = 180.0


def _ping(server: tuple[str, int], timeout: int) -> bool:
    """Open a client against `server` and do a trivial read to confirm liveness."""
    try:
        from mootdx.quotes import Quotes

        client = Quotes.factory(market="std", timeout=timeout, server=server)
        # A trivial market-count call exercises the socket without depending on
        # any specific symbol. markets() returns a list of (name, code, ...).
        _ = client.markets()
        return True
    except Exception as exc:  # noqa: BLE001 — any failure means try next server
        logger.debug("mootdx server %s:%d unreachable: %s", server[0], server[1], exc)
        return False


def pick_server(timeout: int = 6) -> tuple[str, int] | None:
    """Return the first reachable TDX server, or None if all candidates fail.

    Probes servers in order and caches the first that works for ``_SERVER_TTL``
    seconds so repeated calls within a request are cheap.
    """
    global _good_server
    cached, expiry = _good_server
    if cached is not None and time.monotonic() < expiry:
        return cached

    for server in _WORKING_SERVERS:
        if _ping(server, timeout):
            _good_server = (server, time.monotonic() + _SERVER_TTL)
            logger.info("mootdx picked server %s:%d", server[0], server[1])
            return server

    _good_server = (None, 0.0)
    logger.warning("mootdx: no reachable TDX server among %d candidates", len(_WORKING_SERVERS))
    return None


def get_quotes(timeout: int = 15) -> Any:
    """Return a mootdx Quotes client pinned to a reachable server.

    Tries the cached/best server first, then falls through the working list.
    Raises ``RuntimeError`` only if every candidate is unreachable — callers
    that already tolerate mootdx absence should catch this.
    """
    from mootdx.quotes import Quotes

    server = pick_server(timeout=min(timeout, 6))
    if server is not None:
        try:
            return Quotes.factory(market="std", timeout=timeout, server=server)
        except Exception as exc:  # noqa: BLE001 — cached server went bad; fall through
            logger.info("mootdx cached server %s failed to build client: %s — retrying list", server, exc)
            global _good_server
            _good_server = (None, 0.0)

    # Last resort: try each remaining server inline.
    for srv in _WORKING_SERVERS:
        try:
            client = Quotes.factory(market="std", timeout=timeout, server=srv)
            _good_server = (srv, time.monotonic() + _SERVER_TTL)
            return client
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError("mootdx: no reachable TDX server (all candidates failed)")


def last_picked_server() -> tuple[str, int] | None:
    """Return the most recently picked server (for health reporting)."""
    srv, _ = _good_server
    return srv
