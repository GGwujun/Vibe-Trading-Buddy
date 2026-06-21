"""RSSHub multi-source feed aggregation routes.

Folo-style information dashboard for displaying RSSHub feeds from
multiple financial news sources.

Routes:
- GET /rsshub/sources — list available sources with health status
- GET /rsshub/feeds   — aggregated articles from all sources (or filtered by source)

Mounted by ``agent/api_server.py`` via ``register_rsshub_routes(app, ...)``.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache (5 min TTL, same as news_routes)
# ---------------------------------------------------------------------------

_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 300


def _cache_get(key: str) -> dict[str, Any] | None:
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and (now - hit.get("_ts", 0)) < _CACHE_TTL:
            return {k: v for k, v in hit.items() if not k.startswith("_")}
    return None


def _cache_set(key: str, val: dict[str, Any]) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = {**val, "_ts": time.time()}


# ---------------------------------------------------------------------------
# RSSHub sources configuration (from news_feed.py)
# ---------------------------------------------------------------------------

_FEEDS = {
    # RSSHub sources (only working ones)
    "华尔街见闻-全球": "/wallstreetcn/news/global",
    "华尔街见闻-中国": "/wallstreetcn/news/china",
    "华尔街见闻-A股": "/wallstreetcn/news/a-stock",

    # Additional RSSHub routes to test
    "知乎-财经": "/zhihu/topic/19552497",
    "知乎-股票": "/zhihu/topic/19550887",
    "界面新闻": "/jiemian",
    "澎湃新闻": "/thepaper",
}

# Source metadata for display
_SOURCE_META = {
    "华尔街见闻-全球": {"color": "#9C27B0", "priority": 1, "desc": "全球宏观视角"},
    "华尔街见闻-中国": {"color": "#E91E63", "priority": 2, "desc": "中国经济动态"},
    "华尔街见闻-A股": {"color": "#673AB7", "priority": 3, "desc": "A股市场分析"},
    "知乎-财经": {"color": "#00BCD4", "priority": 4, "desc": "财经话题讨论"},
    "知乎-股票": {"color": "#009688", "priority": 5, "desc": "股票投资分享"},
    "界面新闻": {"color": "#FF5722", "priority": 6, "desc": "商业财经报道"},
    "澎湃新闻": {"color": "#795548", "priority": 7, "desc": "时政财经要闻"},
}


# ---------------------------------------------------------------------------
# RSSHub fetching (reuse logic from news_feed.py)
# ---------------------------------------------------------------------------

def _fetch_rsshub_feed(route: str, timeout: int = 10) -> list[dict[str, Any]]:
    """Fetch a single RSSHub route, parse items, return structured articles."""
    import os
    import requests as http
    import xml.etree.ElementTree as ET
    from bs4 import BeautifulSoup

    base = os.getenv("RSSHUB_URL", "http://localhost:1200").rstrip("/")
    url = f"{base}{route}"
    try:
        resp = http.get(url, timeout=timeout)
    except http.RequestException as exc:
        logger.info("rsshub_routes: fetch %s failed: %s", route, exc)
        return []
    if resp.status_code != 200 or not resp.content:
        logger.info("rsshub_routes: fetch %s returned %s", route, resp.status_code)
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        logger.info("rsshub_routes: parse %s failed: %s", route, exc)
        return []

    items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    logger.info("rsshub_routes: route %s found %d items", route, len(items))
    out: list[dict[str, Any]] = []
    for it in items[:20]:
        # 使用 findtext 直接获取文本，更健壮
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        desc_raw = (it.findtext("description") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()

        # 保留原始 HTML 内容，不做解析，前端负责渲染
        # 同时提供纯文本摘要（去除 HTML 标签，用于列表预览）
        soup = BeautifulSoup(desc_raw, "html.parser")
        text_summary = soup.get_text(separator=" ", strip=True)[:200]

        out.append({
            "id": str(uuid.uuid4()),
            "title": title,
            "url": link,
            "snippet": text_summary,  # 纯文本摘要，用于列表
            "content": desc_raw,      # 原始 HTML，用于详情页
            "published": pub,
        })
    return out


def _time_ago(date_str: str) -> str:
    """Convert timestamp to human-readable relative time."""
    if not date_str:
        return ""
    try:
        # Try multiple date formats
        for fmt in ["%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"]:
            try:
                dt = datetime.strptime(date_str.strip(), fmt)
                break
            except ValueError:
                continue
        else:
            return date_str[:16] if len(date_str) > 16 else date_str

        # Make timezone-aware if naive
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        diff = datetime.now(timezone.utc) - dt
        mins = max(0, int(diff.total_seconds() / 60))
        if mins < 60:
            return f"{mins} 分钟前"
        hrs = mins // 60
        if hrs < 24:
            return f"{hrs} 小时前"
        days = hrs // 24
        return f"{days} 天前"
    except Exception:
        return date_str[:16] if len(date_str) > 16 else date_str


def _fetch_all_feeds() -> dict[str, Any]:
    """Fetch all RSSHub sources, aggregate articles, return structured response."""
    sources_info: list[dict[str, Any]] = []
    all_articles: list[dict[str, Any]] = []

    for name, route in _FEEDS.items():
        items = _fetch_rsshub_feed(route)
        healthy = len(items) > 0
        meta = _SOURCE_META.get(name, {})
        sources_info.append({
            "name": name,
            "route": route,
            "count": len(items),
            "healthy": healthy,
            "color": meta.get("color", "#666"),
            "priority": meta.get("priority", 99),
            "desc": meta.get("desc", ""),
        })

        for item in items:
            all_articles.append({
                **item,
                "source": name,
                "source_color": meta.get("color", "#666"),
                "published_ago": _time_ago(item.get("published", "")),
            })

    # Sort by published time (newest first), then by source priority
    all_articles.sort(
        key=lambda a: (a.get("published", ""), -_SOURCE_META.get(a["source"], {}).get("priority", 99)),
        reverse=True
    )

    return {
        "sources": sorted(sources_info, key=lambda s: s["priority"]),
        "articles": all_articles[:100],  # Limit to 100 most recent
        "selected_source": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _fetch_single_feed(source_name: str) -> dict[str, Any]:
    """Fetch a single RSSHub source, return filtered response."""
    route = _FEEDS.get(source_name)
    if not route:
        return {"error": f"Unknown source: {source_name}"}

    # First get all sources info (for sidebar display)
    all_sources: list[dict[str, Any]] = []
    for name, r in _FEEDS.items():
        meta = _SOURCE_META.get(name, {})
        all_sources.append({
            "name": name,
            "route": r,
            "count": 0,  # Will update selected one
            "healthy": True,  # Assume healthy unless proven
            "color": meta.get("color", "#666"),
            "priority": meta.get("priority", 99),
            "desc": meta.get("desc", ""),
        })

    items = _fetch_rsshub_feed(route)
    meta = _SOURCE_META.get(source_name, {})

    # Update the selected source's count
    for s in all_sources:
        if s["name"] == source_name:
            s["count"] = len(items)
            s["healthy"] = len(items) > 0

    articles = [
        {
            **item,
            "source": source_name,
            "source_color": meta.get("color", "#666"),
            "published_ago": _time_ago(item.get("published", "")),
        }
        for item in items
    ]

    return {
        "sources": sorted(all_sources, key=lambda s: s["priority"]),
        "articles": articles,
        "selected_source": source_name,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class RssSource(BaseModel):
    name: str
    route: str
    count: int = 0
    healthy: bool = True
    color: str = "#666"
    priority: int = 99
    desc: str = ""

class RssArticle(BaseModel):
    id: str
    title: str
    url: str
    source: str
    source_color: str = "#666"
    published: str = ""
    published_ago: str = ""
    snippet: str = ""       # 纯文本摘要，用于列表预览
    content: str = ""       # 原始 HTML，用于详情页富文本渲染

class RssFeedResponse(BaseModel):
    sources: list[RssSource]
    articles: list[RssArticle]
    selected_source: str | None = None
    updated_at: str

class RssSourcesResponse(BaseModel):
    sources: list[RssSource]
    updated_at: str


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

AuthDep = Callable[..., Awaitable[Any] | Any]


def register_rsshub_routes(
    app: FastAPI,
    require_auth: AuthDep | None = None,
    require_event_stream_auth: AuthDep | None = None,
) -> None:
    if require_auth is None or require_event_stream_auth is None:
        import sys as _sys
        host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        if host is None:
            raise RuntimeError("register_rsshub_routes: api_server not in sys.modules")
        if require_auth is None:
            require_auth = host.require_auth
        if require_event_stream_auth is None:
            require_event_stream_auth = host.require_event_stream_auth

    @app.get("/rsshub/sources", response_model=RssSourcesResponse, dependencies=[Depends(require_auth)])
    async def list_sources(request: Request) -> dict[str, Any]:
        """List available RSSHub sources with metadata."""
        cache_key = "rsshub_sources"
        cached = _cache_get(cache_key)
        if cached:
            return cached

        # Quick fetch to get counts
        sources_info: list[dict[str, Any]] = []
        for name, route in _FEEDS.items():
            items = _fetch_rsshub_feed(route, timeout=5)
            meta = _SOURCE_META.get(name, {})
            sources_info.append({
                "name": name,
                "route": route,
                "count": len(items),
                "healthy": len(items) > 0,
                "color": meta.get("color", "#666"),
                "priority": meta.get("priority", 99),
                "desc": meta.get("desc", ""),
            })

        result = {
            "sources": sorted(sources_info, key=lambda s: s["priority"]),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _cache_set(cache_key, result)
        return result

    @app.get("/rsshub/feeds", response_model=RssFeedResponse, dependencies=[Depends(require_auth)])
    async def list_feeds(
        request: Request,
        source: str = Query("", max_length=50, description="Filter by source name (e.g. '财联社电报')"),
        limit: int = Query(50, ge=10, le=100, description="Max articles to return"),
    ) -> dict[str, Any]:
        """Get aggregated RSSHub articles from all sources or a specific source."""
        source = source.strip()
        cache_key = f"rsshub_feeds:{source}:{limit}"
        cached = _cache_get(cache_key)
        if cached:
            logger.info("rsshub: returning cached feeds for source=%s", source)
            return cached

        import asyncio
        loop = asyncio.get_event_loop()

        if source:
            result = await loop.run_in_executor(None, _fetch_single_feed, source)
        else:
            result = await loop.run_in_executor(None, _fetch_all_feeds)

        logger.info("rsshub: fetched %d articles for source=%s", len(result.get("articles", [])), source)

        if "error" in result:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail=result["error"])

        result["articles"] = result["articles"][:limit]
        _cache_set(cache_key, result)
        return result