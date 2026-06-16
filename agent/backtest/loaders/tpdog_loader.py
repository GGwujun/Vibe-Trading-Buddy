"""TPDog loader: A-share OHLCV via tpdog.com HTTPS API (token-gated).

TPDog (托普量化, https://www.tpdog.com) is a paid HTTPS market-data service.
Unlike mootdx (TCP-direct, IP-fragile on cloud hosts) this loader reaches the
official REST API over HTTPS, so it stays reachable where the TDX server list
goes stale. Requires ``TPDOG_TOKEN`` (configured via the Settings UI →
agent/.env).

Scope: A-share daily OHLCV only (沪/深). tpdog's daily-K endpoint caps each
call at a 1-month window, so ranges are sliced into ≤28-day chunks and
concatenated. Intraday / weekly / monthly are not exposed here — the mootdx
loader covers those and tpdog's per-call cost makes them uneconomical.

Registered as ``tpdog`` and inserted into the ``a_share`` fallback chain
(ahead of akshare, after mootdx), so a backtest resolves to it automatically
when mootdx is unreachable.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import pandas as pd

from backtest.loaders.base import cached_loader_fetch, validate_date_range
from backtest.loaders.registry import register

logger = logging.getLogger(__name__)


def _to_tpdog_code(code: str) -> Optional[str]:
    """Project code (600206.SH / 000001.SZ / bare 6-digit) → tpdog (sh.600206).

    北交所 (.BJ / 4xxxxx / 8xxxxx) has no tpdog coverage → None.
    """
    upper = code.upper()
    if upper.endswith(".BJ"):
        return None
    if upper.endswith(".SH"):
        return "sh." + upper[:-3]
    if upper.endswith(".SZ"):
        return "sz." + upper[:-3]
    digits = code.replace(".", "")
    if len(digits) == 6 and digits.isdigit():
        if digits[0] == "8" or digits[0] == "4":
            return None  # 北交所
        return ("sh." if digits[0] == "6" else "sz.") + digits
    return None


@register
class DataLoader:
    """TPDog-backed A-share daily OHLCV loader (HTTPS, token-gated)."""

    name = "tpdog"
    markets = {"a_share"}
    # Token presence is checked at is_available() time, not registration time,
    # so a missing token never breaks the import / fallback chain.
    requires_auth = True

    def is_available(self) -> bool:
        """Available if tpdog_client can read a non-placeholder TPDOG_TOKEN."""
        try:
            from src.data.tpdog_client import is_configured
            return is_configured()
        except Exception:
            return False

    def fetch(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        *,
        interval: str = "1D",
        fields: Optional[List[str]] = None,
    ) -> Dict[str, pd.DataFrame]:
        """Fetch A-share daily OHLCV via tpdog.

        Args:
            codes: Symbol list (`.SH/.SZ` suffix or bare 6-digit tickers).
                北交所 and non-A-share symbols are silently skipped.
            start_date / end_date: YYYY-MM-DD.
            interval: Only ``1D`` is supported.
            fields: Ignored.

        Returns:
            Mapping symbol -> OHLCV DataFrame (index ``trade_date``).

        Raises:
            ValueError: If ``interval`` is not ``1D``.
        """
        validate_date_range(start_date, end_date)
        if interval != "1D":
            raise ValueError(
                f"Unsupported interval for tpdog: {interval!r}. "
                f"Only daily (1D) is supported; use mootdx for intraday/weekly/monthly."
            )

        result: Dict[str, pd.DataFrame] = {}
        for code in codes:
            tpdog_code = _to_tpdog_code(code)
            if tpdog_code is None:
                logger.debug("tpdog: skipping unsupported symbol %s", code)
                continue
            try:
                df = cached_loader_fetch(
                    source=self.name,
                    symbol=code,
                    timeframe=interval,
                    start_date=start_date,
                    end_date=end_date,
                    fields=None,
                    fetch=lambda c=code, tc=tpdog_code: self._fetch_one(tc, start_date, end_date),
                )
                if df is not None and not df.empty:
                    result[code] = df
            except Exception as exc:
                logger.warning("tpdog failed for %s: %s", code, exc)
        return result

    def _fetch_one(
        self, tpdog_code: str, start_date: str, end_date: str,
    ) -> Optional[pd.DataFrame]:
        """Fetch one symbol's daily K-line over [start_date, end_date]."""
        from src.data.tpdog_client import call

        frames: List[pd.DataFrame] = []
        start_ts = pd.Timestamp(start_date)
        end_ts = pd.Timestamp(end_date)
        cursor = end_ts
        # tpdog caps each call at a 1-month window — slice into ≤28-day chunks.
        while cursor >= start_ts:
            slice_start = max(start_ts, cursor - pd.Timedelta(days=27))
            content = call(
                "stock_his/daily",
                code=tpdog_code,
                start=slice_start.strftime("%Y-%m-%d"),
                end=cursor.strftime("%Y-%m-%d"),
            )
            if content:
                frames.append(self._rows_to_df(content))
            cursor = slice_start - pd.Timedelta(days=1)
        if not frames:
            return None
        out = pd.concat(frames)
        out = out[~out.index.duplicated(keep="last")].sort_index()
        return out if not out.empty else None

    @staticmethod
    def _rows_to_df(rows: List[dict]) -> pd.DataFrame:
        """Normalize tpdog daily-K rows → OHLCV DataFrame (index ``trade_date``)."""
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df["trade_date"] = pd.to_datetime(df["date"])
        df = df.set_index("trade_date")
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df[["open", "high", "low", "close", "volume"]].dropna(
            subset=["open", "high", "low", "close"]
        )
