import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  ArrowRight,
  BarChart3,
  Bot,
  CalendarClock,
  CheckCircle2,
  GitBranch,
  Layers,
  Lightbulb,
  Loader2,
  Newspaper,
  RefreshCw,
  ShieldCheck,
  Target,
  TrendingDown,
  TrendingUp,
  Zap,
} from "lucide-react";
import { api, type AlphaForgeReportItem, type OpportunityCategory, type RunListItem } from "@/lib/api";
import { cached as cachedFetch } from "@/lib/cache";
import { cn } from "@/lib/utils";

interface DashboardState {
  opportunities: OpportunityCategory[];
  reports: AlphaForgeReportItem[];
  runs: RunListItem[];
  errors: string[];
}

const QUICK_ACTIONS = [
  {
    to: "/agent",
    icon: Bot,
    title: "发起开放研究",
    desc: "让智能体读取数据、网页、文件并给出可追踪结论。",
  },
  {
    to: "/alpha-forge",
    icon: Zap,
    title: "生成投研报告",
    desc: "用多 Agent 流水线完成单标的深度分析。",
  },
  {
    to: "/tracking-dashboard",
    icon: Target,
    title: "检查持仓动作",
    desc: "从趋势、资金、事件和风险维度审视仓位。",
  },
  {
    to: "/alpha-zoo",
    icon: Layers,
    title: "验证因子想法",
    desc: "浏览、筛选并批量评估内置 Alpha 因子。",
  },
];

const INTELLIGENCE_LINKS = [
  { to: "/news", icon: Newspaper, label: "新闻", desc: "市场要闻与自选股新闻" },
  { to: "/events", icon: CalendarClock, label: "事件", desc: "概率事件与历史变化" },
  { to: "/opportunity", icon: Lightbulb, label: "机会", desc: "系统扫描出的候选标的" },
  { to: "/logic-chain", icon: GitBranch, label: "逻辑链", desc: "从宏观到交易的分层推理" },
];

