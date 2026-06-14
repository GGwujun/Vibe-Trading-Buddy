"""Prediction-market events HTTP routes for the Web UI.

Mounted by ``agent/api_server.py`` via ``register_events_routes(app, ...)``.

Routes (auth via the caller-supplied ``require_auth`` /
``require_event_stream_auth`` dependencies):

- ``GET /events``                               — list live events grouped by category
- ``GET /events/{source}/{event_id}/history``   — probability time series

Data sources (all public, no auth needed):
- Polymarket Gamma API  — primary: geopolitics, crypto, politics, tech
- Kalshi public API     — secondary: economic events
- Polymarket CLOB API   — historical probability time series

Caching: in-memory with 5-min TTL, guarded by ``threading.Lock``.
No persistence — acceptable for a read-only dashboard.

No mock data. If both APIs fail, returns empty categories + error flag.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Overseas proxy support — route blocked foreign APIs through the proxy
# ---------------------------------------------------------------------------

def _proxy_get_json(url: str, timeout: int = 15) -> dict[str, Any] | None:
    """Fetch JSON from a URL via the overseas proxy if configured."""
    import os as _os
    proxy = _os.getenv("OVERSEAS_PROXY_URL", "").strip()
    if not proxy:
        return None
    secret = _os.getenv("PROXY_SECRET", "").strip()
    import requests as _http
    from urllib.parse import quote
    try:
        # URL-encode the target URL so query params go to the target, not the proxy
        resp = _http.get(
            f"{proxy.rstrip('/')}/fetch",
            params={"url": url, "strategy": "json"},
            headers={"X-Proxy-Key": secret} if secret else {},
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "ok" and data.get("content"):
                import json as _json
                return _json.loads(data["content"])
    except Exception as exc:
        logger.info("events proxy fetch failed for %s: %s", url[:60], exc)
    return None


# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------

_EVENTS_CACHE: dict[str, Any] | None = None
_EVENTS_CACHE_TS: float = 0.0
_HISTORY_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_SECONDS = 300  # 5 min

# ---------------------------------------------------------------------------
# API base URLs (all public — no auth)
# ---------------------------------------------------------------------------

_POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
_POLYMARKET_CLOB = "https://clob.polymarket.com"
_KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"
_API_TIMEOUT = 15.0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_volume_raw(vol: str) -> float:
    """Parse human-friendly volume string → raw dollars."""
    v = vol.replace("$", "").replace(",", "").strip()
    if not v:
        return 0.0
    if v.endswith("M"):
        return float(v[:-1]) * 1_000_000
    if v.endswith("K"):
        return float(v[:-1]) * 1_000
    return float(v)


def _fmt_volume(dollars: float) -> str:
    """Format dollar volume → human-friendly."""
    if dollars >= 1_000_000:
        return f"${dollars / 1_000_000:.1f}M"
    if dollars >= 1_000:
        return f"${dollars / 1_000:.0f}K"
    return f"${dollars:.0f}"


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Parse float without raising."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_json_array(raw: Any) -> list[str]:
    """Parse a JSON-stringified array from Polymarket."""
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str) and raw.strip():
        try:
            return [str(x) for x in json.loads(raw)]
        except (json.JSONDecodeError, TypeError):
            pass
    return []


# ---------------------------------------------------------------------------
# Category mapping from Polymarket tags
# ---------------------------------------------------------------------------

# Tag → category id mapping. First match wins.
_TAG_TO_CATEGORY: dict[str, str] = {
    # Geopolitical
    "geopolitics": "geopolitical",
    "war": "geopolitical",
    "military": "geopolitical",
    "nato": "geopolitical",
    "ukraine": "geopolitical",
    "russia": "geopolitical",
    "china": "geopolitical",
    "iran": "geopolitical",
    "israel": "geopolitical",
    "sanctions": "geopolitical",
    # Politics
    "politics": "politics",
    "election": "politics",
    "president": "politics",
    "congress": "politics",
    "senate": "politics",
    "governor": "politics",
    "uk": "politics",
    "france": "politics",
    "germany": "politics",
    "eu": "politics",
    # Crypto / Finance
    "crypto": "crypto",
    "bitcoin": "crypto",
    "ethereum": "crypto",
    "defi": "crypto",
    "finance": "crypto",
    "business": "crypto",
    "stock": "crypto",
    "market": "crypto",
    "ipo": "crypto",
    "economy": "crypto",
    # Tech / Science
    "tech": "tech",
    "ai": "tech",
    "science": "tech",
    "space": "tech",
    "climate": "tech",
    # World / other interesting events
    "world": "world",
    "sports": "world",
    "entertainment": "world",
}

CATEGORY_LABELS: dict[str, str] = {
    "geopolitical": "地缘政治",
    "politics": "政治选举",
    "crypto": "加密与金融",
    "tech": "科技",
    "world": "全球事件",
}


def _categorize_polymarket(tags: list[str]) -> str:
    """Map Polymarket tags to our category id."""
    if not tags:
        return "world"
    for tag in tags:
        tag_lower = tag.strip().lower()
        if tag_lower in _TAG_TO_CATEGORY:
            return _TAG_TO_CATEGORY[tag_lower]
    return "world"


# ---------------------------------------------------------------------------
# Polymarket Gamma API
# ---------------------------------------------------------------------------


async def _fetch_polymarket_events(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Fetch active events from Polymarket Gamma API.

    Returns flat list of event dicts with embedded market data.
    Tries direct first, falls back to overseas proxy (Polymarket is blocked from CN).
    """
    url = f"{_POLYMARKET_GAMMA}/events"
    params = {
        "active": "true",
        "closed": "false",
        "limit": "80",
        "volume_min": "5000",
    }
    raw_events = None
    # Try direct
    try:
        resp = await client.get(url, params=params, timeout=_API_TIMEOUT)
        resp.raise_for_status()
        raw_events = resp.json()
    except Exception as exc:
        logger.info("Polymarket direct failed: %s, trying proxy...", exc)
    # Try proxy
    if raw_events is None:
        import asyncio
        raw_events = await asyncio.to_thread(_proxy_get_json, url + "?active=true&closed=false&limit=80&volume_min=5000", _API_TIMEOUT)
    if raw_events is None:
        raise RuntimeError("Polymarket unavailable (direct + proxy both failed)")

    results: list[dict[str, Any]] = []
    for evt in raw_events:
        evt_title = evt.get("title", "Unknown")
        evt_tags = [t.get("label", "") if isinstance(t, dict) else str(t) for t in evt.get("tags", [])]
        category = _categorize_polymarket(evt_tags)
        end_date = (evt.get("endDate") or evt.get("closeTime") or "")[:10]

        for mkt in evt.get("markets", []):
            if not mkt.get("active", True) or mkt.get("closed", False):
                continue

            outcome_prices = _parse_json_array(mkt.get("outcomePrices"))
            # outcomePrices[0] = Yes price
            yes_price = _safe_float(outcome_prices[0]) if len(outcome_prices) > 0 else 0.0
            volume_24h = _safe_float(mkt.get("volume24hr"))
            total_volume = _safe_float(mkt.get("volume"))
            one_day_change = _safe_float(mkt.get("oneDayPriceChange"))

            # Get CLOB token IDs for history lookups
            token_ids = _parse_json_array(mkt.get("clobTokenIds"))

            if yes_price <= 0 and total_volume < 1000:
                continue  # skip dead markets

            mkt_title = mkt.get("question") or evt_title
            results.append({
                "id": f"poly_{mkt.get('id', '')}",
                "title": mkt_title[:120],
                "threshold": "—",
                "probability": round(yes_price, 4),
                "prob_change_24h": round(one_day_change, 4),
                "volume": _fmt_volume(volume_24h if volume_24h > 0 else total_volume),
                "resolve_time": (mkt.get("endDate") or end_date)[:10],
                "source": "polymarket",
                "_category": category,
                "_volume_raw": total_volume,
                "_token_id": token_ids[0] if token_ids else "",
            })

    # Sort by volume descending within each category
    results.sort(key=lambda e: e["_volume_raw"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Kalshi API
# ---------------------------------------------------------------------------

# Kalshi events are sparse for near-term economic data, so we use broader
# matching and lower volume thresholds than Polymarket.
_KALSHI_ECONOMIC_KEYWORDS = [
    "cpi", "inflation", "gdp", "recession", "unemployment", "fed ",
    "interest rate", "treasury", "oil", "gasoline", "energy",
    "stock market", "s&p 500", "nasdaq", "dow jones",
    "tariff", "trade", "budget", "deficit", "debt ceiling",
    "housing", "mortgage", "consumer", "earnings", "revenue",
    "us dollar", "bond yield", "commodity", "supply chain",
    "wage", "jobs", "payroll", "economic growth",
    "sanctions", "export", "import",
    "carbon", "emission", "climate",
    "economy", "economic", "fiscal", "monetary",
    "tax", "regulation", "pmi", "retail sales",
    "gold price", "silver", "copper", "steel",
    "bitcoin", "btc", "ethereum", "crypto",
]

_KALSHI_BLACKLIST = [
    "james bond", "bond song", "bond girl", "bond movie",
    "bond actor", "bond film", "pop culture",
]


def _is_kalshi_economic(title: str, threshold: str) -> bool:
    text = f"{title} {threshold}".lower()
    if any(bl in text for bl in _KALSHI_BLACKLIST):
        return False
    return any(kw in text for kw in _KALSHI_ECONOMIC_KEYWORDS)


async def _fetch_kalshi_events(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Fetch economic events from Kalshi public API."""
    url = f"{_KALSHI_BASE}/events"
    params = {"status": "open", "with_nested_markets": "true", "limit": "50"}
    resp = await client.get(url, params=params, timeout=_API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    events: list[dict[str, Any]] = []
    for evt in data.get("events", []):
        for mkt in evt.get("markets", []):
            if mkt.get("status") != "active":
                continue
            mkt_title = mkt.get("title", "") or evt.get("title", "")
            sub_title = mkt.get("sub_title", "") or "—"
            if not _is_kalshi_economic(mkt_title, sub_title):
                continue

            last_price = _safe_float(mkt.get("last_price_dollars"))
            prev_price = _safe_float(mkt.get("previous_price_dollars"), last_price)
            volume_24h = _safe_float(mkt.get("volume_24h_fp"))

            events.append({
                "id": f"kalshi_{mkt.get('ticker', '')}",
                "title": mkt_title[:120],
                "threshold": sub_title[:60],
                "probability": round(last_price, 4),
                "prob_change_24h": round(last_price - prev_price, 4),
                "volume": _fmt_volume(volume_24h),
                "resolve_time": (mkt.get("close_time") or "")[:10],
                "source": "kalshi",
                "_category": "crypto",  # Put Kalshi economic events in Crypto & Finance
                "_volume_raw": volume_24h,
                "_ticker": mkt.get("ticker", ""),
                "_series_ticker": evt.get("series_ticker", ""),
            })

    events.sort(key=lambda e: e["_volume_raw"], reverse=True)
    return events


# ---------------------------------------------------------------------------
# Polymarket CLOB — history
# ---------------------------------------------------------------------------


async def _fetch_polymarket_history(client: httpx.AsyncClient, token_id: str) -> list[dict[str, Any]]:
    """Fetch probability time series from Polymarket CLOB."""
    url = f"{_POLYMARKET_CLOB}/prices-history"
    params = {"market": token_id, "interval": "1d"}
    resp = await client.get(url, params=params, timeout=_API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    history = data.get("history", [])
    return [
        {
            "time": datetime.fromtimestamp(entry["t"], tz=timezone.utc).isoformat(),
            "probability": round(_safe_float(entry.get("p", 0)), 4),
        }
        for entry in history
    ]


# ---------------------------------------------------------------------------
# Build the events payload
# ---------------------------------------------------------------------------


async def _build_events_payload() -> dict[str, Any]:
    """Fetch live events from Polymarket + Kalshi, group by category."""
    errors: list[str] = []
    all_events: list[dict[str, Any]] = []

    # --- Polymarket (primary) ---
    try:
        async with httpx.AsyncClient() as client:
            pm_events = await _fetch_polymarket_events(client)
            all_events.extend(pm_events)
            logger.info("Polymarket: %d events fetched", len(pm_events))
    except Exception as exc:
        msg = f"Polymarket API unavailable: {exc}"
        logger.warning(msg)
        errors.append(msg)

    # --- Kalshi (secondary, economic) ---
    try:
        async with httpx.AsyncClient() as client:
            k_events = await _fetch_kalshi_events(client)
            all_events.extend(k_events)
            logger.info("Kalshi: %d economic events fetched", len(k_events))
    except Exception as exc:
        msg = f"Kalshi API unavailable: {exc}"
        logger.warning(msg)
        errors.append(msg)

    # --- Group by category ---
    categories_map: dict[str, list[dict[str, Any]]] = {}
    for e in all_events:
        cat = e.pop("_category", "world")
        categories_map.setdefault(cat, []).append(e)

    # Build ordered category list, cap each at top N by volume
    _MAX_PER_CATEGORY = 30
    category_order = ["geopolitical", "politics", "crypto", "tech", "world"]
    categories: list[dict[str, Any]] = []
    for cat_id in category_order:
        events = categories_map.get(cat_id, [])
        if not events:
            continue
        # Already sorted by volume from the fetchers — just take top N
        top_events = events[:_MAX_PER_CATEGORY]
        # Keep _token_id for history lookups, strip other internal fields
        clean_events: list[dict[str, Any]] = []
        for e in top_events:
            clean = {k: v for k, v in e.items() if not k.startswith("_") or k == "_token_id"}
            clean_events.append(clean)
        categories.append({
            "id": cat_id,
            "label": CATEGORY_LABELS.get(cat_id, cat_id.title()),
            "source": "Polymarket" if any(e["source"] == "polymarket" for e in events) else "Polymarket + Kalshi",
            "events": clean_events,
        })

    return {
        "categories": categories,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "errors": errors if errors else None,
    }


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class EventItem(BaseModel):
    id: str
    title: str
    threshold: str = "—"
    probability: float
    prob_change_24h: float = 0.0
    volume: str = ""
    resolve_time: str = ""
    source: str


class EventsCategory(BaseModel):
    id: str
    label: str
    source: str = ""
    events: list[dict[str, Any]] = Field(default_factory=list)


class EventsListResponse(BaseModel):
    categories: list[dict[str, Any]]
    updated_at: str
    errors: list[str] | None = None


class HistoryPoint(BaseModel):
    time: str
    probability: float


class EventHistoryResponse(BaseModel):
    event_id: str
    source: str
    history: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

AuthDep = Callable[..., Awaitable[Any] | Any]


def register_events_routes(
    app: FastAPI,
    require_auth: AuthDep | None = None,
    require_event_stream_auth: AuthDep | None = None,
) -> None:
    """Mount the events routes onto ``app``."""
    if require_auth is None or require_event_stream_auth is None:
        import sys as _sys

        host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        if host is None:
            raise RuntimeError(
                "register_events_routes: api_server module not in sys.modules; "
                "pass require_auth/require_event_stream_auth explicitly"
            )
        if require_auth is None:
            require_auth = host.require_auth
        if require_event_stream_auth is None:
            require_event_stream_auth = host.require_event_stream_auth

    _EVENT_ID_RE = __import__("re").compile(r"^[a-z_][a-z0-9_:-]{1,256}$")

    @app.get("/events", response_model=EventsListResponse, dependencies=[Depends(require_auth)])
    async def list_events(request: Request) -> dict[str, Any]:
        global _EVENTS_CACHE, _EVENTS_CACHE_TS

        now = time.time()
        with _CACHE_LOCK:
            if _EVENTS_CACHE is not None and (now - _EVENTS_CACHE_TS) < _CACHE_TTL_SECONDS:
                return _EVENTS_CACHE

        payload = await _build_events_payload()

        with _CACHE_LOCK:
            _EVENTS_CACHE = payload
            _EVENTS_CACHE_TS = time.time()

        return payload

    @app.get(
        "/events/{source}/{event_id}/history",
        response_model=EventHistoryResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_event_history(
        source: str,
        event_id: str,
        request: Request,
        days: int = Query(90, ge=1, le=365),
    ) -> dict[str, Any]:
        if not _EVENT_ID_RE.match(event_id):
            raise HTTPException(status_code=400, detail="invalid event_id")
        if source not in ("kalshi", "polymarket"):
            raise HTTPException(status_code=400, detail="source must be kalshi or polymarket")

        cache_key = f"{source}:{event_id}:{days}"
        now = time.time()
        with _CACHE_LOCK:
            cached = _HISTORY_CACHE.get(cache_key)
            if cached is not None and (now - cached.get("_ts", 0)) < _CACHE_TTL_SECONDS:
                return {"event_id": event_id, "source": source, "history": cached["history"]}

        history: list[dict[str, Any]] = []
        error: str | None = None

        if source == "polymarket":
            try:
                # event_id format: "poly_<market_id>" — we need to extract the
                # CLOB token ID. For v1, loop through the events cache to find it.
                token_id = ""
                with _CACHE_LOCK:
                    if _EVENTS_CACHE:
                        for cat in _EVENTS_CACHE.get("categories", []):
                            for e in cat.get("events", []):
                                if e.get("id") == event_id and e.get("source") == "polymarket":
                                    token_id = e.get("_token_id", "")
                                    break
                if token_id:
                    async with httpx.AsyncClient() as client:
                        history = await _fetch_polymarket_history(client, token_id)
                else:
                    error = "Token ID not found in cache — refresh /events first"
            except Exception as exc:
                logger.warning("Polymarket history fetch failed for %s: %s", event_id, exc)
                error = f"History unavailable: {exc}"
        else:
            # Kalshi history — not implemented in v1
            error = "Kalshi history not yet available — use Polymarket events"

        if days < len(history):
            history = history[-days:]

        payload = {"event_id": event_id, "source": source, "history": history}
        if error and not history:
            payload["error"] = error

        with _CACHE_LOCK:
            _HISTORY_CACHE[cache_key] = {"history": history, "_ts": time.time()}

        return payload
