#!/usr/bin/env python3
"""One-shot backfill of market data into the SQLite store.

Drives :func:`src.data.market_sync.run_daily_sync` to pull historical bars /
dragon-tiger / pools / ETF into ``~/.vibe-trading/market.db``. Resumable:
``run_daily_sync`` is idempotent (per-code ``last_daily_date`` short-circuits,
snapshots upsert), so Ctrl-C preserves everything pulled and a re-run
continues where it left off.

WARNING: full A-share daily-K is the dominant credit cost
(~1 credit per code per month slice). Use ``--codes`` / ``--max-codes`` to
limit the universe and ``--yes`` to confirm the printed estimate.

Usage::

    # small range, specific codes (safe smoke test)
    python -m scripts.backfill_market --datasets daily \\
        --codes 600206.SH,000001.SZ --start 2026-05-01 --end 2026-06-15 --yes

    # full A-share, 2 years daily + dragon/pool (expensive!)
    python -m scripts.backfill_market --years 2 --datasets daily,dragon,pool --yes

Run from the repo root so ``agent`` is importable, or set PYTHONPATH=agent.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Make `agent/src/...` importable when run as `python -m scripts.backfill_market`.
_AGENT = Path(__file__).resolve().parent.parent / "agent"
if str(_AGENT) not in sys.path:
    sys.path.insert(0, str(_AGENT))

try:
    from dotenv import load_dotenv

    load_dotenv(_AGENT / ".env", override=False)
except Exception:
    pass

from src.data.market_sync import run_daily_sync  # noqa: E402
from src.data.market_store import get_market_store  # noqa: E402


def _trading_dates(start: str, end: str) -> list[str]:
    """Enumerate calendar dates [start, end]; sync filters non-trading days
    via tpdog per-code, so we just walk calendar days and let the store's
    idempotency skip what's already there."""
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    out = []
    cur = s
    while cur <= e:
        out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return out


def _estimate_credits(codes_n: int, dates_n: int, datasets: set[str], years: int) -> int:
    est = 0
    if "daily" in datasets:
        # 1 credit per code per ~28-day slice; ~12.2 slices/year
        est += int(codes_n * years * 12.2)
    if "dragon" in datasets:
        est += dates_n
    if "pool" in datasets:
        est += dates_n * 5
    if "etf" in datasets:
        est += 80 * years * 12
    return est


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill market data into SQLite.")
    ap.add_argument("--years", type=int, default=2, help="daily/etf lookback years")
    ap.add_argument("--start", help="override start date (yyyy-MM-dd)")
    ap.add_argument("--end", help="override end date (yyyy-MM-dd)")
    ap.add_argument("--codes", help="comma-separated codes (project form 600206.SH)")
    ap.add_argument("--max-codes", type=int, help="cap the code universe size")
    ap.add_argument("--datasets", default="daily,dragon,pool,etf",
                    help="comma-separated: daily,dragon,pool,etf,premium,capital")
    ap.add_argument("--universe", default="default", choices=["default", "all"])
    ap.add_argument("--lookback-days", type=int, default=None,
                    help="initial daily lookback when a code is cold; use 0 for today-only")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    datasets = {d.strip() for d in args.datasets.split(",") if d.strip()}

    # Mark this whole process as background work so the shared limiter reserves
    # outbound-call slots for foreground (user) requests during the backfill.
    from src.data.rate_limiter import mark_background
    mark_background(True)

    store = get_market_store()
    if store is None:
        print("[!] market store unavailable; set TPDOG_TOKEN and retry.", file=sys.stderr)
        return 1

    today = datetime.now().strftime("%Y-%m-%d")
    end = args.end or today
    if args.start:
        start = args.start
    else:
        start = (datetime.now() - timedelta(days=args.years * 365)).strftime("%Y-%m-%d")

    # Resolve code universe.
    codes = None
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    elif "daily" in datasets:
        from src.data.market_sync import _all_a_share_codes
        codes = _all_a_share_codes()
        if args.max_codes:
            codes = codes[: args.max_codes]

    dates_n = len(_trading_dates(start, end))
    codes_n = len(codes) if codes else (5000 if "daily" in datasets else 0)
    est = _estimate_credits(codes_n, dates_n, datasets, args.years)

    print(f"Backfill plan:")
    print(f"  window      : {start} .. {end} ({dates_n} calendar days)")
    print(f"  datasets    : {sorted(datasets)}")
    print(f"  daily codes : {codes_n}")
    print(f"  est credits : ~{est:,}  (daily dominates)")
    if not args.yes:
        try:
            input("\nProceed? [Enter] to confirm, Ctrl-C to abort ... ")
        except KeyboardInterrupt:
            print("\naborted.")
            return 130

    # Drive daily sync once (run_daily_sync walks the code universe + slices
    # the full window internally, resuming from last_daily_date per code).
    if "daily" in datasets:
        print("\n[daily] syncing (resumable; this is the long part)...")
        rows = run_daily_sync(
            end, store=store, codes=codes, datasets={"daily"},
            deadline_seconds=86400,
            lookback_days=args.lookback_days if args.lookback_days is not None else args.years * 365,
        )
        print(f"[daily] wrote {rows.get('daily', 0)} rows")

    # Per-date snapshot datasets: walk calendar days.
    snap = datasets & {"dragon", "pool", "etf", "premium"}
    if snap:
        print(f"\n[snapshots] walking {dates_n} dates for {sorted(snap)}...")
        for d in _trading_dates(start, end):
            try:
                run_daily_sync(d, store=store, datasets=snap, deadline_seconds=3600)
            except Exception as exc:  # noqa: BLE001
                print(f"  {d}: {exc}", file=sys.stderr)
        print("[snapshots] done")

    counts = store.table_counts()
    print(f"\nFinal table counts: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
