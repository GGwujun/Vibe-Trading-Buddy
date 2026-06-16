"""Data-source health check — ``GET /data-health``.

Each configured upstream (mootdx / akshare / RSSHub / overseas proxy) is probed
with a short timeout and reported as ``{ok, latency_ms, detail}``. This is the
single pane of glass for "why can't the Aliyun deployment pull data": instead of
guessing which source 403'd or timed out, the operator opens the settings page
and sees green/red per source.

All probes are bounded (short timeouts) and never raise — a failing source
returns ``ok=False`` with a short error string, not an HTTP error. The endpoint
is admin-gated (mounted with ``require_admin``).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

import requests
from fastapi import Depends, FastAPI
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = 6  # seconds per source — keep the whole endpoint snappy


class SourceHealth(BaseModel):
    name: str
    ok: bool
    latency_ms: int
    detail: str


class DataHealthReport(BaseModel):
    sources: list[SourceHealth]
    summary_ok: int
    summary_total: int


def _probe(name: str, fn: Callable[[], Any]) -> SourceHealth:
    """Run a probe fn, capture ok/latency/error. Never raises."""
    t0 = time.monotonic()
    try:
        result = fn()
        ms = int((time.monotonic() - t0) * 1000)
        detail = result if isinstance(result, str) else "ok"
        return SourceHealth(name=name, ok=True, latency_ms=ms, detail=detail)
    except Exception as exc:  # noqa: BLE001 — health probes swallow everything
        ms = int((time.monotonic() - t0) * 1000)
        msg = str(exc) or exc.__class__.__name__
        return SourceHealth(name=name, ok=False, latency_ms=ms, detail=msg[:200])


def _probe_mootdx() -> str:
    from src.data.mootdx_helper import pick_server, last_picked_server

    server = pick_server(timeout=_PROBE_TIMEOUT) or last_picked_server()
    if server is None:
        raise RuntimeError("no reachable TDX server (all candidates failed)")
    # Actually exercise the client so "factory built" != "can read quotes".
    from src.data.mootdx_helper import get_quotes

    client = get_quotes(timeout=_PROBE_TIMEOUT)
    markets = client.markets()
    if not markets:
        raise RuntimeError("connected but markets() returned empty")
    return f"server {server[0]}:{server[1]}"


def _probe_akshare() -> str:
    import akshare as ak

    # A cheap, stable call: latest A-share trading calendar / a tiny spot pull.
    df = ak.stock_zh_a_spot_em()
    if df is None or df.empty:
        raise RuntimeError("stock_zh_a_spot_em returned empty")
    return f"{len(df)} A-share rows"


def _probe_rsshub() -> str:
    base = os.getenv("RSSHUB_URL", "http://localhost:1200").rstrip("/")
    resp = requests.get(f"{base}/healthz", timeout=_PROBE_TIMEOUT)
    # Some RSSHub versions expose /healthz, others only /health; accept either.
    if resp.status_code == 404:
        resp = requests.get(f"{base}/health", timeout=_PROBE_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"RSSHub HTTP {resp.status_code}")
    return base


def _probe_overseas_proxy() -> str:
    proxy = os.getenv("OVERSEAS_PROXY_URL", "").strip()
    if not proxy:
        raise RuntimeError("OVERSEAS_PROXY_URL not configured (海外代理未配置)")
    secret = os.getenv("PROXY_SECRET", "").strip()
    resp = requests.get(
        f"{proxy.rstrip('/')}/health",
        headers={"X-Proxy-Key": secret} if secret else {},
        timeout=_PROBE_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"proxy HTTP {resp.status_code}")
    return proxy


def _probe_tpdog() -> str:
    """Probe TPDog (托普量化) HTTPS data source. Skips cleanly when no token."""
    from src.data.tpdog_client import TpdogError, TpdogNotConfiguredError, call

    try:
        # Cheapest live call: 1 积分, 30 次/秒 — current-year trading days.
        call("trading_day/year", year="2026")
        return "token ok"
    except TpdogNotConfiguredError:
        raise RuntimeError("TPDOG_TOKEN 未配置")
    except TpdogError as exc:
        raise RuntimeError(str(exc))


def register_data_health_routes(app: FastAPI) -> None:
    """Mount ``GET /data-health`` (admin only)."""
    from src.api.auth_routes import require_admin

    @app.get(
        "/data-health",
        response_model=DataHealthReport,
        dependencies=[Depends(require_admin)],
    )
    async def data_health() -> DataHealthReport:
        probes = [
            _probe("mootdx (A股行情)", _probe_mootdx),
            _probe("akshare (全球/宏观)", _probe_akshare),
            _probe("RSSHub (新闻聚合)", _probe_rsshub),
            _probe("overseas_proxy (海外源)", _probe_overseas_proxy),
            _probe("tpdog (托普量化)", _probe_tpdog),
        ]
        return DataHealthReport(
            sources=probes,
            summary_ok=sum(1 for p in probes if p.ok),
            summary_total=len(probes),
        )
