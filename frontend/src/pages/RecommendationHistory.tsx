import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import type { LucideIcon } from "lucide-react";
import {
  ArrowUpRight,
  BarChart3,
  CalendarDays,
  ChevronDown,
  Clock3,
  GitBranch,
  History,
  Loader2,
  Newspaper,
  RefreshCw,
  Search,
  ShieldAlert,
  Sparkles,
  Star,
  Target,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import { toast } from "sonner";
import { api, type DailyRecommendationBacktestResponse, type DailyRecommendationItem } from "@/lib/api";
import { cn } from "@/lib/utils";

function fmtPct(value?: number | null): string {
  if (value === undefined || value === null || Number.isNaN(value)) return "-";
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function fmtPrice(value?: number | null): string {
  if (!value) return "-";
  return value >= 1000 ? value.toFixed(0) : value.toFixed(2);
}

function retTone(value?: number | null): string {
  if (value === undefined || value === null) return "text-muted-foreground";
  if (value > 0) return "text-success";
  if (value < 0) return "text-danger";
  return "text-muted-foreground";
}

function slotLabel(slot: string): string {
  if (slot === "morning") return "9:27";
  if (slot === "afternoon") return "14:30";
  return "手动";
}

function resultLabel(item: DailyRecommendationItem): { label: string; tone: "good" | "bad" | "warn" | "neutral" } {
  const value = item.performance.t3?.return_pct ?? item.performance.t1?.return_pct ?? item.performance.latest_return_pct;
  if (value === undefined || value === null) return { label: "数据不足", tone: "neutral" };
  if (value >= 2) return { label: "命中", tone: "good" };
  if (value <= -2) return { label: "失败", tone: "bad" };
  return { label: "观察中", tone: "warn" };
}

function aiDecisionLabel(value?: string): string {
  if (value === "recommend") return "AI复核通过";
  if (value === "watch") return "AI建议观察";
  if (value === "reject") return "AI剔除";
  return "AI待确认";
}

function factorTone(score?: number): "good" | "bad" | "warn" | "neutral" {
  if (score === undefined) return "neutral";
  if (score >= 0.6) return "good";
  if (score <= 0.4) return "bad";
  return "warn";
}

function factorLabel(item: DailyRecommendationItem): string {
  return item.factor_review?.summary || "因子待确认";
}

function recommendationStrength(item: DailyRecommendationItem): {
  label: string;
  score: number;
  stars: number;
  tone: "good" | "warn" | "neutral";
} {
  const base = Number.isFinite(item.score) ? item.score : 0.5;
  const ai = item.ai_review?.score;
  const factor = item.factor_review?.score;
  const score = base * 0.5 + (ai ?? base) * 0.3 + (factor ?? base) * 0.2;
  const normalized = Math.max(0.01, Math.min(0.99, score));

  if (normalized >= 0.78) return { label: "强推荐", score: normalized, stars: 5, tone: "good" };
  if (normalized >= 0.66) return { label: "偏强", score: normalized, stars: 4, tone: "good" };
  if (normalized >= 0.54) return { label: "中性", score: normalized, stars: 3, tone: "warn" };
  return { label: "观察", score: normalized, stars: 2, tone: "neutral" };
}

function avg(values: number[]): number | null {
  return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
}

export function RecommendationHistory() {
  const [days, setDays] = useState(30);
  const [data, setData] = useState<DailyRecommendationBacktestResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await api.getDailyRecommendationBacktest(days));
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "加载推荐历史失败");
    } finally {
      setLoading(false);
    }
  }, [days]);

  useEffect(() => {
    load();
  }, [load]);

  const groups = useMemo(() => {
    const items = data?.items ?? [];
    const byDate = new Map<string, DailyRecommendationItem[]>();
    for (const item of items) {
      const rows = byDate.get(item.date) ?? [];
      rows.push(item);
      byDate.set(item.date, rows);
    }
    return Array.from(byDate.entries())
      .sort(([a], [b]) => b.localeCompare(a))
      .map(([date, rows]) => ({
        date,
        rows: rows.sort((a, b) => {
          if (a.slot !== b.slot) return a.slot === "morning" ? -1 : 1;
          return a.rank - b.rank;
        }),
      }));
  }, [data]);

  return (
    <div className="flex h-[calc(100vh-3.5rem)] flex-col bg-muted/20">
      <header className="shrink-0 border-b bg-background px-4 py-4 md:px-6">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <div className="flex items-center gap-2">
              <History className="h-5 w-5 text-primary" />
              <h1 className="text-xl font-semibold tracking-tight">推荐历史</h1>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">按交易日复盘推荐股票、推荐后涨跌和当时推荐理由。</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {[7, 30, 90].map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => setDays(item)}
                className={cn(
                  "h-9 rounded-md border px-3 text-xs transition-colors",
                  days === item ? "border-primary/40 bg-primary/10 text-primary" : "bg-background text-muted-foreground hover:bg-muted hover:text-foreground",
                )}
              >
                近 {item} 天
              </button>
            ))}
            <button
              type="button"
              onClick={load}
              disabled={loading}
              className="inline-flex h-9 items-center gap-1.5 rounded-md border px-3 text-xs text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-50"
            >
              <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
              刷新
            </button>
            <Link to="/daily-recommendations" className="h-9 rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground hover:opacity-90">
              今日推荐
            </Link>
          </div>
        </div>
      </header>

      <main className="min-h-0 flex-1 overflow-y-auto p-4 md:p-6">
        {loading ? (
          <div className="flex h-full items-center justify-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-5 w-5 animate-spin" />
            正在加载历史表现
          </div>
        ) : !data || groups.length === 0 ? (
          <Empty />
        ) : (
          <div className="mx-auto max-w-7xl space-y-4">
            <Summary data={data} />
            <StrategySummary items={data.items} />
            {groups.map((group) => (
              <HistoryDay
                key={group.date}
                date={group.date}
                items={group.rows}
                expandedId={expandedId}
                onToggle={(id) => setExpandedId((current) => current === id ? null : id)}
              />
            ))}
          </div>
        )}
      </main>
    </div>
  );
}

