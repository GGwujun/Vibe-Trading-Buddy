import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import {
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  Clock,
  Download,
  FileDown,
  FileSearch,
  FileText,
  Loader2,
  Play,
  RefreshCw,
  ShieldCheck,
  Users,
  XCircle,
  Zap,
} from "lucide-react";
import { toast } from "sonner";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { api, type AlphaForgeReportDetail, type AlphaForgeReportItem, type AlphaForgeRunDetail } from "@/lib/api";
import { cn } from "@/lib/utils";

const SIGNAL_COLORS: Record<string, string> = {
  BUY: "text-success bg-success/10",
  SELL: "text-danger bg-danger/10",
  HOLD: "text-warning bg-warning/10",
  "买入": "text-success bg-success/10",
  "卖出": "text-danger bg-danger/10",
  "持有": "text-warning bg-warning/10",
  "减持": "text-danger bg-danger/10",
};

const AGENT_LABELS: Record<string, string> = {
  technical_analyst: "技术分析",
  sentiment_analyst: "情绪分析",
  news_analyst: "新闻舆情",
  fundamental_analyst: "基本面",
  policy_analyst: "政策分析",
  capital_flow_analyst: "资金追踪",
  lockup_analyst: "解禁监控",
  quality_gate: "质量门控",
  bull_case: "多方论证",
  bear_case: "空方论证",
  neutral_synthesis: "中立综合",
  trader: "交易决策",
  risk_officer: "风控评估",
  portfolio_manager: "最终裁决",
};

const TASK_STATUS_ICONS: Record<string, typeof Play> = {
  pending: Clock,
  in_progress: Loader2,
  completed: CheckCircle2,
  failed: XCircle,
  blocked: AlertTriangle,
  cancelled: XCircle,
};

const DEFAULT_PIPELINE = [
  { id: "research", title: "研究员并行分析", desc: "技术、情绪、新闻、基本面、政策、资金和解禁多维度采集。" },
  { id: "quality", title: "质量门控", desc: "检查证据完整性、冲突点和数据缺口，降低空洞结论。" },
  { id: "debate", title: "多空辩论", desc: "多方、空方和中立综合分别输出核心论据。" },
  { id: "decision", title: "交易决策", desc: "形成信号、评级、关键条件和交易计划草案。" },
  { id: "risk", title: "风控审核", desc: "审查风险暴露、止损条件和不适合交易的情形。" },
  { id: "pm", title: "PM 最终裁决", desc: "整合为可归档、可下载、可复查的投研报告。" },
];

const REPORT_SECTIONS = ["投资结论", "核心逻辑", "多方观点", "空方观点", "风险清单", "交易计划", "证据摘要"];

const MARKET_LABELS: Record<string, string> = {
  "A-shares": "A 股",
  "Hong Kong": "港股",
  US: "美股",
  crypto: "加密货币",
};

function signalBadge(signal: string) {
  const color = SIGNAL_COLORS[signal] || "text-muted-foreground bg-muted";
  return (
    <span className={cn("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-semibold", color)}>
      {signal || "暂无"}
    </span>
  );
}

function statusLabel(status?: string | null): string {
  switch (status) {
    case "pending": return "等待中";
    case "in_progress": return "执行中";
    case "completed": return "已完成";
    case "failed": return "失败";
    case "blocked": return "已阻塞";
    case "cancelled": return "已取消";
    case "success": return "成功";
    default: return status || "暂无状态";
  }
}

function shortDate(value?: string | null): string {
  return value ? value.slice(0, 10) : "暂无日期";
}

function terminalStatus(status?: string | null): boolean {
  return ["completed", "failed", "cancelled"].includes(status || "");
}

/**
 * Normalize agent-generated markdown so tables render correctly.
 *
 * LLM reports often emit a table immediately after a heading or paragraph
 * with NO blank line between them:
 *   ### 1.1 辩论评分总览
 *   | 辩论方 | 核心论据 |
 *   |--------|---------|
 *
 * Standard Markdown + GFM require a blank line before a table, otherwise the
 * whole thing is parsed as a single paragraph (the "1.1 辩论评分总览 | 辩论方..."
 * rendering bug). This inserts a blank line before any line that starts a GFM
 * table (a `|` row followed by a `|---|` separator) when the previous line is
 * non-empty. Also collapses 3+ blank lines into 1.
 */
