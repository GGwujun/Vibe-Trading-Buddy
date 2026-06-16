"""AlphaForge 投研报告 HTTP routes for the Web UI.

Mounted by ``agent/api_server.py`` via ``register_alpha_forge_routes(app, ...)``.

Routes:
- ``GET  /alpha-forge/reports``                  — list saved reports
- ``GET  /alpha-forge/reports/{report_id}``       — report detail (MD + metadata)
- ``GET  /alpha-forge/reports/{report_id}/download`` — download MD or PDF
- ``POST /alpha-forge/runs``                      — create a new AlphaForge run
- ``GET  /alpha-forge/runs/{run_id}``             — get run status
- ``GET  /alpha-forge/runs/{run_id}/events``      — SSE live progress

Report storage: ``~/.vibe-trading/alpha_forge_reports/``
Each report: ``{report_id}/report.md`` + ``{report_id}/meta.json``
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, Request

from src.api.auth_routes import require_user  # JWT validator → returns user dict
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Report storage
# ---------------------------------------------------------------------------

REPORTS_ROOT = Path.home() / ".vibe-trading" / "alpha_forge_reports"


def _get_store():
    """Return a SwarmStore pointing at the SAME base_dir the runtime uses.

    The runtime (api_server._get_swarm_runtime) builds SwarmStore from
    ``swarm_runs_root()`` (agent/.swarm/runs). SwarmStore has NO default
    base_dir — ``SwarmStore()`` raises TypeError. We must reuse the exact
    same root so runs created by the runtime are visible here.
    """
    from src.swarm.store import SwarmStore, swarm_runs_root
    return SwarmStore(base_dir=swarm_runs_root())

# SSE manager singleton — populated by register_alpha_forge_routes
_sse_manager: dict[str, Any] = {}

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class AlphaForgeRunRequest(BaseModel):
    target: str = Field(..., description="目标股票代码，如 300253.SZ")
    market: str = Field(default="A-shares", description="市场")

class AlphaForgeRunResponse(BaseModel):
    run_id: str
    status: str
    target: str
    market: str
    created_at: str

class ReportMeta(BaseModel):
    report_id: str
    target: str
    stock_name: str = ""
    market: str
    analysis_date: str
    created_at: str
    signal: str = ""  # BUY / SELL / HOLD
    rating: str = ""  # Overweight / Equal-weight / Underweight

class ReportListItem(BaseModel):
    report_id: str
    target: str
    stock_name: str
    market: str
    analysis_date: str
    created_at: str
    signal: str
    rating: str

class ReportDetail(BaseModel):
    report_id: str
    target: str
    stock_name: str
    market: str
    analysis_date: str
    created_at: str
    signal: str
    rating: str
    content_md: str

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_reports_root() -> None:
    REPORTS_ROOT.mkdir(parents=True, exist_ok=True)


def _sanitize_filename(name: str) -> str:
    """Remove dangerous characters from filenames."""
    return re.sub(r"[<>:\"/\\|?*]", "_", name)


def _extract_metadata_from_md(content: str) -> dict[str, str]:
    """Extract metadata from AlphaForge markdown report.

    Prefers machine-readable blocks (``<!-- DECISION: {json} -->`` from the
    trader, ``<!-- VERDICT: {json} -->`` from the PM) for the structured
    fields (signal/rating/prices); falls back to frontmatter regex scraping so
    older reports without blocks still work.
    """
    meta: dict[str, str] = {}
    decision = _parse_decision_block(content, "DECISION")
    verdict = _parse_decision_block(content, "VERDICT")

    lines = content.split("\n")
    for line in lines[:20]:
        line = line.strip()
        if line.startswith("- **股票代码**") or line.startswith("- **股票代码**："):
            m = re.search(r"[-：:]\s*(\S+)", line)
            if m: meta["target"] = m.group(1)
        elif "**分析日期**" in line:
            m = re.search(r"[-：:]\s*(\S+)", line)
            if m: meta["analysis_date"] = m.group(1)
        elif "**生成时间**" in line:
            m = re.search(r"[-：:]\s*(\S+)", line)
            if m: meta["created_at"] = m.group(1)
        elif "**交易信号**" in line:
            m = re.search(r"\*\*(卖出|买入|持有|BUY|SELL|HOLD)\*\*", line)
            if m: meta.setdefault("signal", m.group(1))
        elif "FINAL TRANSACTION PROPOSAL" in line:
            m = re.search(r"\*\*(SELL|BUY|HOLD)\*\*", line)
            if m: meta.setdefault("signal", m.group(1))
        elif "**投资评级" in line or "最终投资评级" in line:
            m = re.search(r"[：:]\s*\**(\S+)\**", line)
            if m: meta.setdefault("rating", m.group(1).strip("*"))

    # Machine-readable blocks override the regex scraping when present.
    # VERDICT (PM, final) wins over DECISION (trader) for action/rating.
    src = {**(decision or {}), **(verdict or {})}
    if src:
        action = src.get("action", "").upper()
        action_cn = {"BUY": "买入", "SELL": "卖出", "HOLD": "持有"}.get(action, action)
        if action_cn:
            meta["signal"] = action_cn
        rating = src.get("rating")
        if rating:
            meta["rating"] = rating
        for field, key in (("entry", "entry"), ("target", "target"), ("stop", "stop_loss"), ("size_pct", "size_pct")):
            val = src.get(field)
            if val not in (None, 0, "0", ""):
                meta[key] = str(val)
    return meta


def _parse_decision_block(content: str, tag: str) -> dict | None:
    """Parse a ``<!-- {tag}: {json} -->`` machine-readable block.

    Returns the parsed JSON dict, or None if absent/malformed. The block is
    emitted by the trader (DECISION) and PM (VERDICT) agents so downstream code
    can rely on structured fields instead of scraping free-text markdown.
    """
    # Take the last occurrence (most final).
    matches = list(re.finditer(rf"<!--\s*{tag}\s*:\s*(\{{.*?\}})\s*-->", content, re.S))
    if not matches:
        return None
    try:
        return json.loads(matches[-1].group(1))
    except (ValueError, json.JSONDecodeError):
        return None


def _list_report_dirs() -> list[Path]:
    """List all report directories sorted by creation time (newest first)."""
    _ensure_reports_root()
    dirs = [d for d in REPORTS_ROOT.iterdir() if d.is_dir()]
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return dirs


def _load_report_meta(report_id: str) -> dict[str, Any] | None:
    """Load report metadata from meta.json."""
    meta_path = REPORTS_ROOT / report_id / "meta.json"
    if not meta_path.exists():
        return None
    return json.loads(meta_path.read_text(encoding="utf-8"))


def _load_report_md(report_id: str) -> str | None:
    """Load report markdown content."""
    md_path = REPORTS_ROOT / report_id / "report.md"
    if not md_path.exists():
        return None
    return md_path.read_text(encoding="utf-8")


def _save_report(report_id: str, content_md: str, meta: dict[str, Any]) -> Path:
    """Save a report to disk. Returns the report directory path."""
    _ensure_reports_root()
    report_dir = REPORTS_ROOT / report_id
    report_dir.mkdir(parents=True, exist_ok=True)

    (report_dir / "report.md").write_text(content_md, encoding="utf-8")
    (report_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


# Pipeline order for assembling the full report. Each entry maps an agent_id
# to (display section title, layer label). Ordered top-to-bottom = how the
# final report reads.
_AGENT_SECTIONS: list[tuple[str, str, str]] = [
    # Layer 1 — parallel research (8 analysts)
    ("technical_analyst", "技术分析", "第一部分：多维度研究"),
    ("sentiment_analyst", "情绪分析", "第一部分：多维度研究"),
    ("news_analyst", "新闻舆情", "第一部分：多维度研究"),
    ("fundamental_analyst", "基本面分析", "第一部分：多维度研究"),
    ("policy_analyst", "政策分析", "第一部分：多维度研究"),
    ("capital_flow_analyst", "资金面分析", "第一部分：多维度研究"),
    ("lockup_analyst", "解禁 / 减持监控", "第一部分：多维度研究"),
    ("global_market_analyst", "国际市场影响", "第一部分：多维度研究"),
    # Layer 2 — quality gate
    ("quality_gate", "质量门控结论", "第二部分：质量门控"),
    # Layer 3 — debate
    ("bull_case", "多方论证", "第三部分：多空辩论"),
    ("bear_case", "空方论证", "第三部分：多空辩论"),
    ("bull_rebuttal", "多方反驳（第二轮）", "第三部分：多空辩论"),
    ("bear_rebuttal", "空方反驳（第二轮）", "第三部分：多空辩论"),
    ("neutral_synthesis", "中性综合", "第三部分：多空辩论"),
    # Layer 4-6 — decision chain
    ("trader", "交易决策", "第四部分：交易决策"),
    ("risk_officer", "风控评估", "第五部分：风控评估"),
    ("portfolio_manager", "最终决策", "第六部分：最终决策"),
]


def _assemble_full_report(run_dir: Path, target: str, stock_name: str) -> str:
    """Assemble the complete report by concatenating every agent's report.md.

    Walks ``run_dir/artifacts/<agent_id>/report.md`` in pipeline order and
    stitches them under structured section headers. Agents that produced no
    report.md (failed / produced text only) are noted as "（无输出）" so the
    reader can see what evidence was actually gathered.

    Args:
        run_dir: The swarm run directory (.swarm/runs/<run_id>).
        target: Stock code (e.g. "300253.SZ").
        stock_name: Resolved stock name (e.g. "卫宁健康").

    Returns:
        The full markdown report string.
    """
    artifacts_dir = run_dir / "artifacts"
    parts: list[str] = []

    # --- Header ---
    display_name = f"{target}" + (f"（{stock_name}）" if stock_name else "")
    header = [
        "# AlphaForge 投研分析报告",
        "",
        f"- **股票代码**：{target}",
    ]
    if stock_name:
        header.append(f"- **股票名称**：{stock_name}")
    header.extend([
        f"- **分析日期**：{datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "- **报告类型**：AI 多 Agent 全流程投研报告（14 Agent / 6 层流水线）",
        "",
        "> ⚠️ 本报告由 AI 多 Agent 系统自动生成，仅供学习研究与技术演示，不构成任何投资建议。"
        "投资决策请咨询持牌专业机构，使用本报告所产生的任何损失由使用者自行承担。",
        "",
        "---",
        "",
    ])
    parts.append("\n".join(header))

    # --- Per-agent sections, grouped by layer ---
    current_layer = None
    for agent_id, title, layer in _AGENT_SECTIONS:
        # Emit a layer header when the layer changes
        if layer != current_layer:
            current_layer = layer
            parts.append(f"\n# {layer}\n")

        report_path = artifacts_dir / agent_id / "report.md"
        if report_path.is_file():
            body = report_path.read_text(encoding="utf-8").strip()
        else:
            # Fall back to summary.md if report.md is missing
            summary_path = artifacts_dir / agent_id / "summary.md"
            if summary_path.is_file():
                body = summary_path.read_text(encoding="utf-8").strip()
            else:
                body = "（该环节未产出可归档内容）"

        parts.append(f"\n## {title}\n")
        parts.append(body)
        parts.append("")  # blank line separator

    return "\n".join(parts).strip() + "\n"

    return report_dir


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register_alpha_forge_routes(
    app: FastAPI,
    require_auth: Callable[[Request], Awaitable[None]],
    require_event_stream_auth: Callable[[Request], Awaitable[None]],
    get_swarm_runtime: Callable[[], Any] | None = None,
) -> None:
    """Register AlphaForge routes on the FastAPI app.

    Args:
        get_swarm_runtime: Callable that returns the SwarmRuntime singleton.
            Passed from api_server.py's _get_swarm_runtime().
    """

    # ── List Reports ──────────────────────────────────────────────
    @app.get("/alpha-forge/reports")
    async def list_reports(request: Request, _=Depends(require_auth)):
        """List all saved AlphaForge reports."""
        reports: list[dict] = []
        for d in _list_report_dirs():
            meta = _load_report_meta(d.name)
            if meta:
                reports.append({
                    "report_id": d.name,
                    "target": meta.get("target", d.name),
                    "stock_name": meta.get("stock_name", ""),
                    "market": meta.get("market", "A-shares"),
                    "analysis_date": meta.get("analysis_date", ""),
                    "created_at": meta.get("created_at", ""),
                    "signal": meta.get("signal", ""),
                    "rating": meta.get("rating", ""),
                })
        return reports

    # ── Get Report Detail ─────────────────────────────────────────
    @app.get("/alpha-forge/reports/{report_id}")
    async def get_report(report_id: str, request: Request, _=Depends(require_auth)):
        """Get a full report with markdown content."""
        meta = _load_report_meta(report_id)
        content = _load_report_md(report_id)
        if meta is None or content is None:
            raise HTTPException(status_code=404, detail=f"Report {report_id!r} not found")

        return {
            "report_id": report_id,
            "target": meta.get("target", ""),
            "stock_name": meta.get("stock_name", ""),
            "market": meta.get("market", "A-shares"),
            "analysis_date": meta.get("analysis_date", ""),
            "created_at": meta.get("created_at", ""),
            "signal": meta.get("signal", ""),
            "rating": meta.get("rating", ""),
            "content_md": content,
        }

    # ── Download Report ───────────────────────────────────────────
    @app.get("/alpha-forge/reports/{report_id}/download")
    async def download_report(
        report_id: str,
        request: Request,
        format: str = Query("md", description="Download format: md or pdf"),
        _=Depends(require_auth),
    ):
        """Download a report as MD or PDF."""
        meta = _load_report_meta(report_id)
        content = _load_report_md(report_id)
        if meta is None or content is None:
            raise HTTPException(status_code=404, detail=f"Report {report_id!r} not found")

        target = meta.get("target", report_id)
        filename_base = f"AlphaForge_{target}_{meta.get('analysis_date', 'unknown')}"

        if format == "md":
            return Response(
                content=content,
                media_type="text/markdown; charset=utf-8",
                headers={
                    "Content-Disposition": f'attachment; filename="{_sanitize_filename(filename_base)}.md"',
                },
            )

        if format == "pdf":
            # Check if PDF already exists (cached)
            pdf_path = REPORTS_ROOT / report_id / "report.pdf"
            if pdf_path.exists():
                return FileResponse(
                    pdf_path,
                    media_type="application/pdf",
                    filename=f"{_sanitize_filename(filename_base)}.pdf",
                )

            # Generate PDF with weasyprint
            try:
                import markdown as md_lib
                from weasyprint import HTML

                # Convert MD to HTML
                md_html = md_lib.markdown(
                    content,
                    extensions=["tables", "fenced_code", "codehilite", "toc", "nl2br"],
                )

                html_template = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: "Microsoft YaHei", "SimSun", sans-serif; font-size: 13px; line-height: 1.7; max-width: 210mm; margin: auto; padding: 20px; color: #333; }}
  h1 {{ font-size: 22px; border-bottom: 2px solid #333; padding-bottom: 8px; }}
  h2 {{ font-size: 18px; border-bottom: 1px solid #999; padding-bottom: 4px; margin-top: 28px; }}
  h3 {{ font-size: 15px; margin-top: 20px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 11px; }}
  th, td {{ border: 1px solid #ddd; padding: 6px 8px; text-align: left; }}
  th {{ background: #f5f5f5; font-weight: bold; }}
  blockquote {{ border-left: 3px solid #ccc; margin: 10px 0; padding: 6px 16px; background: #f9f9f9; }}
  code {{ background: #f4f4f4; padding: 2px 4px; border-radius: 3px; font-size: 12px; }}
  pre {{ background: #f4f4f4; padding: 12px; border-radius: 4px; overflow-x: auto; }}
  hr {{ border: none; border-top: 1px solid #ddd; margin: 20px 0; }}
</style>
</head>
<body>{md_html}</body>
</html>"""

                pdf_bytes = HTML(string=html_template).write_pdf()
                # Cache PDF
                pdf_path.write_bytes(pdf_bytes)

                return Response(
                    content=pdf_bytes,
                    media_type="application/pdf",
                    headers={
                        "Content-Disposition": f'attachment; filename="{_sanitize_filename(filename_base)}.pdf"',
                    },
                )
            except ImportError as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"PDF generation dependency missing: {e}. Install weasyprint and markdown.",
                )
            except Exception as e:
                logger.error("PDF generation failed for %s: %s", report_id, e, exc_info=True)
                raise HTTPException(status_code=500, detail=f"PDF generation failed: {e}")

        raise HTTPException(status_code=400, detail=f"Unknown format: {format!r}. Use 'md' or 'pdf'.")

    # ── Create AlphaForge Run ─────────────────────────────────────
    @app.post("/alpha-forge/runs")
    async def create_alpha_forge_run(
        body: AlphaForgeRunRequest,
        request: Request,
        user=Depends(require_user),
    ):
        """Create a new AlphaForge analysis run using the swarm preset."""
        from src.swarm.presets import build_run_from_preset
        from src.swarm.store import SwarmStore

        if get_swarm_runtime is None:
            raise HTTPException(
                status_code=503,
                detail="Swarm runtime not available. The server was started without swarm support.",
            )

        swarm_runtime = get_swarm_runtime()

        try:
            swarm_run = swarm_runtime.start_run(
                preset_name="alpha_forge",
                user_vars={"target": body.target, "market": body.market},
            )
        except Exception as e:
            logger.error("Failed to start alpha_forge run: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to start run: {e}")

        # ── Credits: consume after the run is created (so run_id is the ref) ──
        from src.credits.store import CreditStore
        from src.credits.constants import COST_ALPHA_FORGE
        credits = CreditStore()
        if not credits.consume(user["id"], COST_ALPHA_FORGE, swarm_run.id, f"AlphaForge {body.target}"):
            # Couldn't bill — cancel the just-started run and refuse.
            try:
                swarm_runtime.cancel_run(swarm_run.id)
            except Exception:
                pass
            raise HTTPException(
                status_code=402,
                detail=f"积分不足，本次分析需要 {COST_ALPHA_FORGE} 积分",
            )
        billing_user_id = user["id"]

        # Register a completion callback to save the report
        def _on_run_complete(run_id: str) -> None:
            """Save the full assembled report when the swarm run completes."""
            try:
                store = _get_store()
                run_dir = store.run_dir(run_id)
                completed_run = store.load_run(run_id)

                # Extract stock name from whatever agent reports are available.
                # PM (portfolio_manager) is the richest source; fall back to any
                # agent's report.md, then to the run's final_report.
                code_part = body.target.split(".")[0]  # e.g. "300253"
                code_re = re.compile(
                    rf"(?:SZ|SH|BJ)?0*{re.escape(code_part)}"
                    rf"(?:\.(?:SZ|SH|BJ))?\s*[（(]?\s*([一-鿿]{{2,8}})\s*[）)]?"
                )
                label_re = re.compile(
                    r"(?:标的|股票名称|公司名称|公司|证券简称)\s*[:：]\s*"
                    r"(?:[A-Za-z0-9.\s/]+\s)?([一-鿿]{2,8})"
                )

                def _extract_name(text: str) -> str:
                    if not text:
                        return ""
                    for line in text.split("\n")[:40]:
                        m = code_re.search(line)
                        if m:
                            return m.group(1)
                    for line in text.split("\n")[:40]:
                        m = label_re.search(line)
                        if m:
                            return m.group(1)
                    return ""

                stock_name = ""
                # Try PM report first, then all agents, then final_report
                candidates = [run_dir / "artifacts" / "portfolio_manager" / "report.md"]
                candidates += [
                    run_dir / "artifacts" / a[0] / "report.md"
                    for a in _AGENT_SECTIONS
                ]
                for cand in candidates:
                    if cand.is_file():
                        stock_name = _extract_name(cand.read_text(encoding="utf-8"))
                        if stock_name:
                            break
                if not stock_name and completed_run and completed_run.final_report:
                    stock_name = _extract_name(completed_run.final_report)

                # Choose the final report content.
                # Preferred: the report_writer agent's unified report (ONE coherent
                # document, written per the strict skeleton). Falls back to the
                # multi-agent assembly only if report_writer produced nothing.
                writer_path = run_dir / "artifacts" / "report_writer" / "report.md"
                if writer_path.is_file():
                    writer_content = writer_path.read_text(encoding="utf-8").strip()
                    if len(writer_content) > 500:  # sanity: real report, not a stub
                        content = writer_content
                        logger.info("Using report_writer unified report for %s", body.target)
                    else:
                        content = _assemble_full_report(run_dir, body.target, stock_name)
                        logger.info("report_writer output too short, falling back to assembly for %s", body.target)
                else:
                    content = _assemble_full_report(run_dir, body.target, stock_name)
                    logger.info("No report_writer output, using assembly for %s", body.target)

                # Generate report ID
                now = datetime.now(timezone.utc)
                ts = now.strftime("%Y%m%d-%H%M%S")
                report_id = f"af_{body.target.replace('.', '_')}_{ts}"

                meta = {
                    "target": body.target,
                    "stock_name": stock_name,
                    "market": body.market,
                    "analysis_date": now.strftime("%Y-%m-%d"),
                    "created_at": now.isoformat(),
                    "run_id": run_id,
                }
                # Extract signal and rating from the PM section
                extra = _extract_metadata_from_md(content)
                meta.update(extra)

                # Validate the LLM decision against hard A-share rules (stop
                # ordering, position bounds, daily-limit sanity). Warnings are
                # surfaced in metadata; nothing is auto-corrected.
                try:
                    from src.analysis.decision_validator import fetch_latest_price, validate_stock_decision
                    price = fetch_latest_price(body.target)
                    decision_warnings = validate_stock_decision(meta, latest_price=price)
                    if decision_warnings:
                        meta["decision_warnings"] = decision_warnings
                        logger.warning("AlphaForge %s decision warnings: %s", body.target, decision_warnings)
                except Exception as exc:  # noqa: BLE001 — validation must never block save
                    logger.debug("decision validation skipped: %s", exc)

                _save_report(report_id, content, meta)
                logger.info("Saved AlphaForge report %s for %s", report_id, body.target)
            except Exception as e:
                logger.error("Failed to save AlphaForge report for run %s: %s", run_id, e, exc_info=True)

        # Register callback with the runtime
        swarm_runtime._live_callbacks[swarm_run.id] = lambda event: None  # placeholder
        # Poll for completion in a background thread, then save the report.
        import threading
        def _poll_completion():
            import time as time_mod
            store_obj = _get_store()
            while True:
                time_mod.sleep(5)
                try:
                    r = store_obj.load_run(swarm_run.id)
                    if r and r.status.value in ("completed", "failed", "cancelled"):
                        if r.status.value == "completed":
                            _on_run_complete(swarm_run.id)
                        else:
                            # Run failed/cancelled → refund (idempotent per run_id).
                            from src.credits.store import CreditStore
                            from src.credits.constants import COST_ALPHA_FORGE
                            CreditStore().refund(billing_user_id, COST_ALPHA_FORGE, swarm_run.id, f"AlphaForge 失败退还 {body.target}")
                        break
                except Exception:
                    break
        threading.Thread(target=_poll_completion, daemon=True).start()

        return AlphaForgeRunResponse(
            run_id=swarm_run.id,
            status=swarm_run.status.value,
            target=body.target,
            market=body.market,
            created_at=swarm_run.created_at,
        )

    # ── Get Run Status ────────────────────────────────────────────
    # ── List Runs ─────────────────────────────────────────────────
    @app.get("/alpha-forge/runs")
    async def list_alpha_forge_runs(request: Request, _=Depends(require_auth)):
        """List all AlphaForge swarm runs (filtered to alpha_forge preset)."""
        store = _get_store()
        all_runs = store.list_runs(limit=100)

        from src.swarm.task_store import TaskStore

        af_runs = []
        for r in all_runs:
            if r.preset_name != "alpha_forge":
                continue
            # Live completed count from per-task files (run.json is stale mid-layer)
            completed_count = 0
            task_count = len(r.tasks)
            try:
                task_store = TaskStore(store.run_dir(r.id))
                live_tasks = task_store.load_all()
                task_count = len(live_tasks)
                completed_count = sum(1 for t in live_tasks if t.status.value == "completed")
            except Exception:
                completed_count = sum(1 for t in r.tasks if t.status.value == "completed")

            af_runs.append({
                "run_id": r.id,
                "status": r.status.value,
                "target": (r.user_vars or {}).get("target", ""),
                "market": (r.user_vars or {}).get("market", "A-shares"),
                "preset_name": r.preset_name,
                "created_at": r.created_at,
                "completed_at": r.completed_at,
                "total_input_tokens": getattr(r, "total_input_tokens", 0),
                "total_output_tokens": getattr(r, "total_output_tokens", 0),
                "task_count": task_count,
                "completed_count": completed_count,
            })
        return af_runs

    @app.get("/alpha-forge/runs/{run_id}")
    async def get_alpha_forge_run(run_id: str, request: Request, _=Depends(require_auth)):
        """Get the status of an AlphaForge swarm run.

        Reads live per-task status from the TaskStore (tasks/*.json), NOT the
        run.json snapshot — run.json is only refreshed at layer boundaries, so
        mid-layer tasks would otherwise all read "pending" even while running.
        """
        store = _get_store()
        run = store.load_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")

        # Live task status from individual task files (real-time), falling back
        # to run.json's snapshot if TaskStore cannot load them.
        live_tasks = []
        try:
            from src.swarm.task_store import TaskStore
            run_dir = store.run_dir(run_id)
            task_store = TaskStore(run_dir)
            live_tasks = task_store.load_all()
        except Exception:
            logger.warning("Failed to load live task status for %s", run_id, exc_info=True)
            live_tasks = []

        tasks_source = live_tasks if live_tasks else run.tasks

        return {
            "run_id": run.id,
            "status": run.status.value,
            "preset_name": run.preset_name,
            "created_at": run.created_at,
            "completed_at": run.completed_at,
            "final_report": run.final_report,
            "total_input_tokens": run.total_input_tokens,
            "total_output_tokens": run.total_output_tokens,
            "tasks": [
                {
                    "id": t.id,
                    "agent_id": t.agent_id,
                    "status": t.status.value,
                }
                for t in tasks_source
            ],
        }

    # ── Force Cancel Run (disk-level, no runtime memory dependency) ──
    @app.post("/alpha-forge/runs/{run_id}/cancel")
    async def cancel_alpha_forge_run(run_id: str, request: Request, _=Depends(require_auth)):
        """Force-cancel an AlphaForge run by marking it cancelled on disk.

        Unlike ``/swarm/runs/{id}/cancel`` (which needs the run to be active
        in the current runtime's memory), this marks the run + every task as
        cancelled directly in the task files and run.json. Survives server
        restarts and handles already-dead runs. The in-flight worker threads
        (if any are still alive in the old process) will wind down on their
        own next iteration; their writes to already-cancelled task files are
        harmless no-ops.
        """
        store = _get_store()
        run = store.load_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")

        # 1. Try the graceful in-memory cancel first (works if run is active).
        cancelled_in_memory = False
        if get_swarm_runtime is not None:
            try:
                cancelled_in_memory = get_swarm_runtime().cancel_run(run_id)
            except Exception:
                cancelled_in_memory = False

        # 2. Force disk-level cancellation regardless.
        from datetime import datetime, timezone
        from src.swarm.models import RunStatus, TaskStatus
        from src.swarm.task_store import TaskStore

        run_dir = store.run_dir(run_id)
        try:
            task_store = TaskStore(run_dir)
            for t in task_store.load_all():
                if t.status not in (TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled):
                    task_store.update_status(t.id, TaskStatus.cancelled)
        except Exception:
            logger.warning("Failed to cancel task files for %s", run_id, exc_info=True)

        run.status = RunStatus.cancelled
        run.completed_at = datetime.now(timezone.utc).isoformat()
        try:
            store.update_run(run)
        except Exception:
            logger.warning("Failed to write cancelled run.json for %s", run_id, exc_info=True)

        return {
            "status": "cancelled",
            "run_id": run_id,
            "in_memory_cancel": cancelled_in_memory,
            "disk_cancel": True,
        }

    # ── SSE Events Stream ─────────────────────────────────────────
    @app.get("/alpha-forge/runs/{run_id}/events")
    async def stream_alpha_forge_events(
        run_id: str,
        request: Request,
        _=Depends(require_event_stream_auth),
    ):
        """SSE stream for live AlphaForge run progress."""
        store = _get_store()

        # Verify run exists
        run = store.load_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")

        async def event_generator():
            events_file = store.run_dir(run_id) / "events.jsonl"
            last_pos = 0

            # Replay existing events
            if events_file.exists():
                try:
                    existing = events_file.read_text(encoding="utf-8")
                    yield f"data: {json.dumps({'type': 'replay_start', 'count': len(existing.splitlines())})}\n\n"
                    for line in existing.splitlines():
                        if line.strip():
                            yield f"data: {line.strip()}\n\n"
                    last_pos = events_file.stat().st_size
                except Exception:
                    pass

            # Watch for new events
            import time as time_mod
            while True:
                if await request.is_disconnected():
                    break
                try:
                    if events_file.exists():
                        current_size = events_file.stat().st_size
                        if current_size > last_pos:
                            with open(events_file, "r", encoding="utf-8") as f:
                                f.seek(last_pos)
                                new_data = f.read()
                                for line in new_data.splitlines():
                                    if line.strip():
                                        yield f"data: {line.strip()}\n\n"
                            last_pos = current_size
                except Exception:
                    pass

                # Check if run is done
                try:
                    current = store.load_run(run_id)
                    if current and current.status.value in ("completed", "failed", "cancelled"):
                        yield f"data: {json.dumps({'type': 'run_done', 'status': current.status.value})}\n\n"
                        break
                except Exception:
                    pass

                await asyncio.sleep(1)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    logger.info("AlphaForge routes registered")