function Empty() {
  return (
    <div className="mx-auto flex min-h-[360px] max-w-3xl flex-col items-center justify-center rounded-md border border-dashed bg-background px-6 text-center">
      <CalendarDays className="h-10 w-10 text-muted-foreground/40" />
      <p className="mt-3 text-sm font-medium">暂无推荐历史</p>
      <p className="mt-1 text-xs text-muted-foreground">生成过每日推荐后，这里会自动累积表现。</p>
    </div>
  );
}

function Summary({ data }: { data: DailyRecommendationBacktestResponse }) {
  return (
    <section className="grid gap-3 md:grid-cols-4">
      <Metric label="推荐数" value={`${data.summary.count}`} />
      <Metric label="T+1样本" value={`${data.summary.t1_count}`} />
      <Metric label="T+1胜率" value={data.summary.t1_win_rate === null ? "-" : `${data.summary.t1_win_rate}%`} />
      <Metric label="T+1均值" value={fmtPct(data.summary.t1_avg_return)} tone={retTone(data.summary.t1_avg_return)} />
      {data.by_slot.map((row) => (
        <div key={row.slot} className="rounded-md border bg-card p-4 md:col-span-2">
          <div className="mb-2 flex items-center justify-between">
            <p className="text-sm font-semibold">{slotLabel(row.slot)} 推荐</p>
            <span className="text-xs text-muted-foreground">{row.count} 条</span>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <Metric label="T+1胜率" value={row.t1_win_rate === null ? "-" : `${row.t1_win_rate}%`} compact />
            <Metric label="T+1均值" value={fmtPct(row.t1_avg_return)} tone={retTone(row.t1_avg_return)} compact />
          </div>
        </div>
      ))}
    </section>
  );
}

