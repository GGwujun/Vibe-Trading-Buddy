import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import type { LucideIcon } from "lucide-react";
import {
  ArrowUpRight,
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

type SlotFilter = "all" | "morning" | "afternoon";

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

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

function slotName(slot: string): string {
  if (slot === "morning") return "9:27";
  if (slot === "afternoon") return "14:30";
  return "手动";
}

function slotTitle(slot: string): string {
  if (slot === "morning") return "早盘";
  if (slot === "afternoon") return "尾盘";
  return "手动";
}

function resultLabel(item: DailyRecommendationItem): { label: string; tone: "good" | "bad" | "warn" | "neutral" } {
  const p = item.performance;
  const value = p.t3?.return_pct ?? p.t1?.return_pct ?? p.latest_return_pct;
  if (value === undefined || value === null) return { label: "等待数据", tone: "neutral" };
  if (value >= 2) return { label: "命中", tone: "good" };
  if (value <= -2) return { label: "偏弱", tone: "bad" };
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

export function DailyRecommendations() {
  const [items, setItems] = useState<DailyRecommendationItem[]>([]);
  const [backtest, setBacktest] = useState<DailyRecommendationBacktestResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState<"morning" | "afternoon" | null>(null);
  const [date, setDate] = useState(today());
  const [slotFilter, setSlotFilter] = useState<SlotFilter>("all");
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [list, bt] = await Promise.all([
        api.listDailyRecommendations({ date, limit: 100 }),
        api.getDailyRecommendationBacktest(30),
      ]);
      setItems(list.items);
      setBacktest(bt);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "加载每日推荐失败");
    } finally {
      setLoading(false);
    }
  }, [date]);

  useEffect(() => {
    load();
  }, [load]);

  const sorted = useMemo(() => {
    return items
      .filter((item) => slotFilter === "all" || item.slot === slotFilter)
      .sort((a, b) => {
        if (a.slot !== b.slot) return a.slot === "morning" ? -1 : 1;
        return a.rank - b.rank;
      });
  }, [items, slotFilter]);

  useEffect(() => {
    if (expandedId && !sorted.some((item) => item.id === expandedId)) {
      setExpandedId(null);
    }
  }, [expandedId, sorted]);

  const generate = async (slot: "morning" | "afternoon") => {
    setGenerating(slot);
    try {
      await api.generateDailyRecommendations(slot, 5);
      toast.success(`${slotName(slot)} 推荐已生成`);
      await load();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "生成推荐失败");
    } finally {
      setGenerating(null);
    }
  };

  const risingCount = sorted.filter((item) => (item.performance.latest_return_pct ?? 0) > 0).length;
  const avgReturn = sorted.length
    ? sorted.reduce((sum, item) => sum + (item.performance.latest_return_pct ?? 0), 0) / sorted.length
    : null;

  return (
    <div className="flex h-[calc(100vh-3.5rem)] flex-col bg-muted/20">
      <header className="shrink-0 border-b bg-background">
        <div className="flex flex-col gap-4 px-4 py-4 md:px-6 xl:flex-row xl:items-center xl:justify-between">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <Target className="h-5 w-5 text-primary" />
              <h1 className="text-xl font-semibold tracking-tight">今日推荐</h1>
              <span className="rounded bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary">推荐列表</span>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              按日期查看推荐股票、推荐后涨跌和当时推荐理由。
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <input
              type="date"
              value={date}
              onChange={(event) => setDate(event.target.value)}
              className="h-9 rounded-md border bg-background px-3 text-xs outline-none transition focus:border-primary/60 focus:ring-2 focus:ring-primary/15"
            />
            <ActionButton
              icon={Clock3}
              label="生成 9:27"
              busy={generating === "morning"}
              disabled={generating !== null}
              onClick={() => generate("morning")}
              primary
            />
            <ActionButton
              icon={Sparkles}
              label="生成 14:30"
              busy={generating === "afternoon"}
              disabled={generating !== null}
              onClick={() => generate("afternoon")}
            />
            <button
              type="button"
              onClick={load}
              disabled={loading}
              className="inline-flex h-9 items-center gap-1.5 rounded-md border px-3 text-xs text-muted-foreground transition hover:bg-muted hover:text-foreground disabled:opacity-50"
            >
              <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
              刷新
            </button>
            <Link
              to="/recommendation-history"
              className="inline-flex h-9 items-center gap-1.5 rounded-md border px-3 text-xs text-muted-foreground transition hover:bg-muted hover:text-foreground"
            >
              <History className="h-3.5 w-3.5" />
              历史
            </Link>
          </div>
        </div>
      </header>

      <main className="min-h-0 flex-1 overflow-y-auto p-4 md:p-6">
        <div className="mx-auto max-w-7xl space-y-4">
          <SummaryBar
            count={sorted.length}
            risingCount={risingCount}
            avgReturn={avgReturn}
            t1WinRate={backtest?.summary.t1_win_rate}
            t1AvgReturn={backtest?.summary.t1_avg_return}
            slotFilter={slotFilter}
            onSlotFilterChange={setSlotFilter}
          />

          {loading ? (
            <div className="flex min-h-[360px] items-center justify-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-5 w-5 animate-spin" />
              正在加载推荐
            </div>
          ) : sorted.length === 0 ? (
            <EmptyState date={date} />
          ) : (
            <RecommendationTable
              items={sorted}
              expandedId={expandedId}
              onToggle={(id) => setExpandedId((current) => current === id ? null : id)}
            />
          )}
        </div>
      </main>
    </div>
  );
}

