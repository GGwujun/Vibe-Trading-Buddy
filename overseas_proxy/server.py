"""Overseas info proxy — lightweight FastAPI that fetches foreign websites /
searches for the CN-domiciled main service.

Deployed on the overseas (non-CN) server. The CN main service calls it when
read_url/web_search need to reach sites that are slow/blocked from CN
(Yahoo Finance, Reuters, DuckDuckGo, ...). From overseas those sites are fast.

Auth: a shared secret in the ``X-Proxy-Key`` header, compared against the
``PROXY_SECRET`` env var. No secret → 403. Keep this behind a firewall that
only allows the CN server's IP.

Endpoints:
  GET /fetch?url=&strategy=raw|jina   — fetch a foreign page → text/markdown
  GET /search?q=&max=                 — DuckDuckGo search → structured results
  GET /health                         — liveness (no auth)
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("overseas_proxy")

app = FastAPI(title="Overseas Info Proxy")
_SECRET = os.getenv("PROXY_SECRET", "").strip()
_JINA_PREFIX = "https://r.jina.ai/"
_TIMEOUT = 20
_MAX_LENGTH = 12000
_STRIP_TAGS = ("script", "style", "noscript", "iframe", "header", "footer", "nav", "aside", "form", "button", "svg")


def _check_auth(x_proxy_key: str | None) -> None:
    if not _SECRET:
        # No secret configured → refuse all (force operator to set one).
        raise HTTPException(status_code=503, detail="PROXY_SECRET not set on proxy")
    if not x_proxy_key or x_proxy_key.strip() != _SECRET:
        raise HTTPException(status_code=403, detail="invalid proxy key")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "secret_configured": bool(_SECRET)}


@app.get("/fetch")
async def fetch(
    url: str = Query(..., description="target URL to fetch"),
    strategy: str = Query("raw", description="raw=direct; jina=via r.jina.ai; json=raw passthrough for APIs"),
    x_proxy_key: str | None = Header(None, alias="X-Proxy-Key"),
) -> JSONResponse:
    """Fetch a foreign page and return extracted text/markdown."""
    _check_auth(x_proxy_key)
    if not re.match(r"^https?://", url, re.I):
        raise HTTPException(status_code=400, detail="url must start with http(s)://")

    if strategy == "json":
        text, title = _fetch_json(url)
    elif strategy == "jina":
        text, title = _fetch_jina(url)
    else:
        text, title = _fetch_raw(url)

    if len(text) > _MAX_LENGTH:
        text = text[:_MAX_LENGTH] + f"\n\n... (truncated, total {len(text)} chars)"

    return JSONResponse({"status": "ok", "url": url, "title": title, "content": text, "length": len(text), "strategy": strategy})


def _fetch_json(url: str) -> tuple[str, str]:
    """Fetch raw response (for JSON APIs — no HTML parsing)."""
    headers = {
        "User-Agent": "Mozilla/5.0 Chrome/120.0",
        "Accept": "application/json,text/html,*/*",
    }
    resp = requests.get(url, headers=headers, timeout=_TIMEOUT, allow_redirects=True)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"upstream HTTP {resp.status_code}")
    return resp.text, ""


def _fetch_raw(url: str) -> tuple[str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    resp = requests.get(url, headers=headers, timeout=_TIMEOUT, allow_redirects=True)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"upstream HTTP {resp.status_code}")
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    for tag in _STRIP_TAGS:
        for n in soup.find_all(tag):
            n.decompose()
    container = soup.find("article") or soup.find("main") or soup.body or soup
    text = container.get_text(separator="\n", strip=True) if container else ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines), title


def _fetch_jina(url: str) -> tuple[str, str]:
    """Jina Reader (r.jina.ai) — overseas server reaches it fast."""
    try:
        resp = requests.get(f"{_JINA_PREFIX}{url}", headers={"Accept": "text/markdown"}, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"jina request failed: {exc}")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"jina HTTP {resp.status_code}")
    text = resp.text
    title = ""
    for line in text.split("\n"):
        if line.startswith("Title:"):
            title = line[6:].strip()
            break
    return text, title


@app.get("/search")
async def search(
    q: str = Query(..., description="search query"),
    max: int = Query(5, ge=1, le=10),
    x_proxy_key: str | None = Header(None, alias="X-Proxy-Key"),
) -> JSONResponse:
    """DuckDuckGo search (overseas server reaches DDG freely)."""
    _check_auth(x_proxy_key)
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            raise HTTPException(status_code=500, detail="ddgs not installed on proxy")
    try:
        with DDGS() as ddgs:
            raw = list(ddgs.text(q, max_results=max))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"search failed: {exc}")
    results = [
        {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
        for r in raw
    ]
    return JSONResponse({"status": "ok", "query": q, "results": results})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "12000"))
    logger.info("overseas_proxy listening on :%d (secret=%s)", port, "yes" if _SECRET else "NO")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
