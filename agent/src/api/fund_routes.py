"""Fund arbitrage HTTP routes for the Web UI.

Mounted by ``agent/api_server.py`` via ``register_fund_arbitrage_routes(app, ...)``.

Routes:
- ``GET  /fund/scan``                        — scan funds for premium/discount opportunities
- ``GET  /fund/{code}``                      — single fund real-time premium detail
- ``GET  /fund/source-status``               — probe which data source is live
- ``POST /fund/analyze``                     — trigger a deep arbitrage report (swarm)
- ``GET  /fund/runs``                        — list fund arbitrage runs
- ``GET  /fund/runs/{run_id}``               — run status (live task files)
- ``POST /fund/runs/{run_id}/cancel``        — force cancel (disk-level)
- ``GET  /fund/runs/{run_id}/events``        — SSE live progress
- ``GET  /fund/reports``                     — list saved reports
- ``GET  /fund/reports/{report_id}``         — report detail
- ``GET  /fund/reports/{report_id}/download``— download md/pdf

Report storage: ``~/.vibe-trading/fund_arbitrage_reports/``
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from src.api.auth_routes import require_user  # JWT validator → returns user dict

logger = logging.getLogger(__name__)

REPORTS_ROOT = Path.home() / ".vibe-trading" / "fund_arbitrage_reports"


def _get_store():
    """SwarmStore at the same root the runtime uses (must match to see runs)."""
    from src.swarm.store import SwarmStore, swarm_runs_root
    return SwarmStore(base_dir=swarm_runs_root())


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class FundAnalyzeRequest(BaseModel):
    fund_code: str = Field(..., description="基金代码，如 161725")
    fund_type: str = Field(default="ETF", description="ETF/LOF/QDII/分级/封基")


class FundAnalyzeResponse(BaseModel):
    run_id: str
    status: str
    fund_code: str
    fund_type: str
    created_at: str


# ---------------------------------------------------------------------------
# Report storage helpers (mirror alpha_forge_routes patterns)
# ---------------------------------------------------------------------------

def _ensure_reports_root() -> None:
    REPORTS_ROOT.mkdir(parents=True, exist_ok=True)


def _sanitize_filename(name: str) -> str:
    return re.sub(r"[<>:\"/\\|?*]", "_", name)


def _load_report_meta(report_id: str) -> dict[str, Any] | None:
    p = REPORTS_ROOT / report_id / "meta.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _load_report_md(report_id: str) -> str | None:
    p = REPORTS_ROOT / report_id / "report.md"
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def _save_report(report_id: str, content_md: str, meta: dict[str, Any]) -> Path:
    _ensure_reports_root()
    d = REPORTS_ROOT / report_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.md").write_text(content_md, encoding="utf-8")
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return d


def _extract_meta_from_md(content: str) -> dict[str, str]:
    """Extract premium rate / rating from the report header.

    Prefers the machine-readable ``<!-- DECISION: {json} -->`` block emitted by
    the report_writer agent; falls back to header regex scraping.
    """
    meta: dict[str, str] = {}
    # Machine-readable block first.
    block = _parse_fund_decision_block(content)
    if block:
        if block.get("rating"):
            meta["rating"] = block["rating"]
        if block.get("action"):
            meta["action"] = block["action"]
        if block.get("premium_pct") not in (None, 0, "0", ""):
            meta["premium_rate"] = f"{block['premium_pct']}%"
        if block.get("net_return_pct") not in (None, 0, "0", ""):
            meta["net_return"] = f"{block['net_return_pct']}%"

    for line in content.split("\n")[:20]:
        if "**折溢价率**" in line:
            m = re.search(r"\*\*(±?\s*-?[\d.]+%)\*\*", line)
            if m:
                meta.setdefault("premium_rate", m.group(1))
        elif "**套利评级**" in line:
            m = re.search(r"\*\*(.+?)\*\*\s*$", line)
            if m:
                meta.setdefault("rating", m.group(1).strip())
    return meta


def _parse_fund_decision_block(content: str) -> dict | None:
    """Parse the ``<!-- DECISION: {json} -->`` block from a fund report."""
    matches = list(re.finditer(r"<!--\s*DECISION\s*:\s*(\{.*?\})\s*-->", content, re.S))
    if not matches:
        return None
    try:
        return json.loads(matches[-1].group(1))
    except (ValueError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

# Static arbitrage economics (round-number defaults; refined per-type).
# Cost = round-trip fee estimate (trade + subscribe/redeem), in % of notional.
_ARBITRAGE_COST_PCT = {"ETF": 0.5, "LOF": 1.2, "QDII": 1.5}
# Settlement cycle (days until subscribed shares can be sold / redemption lands).
_SETTLE_DAYS = {"ETF": 1, "LOF": 2, "QDII": 3}
# Min history (trading days) before a percentile is trustworthy. Below this the
# UI shows nothing — never a fake signal from thin history.
_MIN_HISTORY_DAYS = 20


def _blocked_status(status: str) -> bool:
    """True if a 申赎 status string indicates suspension (blocking arbitrage)."""
    s = status or ""
    return any(k in s for k in ("暂停", "停止", "封闭", "结束"))


def _enrich_items(items: list[dict], store: Any, trade_date: str | None) -> None:
    """Add per-item derived fields used by the UI (in place).

    - direction: 申购套利 / 赎回套利
    - net_return: |premium| minus round-trip cost (clamped ≥ 0)
    - settle_days: by fund type
    - is_stale_nav: NAV date != snapshot date (NAV lag, e.g. QDII T-1)
    - can_trade: false if subscribe/redeem is suspended (direction-dependent)
    - premium_percentile / amount_percentile: only when ≥ _MIN_HISTORY_DAYS exist
    """
    for r in items:
        premium = float(r.get("premium_rate") or 0.0)
        ftype = (r.get("type") or "").upper()
        r["direction"] = "申购套利" if premium > 0 else ("赎回套利" if premium < 0 else "—")
        cost = _ARBITRAGE_COST_PCT.get(ftype, 1.0)
        r["net_return"] = round(max(0.0, abs(premium) - cost), 3)
        r["settle_days"] = _SETTLE_DAYS.get(ftype, 2)
        # Stale NAV: only flag when a real nav_date exists AND differs from snapshot.
        nav_date = str(r.get("nav_date") or "").strip()
        r["is_stale_nav"] = bool(nav_date and trade_date and nav_date != trade_date)
        sub = str(r.get("subscribe_status") or "")
        red = str(r.get("redeem_status") or "")
        # 申购套利 needs subscription open; 赎回套利 needs redemption open.
        if premium > 0:
            r["can_trade"] = not _blocked_status(sub) if sub else True
        elif premium < 0:
            r["can_trade"] = not _blocked_status(red) if red else True
        else:
            r["can_trade"] = True
        # Placeholders for percentile (filled below if history suffices).
        r["premium_percentile"] = None
        r["amount_percentile"] = None

    # Percentile only when enough history exists (skeleton: auto-enables later).
    if store is None:
        return
    for r in items:
        code = r.get("code")
        if not code:
            continue
        try:
            hist = store.get_fund_premium_history(code, _MIN_HISTORY_DAYS)
        except Exception:
            hist = []
        if len(hist) < _MIN_HISTORY_DAYS:
            continue  # not enough history → leave null (no fake signal)
        cur_prem = abs(float(r.get("premium_rate") or 0.0))
        cur_amt = float(r.get("amount") or 0.0)
        prems = [abs(float(h.get("premium_rate") or 0.0)) for h in hist]
        amts = [float(h.get("amount") or 0.0) for h in hist]
        r["premium_percentile"] = round(_percentile(cur_prem, prems), 2)
        r["amount_percentile"] = round(_percentile(cur_amt, amts), 2)


def _percentile(value: float, sample: list[float]) -> float:
    """Fraction (0-100) of `sample` strictly below `value`."""
    if not sample:
        return 0.0
    below = sum(1 for s in sample if s < value)
    return below / len(sample) * 100.0


def register_fund_arbitrage_routes(
    app: FastAPI,
    require_auth: Callable[[Request], Awaitable[None]],
    require_event_stream_auth: Callable[[Request], Awaitable[None]],
    get_swarm_runtime: Callable[[], Any] | None = None,
) -> None:
    """Register fund arbitrage routes."""

    # ── Scan ──────────────────────────────────────────────────────
    @app.get("/fund/scan")
    async def scan_funds(
        request: Request,
        type: str = Query("ETF", description="ETF/LOF/ALL"),
        min_premium: float = Query(0.5, ge=0, description="最小 |折溢价率|%"),
        page: int = Query(1, ge=1, description="页码，1-based"),
        page_size: int = Query(20, ge=1, le=200, description="每页条数"),
        sort: str = Query(
            "premium_abs",
            description="排序：premium_abs=|折溢价|降序(默认) / premium_desc=溢价优先 / "
            "premium_asc=折价优先 / amount_desc=成交额降序(流动性优先)",
        ),
        limit: int | None = Query(None, ge=1, le=200, description="兼容旧前端，等价于 page_size"),
        _=Depends(require_auth),
    ):
        """Scan funds for premium/discount arbitrage opportunities.

        Reads the latest persisted snapshot (synced every ~5 min during trading
        by the market-sync daemon). Falls back to a live scan when the DB is
        cold (first run / non-trading day before any sync). Filtering by type
        and min premium + pagination happen in memory — the snapshot is ≤200
        rows per trade_date.
        """
        from src.data.fund_premium import scan_fund_premium
        from src.data.market_store import get_market_store

        # Legacy `limit` param maps to page_size.
        if limit is not None:
            page_size = limit

        fund_type_upper = type.upper()
        want_all = fund_type_upper in ("ALL", "全部")

        def _paginate(rows: list[dict]) -> tuple[int, int, list[dict]]:
            # Filter: type (ALL skips) + min premium. scan_fund_premium already
            # dropped |premium|>50 (stale-NAV noise), so we only apply the
            # caller's lower bound here.
            filtered = [
                r for r in rows
                if (want_all or r.get("type", "").upper() == fund_type_upper)
                and abs(float(r.get("premium_rate") or 0.0)) >= min_premium
            ]
            # Sort by the requested key (stable tiebreak: |premium| desc).
            _prem = lambda r: float(r.get("premium_rate") or 0.0)
            _amt = lambda r: float(r.get("amount") or 0.0)
            if sort == "amount_desc":
                filtered.sort(key=lambda r: (_amt(r), abs(_prem(r))), reverse=True)
            elif sort == "premium_asc":
                # 折价优先（折价率最负在前）→ 升序
                filtered.sort(key=lambda r: (_prem(r), -abs(_prem(r))))
            elif sort == "premium_desc":
                # 溢价优先（溢价率最正在前）→ 降序
                filtered.sort(key=lambda r: (_prem(r), abs(_prem(r))), reverse=True)
            else:  # premium_abs (default): |折溢价| 降序，套利空间最大
                filtered.sort(key=lambda r: abs(_prem(r)), reverse=True)
            total = len(filtered)
            total_pages = max(1, (total + page_size - 1) // page_size)
            start = (page - 1) * page_size
            page_items = filtered[start:start + page_size]
            return total, total_pages, page_items

        trade_date = None
        source = "live"
        updated_at: str | None = None
        rows: list[dict] = []

        store = get_market_store()
        if store is not None:
            trade_date = store.latest_date("fund_premium_snapshot")
        if trade_date:
            rows = store.get_fund_premium(trade_date)
            if rows:
                source = "snapshot"
                # Per-row updated_at is stamped on every upsert; MAX = latest sync.
                times = [r.get("updated_at") for r in rows if r.get("updated_at")]
                updated_at = max(times) if times else None
                # Overlay authoritative daily-refreshed names from fund_master
                # (falls back to snapshot.name if fund_master is cold/empty).
                master_names = store.get_fund_master_names([r.get("code") for r in rows if r.get("code")])
                if master_names:
                    for r in rows:
                        nm = master_names.get(r.get("code"))
                        if nm:
                            r["name"] = nm

        # Cold cache (no snapshot yet) → live scan so the page still works.
        if source == "live":
            try:
                rows = scan_fund_premium(fund_type="ALL", min_abs_premium=0.0, limit=3000)
            except Exception as exc:
                logger.error("fund scan live fallback failed: %s", exc, exc_info=True)
                raise HTTPException(status_code=500, detail=f"扫描失败: {exc}")
            trade_date = None
            updated_at = None

        total, total_pages, page_items = _paginate(rows)
        _enrich_items(page_items, store, trade_date)
        return {
            "status": "ok",
            "source": source,
            "trade_date": trade_date,
            "updated_at": updated_at,
            "count": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "items": page_items,
        }

    # ── Source status ─────────────────────────────────────────────
    @app.get("/fund/source-status")
    async def source_status(request: Request, _=Depends(require_auth)):
        """Probe which data source is currently live (em/ths/mootdx)."""
        from src.data.fund_premium import scan_source_status
        return scan_source_status()

    # ── Single fund detail ────────────────────────────────────────
    @app.get("/fund/{code}")
    async def fund_detail(code: str, request: Request, _=Depends(require_auth)):
        """Real-time premium snapshot for a single fund."""
        from src.data.fund_premium import get_fund_detail
        result = get_fund_detail(code)
        if result.get("status") != "ok":
            raise HTTPException(status_code=404, detail=result.get("error", "未取到数据"))
        return result

    # ── List saved reports ────────────────────────────────────────
    @app.get("/fund/reports")
    async def list_reports(request: Request, _=Depends(require_auth)):
        _ensure_reports_root()
        out = []
        for d in sorted(REPORTS_ROOT.iterdir() if REPORTS_ROOT.exists() else [], key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir():
                continue
            meta = _load_report_meta(d.name)
            if meta:
                out.append({
                    "report_id": d.name,
                    "fund_code": meta.get("fund_code", ""),
                    "fund_name": meta.get("fund_name", ""),
                    "fund_type": meta.get("fund_type", ""),
                    "analysis_date": meta.get("analysis_date", ""),
                    "created_at": meta.get("created_at", ""),
                    "premium_rate": meta.get("premium_rate", ""),
                    "rating": meta.get("rating", ""),
                })
        return out

    # ── Report detail ─────────────────────────────────────────────
    @app.get("/fund/reports/{report_id}")
    async def get_report(report_id: str, request: Request, _=Depends(require_auth)):
        meta = _load_report_meta(report_id)
        content = _load_report_md(report_id)
        if meta is None or content is None:
            raise HTTPException(status_code=404, detail=f"报告 {report_id!r} 不存在")
        return {
            "report_id": report_id,
            "fund_code": meta.get("fund_code", ""),
            "fund_name": meta.get("fund_name", ""),
            "fund_type": meta.get("fund_type", ""),
            "analysis_date": meta.get("analysis_date", ""),
            "created_at": meta.get("created_at", ""),
            "premium_rate": meta.get("premium_rate", ""),
            "rating": meta.get("rating", ""),
            "content_md": content,
        }

    # ── Download ──────────────────────────────────────────────────
    @app.get("/fund/reports/{report_id}/download")
    async def download_report(
        report_id: str,
        request: Request,
        format: str = Query("md"),
        _=Depends(require_auth),
    ):
        meta = _load_report_meta(report_id)
        content = _load_report_md(report_id)
        if meta is None or content is None:
            raise HTTPException(status_code=404, detail=f"报告 {report_id!r} 不存在")
        code = meta.get("fund_code", report_id)
        base = f"FundArbitrage_{code}_{meta.get('analysis_date', 'unknown')}"

        if format == "md":
            return Response(
                content=content, media_type="text/markdown; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{_sanitize_filename(base)}.md"'},
            )
        if format == "pdf":
            pdf_path = REPORTS_ROOT / report_id / "report.pdf"
            if pdf_path.exists():
                return FileResponse(pdf_path, media_type="application/pdf", filename=f"{_sanitize_filename(base)}.pdf")
            try:
                import markdown as md_lib
                from weasyprint import HTML
                md_html = md_lib.markdown(content, extensions=["tables", "fenced_code", "codehilite", "toc", "nl2br"])
                html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><style>
  body {{ font-family: "Microsoft YaHei","SimSun",sans-serif; font-size:13px; line-height:1.7; max-width:210mm; margin:auto; padding:20px; color:#333; }}
  h1 {{ font-size:22px; border-bottom:2px solid #333; padding-bottom:8px; }}
  h2 {{ font-size:18px; border-bottom:1px solid #999; padding-bottom:4px; margin-top:28px; }}
  table {{ border-collapse:collapse; width:100%; margin:12px 0; font-size:11px; }}
  th,td {{ border:1px solid #ddd; padding:6px 8px; text-align:left; }}
  th {{ background:#f5f5f5; font-weight:bold; }}
  blockquote {{ border-left:3px solid #ccc; margin:10px 0; padding:6px 16px; background:#f9f9f9; }}
  pre {{ background:#f4f4f4; padding:12px; border-radius:4px; overflow-x:auto; }}
</style></head><body>{md_html}</body></html>"""
                pdf_bytes = HTML(string=html).write_pdf()
                pdf_path.write_bytes(pdf_bytes)
                return Response(content=pdf_bytes, media_type="application/pdf",
                                headers={"Content-Disposition": f'attachment; filename="{_sanitize_filename(base)}.pdf"'})
            except Exception as e:
                logger.error("fund PDF gen failed: %s", e, exc_info=True)
                raise HTTPException(status_code=500, detail=f"PDF 生成失败: {e}")
        raise HTTPException(status_code=400, detail=f"未知格式: {format!r}")

    # ── Trigger deep analysis ─────────────────────────────────────
    @app.post("/fund/analyze")
    async def analyze_fund(body: FundAnalyzeRequest, request: Request, user=Depends(require_user)):
        if get_swarm_runtime is None:
            raise HTTPException(status_code=503, detail="Swarm runtime 不可用")
        swarm_runtime = get_swarm_runtime()
        try:
            swarm_run = swarm_runtime.start_run(
                preset_name="fund_arbitrage",
                user_vars={"fund_code": body.fund_code, "fund_type": body.fund_type},
            )
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="fund_arbitrage 预设未找到")
        except Exception as e:
            logger.error("fund analyze start failed: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=f"启动分析失败: {e}")

        # ── Credits: consume after run created (run_id is the refund ref) ──
        from src.credits.store import CreditStore
        from src.credits.constants import COST_FUND_ARBITRAGE
        credits = CreditStore()
        if not credits.consume(user["id"], COST_FUND_ARBITRAGE, swarm_run.id, f"基金套利 {body.fund_code}"):
            try:
                swarm_runtime.cancel_run(swarm_run.id)
            except Exception:
                pass
            raise HTTPException(
                status_code=402,
                detail=f"积分不足，本次分析需要 {COST_FUND_ARBITRAGE} 积分",
            )
        billing_user_id = user["id"]

        # Background poll → save report on completion
        import threading

        def _on_complete(run_id: str) -> None:
            try:
                store = _get_store()
                run_dir = store.run_dir(run_id)
                # Prefer report_writer output
                writer_path = run_dir / "artifacts" / "report_writer" / "report.md"
                if writer_path.is_file() and len(writer_path.read_text(encoding="utf-8").strip()) > 500:
                    content = writer_path.read_text(encoding="utf-8").strip()
                else:
                    # Fallback: assemble from all agents
                    content = _assemble_from_artifacts(run_dir, body.fund_code)

                now = datetime.now(timezone.utc)
                report_id = f"fund_{body.fund_code}_{now.strftime('%Y%m%d-%H%M%S')}"
                meta = {
                    "fund_code": body.fund_code,
                    "fund_name": _extract_name(content),
                    "fund_type": body.fund_type,
                    "analysis_date": now.strftime("%Y-%m-%d"),
                    "created_at": now.isoformat(),
                    "run_id": run_id,
                }
                meta.update(_extract_meta_from_md(content))
                # Validate the arbitrage decision (premium/net-return consistency).
                try:
                    from src.analysis.decision_validator import validate_fund_decision
                    warnings = validate_fund_decision(meta)
                    if warnings:
                        meta["decision_warnings"] = warnings
                        logger.warning("Fund %s decision warnings: %s", body.fund_code, warnings)
                except Exception:  # noqa: BLE001
                    logger.debug("fund decision validation skipped", exc_info=True)
                _save_report(report_id, content, meta)
                logger.info("Saved fund report %s for %s", report_id, body.fund_code)
            except Exception as e:
                logger.error("Failed to save fund report for run %s: %s", run_id, e, exc_info=True)

        def _poll():
            import time
            store = _get_store()
            while True:
                time.sleep(5)
                try:
                    r = store.load_run(swarm_run.id)
                    if r and r.status.value in ("completed", "failed", "cancelled"):
                        if r.status.value == "completed":
                            _on_complete(swarm_run.id)
                        else:
                            # Failed/cancelled → refund (idempotent per run_id).
                            from src.credits.store import CreditStore
                            from src.credits.constants import COST_FUND_ARBITRAGE
                            CreditStore().refund(billing_user_id, COST_FUND_ARBITRAGE, swarm_run.id, f"基金套利失败退还 {body.fund_code}")
                        break
                except Exception:
                    break

        threading.Thread(target=_poll, daemon=True).start()

        return FundAnalyzeResponse(
            run_id=swarm_run.id, status=swarm_run.status.value,
            fund_code=body.fund_code, fund_type=body.fund_type,
            created_at=swarm_run.created_at,
        )

    # ── List runs ─────────────────────────────────────────────────
    @app.get("/fund/runs")
    async def list_runs(request: Request, _=Depends(require_auth)):
        store = _get_store()
        from src.swarm.task_store import TaskStore
        out = []
        for r in store.list_runs(limit=100):
            if r.preset_name != "fund_arbitrage":
                continue
            done = 0
            total = len(r.tasks)
            try:
                ts = TaskStore(store.run_dir(r.id))
                live = ts.load_all()
                total = len(live)
                done = sum(1 for t in live if t.status.value == "completed")
            except Exception:
                pass
            out.append({
                "run_id": r.id, "status": r.status.value,
                "fund_code": (r.user_vars or {}).get("fund_code", ""),
                "fund_type": (r.user_vars or {}).get("fund_type", ""),
                "created_at": r.created_at, "completed_at": r.completed_at,
                "task_count": total, "completed_count": done,
            })
        return out

    # ── Run status (live task files) ──────────────────────────────
    @app.get("/fund/runs/{run_id}")
    async def get_run(run_id: str, request: Request, _=Depends(require_auth)):
        store = _get_store()
        run = store.load_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"运行 {run_id!r} 不存在")
        live = []
        try:
            from src.swarm.task_store import TaskStore
            ts = TaskStore(store.run_dir(run_id))
            live = ts.load_all()
        except Exception:
            pass
        src = live if live else run.tasks
        return {
            "run_id": run.id, "status": run.status.value,
            "preset_name": run.preset_name, "created_at": run.created_at,
            "completed_at": run.completed_at, "final_report": run.final_report,
            "tasks": [{"id": t.id, "agent_id": t.agent_id, "status": t.status.value} for t in src],
        }

    # ── Force cancel ──────────────────────────────────────────────
    @app.post("/fund/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, request: Request, _=Depends(require_auth)):
        store = _get_store()
        run = store.load_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"运行 {run_id!r} 不存在")
        in_mem = False
        if get_swarm_runtime is not None:
            try:
                in_mem = get_swarm_runtime().cancel_run(run_id)
            except Exception:
                pass
        from src.swarm.models import RunStatus, TaskStatus
        from src.swarm.task_store import TaskStore
        run_dir = store.run_dir(run_id)
        try:
            ts = TaskStore(run_dir)
            for t in ts.load_all():
                if t.status not in (TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled):
                    ts.update_status(t.id, TaskStatus.cancelled)
        except Exception:
            pass
        run.status = RunStatus.cancelled
        run.completed_at = datetime.now(timezone.utc).isoformat()
        try:
            store.update_run(run)
        except Exception:
            pass
        return {"status": "cancelled", "run_id": run_id, "disk_cancel": True}

    # ── SSE events ────────────────────────────────────────────────
    @app.get("/fund/runs/{run_id}/events")
    async def stream_events(run_id: str, request: Request, _=Depends(require_event_stream_auth)):
        import asyncio
        store = _get_store()
        if store.load_run(run_id) is None:
            raise HTTPException(status_code=404, detail=f"运行 {run_id!r} 不存在")

        async def gen():
            ef = store.run_dir(run_id) / "events.jsonl"
            pos = 0
            if ef.exists():
                try:
                    existing = ef.read_text(encoding="utf-8")
                    for line in existing.splitlines():
                        if line.strip():
                            yield f"data: {line.strip()}\n\n"
                    pos = ef.stat().st_size
                except Exception:
                    pass
            import time
            while True:
                if await request.is_disconnected():
                    break
                try:
                    if ef.exists() and ef.stat().st_size > pos:
                        with open(ef, "r", encoding="utf-8") as f:
                            f.seek(pos)
                            for line in f.read().splitlines():
                                if line.strip():
                                    yield f"data: {line.strip()}\n\n"
                        pos = ef.stat().st_size
                except Exception:
                    pass
                try:
                    cur = store.load_run(run_id)
                    if cur and cur.status.value in ("completed", "failed", "cancelled"):
                        yield f"data: {json.dumps({'type': 'run_done', 'status': cur.status.value})}\n\n"
                        break
                except Exception:
                    pass
                await asyncio.sleep(1)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

    logger.info("Fund arbitrage routes registered")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SECTIONS = [
    ("data_collector", "数据采集（共享事实表）"),
    ("premium_analyst", "折溢价分析"),
    ("liquidity_analyst", "流动性评估"),
    ("holdings_analyst", "成分股与持仓"),
    ("cost_analyst", "套利成本核算"),
    ("risk_officer", "风控评估"),
    ("report_writer", "最终报告"),
]


def _assemble_from_artifacts(run_dir: Path, fund_code: str) -> str:
    """Fallback: stitch all agent report.md into one document (if report_writer failed)."""
    artifacts = run_dir / "artifacts"
    parts = [f"# 基金套利分析报告（拼合版）— {fund_code}\n"]
    for agent_id, title in _SECTIONS:
        rp = artifacts / agent_id / "report.md"
        if rp.is_file():
            body = rp.read_text(encoding="utf-8").strip()
        else:
            sp = artifacts / agent_id / "summary.md"
            body = sp.read_text(encoding="utf-8").strip() if sp.is_file() else "（该环节未产出内容）"
        parts.append(f"\n## {title}\n\n{body}\n")
    return "\n".join(parts)


def _extract_name(content: str) -> str:
    """Extract fund name from report content."""
    for line in content.split("\n")[:15]:
        m = re.search(r"\*\*基金名称\*\*[：:]\s*(\S+)", line)
        if m:
            return m.group(1)
    return ""
