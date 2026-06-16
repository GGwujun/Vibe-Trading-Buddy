"""TPDog (托普量化) HTTP client.

Thin wrapper around https://www.tpdog.com/api — reads the project-configured
``TPDOG_TOKEN`` (managed via the Settings UI → agent/.env), performs GET calls
with a short timeout, and validates the unified ``{code, message, content}``
envelope. ``code == 1000`` means success; anything else raises ``TpdogError``.

All higher-level loaders/routes import from here so token handling and error
formatting live in one place. See ``agent/src/data/tpdog_doc.json`` for the full
endpoint catalogue (90 interfaces, fetched via scripts/fetch_tpdog_docs.py).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://www.tpdog.com/api/hs"
DEFAULT_TIMEOUT = 10  # seconds


class TpdogError(RuntimeError):
    """Raised when tpdog returns a non-1000 code or the call fails."""

    def __init__(self, code: Optional[int], message: str) -> None:
        self.code = code
        super().__init__(f"[tpdog] {code}: {message}" if code else f"[tpdog] {message}")


class TpdogNotConfiguredError(TpdogError):
    """Raised when TPDOG_TOKEN is missing or a placeholder."""

    def __init__(self) -> None:
        super().__init__(None, "TPDOG_TOKEN 未配置（请在设置页填入托普量化 Token）")


def get_token() -> str:
    """Return the configured TPDOG_TOKEN, or raise if unset/placeholder."""
    token = os.environ.get("TPDOG_TOKEN", "").strip()
    if not token or token.lower() == "your-tpdog-token":
        raise TpdogNotConfiguredError()
    return token


def is_configured() -> bool:
    """True when a non-placeholder TPDOG_TOKEN is available."""
    try:
        get_token()
        return True
    except TpdogNotConfiguredError:
        return False


def call(path: str, **params: Any) -> List[Dict[str, Any]]:
    """GET ``BASE_URL/{path}`` with token + params; return ``content`` list.

    ``path`` is everything after ``/api/hs/``, e.g. ``trading_day/year`` or
    ``stock_his/daily``. Empty params are dropped. Raises ``TpdogError`` on any
    non-1000 response, and ``requests.RequestException`` on network failure.
    """
    token = get_token()
    query: Dict[str, Any] = {k: v for k, v in params.items() if v is not None and v != ""}
    query["token"] = token
    url = f"{BASE_URL}/{path.lstrip('/')}"
    # Gate every outbound call through the shared limiter so a full-market
    # backfill can't starve foreground requests or trigger anti-bot bans.
    from src.data.rate_limiter import market_limiter

    with market_limiter:
        resp = requests.get(url, params=query, timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError as exc:
        raise TpdogError(None, f"non-JSON response: {resp.text[:120]}") from exc
    code = data.get("code")
    if code != 1000:
        raise TpdogError(code, str(data.get("message", "未知错误")))
    content = data.get("content")
    # Most endpoints return a list; a few (e.g. etf/daily) return a single
    # object. Wrap dicts so callers always get a list, per the call() contract.
    if isinstance(content, list):
        return content
    if isinstance(content, dict):
        return [content]
    return []