function formatPct(value: number | undefined): string {
  if (!Number.isFinite(value)) return "暂无";
  const pct = Number(value) * 100;
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`;
}

function formatChange(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function shortDate(value?: string | null): string {
  if (!value) return "暂无日期";
  return value.slice(0, 10);
}

function signalTone(signal?: string): string {
  const normalized = (signal || "").toUpperCase();
  if (["BUY", "买入"].includes(normalized) || signal === "买入") return "text-success bg-success/10";
  if (["SELL", "卖出", "减持"].includes(normalized) || signal === "卖出" || signal === "减持") return "text-danger bg-danger/10";
  if (["HOLD", "持有"].includes(normalized) || signal === "持有") return "text-warning bg-warning/10";
  return "text-muted-foreground bg-muted";
}

// Cached fetchers for the three dashboard sources. 60s TTL; module-level so
// navigating away and back returns instantly, with a silent background refresh.
const oppCache = cachedFetch("home:opportunities", () => api.listOpportunities(), 60_000);
const reportsCache = cachedFetch<AlphaForgeReportItem[]>("home:reports", () => api.listAlphaForgeReports(), 60_000);
const runsCache = cachedFetch<RunListItem[]>("home:runs", () => api.listRuns(), 60_000);

export function Home() {
  const [data, setData] = useState<DashboardState>({
    opportunities: [],
    reports: [],
    runs: [],
    errors: [],
  });
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  // stale-while-revalidate: read() returns cached data instantly (if present)
  // and triggers a background refresh when stale. First-ever load waits.
  const loadDashboard = async () => {
    const errors: string[] = [];
    const [oppR, repR, runR] = await Promise.allSettled([
      oppCache.read(),
      reportsCache.read(),
      runsCache.read(),
    ]);

    const opportunities = oppR.status === "fulfilled" ? oppR.value.data.categories : [];
    if (oppR.status === "rejected") errors.push("机会清单暂不可用");

    const reports = repR.status === "fulfilled" ? repR.value.data : [];
    if (repR.status === "rejected") errors.push("投研报告暂不可用");

    const runs = runR.status === "fulfilled" ? runR.value.data : [];
    if (runR.status === "rejected") errors.push("回测记录暂不可用");

    setData({ opportunities, reports, runs, errors });
    setLoading(false);
  };

  // Force re-fetch (refresh button) — bypasses TTL.
  const forceRefresh = async () => {
    setRefreshing(true);
    const errors: string[] = [];
    const [oppR, repR, runR] = await Promise.allSettled([
      oppCache.refresh(),
      reportsCache.refresh(),
      runsCache.refresh(),
    ]);
    const opportunities = oppR.status === "fulfilled" ? oppR.value.categories : [];
    if (oppR.status === "rejected") errors.push("机会清单暂不可用");
    const reports = repR.status === "fulfilled" ? repR.value : [];
    if (repR.status === "rejected") errors.push("投研报告暂不可用");
    const runs = runR.status === "fulfilled" ? runR.value : [];
    if (runR.status === "rejected") errors.push("回测记录暂不可用");
    setData({ opportunities, reports, runs, errors });
    setRefreshing(false);
  };

  useEffect(() => {
    loadDashboard();
    // After the initial read (which may have kicked off bg refreshes), poll
    // once more shortly after so a stale-while-revalidate update is picked up
    // without the user clicking refresh.
    const t = setTimeout(() => { loadDashboard(); }, 2500);
    return () => clearTimeout(t);
  }, []);

  const topOpportunities = useMemo(
    () => data.opportunities.flatMap((category) => category.opportunities).slice(0, 5),
    [data.opportunities],
  );
  const latestReports = data.reports.slice(0, 4);
  const latestRuns = data.runs.slice(0, 4);

  const reportCount = data.reports.length;
  const opportunityCount = data.opportunities.reduce((sum, category) => sum + category.opportunities.length, 0);
  const successfulRuns = data.runs.filter((run) => run.status === "success").length;

  return (
    <div className="min-h-full bg-background">
      <header className="border-b bg-card/80">
        <div className="mx-auto flex max-w-7xl flex-col gap-4 px-4 py-5 md:px-6">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
            <div>
              <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
                <BarChart3 className="h-4 w-4 text-primary" />
                工作台
              </div>
              <h1 className="mt-1 text-2xl font-semibold tracking-tight">今日总览</h1>
              <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
                汇总机会、投研报告和回测记录，把分散功能收束成下一步行动。
              </p>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={forceRefresh}
                disabled={refreshing || loading}
                className="inline-flex items-center gap-2 rounded-md border bg-background px-3 py-2 text-sm text-muted-foreground transition-colors hover:border-primary/35 hover:bg-primary/5 hover:text-foreground disabled:opacity-50"
              >
                <RefreshCw className={cn("h-4 w-4", refreshing && "animate-spin")} />
                刷新
              </button>
              <Link
                to="/agent"
                className="inline-flex items-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90"
              >
                开始研究
                <ArrowRight className="h-4 w-4" />
              </Link>
            </div>
          </div>

          {data.errors.length > 0 && (
            <div className="rounded-md border border-warning/30 bg-warning/5 px-3 py-2 text-xs text-muted-foreground">
              {data.errors.join("，")}。请检查后端服务、鉴权或数据源配置。
            </div>
          )}
        </div>
      </header>

      <main className="mx-auto max-w-7xl space-y-6 px-4 py-6 md:px-6">
        {loading ? (
          <div className="flex min-h-64 items-center justify-center rounded-md border bg-card text-sm text-muted-foreground">
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            正在加载工作台…
          </div>
        ) : (
          <>
            <section className="grid gap-3 md:grid-cols-3">
              <MetricPanel
                icon={Lightbulb}
                label="候选机会"
                value={String(opportunityCount)}
                desc={topOpportunities[0]?.name ? `最高置信度：${topOpportunities[0].name}` : "暂无扫描结果"}
              />
              <MetricPanel
                icon={Zap}
                label="投研报告"
                value={String(reportCount)}
                desc={latestReports[0] ? `最新：${latestReports[0].stock_name || latestReports[0].target}` : "暂无报告"}
              />
              <MetricPanel
                icon={ShieldCheck}
                label="成功回测"
                value={`${successfulRuns}/${data.runs.length}`}
                desc={latestRuns[0]?.prompt ? latestRuns[0].prompt.slice(0, 28) : "暂无回测记录"}
              />
            </section>

            <section className="grid gap-6 xl:grid-cols-[minmax(0,1.25fr)_minmax(360px,0.75fr)]">
              <div className="space-y-6">
                <Panel
                  title="下一步行动"
                  desc="按当前工作流选择入口，避免在页面之间来回找功能。"
                >
                  <div className="grid gap-3 md:grid-cols-2">
                    {QUICK_ACTIONS.map(({ to, icon: Icon, title, desc }) => (
                      <Link
                        key={to}
                        to={to}
                        className="group rounded-md border bg-card p-4 transition-colors hover:border-primary/45 hover:bg-primary/[0.03]"
                      >
                        <div className="flex items-start gap-3">
                          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-primary/15 bg-primary/10 text-primary">
                            <Icon className="h-4 w-4" />
                          </div>
                          <div className="min-w-0">
                            <div className="flex items-center gap-2">
                              <h3 className="text-sm font-semibold">{title}</h3>
                              <ArrowRight className="h-3.5 w-3.5 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
                            </div>
                            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{desc}</p>
                          </div>
                        </div>
                      </Link>
                    ))}
                  </div>
                </Panel>

                <Panel
                  title="机会清单"
                  desc="来自市场扫描的候选标的，适合继续做逻辑链或 AlphaForge 深度研究。"
                  action={<Link to="/opportunity" className="text-xs text-primary hover:underline">查看全部</Link>}
                >
                  {topOpportunities.length === 0 ? (
                    <EmptyState text="暂无机会数据。请确认数据源后刷新。" />
                  ) : (
                    <div className="overflow-hidden rounded-md border bg-card">
                      <table className="w-full text-sm">
                        <thead className="bg-muted/35 text-xs text-muted-foreground">
                          <tr>
                            <th className="px-3 py-2 text-left font-medium">标的</th>
                            <th className="px-3 py-2 text-right font-medium">价格</th>
                            <th className="px-3 py-2 text-right font-medium">涨跌</th>
                            <th className="px-3 py-2 text-right font-medium">置信度</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y">
                          {topOpportunities.map((item) => (
                            <tr key={`${item.category}-${item.symbol}`} className="hover:bg-primary/[0.03]">
                              <td className="px-3 py-2">
                                <div className="font-medium">{item.name}</div>
                                <div className="font-mono text-[11px] text-muted-foreground">{item.symbol}</div>
                              </td>
                              <td className="px-3 py-2 text-right font-mono tabular-nums">{item.price.toFixed(2)}</td>
                              <td className={cn("px-3 py-2 text-right font-mono tabular-nums", item.change_pct >= 0 ? "text-success" : "text-danger")}>
                                {item.change_pct >= 0 ? <TrendingUp className="mr-1 inline h-3 w-3" /> : <TrendingDown className="mr-1 inline h-3 w-3" />}
                                {formatChange(item.change_pct)}
                              </td>
                              <td className="px-3 py-2 text-right font-mono tabular-nums">{Math.round(item.confidence * 100)}%</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </Panel>
              </div>

              <div className="space-y-6">
                <Panel title="市场情报" desc="把新闻、事件、机会和推理工具串成研究入口。">
                  <div className="space-y-2">
                    {INTELLIGENCE_LINKS.map(({ to, icon: Icon, label, desc }) => (
                      <Link key={to} to={to} className="group flex items-center gap-3 rounded-md border bg-card px-3 py-2.5 transition-colors hover:border-primary/35 hover:bg-primary/[0.03]">
                        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-primary/15 bg-primary/10 text-primary">
                          <Icon className="h-4 w-4" />
                        </span>
                        <div className="min-w-0 flex-1">
                          <div className="text-sm font-medium">{label}</div>
                          <div className="truncate text-xs text-muted-foreground">{desc}</div>
                        </div>
                        <ArrowRight className="h-3.5 w-3.5 text-muted-foreground transition-colors group-hover:text-primary" />
                      </Link>
                    ))}
                  </div>
                </Panel>

                <Panel
                  title="最近投研报告"
                  desc="AlphaForge 产出的报告和信号。"
                  action={<Link to="/alpha-forge" className="text-xs text-primary hover:underline">进入 AlphaForge</Link>}
                >
                  {latestReports.length === 0 ? (
                    <EmptyState text="暂无投研报告。可以先生成一份 AlphaForge 报告。" />
                  ) : (
                    <div className="space-y-2">
                      {latestReports.map((report) => (
                        <Link
                          key={report.report_id}
                          to="/alpha-forge"
                          className="block rounded-md border bg-card px-3 py-2 transition-colors hover:border-primary/35 hover:bg-primary/[0.03]"
                        >
                          <div className="flex items-center justify-between gap-2">
                            <span className="truncate text-sm font-medium">{report.stock_name || report.target}</span>
                            <span className={cn("shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium", signalTone(report.signal))}>
                              {report.signal || "暂无"}
                            </span>
                          </div>
                          <div className="mt-1 flex items-center gap-2 text-[11px] text-muted-foreground">
                            <span className="font-mono">{report.target}</span>
                            <span>{report.market}</span>
                            <span>{shortDate(report.analysis_date || report.created_at)}</span>
                          </div>
                        </Link>
                      ))}
                    </div>
                  )}
                </Panel>

                <Panel
                  title="最近回测"
                  desc="策略实验室的最新运行记录。"
                  action={<Link to="/compare" className="text-xs text-primary hover:underline">对比回测</Link>}
                >
                  {latestRuns.length === 0 ? (
                    <EmptyState text="暂无回测记录。可以从智能体或因子工厂发起验证。" />
                  ) : (
                    <div className="space-y-2">
                      {latestRuns.map((run) => (
                        <Link
                          key={run.run_id}
                          to={`/runs/${run.run_id}`}
                          className="block rounded-md border bg-card px-3 py-2 transition-colors hover:border-primary/35 hover:bg-primary/[0.03]"
                        >
                          <div className="flex items-center justify-between gap-2">
                            <span className="truncate text-sm font-medium">{run.prompt || run.run_id}</span>
                            <span className={cn("shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium", run.status === "success" ? "bg-success/10 text-success" : "bg-muted text-muted-foreground")}>
                              {run.status === "success" ? "成功" : run.status}
                            </span>
                          </div>
                          <div className="mt-1 flex items-center justify-between gap-2 text-[11px] text-muted-foreground">
                            <span>{shortDate(run.created_at)}</span>
                            <span className="font-mono">收益 {formatPct(run.total_return)} / 夏普 {run.sharpe?.toFixed(2) ?? "暂无"}</span>
                          </div>
                        </Link>
                      ))}
                    </div>
                  )}
                </Panel>
              </div>
            </section>
          </>
        )}
      </main>
    </div>
  );
}

function MetricPanel({
  icon: Icon,
  label,
  value,
  desc,
}: {
  icon: typeof BarChart3;
  label: string;
  value: string;
  desc: string;
}) {
  return (
    <div className="rounded-md border bg-card p-4 shadow-sm shadow-black/[0.02]">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-xs font-medium text-muted-foreground">{label}</div>
          <div className="mt-1 text-2xl font-semibold tracking-tight">{value}</div>
        </div>
        <div className="flex h-9 w-9 items-center justify-center rounded-md border border-primary/15 bg-primary/10 text-primary">
          <Icon className="h-4 w-4" />
        </div>
      </div>
      <p className="mt-3 truncate text-xs text-muted-foreground">{desc}</p>
    </div>
  );
}

function Panel({
  title,
  desc,
  action,
  children,
}: {
  title: string;
  desc?: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="rounded-md border bg-card shadow-sm shadow-black/[0.02]">
      <div className="flex items-start justify-between gap-3 border-b bg-muted/15 px-4 py-3">
        <div>
          <h2 className="text-sm font-semibold">{title}</h2>
          {desc && <p className="mt-0.5 text-xs text-muted-foreground">{desc}</p>}
        </div>
        {action}
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

function EmptyState({ text }: { text: string }) {
  return (
    <div className="flex min-h-24 items-center justify-center rounded-md border border-dashed text-center text-sm text-muted-foreground">
      <CheckCircle2 className="mr-2 h-4 w-4 opacity-60" />
      {text}
    </div>
  );
}