function SummaryBar({
  count,
  risingCount,
  avgReturn,
  t1WinRate,
  t1AvgReturn,
  slotFilter,
  onSlotFilterChange,
}: {
  count: number;
  risingCount: number;
  avgReturn: number | null;
  t1WinRate?: number | null;
  t1AvgReturn?: number | null;
  slotFilter: SlotFilter;
  onSlotFilterChange: (value: SlotFilter) => void;
}) {
  return (
    <section className="rounded-md border bg-background p-3 md:p-4">
      <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
        <div className="flex w-fit rounded-md border bg-muted/30 p-1">
          <Segment label="全部" active={slotFilter === "all"} onClick={() => onSlotFilterChange("all")} />
          <Segment label="9:27" active={slotFilter === "morning"} onClick={() => onSlotFilterChange("morning")} />
          <Segment label="14:30" active={slotFilter === "afternoon"} onClick={() => onSlotFilterChange("afternoon")} />
        </div>

        <div className="grid grid-cols-2 gap-2 text-xs md:grid-cols-5">
          <SummaryMetric label="当日推荐" value={`${count}`} />
          <SummaryMetric label="当前上涨" value={`${risingCount}`} tone={risingCount ? "text-success" : undefined} />
          <SummaryMetric label="当前平均" value={fmtPct(avgReturn)} tone={retTone(avgReturn)} />
          <SummaryMetric label="近30天T+1胜率" value={t1WinRate === null || t1WinRate === undefined ? "-" : `${t1WinRate}%`} />
          <SummaryMetric label="近30天T+1均值" value={fmtPct(t1AvgReturn)} tone={retTone(t1AvgReturn)} />
        </div>
      </div>
    </section>
  );
}

function Segment({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "h-7 rounded px-3 text-xs font-medium transition",
        active ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground",
      )}
    >
      {label}
    </button>
  );
}