function StrategySummary({ items }: { items: DailyRecommendationItem[] }) {
  const rows = useMemo(() => {
    const grouped = new Map<string, DailyRecommendationItem[]>();
    for (const item of items) {
      const key = item.strategy || item.category || "综合推荐";
      grouped.set(key, [...(grouped.get(key) ?? []), item]);
    }
    return Array.from(grouped.entries())
      .map(([strategy, rows]) => {
        const t1 = rows.map((item) => item.performance.t1?.return_pct).filter((value): value is number => value !== undefined && value !== null);
        const t3 = rows.map((item) => item.performance.t3?.return_pct).filter((value): value is number => value !== undefined && value !== null);
        const t1Wins = t1.filter((value) => value > 0).length;
        return {
          strategy,
          count: rows.length,
          t1Count: t1.length,
          t1WinRate: t1.length ? t1Wins / t1.length * 100 : null,
          t3Avg: avg(t3),
        };
      })
      .sort((a, b) => b.count - a.count)
      .slice(0, 6);
  }, [items]);

  if (!rows.length) return null;
  return (
    <section className="rounded-md border bg-card p-4">
      <div className="mb-3 flex items-center gap-2">
        <BarChart3 className="h-4 w-4 text-primary" />
        <h2 className="text-sm font-semibold">按策略复盘</h2>
      </div>
      <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
        {rows.map((row) => (
          <div key={row.strategy} className="rounded-md border bg-background px-3 py-2">
            <div className="flex items-center justify-between gap-2">
              <p className="truncate text-sm font-medium">{row.strategy}</p>
              <span className="text-xs text-muted-foreground">{row.count}条</span>
            </div>
            <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
              <Metric label="T+1胜率" value={row.t1WinRate === null ? "-" : `${row.t1WinRate.toFixed(1)}%`} compact />
              <Metric label="T+3均值" value={fmtPct(row.t3Avg)} tone={retTone(row.t3Avg)} compact />
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function Metric({ label, value, tone, compact }: { label: string; value: string; tone?: string; compact?: boolean }) {
  return (
    <div className={cn("rounded-md border bg-card", compact ? "px-3 py-2" : "p-4")}>
      <p className={cn(compact ? "text-sm" : "text-xl", "font-semibold tabular-nums", tone)}>{value}</p>
      <p className="mt-0.5 text-[10px] text-muted-foreground">{label}</p>
    </div>
  );
}

function HistoryDay({
  date,
  items,
  expandedId,
  onToggle,
}: {
  date: string;
  items: DailyRecommendationItem[];
  expandedId: string | null;
  onToggle: (id: string) => void;
}) {
  const avg = items.length ? items.reduce((sum, item) => sum + (item.performance.latest_return_pct ?? 0), 0) / items.length : 0;
  return (
    <section className="overflow-hidden rounded-md border bg-card">
      <div className="flex flex-col gap-1 border-b bg-background px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="text-sm font-semibold">{date}</p>
          <p className="text-xs text-muted-foreground">{items.length} 条推荐</p>
        </div>
        <span className={cn("text-xs font-semibold tabular-nums", retTone(avg))}>当前平均 {fmtPct(avg)}</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[1040px] text-sm">
          <thead className="border-b bg-muted/30 text-xs text-muted-foreground">
            <tr>
              <th className="px-3 py-2 text-left font-medium">股票</th>
              <th className="px-3 py-2 text-left font-medium">时段</th>
              <th className="px-3 py-2 text-left font-medium">强度</th>
              <th className="px-3 py-2 text-left font-medium">来源/策略</th>
              <th className="px-3 py-2 text-left font-medium">推荐理由</th>
              <th className="px-3 py-2 text-right font-medium">推荐价</th>
              <th className="px-3 py-2 text-right font-medium">T+0</th>
              <th className="px-3 py-2 text-right font-medium">T+1</th>
              <th className="px-3 py-2 text-right font-medium">T+3</th>
              <th className="px-3 py-2 text-right font-medium">T+5</th>
              <th className="px-3 py-2 text-right font-medium">最新</th>
              <th className="px-3 py-2 text-left font-medium">结果</th>
              <th className="px-3 py-2 text-right font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => {
              const open = expandedId === item.id;
              const result = resultLabel(item);
              return (
                <Fragment key={item.id}>
                  <tr className={cn("border-b hover:bg-muted/25", open && "bg-primary/5")}>
                    <td className="px-3 py-3">
                      <p className="text-xs font-medium">{item.name}</p>
                      <p className="font-mono text-[10px] text-muted-foreground">{item.symbol}</p>
                    </td>
                    <td className="px-3 py-3 text-xs">{slotLabel(item.slot)}</td>
                    <td className="px-3 py-3">
                      <StrengthBadge item={item} />
                    </td>
                    <td className="px-3 py-3">
                      <p className="text-xs text-muted-foreground">{item.strategy || item.category}</p>
                      <div className="mt-1 flex flex-wrap gap-1">
                        <StatusBadge label={aiDecisionLabel(item.ai_review?.decision)} tone={item.ai_review?.decision === "recommend" ? "good" : "warn"} />
                        <StatusBadge label={factorLabel(item)} tone={factorTone(item.factor_review?.score)} />
                      </div>
                    </td>
                    <td className="max-w-[260px] px-3 py-3 text-xs text-muted-foreground">
                      <p className="line-clamp-2">{item.reason || "暂无推荐理由"}</p>
                    </td>
                    <td className="px-3 py-3 text-right font-mono text-xs">¥{fmtPrice(item.price_at_pick)}</td>
                    <ReturnCell value={item.performance.t0?.return_pct} />
                    <ReturnCell value={item.performance.t1?.return_pct} />
                    <ReturnCell value={item.performance.t3?.return_pct} />
                    <ReturnCell value={item.performance.t5?.return_pct} />
                    <ReturnCell value={item.performance.latest_return_pct} icon />
                    <td className="px-3 py-3">
                      <StatusBadge label={result.label} tone={result.tone} />
                    </td>
                    <td className="px-3 py-3 text-right">
                      <div className="flex justify-end gap-2">
                        <button
                          type="button"
                          onClick={() => onToggle(item.id)}
                          className="inline-flex items-center gap-1 rounded-md border px-2.5 py-1 text-xs hover:bg-muted"
                        >
                          复盘
                          <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", open && "rotate-180")} />
                        </button>
                        <Link to={`/logic-chain?symbol=${encodeURIComponent(item.symbol)}&q=${encodeURIComponent(item.reason || item.name)}`} className="rounded-md border px-2.5 py-1 text-xs hover:bg-muted">
                          逻辑
                        </Link>
                      </div>
                    </td>
                  </tr>
                  {open && (
                    <tr className="border-b bg-primary/5">
                      <td colSpan={13} className="px-4 py-4">
                        <HistoryDetail item={item} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function HistoryDetail({ item }: { item: DailyRecommendationItem }) {
  const q = encodeURIComponent(item.reason || item.name);
  const symbol = encodeURIComponent(item.symbol);
  return (
    <div className="grid gap-3 xl:grid-cols-[1fr_1fr_1fr_260px]">
      <DetailBlock icon={Target} title="当时为什么推荐" body={item.reason || "暂无推荐理由"} />
      <DetailBlock icon={ShieldAlert} title="风险/失效条件" body={item.risk_note || "如果价格、量能或板块同步性转弱，推荐假设需要降级。"} muted />
      <AiFactorBlock item={item} />
      <div className="rounded-md border bg-background p-3">
        <div className="mb-2 flex items-center gap-2 text-sm font-medium">
          <Search className="h-4 w-4 text-primary" />
          证据回放
        </div>
        <div className="grid gap-2">
          <EvidenceLink icon={Clock3} label="加入观察" to={`/watchlist-schedule?symbol=${symbol}`} primary />
          <EvidenceLink icon={Newspaper} label="当时新闻" to={`/news?symbol=${symbol}&q=${q}`} />
          <EvidenceLink icon={TrendingUp} label="相关事件" to={`/events?q=${q}&symbol=${symbol}`} />
          <EvidenceLink icon={GitBranch} label="逻辑链" to={`/logic-chain?symbol=${symbol}&q=${q}`} />
        </div>
      </div>
    </div>
  );
}

function AiFactorBlock({ item }: { item: DailyRecommendationItem }) {
  const ai = item.ai_review;
  const factor = item.factor_review;
  const strength = recommendationStrength(item);
  const bullish = factor?.top_bullish?.filter((entry) => entry.label).slice(0, 2) ?? [];
  const bearish = factor?.top_bearish?.filter((entry) => entry.label).slice(0, 2) ?? [];
  return (
    <div className="rounded-md border bg-background px-4 py-3">
      <div className="mb-2 flex items-center gap-2 text-sm font-medium">
        <Sparkles className="h-4 w-4 text-primary" />
        AI + 因子复核
      </div>
      <div className="grid grid-cols-2 gap-2 text-xs">
        <CompactMetric label="综合强度" value={`${strength.label} ${strength.score.toFixed(2)}`} />
        <CompactMetric label="AI评分" value={ai?.score === undefined ? "-" : ai.score.toFixed(2)} />
        <CompactMetric label="因子评分" value={factor?.score === undefined ? "-" : factor.score.toFixed(2)} />
        <CompactMetric label="AI结论" value={ai?.decision || "-"} />
      </div>
      {(ai?.summary || factor?.summary) && (
        <p className="mt-2 text-xs leading-5 text-muted-foreground">
          {ai?.summary || factor?.summary}
        </p>
      )}
      {(bullish.length > 0 || bearish.length > 0) && (
        <div className="mt-2 space-y-1 text-[11px] text-muted-foreground">
          {bullish.length > 0 && <p>偏强因子：{bullish.map((entry) => entry.label).join("、")}</p>}
          {bearish.length > 0 && <p>偏弱因子：{bearish.map((entry) => entry.label).join("、")}</p>}
        </div>
      )}
    </div>
  );
}

function CompactMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border bg-muted/20 px-2.5 py-2">
      <p className="font-semibold tabular-nums">{value}</p>
      <p className="mt-0.5 text-[10px] text-muted-foreground">{label}</p>
    </div>
  );
}

function DetailBlock({
  icon: Icon,
  title,
  body,
  muted,
}: {
  icon: LucideIcon;
  title: string;
  body: string;
  muted?: boolean;
}) {
  return (
    <div className={cn("rounded-md border px-4 py-3", muted ? "bg-background/70" : "bg-background")}>
      <div className="mb-2 flex items-center gap-2 text-sm font-medium">
        <Icon className={cn("h-4 w-4", muted ? "text-warning" : "text-primary")} />
        {title}
      </div>
      <p className="text-sm leading-6 text-foreground">{body}</p>
    </div>
  );
}

function ReturnCell({ value, icon }: { value?: number | null; icon?: boolean }) {
  return (
    <td className={cn("px-3 py-3 text-right text-xs font-semibold tabular-nums", retTone(value))}>
      {icon && value !== undefined && value !== null && (
        value >= 0 ? <TrendingUp className="mr-1 inline h-3 w-3" /> : <TrendingDown className="mr-1 inline h-3 w-3" />
      )}
      {fmtPct(value)}
    </td>
  );
}

function StrengthBadge({ item }: { item: DailyRecommendationItem }) {
  const strength = recommendationStrength(item);
  return (
    <div className="min-w-[86px]">
      <div
        className={cn(
          "inline-flex items-center gap-0.5 rounded px-2 py-1",
          strength.tone === "good" && "bg-success/10 text-success",
          strength.tone === "warn" && "bg-warning/10 text-warning",
          strength.tone === "neutral" && "bg-muted text-muted-foreground",
        )}
        title={`综合强度 ${strength.score.toFixed(2)}，由推荐分、AI评分和因子评分加权得到`}
      >
        {Array.from({ length: 5 }).map((_, index) => (
          <Star
            key={index}
            className={cn("h-3 w-3", index < strength.stars ? "fill-current" : "opacity-25")}
          />
        ))}
      </div>
      <p className="mt-1 text-[10px] font-medium text-muted-foreground">{strength.label} · {strength.score.toFixed(2)}</p>
    </div>
  );
}

function StatusBadge({ label, tone }: { label: string; tone: "good" | "bad" | "warn" | "neutral" }) {
  return (
    <span
      className={cn(
        "inline-flex rounded px-2 py-1 text-[10px] font-medium",
        tone === "good" && "bg-success/10 text-success",
        tone === "bad" && "bg-danger/10 text-danger",
        tone === "warn" && "bg-warning/10 text-warning",
        tone === "neutral" && "bg-muted text-muted-foreground",
      )}
    >
      {label}
    </span>
  );
}

function EvidenceLink({ icon: Icon, label, to, primary }: { icon: LucideIcon; label: string; to: string; primary?: boolean }) {
  return (
    <Link
      to={to}
      className={cn(
        "flex items-center justify-between rounded-md border px-3 py-2 text-xs transition",
        primary
          ? "border-primary/45 bg-primary text-primary-foreground hover:opacity-90"
          : "bg-card hover:border-primary/35 hover:bg-primary/5",
      )}
    >
      <span className="flex items-center gap-2">
        <Icon className={cn("h-3.5 w-3.5", primary ? "text-primary-foreground" : "text-primary")} />
        {label}
      </span>
      <ArrowUpRight className={cn("h-3.5 w-3.5", primary ? "text-primary-foreground" : "text-muted-foreground")} />
    </Link>
  );
}
