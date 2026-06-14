"""RSSHub-backed news aggregation (Tier 3 of the info chain).

RSSHub turns any financial site into a structured RSS feed. We subscribe to
several A-share-relevant routes and return normalized items. More stable and
broader than ad-hoc web_search scraping.

RSSHub instance is configured via env ``RSSHUB_URL`` (default
http://rsshub:1200 inside docker-compose, http://localhost:1200 locally).

Routes used:
  - /eastmoney/news/{channel}   — 东方财富
  - /cls/telegraph              — 财联社电报
  - /xueqiu/hots                — 雪球热帖
  - /10jqka/{channel}           — 同花顺

All functions return plain dicts/lists, never raise.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any

import requests

logger = logging.getLogger(__name__)

_CACHE_TTL = 300
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> Any | None:
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (time.time() - hit[0]) < _CACHE_TTL:
            return hit[1]
    return None


def _cache_set(key: str, val: Any) -> None:
    with _cache_lock:
        _cache[key] = (time.time(), val)


def _rsshub_base() -> str:
    return os.getenv("RSSHUB_URL", "http://localhost:1200").rstrip("/")


def _fetch_feed(route: str, timeout: int = 10) -> list[dict[str, Any]]:
    """Fetch an RSSHub route and parse items. Returns [] on any failure."""
    url = f"{_rsshub_base()}{route}"
    try:
        resp = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        logger.info("news_feed: fetch %s failed: %s", route, exc)
        return []
    if resp.status_code != 200 or not resp.content:
        return []
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        logger.info("news_feed: parse %s failed: %s", route, exc)
        return []

    # RSS 2.0: channel/item ; Atom: feed/entry
    items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    out: list[dict[str, Any]] = []
    for it in items[:20]:
        def _txt(tag: str) -> str:
            node = it.find(tag) or it.find(f"{{http://www.w3.org/2005/Atom}}{tag}")
            return (node.text or "").strip() if node is not None and node.text else ""
        out.append({
            "title": _txt("title"),
            "link": _txt("link"),
            "description": _txt("description")[:200],
            "pub_date": _txt("pubDate"),
        })
    return out


# Curated routes for A-share + international context. Keys are display names.
# Routes follow RSSHub official docs; some may be unavailable depending on the
# RSSHub version / source anti-scraping — failed feeds return [] silently.
_FEEDS = {
    "财联社电报": "/cls/telegraph",
    "东方财富-财经": "/eastmoney/news/cjpl",
    "雪球热帖": "/xueqiu/hots",
    "同花顺-财经": "/10jqka/cjzx",
    "华尔街见闻": "/wallstreetcn/news/global",
    "新浪财经": "/sina/finance",
    "金十数据-快讯": "/jin10/",
}


def get_news(limit_per_feed: int = 15) -> dict[str, Any]:
    """Aggregate news from all configured RSSHub feeds.

    Returns {feed_name: [items]} with a flat `all` list merged + deduped.
    """
    cache_key = f"news:{limit_per_feed}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    result: dict[str, Any] = {"feeds": {}, "all": []}
    seen_titles: set[str] = set()
    for name, route in _FEEDS.items():
        items = _fetch_feed(route)[:limit_per_feed]
        result["feeds"][name] = items
        for it in items:
            t = it["title"]
            if t and t not in seen_titles:
                seen_titles.add(t)
                result["all"].append({"source": name, **it})
    _cache_set(cache_key, result)
    return result


def get_stock_news(stock_name: str, limit: int = 20) -> list[dict[str, Any]]:
    """Best-effort: search aggregated news titles for a stock name/keyword."""
    all_news = get_news().get("all", [])
    kw = (stock_name or "").strip()
    if not kw:
        return all_news[:limit]
    matched = [n for n in all_news if kw in n.get("title", "") or kw in n.get("description", "")]
    return (matched or all_news)[:limit]