function RecommendationTable({
  items,
  expandedId,
  onToggle,
}: {
  items: DailyRecommendationItem[];
  expandedId: string | null;
  onToggle: (id: string) => void;
}) {
  return (
    <div className="overflow-hidden rounded-md border bg-card">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[1080px] text-sm">
          <thead className="border-b bg-muted/30 text-xs text-muted-foreground">
            <tr>
              <th className="px-3 py-2 text-left font-medium">排名</th>
              <th className="px-3 py-2 text-left font-medium">股票</th>
              <th className="px-3 py-2 text-left font-medium">强度</th>
              <th className="px-3 py-2 text-left font-medium">来源/策略</th>
              <th className="px-3 py-2 text-left font-medium">推荐理由</th>
              <th className="px-3 py-2 text-right font-medium">推荐价</th>
              <th className="px-3 py-2 text-right font-medium">当前</th>
              <th className="px-3 py-2 text-right font-medium">T+0</th>
              <th className="px-3 py-2 text-right font-medium">T+1</th>
              <th className="px-3 py-2 text-right font-medium">T+3</th>
              <th className="px-3 py-2 text-right font-medium">T+5</th>
              <th className="px-3 py-2 text-left font-medium">状态</th>
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
                      <span className="rounded bg-muted px-2 py-1 text-xs font-semibold text-muted-foreground">#{item.rank}</span>
                    </td>
                    <td className="px-3 py-3">
                      <p className="font-medium">{item.name}</p>
                      <p className="font-mono text-[10px] text-muted-foreground">{item.symbol}</p>
                    </td>
                    <td className="px-3 py-3">
                      <StrengthBadge item={item} />
                    </td>
                    <td className="px-3 py-3">
                      <p className="text-xs font-medium">{item.strategy || item.category || "综合推荐"}</p>
                      <div className="mt-1 flex flex-wrap gap-1">
                        <StatusBadge label={aiDecisionLabel(item.ai_review?.decision)} tone={item.ai_review?.decision === "recommend" ? "good" : "warn"} />
                        <StatusBadge label={factorLabel(item)} tone={factorTone(item.factor_review?.score)} />
                      </div>
                      <p className="mt-1 text-[10px] text-muted-foreground">{slotTitle(item.slot)} {slotName(item.slot)} · {item.source || "system"}</p>
                    </td>
                    <td className="max-w-[260px] px-3 py-3">
                      <p className="line-clamp-2 text-xs leading-relaxed text-muted-foreground">{item.reason || "暂无推荐理由"}</p>
                    </td>
                    <td className="px-3 py-3 text-right font-mono text-xs">¥{fmtPrice(item.price_at_pick)}</td>
                    <ReturnCell value={item.performance.latest_return_pct} icon />
                    <ReturnCell value={item.performance.t0?.return_pct} />
                    <ReturnCell value={item.performance.t1?.return_pct} />
                    <ReturnCell value={item.performance.t3?.return_pct} />
                    <ReturnCell value={item.performance.t5?.return_pct} />
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
                          展开
                          <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", open && "rotate-180")} />
                        </button>
                        <Link
                          to={`/logic-chain?symbol=${encodeURIComponent(item.symbol)}&q=${encodeURIComponent(item.reason || item.name)}`}
                          className="rounded-md bg-primary px-2.5 py-1 text-xs text-primary-foreground hover:opacity-90"
                        >
                          逻辑
                        </Link>
                      </div>
                    </td>
                  </tr>
                  {open && (
                    <tr className="border-b bg-primary/5">
                      <td colSpan={13} className="px-4 py-4">
                        <RecommendationDetail item={item} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function RecommendationDetail({ item }: { item: DailyRecommendationItem }) {
  const q = encodeURIComponent(item.reason || item.name);
  const symbol = encodeURIComponent(item.symbol);
  return (
    <div className="grid gap-3 xl:grid-cols-[1fr_1fr_1fr_260px]">
      <DecisionBlock icon={Target} title="为什么推荐" body={item.reason || "暂无推荐理由"} />
      <DecisionBlock icon={ShieldAlert} title="风险/失效条件" body={item.risk_note || "如果价格、量能或板块同步性转弱，推荐假设需要降级。"} muted />
      <AiFactorBlock item={item} />
      <div className="rounded-md border bg-background p-3">
        <div className="mb-2 flex items-center gap-2 text-sm font-medium">
          <Search className="h-4 w-4 text-primary" />
          相关证据
        </div>
        <div className="grid gap-2">
          <EvidenceLink icon={Clock3} label="加入观察" to={`/watchlist-schedule?symbol=${symbol}`} primary />
          <EvidenceLink icon={Newspaper} label="相关新闻" to={`/news?symbol=${symbol}&q=${q}`} />
          <EvidenceLink icon={TrendingUp} label="相关事件" to={`/events?q=${q}&symbol=${symbol}`} />
          <EvidenceLink icon={Search} label="候选池来源" to={`/opportunity?symbol=${symbol}`} />
          <EvidenceLink icon={GitBranch} label="完整分析" to={`/logic-chain?symbol=${symbol}&q=${q}`} />
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

function EmptyState({ date }: { date: string }) {
  return (
    <div className="flex items-center justify-center p-6">
      <div className="flex min-h-[360px] w-full max-w-2xl flex-col items-center justify-center rounded-md border border-dashed bg-background px-6 text-center">
        <CalendarDays className="h-10 w-10 text-muted-foreground/40" />
        <p className="mt-3 text-sm font-medium">{date} 还没有推荐</p>
        <p className="mt-1 max-w-md text-xs leading-relaxed text-muted-foreground">
          交易日会在 9:27 和 14:30 自动生成，也可以用右上角按钮手动生成。
        </p>
      </div>
    </div>
  );
}

function ActionButton({
  icon: Icon,
  label,
  busy,
  disabled,
  onClick,
  primary,
}: {
  icon: LucideIcon;
  label: string;
  busy?: boolean;
  disabled?: boolean;
  onClick: () => void;
  primary?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "inline-flex h-9 items-center gap-1.5 rounded-md px-3 text-xs font-medium transition disabled:opacity-50",
        primary ? "bg-primary text-primary-foreground hover:opacity-90" : "border hover:bg-muted",
      )}
    >
      {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Icon className="h-3.5 w-3.5" />}
      {label}
    </button>
  );
}

function SummaryMetric({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded-md border bg-card px-3 py-2">
      <p className={cn("text-sm font-semibold tabular-nums", tone)}>{value}</p>
      <p className="mt-0.5 text-[10px] text-muted-foreground">{label}</p>
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

function DecisionBlock({
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

export function RecommendationBacktestSummary({ backtest }: { backtest: DailyRecommendationBacktestResponse | null }) {
  if (!backtest) return null;
  return (
    <section className="rounded-md border bg-card p-4">
      <div className="mb-3 flex items-center gap-2">
        <History className="h-4 w-4 text-primary" />
        <h2 className="text-sm font-semibold">近 30 天表现</h2>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <SummaryMetric label="推荐数" value={`${backtest.summary.count}`} />
        <SummaryMetric label="T+1样本" value={`${backtest.summary.t1_count}`} />
        <SummaryMetric label="T+1胜率" value={backtest.summary.t1_win_rate === null ? "-" : `${backtest.summary.t1_win_rate}%`} />
        <SummaryMetric label="T+1均值" value={fmtPct(backtest.summary.t1_avg_return)} tone={retTone(backtest.summary.t1_avg_return)} />
      </div>
    </section>
  );
}