function normalizeMarkdown(md: string): string {
  if (!md) return md;
  const lines = md.split("\n");
  const out: string[] = [];
  // GFM table separator: |---|---| or | --- | :---: |
  const isSeparator = (s: string) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$/.test(s);
  const isTableRow = (s: string) => /^\s*\|.*\|\s*$/.test(s) || /^\s*\|.*\|/.test(s);

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const prev = out[out.length - 1];
    // If this line starts a table (row + next is separator) and the previous
    // output line is non-empty & not blank, insert a blank line first.
    if (
      isTableRow(line) &&
      i + 1 < lines.length &&
      isSeparator(lines[i + 1]) &&
      prev !== undefined &&
      prev.trim() !== ""
    ) {
      out.push("");
    }
    out.push(line);
  }

  // Collapse 3+ consecutive blank lines into 1.
  const collapsed: string[] = [];
  let blankRun = 0;
  for (const l of out) {
    if (l.trim() === "") {
      blankRun += 1;
      if (blankRun <= 1) collapsed.push(l);
    } else {
      blankRun = 0;
      collapsed.push(l);
    }
  }
  return collapsed.join("\n");
}

export function AlphaForge() {
  const [stockInput, setStockInput] = useState("");
  const [market, setMarket] = useState("A-shares");
  const [reports, setReports] = useState<AlphaForgeReportItem[]>([]);
  const [reportsLoading, setReportsLoading] = useState(true);
  const [selectedReport, setSelectedReport] = useState<AlphaForgeReportDetail | null>(null);
  const [reportLoading, setReportLoading] = useState(false);
  const [activeTab, setActiveTab] = useState<"new" | "history">("new");

  const [running, setRunning] = useState(false);
  const [runInfo, setRunInfo] = useState<AlphaForgeRunDetail | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const loadReports = useCallback(async () => {
    setReportsLoading(true);
    try {
      const list = await api.listAlphaForgeReports();
      setReports(list);
    } catch {
      // The report list is nice-to-have for the workspace shell.
    } finally {
      setReportsLoading(false);
    }
  }, []);

  useEffect(() => { loadReports(); }, [loadReports]);

  const startPolling = useCallback((runId: string) => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const detail = await api.getAlphaForgeRun(runId);
        setRunInfo(detail);
        if (terminalStatus(detail.status)) {
          if (pollRef.current) clearInterval(pollRef.current);
          setRunning(false);
          if (detail.status === "completed") {
            toast.success("AlphaForge 分析完成");
            loadReports();
          } else {
            setRunError(`运行${statusLabel(detail.status)}`);
          }
        }
      } catch {
        // Polling errors are transient while the backend is busy or restarting.
      }
    }, 3000);
  }, [loadReports]);

  useEffect(() => {
    (async () => {
      try {
        const runs = await api.listAlphaForgeRuns();
        // Only resume a run that is (a) non-terminal AND (b) created within
        // the last 30 minutes. Stale/older "running" entries are almost always
        // zombie states (cancelled on disk but the list endpoint lagged, or a
        // crashed process) — resuming them would re-poll forever. The 30-min
        // window covers the longest realistic AlphaForge run.
        const STALE_MS = 30 * 60 * 1000;
        const now = Date.now();
        const inProgress = runs.find((run) => {
          if (terminalStatus(run.status)) return false;
          const createdMs = run.created_at ? Date.parse(run.created_at) : 0;
          if (!createdMs) return false; // can't verify freshness → skip
          return now - createdMs < STALE_MS;
        });
        if (inProgress) {
          setRunning(true);
          setActiveTab("new");
          const detail = await api.getAlphaForgeRun(inProgress.run_id);
          // Double-check the detailed run is still non-terminal (the list
          // snapshot can lag; the detail endpoint reads live task files).
          if (terminalStatus(detail.status)) {
            setRunning(false);
            return;
          }
          setRunInfo(detail);
          startPolling(inProgress.run_id);
          toast.info(`已恢复进行中的分析：${inProgress.target}`);
        }
      } catch {
        // Older backend builds may not expose the run list endpoint.
      }
    })();
    // Intentionally runs once on mount; startPolling is stable enough for this recovery path.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const viewReport = useCallback(async (reportId: string) => {
    setReportLoading(true);
    setActiveTab("history");
    try {
      const detail = await api.getAlphaForgeReport(reportId);
      setSelectedReport(detail);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载报告失败");
    } finally {
      setReportLoading(false);
    }
  }, []);

  const downloadReport = useCallback((reportId: string, format: "md" | "pdf") => {
    const url = api.getAlphaForgeReportDownloadUrl(reportId, format);
    window.open(url, "_blank");
  }, []);

  const startRun = useCallback(async (event: FormEvent) => {
    event.preventDefault();
    const target = stockInput.trim();
    if (!target || running) return;

    setRunning(true);
    setRunError(null);
    setRunInfo(null);
    setSelectedReport(null);
    setActiveTab("new");

    try {
      const run = await api.createAlphaForgeRun(target, market);
      setRunInfo({
        ...run,
        preset_name: "alpha_forge",
        completed_at: null,
        final_report: null,
        total_input_tokens: 0,
        total_output_tokens: 0,
        tasks: [],
      });
      startPolling(run.run_id);
    } catch (error) {
      setRunning(false);
      const message = error instanceof Error ? error.message : "启动分析失败";
      setRunError(message);
      toast.error(message);
    }
  }, [market, running, startPolling, stockInput]);

  const cancelRun = useCallback(async () => {
    if (!runInfo) return;
    try {
      await api.cancelAlphaForgeRun(runInfo.run_id);
      if (pollRef.current) clearInterval(pollRef.current);
      setRunning(false);
      toast.info("已取消分析");
    } catch {
      toast.error("取消失败");
    }
  }, [runInfo]);

  useEffect(() => {
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, []);

  const taskProgress = useMemo(() => {
    if (!runInfo?.tasks) return null;
    return {
      total: runInfo.tasks.length,
      done: runInfo.tasks.filter((task) => task.status === "completed").length,
    };
  }, [runInfo]);

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <header className="shrink-0 border-b bg-card/60 px-6 py-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-md bg-primary/10">
              <Zap className="h-4 w-4 text-primary" />
            </div>
            <div>
              <h1 className="text-lg font-semibold tracking-tight">AlphaForge</h1>
              <p className="text-xs text-muted-foreground">多 Agent 投研报告系统</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={loadReports}
              disabled={reportsLoading}
              className="inline-flex items-center gap-2 rounded-md border px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-50"
              title="刷新报告列表"
            >
              <RefreshCw className={cn("h-4 w-4", reportsLoading && "animate-spin")} />
              刷新
            </button>
          </div>
        </div>
      </header>

      <div className="shrink-0 border-b px-6">
        {([
          { id: "new" as const, icon: Play, label: "新建分析" },
          { id: "history" as const, icon: FileText, label: `历史报告 (${reports.length})` },
        ]).map((tab) => (
          <button
            key={tab.id}
            onClick={() => { setActiveTab(tab.id); if (tab.id === "history") loadReports(); }}
            className={cn(
              "inline-flex items-center gap-2 border-b-2 px-4 py-3 text-sm font-medium transition-colors",
              activeTab === tab.id
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            <tab.icon className="h-4 w-4" />
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === "new" ? (
        <NewAnalysisView
          market={market}
          running={running}
          runError={runError}
          runInfo={runInfo}
          stockInput={stockInput}
          taskProgress={taskProgress}
          onCancelRun={cancelRun}
          onMarketChange={setMarket}
          onStartRun={startRun}
          onStockInputChange={setStockInput}
        />
      ) : (
        <HistoryView
          reports={reports}
          reportsLoading={reportsLoading}
          reportLoading={reportLoading}
          selectedReport={selectedReport}
          onDownloadReport={downloadReport}
          onViewReport={viewReport}
        />
      )}
    </div>
  );
}

function NewAnalysisView({
  market,
  running,
  runError,
  runInfo,
  stockInput,
  taskProgress,
  onCancelRun,
  onMarketChange,
  onStartRun,
  onStockInputChange,
}: {
  market: string;
  running: boolean;
  runError: string | null;
  runInfo: AlphaForgeRunDetail | null;
  stockInput: string;
  taskProgress: { total: number; done: number } | null;
  onCancelRun: () => void;
  onMarketChange: (value: string) => void;
  onStartRun: (event: FormEvent) => void;
  onStockInputChange: (value: string) => void;
}) {
  return (
    <main className="flex-1 overflow-auto">
      <div className="grid min-h-full gap-6 p-6 xl:grid-cols-[360px_minmax(0,1fr)_320px]">
        <section className="space-y-4">
          <Panel title="任务配置" desc="选择标的和市场，启动一条可追踪的投研流水线。">
            <form onSubmit={onStartRun} className="space-y-4">
              <label className="grid gap-2">
                <span className="text-sm font-medium">股票/资产代码</span>
                <input
                  value={stockInput}
                  onChange={(event) => onStockInputChange(event.target.value)}
                  placeholder="例如：300253.SZ"
                  className="w-full rounded-md border bg-background px-3 py-2 text-sm outline-none transition focus:ring-2 focus:ring-primary/30"
                  disabled={running}
                />
              </label>

              <label className="grid gap-2">
                <span className="text-sm font-medium">市场</span>
                <select
                  value={market}
                  onChange={(event) => onMarketChange(event.target.value)}
                  className="w-full rounded-md border bg-background px-3 py-2 text-sm outline-none transition focus:ring-2 focus:ring-primary/30"
                  disabled={running}
                >
                  <option value="A-shares">A 股</option>
                  <option value="Hong Kong">港股</option>
                  <option value="US">美股</option>
                  <option value="crypto">加密货币</option>
                </select>
              </label>

              <button
                type="submit"
                disabled={!stockInput.trim() || running}
                className="inline-flex w-full items-center justify-center gap-2 rounded-md bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                {running ? "分析中" : "开始分析"}
              </button>
            </form>
          </Panel>

          {(running || runInfo || runError) && (
            <Panel title="运行状态" desc={runInfo ? `${runInfo.run_id} · ${MARKET_LABELS[market] || market}` : undefined}>
              <RunStatusCard
                running={running}
                runError={runError}
                runInfo={runInfo}
                taskProgress={taskProgress}
                onCancelRun={onCancelRun}
              />
            </Panel>
          )}
        </section>

        <section className="space-y-4">
          <Panel
            title="Agent 流水线"
            desc="从研究员并行分析到 PM 最终裁决，展示报告生成过程中的关键角色。"
          >
            <AgentPipeline runInfo={runInfo} />
          </Panel>

          <Panel title="当前输出" desc="运行完成后，报告会进入历史报告并支持 Markdown / PDF 下载。">
            {runInfo?.status === "completed" ? (
              <div className="flex items-start gap-3 rounded-md border border-success/30 bg-success/5 p-3 text-sm">
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" />
                <div>
                  <div className="font-medium text-success">分析已完成</div>
                  <p className="mt-1 text-xs text-muted-foreground">报告已保存到历史报告列表，可切换到“历史报告”查看和下载。</p>
                </div>
              </div>
            ) : (
              <div className="grid gap-2 sm:grid-cols-2">
                {REPORT_SECTIONS.map((section) => (
                  <div key={section} className="rounded-md border bg-muted/20 px-3 py-2 text-sm">
                    {section}
                  </div>
                ))}
              </div>
            )}
          </Panel>
        </section>

        <aside className="space-y-4">
          <Panel title="报告说明" desc="AlphaForge 适合正式、可归档的单标的深度研究。">
            <div className="space-y-3 text-sm text-muted-foreground">
              <InfoLine icon={Users} title="多 Agent 协作" desc="多个专业角色并行分析，再经过质量门控和观点综合。" />
              <InfoLine icon={ShieldCheck} title="风险先行" desc="报告输出会包含风险清单、止损条件和不适合交易的情形。" />
              <InfoLine icon={FileSearch} title="可复查报告" desc="投研结论以 Markdown 和 PDF 形式归档，方便后续复盘。" />
            </div>
          </Panel>

          <Panel title="建议用法">
            <ol className="space-y-2 text-sm text-muted-foreground">
              <li className="flex gap-2"><span className="font-mono text-primary">1.</span>先从机会清单或新闻页发现标的。</li>
              <li className="flex gap-2"><span className="font-mono text-primary">2.</span>用逻辑链做快速初筛。</li>
              <li className="flex gap-2"><span className="font-mono text-primary">3.</span>用 AlphaForge 生成正式报告。</li>
              <li className="flex gap-2"><span className="font-mono text-primary">4.</span>把结论带入跟踪看板或策略验证。</li>
            </ol>
          </Panel>
        </aside>
      </div>
    </main>
  );
}

function RunStatusCard({
  running,
  runError,
  runInfo,
  taskProgress,
  onCancelRun,
}: {
  running: boolean;
  runError: string | null;
  runInfo: AlphaForgeRunDetail | null;
  taskProgress: { total: number; done: number } | null;
  onCancelRun: () => void;
}) {
  const status = runInfo?.status;
  const progressPct = taskProgress?.total ? Math.round((taskProgress.done / taskProgress.total) * 100) : 0;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <span className="text-sm font-medium">{statusLabel(status || (running ? "in_progress" : null))}</span>
        {running ? (
          <span className="inline-flex items-center gap-1 text-xs font-medium text-primary">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            正在运行
          </span>
        ) : status === "completed" ? (
          <span className="inline-flex items-center gap-1 text-xs font-medium text-success">
            <CheckCircle2 className="h-3.5 w-3.5" />
            已完成
          </span>
        ) : null}
      </div>

      {taskProgress && (
        <>
          <div className="h-2 overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${progressPct}%` }} />
          </div>
          <div className="flex justify-between text-xs text-muted-foreground">
            <span>{taskProgress.done}/{taskProgress.total} 个 Agent 完成</span>
            <span>{progressPct}%</span>
          </div>
        </>
      )}

      {runInfo?.tasks && runInfo.tasks.length > 0 && (
        <div className="max-h-56 space-y-1 overflow-auto">
          {runInfo.tasks.map((task) => {
            const Icon = TASK_STATUS_ICONS[task.status] || Clock;
            return (
              <div key={task.id} className="grid grid-cols-[1rem_minmax(0,1fr)_auto] items-center gap-2 rounded-md border bg-muted/20 px-2 py-1.5 text-xs">
                <Icon className={cn(
                  "h-3.5 w-3.5",
                  task.status === "in_progress" && "animate-spin text-primary",
                  task.status === "completed" && "text-success",
                  task.status === "failed" && "text-danger",
                )} />
                <span className="truncate">{AGENT_LABELS[task.agent_id] || task.agent_id}</span>
                <span className="text-muted-foreground">{statusLabel(task.status)}</span>
              </div>
            );
          })}
        </div>
      )}

      {running && (
        <button
          onClick={onCancelRun}
          className="inline-flex w-full items-center justify-center gap-2 rounded-md border border-danger/40 px-3 py-2 text-sm font-medium text-danger transition-colors hover:bg-danger/5"
        >
          <XCircle className="h-4 w-4" />
          取消分析
        </button>
      )}

      {runError && (
        <div className="rounded-md border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">
          {runError}
        </div>
      )}
    </div>
  );
}

function AgentPipeline({ runInfo }: { runInfo: AlphaForgeRunDetail | null }) {
  const tasksByAgent = new Map((runInfo?.tasks || []).map((task) => [task.agent_id, task.status]));

  return (
    <div className="grid gap-3 lg:grid-cols-2">
      {DEFAULT_PIPELINE.map((step, index) => {
        const relatedStatuses = Array.from(tasksByAgent.entries())
          .filter(([agent]) => pipelineMatchesAgent(step.id, agent))
          .map(([, status]) => status);
        const completed = relatedStatuses.length > 0 && relatedStatuses.every((status) => status === "completed");
        const active = relatedStatuses.includes("in_progress");
        return (
          <div key={step.id} className="rounded-md border bg-card p-3">
            <div className="flex items-start gap-3">
              <div className={cn(
                "flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-xs font-semibold",
                completed ? "bg-success/10 text-success" : active ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground",
              )}>
                {completed ? <CheckCircle2 className="h-4 w-4" /> : index + 1}
              </div>
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <h3 className="text-sm font-semibold">{step.title}</h3>
                  {active && <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />}
                </div>
                <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{step.desc}</p>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function pipelineMatchesAgent(stepId: string, agentId: string): boolean {
  if (stepId === "research") return agentId.endsWith("_analyst") || agentId === "capital_flow_analyst" || agentId === "lockup_analyst";
  if (stepId === "quality") return agentId === "quality_gate";
  if (stepId === "debate") return ["bull_case", "bear_case", "neutral_synthesis"].includes(agentId);
  if (stepId === "decision") return agentId === "trader";
  if (stepId === "risk") return agentId === "risk_officer";
  if (stepId === "pm") return agentId === "portfolio_manager";
  return false;
}

function HistoryView({
  reports,
  reportsLoading,
  reportLoading,
  selectedReport,
  onDownloadReport,
  onViewReport,
}: {
  reports: AlphaForgeReportItem[];
  reportsLoading: boolean;
  reportLoading: boolean;
  selectedReport: AlphaForgeReportDetail | null;
  onDownloadReport: (reportId: string, format: "md" | "pdf") => void;
  onViewReport: (reportId: string) => void;
}) {
  return (
    <div className="flex min-h-0 flex-1 overflow-hidden">
      <aside className="w-80 shrink-0 overflow-auto border-r bg-card/50 p-4">
        <div className="mb-3 flex items-center justify-between gap-3">
          <h2 className="text-sm font-semibold">历史报告</h2>
          {reportsLoading && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />}
        </div>
        {reportsLoading ? (
          <div className="flex justify-center py-8 text-muted-foreground">
            <Loader2 className="h-5 w-5 animate-spin" />
          </div>
        ) : reports.length === 0 ? (
          <p className="rounded-md border border-dashed px-3 py-6 text-center text-xs text-muted-foreground">
            暂无报告，请先新建分析。
          </p>
        ) : (
          <div className="space-y-1">
            {reports.map((report) => (
              <button
                key={report.report_id}
                onClick={() => onViewReport(report.report_id)}
                className={cn(
                  "w-full rounded-md border border-transparent px-3 py-2.5 text-left transition-colors hover:bg-muted",
                  selectedReport?.report_id === report.report_id && "border-primary/30 bg-primary/10",
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-medium">{report.stock_name || report.target}</span>
                  {signalBadge(report.signal)}
                </div>
                <div className="mt-1 flex items-center gap-2 text-[11px] text-muted-foreground">
                  <span className="font-mono">{report.target}</span>
                  <span>{MARKET_LABELS[report.market] || report.market}</span>
                  <span>{shortDate(report.analysis_date || report.created_at)}</span>
                </div>
              </button>
            ))}
          </div>
        )}
      </aside>

      <main className="min-w-0 flex-1 overflow-auto">
        {reportLoading ? (
          <div className="flex h-full items-center justify-center text-muted-foreground">
            <Loader2 className="mr-2 h-5 w-5 animate-spin" />
            正在加载报告…
          </div>
        ) : selectedReport ? (
          <ReportViewer
            report={selectedReport}
            onDownload={(format) => onDownloadReport(selectedReport.report_id, format)}
          />
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-3 text-muted-foreground">
            <BarChart3 className="h-12 w-12 opacity-30" />
            <p className="text-sm">选择一份报告查看</p>
            <p className="text-xs opacity-70">从左侧列表选择已有报告，或回到新建分析生成新的报告。</p>
          </div>
        )}
      </main>
    </div>
  );
}

function ReportViewer({
  report,
  onDownload,
}: {
  report: AlphaForgeReportDetail;
  onDownload: (format: "md" | "pdf") => void;
}) {
  return (
    <div className="mx-auto max-w-5xl p-6">
      <div className="mb-6 flex flex-col gap-4 border-b pb-4 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="mb-2 flex flex-wrap items-center gap-3">
            <h2 className="text-xl font-semibold">{report.stock_name || report.target}</h2>
            {signalBadge(report.signal)}
            {report.rating && (
              <span className="rounded-full bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground">
                {report.rating}
              </span>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
            <span className="font-mono">{report.target}</span>
            <span>{MARKET_LABELS[report.market] || report.market}</span>
            <span>分析日期：{shortDate(report.analysis_date || report.created_at)}</span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => onDownload("md")}
            className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium transition-colors hover:bg-muted"
          >
            <FileDown className="h-3.5 w-3.5" />
            下载 Markdown
          </button>
          <button
            onClick={() => onDownload("pdf")}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-opacity hover:opacity-90"
          >
            <Download className="h-3.5 w-3.5" />
            下载 PDF
          </button>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_240px]">
        <article className="prose prose-sm max-w-none dark:prose-invert prose-headings:border-b prose-headings:pb-2 prose-headings:mt-8 prose-headings:mb-4 prose-h1:text-2xl prose-h2:text-xl prose-h3:text-lg prose-table:text-xs prose-th:bg-muted/50 prose-th:font-semibold prose-td:px-3 prose-td:py-2 prose-blockquote:border-l-primary prose-blockquote:bg-muted/20 prose-blockquote:px-4 prose-blockquote:py-1 prose-code:rounded prose-code:bg-muted prose-code:px-1 prose-code:py-0.5 prose-li:text-sm prose-p:text-sm [&_pre]:bg-muted/40 [&_pre]:p-4 [&_pre]:rounded-lg [&_pre]:overflow-x-auto [&_pre]:text-[12px] [&_pre]:leading-[1.5] [&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_pre_code]:font-mono [&_pre_code]:whitespace-pre [&_pre_code]:text-foreground/90 [&_pre_code]:tracking-tight [&_code]:font-mono">
          <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
            {normalizeMarkdown(report.content_md)}
          </ReactMarkdown>
        </article>

        <aside className="space-y-3">
          <div className="rounded-md border bg-card p-3">
            <h3 className="text-sm font-semibold">报告目录</h3>
            <div className="mt-2 space-y-1 text-xs text-muted-foreground">
              {REPORT_SECTIONS.map((section) => (
                <div key={section} className="rounded bg-muted/30 px-2 py-1">{section}</div>
              ))}
            </div>
          </div>
          <div className="rounded-md border border-warning/30 bg-warning/5 p-3">
            <div className="flex items-start gap-2">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
              <div className="text-xs text-muted-foreground">
                <p className="font-semibold text-warning">免责声明</p>
                <p className="mt-1 leading-relaxed">
                  本报告由 AI 多 Agent 系统自动生成，仅供学习研究与技术演示，不构成任何投资建议。投资决策请咨询持牌专业机构。
                </p>
              </div>
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}

function Panel({
  title,
  desc,
  children,
}: {
  title: string;
  desc?: string;
  children: ReactNode;
}) {
  return (
    <section className="rounded-md border bg-card">
      <div className="border-b px-4 py-3">
        <h2 className="text-sm font-semibold">{title}</h2>
        {desc && <p className="mt-0.5 text-xs text-muted-foreground">{desc}</p>}
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

function InfoLine({
  icon: Icon,
  title,
  desc,
}: {
  icon: typeof Users;
  title: string;
  desc: string;
}) {
  return (
    <div className="flex gap-3">
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
        <Icon className="h-4 w-4" />
      </div>
      <div>
        <div className="font-medium text-foreground">{title}</div>
        <p className="mt-0.5 text-xs leading-relaxed">{desc}</p>
      </div>
    </div>
  );
}
