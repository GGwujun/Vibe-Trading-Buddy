"""Independent tracking watchlist (~/.vibe-trading/tracking_watchlist.json).

A SEPARATE watchlist from the position watchlist (``watchlist.json``) used by
the Tracking Dashboard. This one backs the "自选 & 定时分析" page where each
watched symbol can be turned into a daily scheduled-analysis task.

Item schema: ``{ symbol, name, added_at }``.

Atomic write via temp-file + rename (crash-safe on all platforms), mirroring
``watchlist_store.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# Beijing time (UTC+8) — matches the convention in ohlcv_cache.py
_CST = timezone(timedelta(hours=8))


def _store_path() -> Path:
    root = Path.home() / ".vibe-trading"
    root.mkdir(parents=True, exist_ok=True)
    return root / "tracking_watchlist.json"


def load_tracking_watchlist() -> list[dict[str, Any]]:
    """Return the current tracking watchlist, or an empty list if absent."""
    path = _store_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict) and "symbol" in item]
    except (json.JSONDecodeError, OSError):
        pass
    return []


def save_tracking_watchlist(items: list[dict[str, Any]]) -> None:
    """Atomically write the full tracking watchlist to disk."""
    path = _store_path()
    tmp = path.with_suffix(".tmp")
    payload = json.dumps(items, ensure_ascii=False, indent=2)
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def remove_from_tracking_watchlist(symbol: str) -> bool:
    """Remove a single symbol from the tracking watchlist. Returns True if removed."""
    items = load_tracking_watchlist()
    before = len(items)
    items = [item for item in items if item.get("symbol") != symbol]
    if len(items) < before:
        save_tracking_watchlist(items)
        return True
    return False


def add_to_tracking_watchlist(symbol: str, name: str = "") -> dict[str, Any]:
    """Add a symbol if absent. Returns the stored item dict.

    Idempotent: re-adding an existing symbol updates its name and is a no-op
    otherwise.
    """
    items = load_tracking_watchlist()
    for item in items:
        if item.get("symbol") == symbol:
            if name and not item.get("name"):
                item["name"] = name
            save_tracking_watchlist(items)
            return item
    item = {
        "symbol": symbol,
        "name": name or symbol,
        "added_at": datetime.now(_CST).isoformat(),
    }
    items.append(item)
    save_tracking_watchlist(items)
    return item
