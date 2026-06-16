import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  BarChart3,
  ChevronDown,
  FileText,
  Loader2,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  Target,
  Trash2,
  TrendingDown,
  TrendingUp,
  Wallet,
  X,
} from "lucide-react";
import { api, type AnalysisDimension, type PositionSignal } from "@/lib/api";
import { cn } from "@/lib/utils";
import { echarts } from "@/lib/echarts";
import { getChartTheme } from "@/lib/chart-theme";
import { useDarkMode } from "@/hooks/useDarkMode";
import { toast } from "sonner";

interface Position {
  symbol: string;
  cost: number;
  shares: number;
  date: string;
}

const REFRESH_MS = 120_000;
const LS_KEY = "vibe-position-watchlist";
const AI_CACHE_KEY = "vibe-position-ai-cache";

const DECISION_COLORS: Record<string, string> = {
  strong_buy: "text-success bg-success/10 border-success/20",
  buy: "text-success bg-success/10 border-success/20",
  hold: "text-warning bg-warning/10 border-warning/20",
  sell: "text-danger bg-danger/10 border-danger/20",
  strong_sell: "text-danger bg-danger/10 border-danger/20",
};

const DIMENSION_NAMES: Record<string, string> = {
  macro: "宏观",
  industry: "行业",
  fundamental: "基本面",
  technical: "技术面",
  capital: "资金面",
  risk: "风险",
  events: "事件",
  alphas: "因子",
};

function fmtPrice(value: number): string {
  return value >= 1000 ? value.toFixed(0) : value.toFixed(3);
}

function fmtPct(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function fmtPnL(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(0)}`;
}

function fmtMoney(value: number): string {
  const sign = value < 0 ? "-" : "";
  const abs = Math.abs(value);
  if (abs >= 10000) return `${sign}${(abs / 10000).toFixed(1)}万`;
  return `${sign}${abs.toFixed(0)}`;
}

function fmtMarketPrice(value: number | undefined): string {
  if (value === undefined || value === null || Number.isNaN(value) || value === 0) return "-";
  return `¥${fmtPrice(value)}`;
}

function fmtVolume(value: number | undefined): string {
  if (!value || value <= 0) return "-";
  // 成交量单位：手（1 手 = 100 股）。数据源返回的是"手"。
  if (value >= 10000) return `${(value / 10000).toFixed(1)}万手`;
  return `${value}手`;
}

function confidenceLabel(value: string): string {
  if (value === "high") return "高置信";
  if (value === "medium") return "中置信";
  return "低置信";
}

function normalizeCode(input: string): string {
  const cleaned = input.trim().toUpperCase();
  if (/^\d{6}\.(SZ|SH)$/.test(cleaned)) return cleaned;
  if (!/^\d{6}$/.test(cleaned)) return "";
  const prefix = cleaned.slice(0, 3);
  if (["000", "001", "002", "003", "004", "159", "300", "301"].includes(prefix)) return `${cleaned}.SZ`;
  return `${cleaned}.SH`;
}

function loadPositions(): Position[] {
  try {
    const raw = localStorage.getItem(LS_KEY);
    const list = raw ? JSON.parse(raw) : [];
    return Array.isArray(list) ? list : [];
  } catch {
    return [];
  }
}

function savePositions(list: Position[]): void {
  localStorage.setItem(LS_KEY, JSON.stringify(list));
  api.saveWatchlist(list).catch(() => {});
}

function positionStats(signal?: PositionSignal | null, position?: Position) {
  if (!signal || !position) return { pnl: 0, pnlPct: 0, value: 0 };
  const value = signal.price * position.shares;
  const pnl = (signal.price - position.cost) * position.shares;
  const pnlPct = position.cost > 0 ? ((signal.price - position.cost) / position.cost) * 100 : 0;
  return { pnl, pnlPct, value };
}

function scoreTone(score: number): string {
  if (score >= 0.6) return "text-success";
  if (score >= 0.4) return "text-warning";
  return "text-danger";
}

function scoreBg(score: number): string {
  if (score >= 0.6) return "bg-success";
  if (score >= 0.4) return "bg-warning";
  return "bg-danger";
}

function isAiError(content: string | null): boolean {
  if (!content) return false;
  return content.includes("失败") || content.includes("出错") || content.includes("HTTP");
}

function RadarChart({ signal, dark }: { signal: PositionSignal; dark: boolean }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    const theme = getChartTheme();
    const chart = echarts.init(ref.current);
    const dims = signal.dimensions;
    const indicators = dims?.length
      ? dims.map((dim) => ({ name: DIMENSION_NAMES[dim.id] || dim.label, max: 1 }))
      : [
          { name: "趋势", max: 1 },
          { name: "技术", max: 1 },
          { name: "动量", max: 1 },
          { name: "事件", max: 1 },
        ];
    const values = dims?.length
      ? dims.map((dim) => dim.score)
      : [signal.trend.score, signal.technical.score, signal.momentum.score, signal.events.score];

    chart.setOption({
      tooltip: { trigger: "item" },
      radar: {
        center: ["50%", "50%"],
        radius: dims?.length ? "64%" : "70%",
        indicator: indicators,
        shape: "polygon",
        axisName: { color: theme.textColor, fontSize: 10 },
        splitArea: { areaStyle: { color: ["transparent"] } },
        splitLine: { lineStyle: { color: theme.gridColor } },
        axisLine: { lineStyle: { color: theme.axisColor } },
      },
      series: [{
        type: "radar",
        data: [{ value: values, name: "信号评分", areaStyle: { color: `${theme.infoColor}33` }, lineStyle: { color: theme.infoColor } }],
        symbol: "circle",
        symbolSize: 4,
      }],
    });

    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(ref.current);
    return () => {
      ro.disconnect();
      chart.dispose();
    };
  }, [dark, signal]);

  return <div ref={ref} className="h-[220px] w-full" />;
}

function ScoreBar({ score }: { score: number }) {
  return (
    <div className="mt-1 flex items-center gap-2">
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
        <div className={cn("h-full rounded-full", scoreBg(score))} style={{ width: `${Math.round(score * 100)}%` }} />
      </div>
      <span className="w-8 text-right text-[10px] tabular-nums text-muted-foreground">{Math.round(score * 100)}分</span>
    </div>
  );
}

function SLTPIndicator({ price, sl, tp, rr }: { price: number; sl?: number | null; tp?: number | null; rr?: number | null }) {
  if (!sl || !tp || tp <= sl) return null;
  const pct = ((price - sl) / (tp - sl)) * 100;
  return (
    <div className="rounded-lg border bg-card p-4">
      <div className="mb-2 flex items-center justify-between">
        <p className="text-sm font-medium">止盈止损区间</p>
        <span className="text-xs text-muted-foreground">盈亏比 {rr ?? "-"}</span>
      </div>
      <div className="relative h-2 rounded-full bg-muted">
        <div className="absolute left-0 top-0 h-full w-[35%] rounded-l-full bg-danger/25" />
        <div className="absolute right-0 top-0 h-full w-[35%] rounded-r-full bg-success/25" />
        <div
          className="absolute top-1/2 h-4 w-4 -translate-y-1/2 rounded-full border-2 border-primary bg-background shadow"
          style={{ left: `${Math.max(2, Math.min(96, pct))}%` }}
        />
      </div>
      <div className="mt-2 flex justify-between text-[10px] text-muted-foreground">
        <span>止损 ¥{fmtPrice(sl)}</span>
        <span>现价 ¥{fmtPrice(price)}</span>
        <span>止盈 ¥{fmtPrice(tp)}</span>
      </div>
    </div>
  );
}

function DimensionAccordion({ dim, defaultOpen }: { dim: AnalysisDimension; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="overflow-hidden rounded-lg border bg-card">
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        className="flex w-full items-center gap-2 px-3 py-2.5 text-left transition-colors hover:bg-muted/40"
      >
        <span className="flex-1 text-xs font-medium">{DIMENSION_NAMES[dim.id] || dim.label}</span>
        <span className={cn("text-xs tabular-nums", scoreTone(dim.score))}>{Math.round(dim.score * 100)}分</span>
        <span className="text-xs">{dim.signal}</span>
        <ChevronDown className={cn("h-3.5 w-3.5 text-muted-foreground transition-transform", open && "rotate-180")} />
      </button>
      {open && (
        <div className="space-y-2 border-t px-3 pb-3 pt-2">
          {dim.summary && <p className="text-xs leading-relaxed text-muted-foreground">{dim.summary}</p>}
          <div className="space-y-1.5">
            {dim.items.map((item, index) => (
              <div key={`${item.label}-${index}`} className="flex items-center justify-between gap-3 text-xs">
                <span className="text-muted-foreground">{item.label}</span>
                <span className="flex items-center gap-1 text-right font-medium">
                  {item.value}
                  <span>{item.signal}</span>
                </span>
              </div>
            ))}
          </div>
          <ScoreBar score={dim.score} />
        </div>
      )}
    </div>
  );
}

function HeaderBar({
  totalValue,
  totalPnL,
  count,
  refreshing,
  disabled,
  onRefresh,
}: {
  totalValue: number;
  totalPnL: number;
  count: number;
  refreshing: boolean;
  disabled: boolean;
  onRefresh: () => void;
}) {
  return (
    <div className="shrink-0 border-b px-4 py-3 md:px-6">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">跟踪看板</h1>
          <p className="mt-1 text-xs text-muted-foreground">持仓信号、AI 决策与风控建议。</p>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <MetricPill label="持仓数" value={`${count}`} />
          <MetricPill label="总市值" value={fmtMoney(totalValue)} />
          <MetricPill label="浮动盈亏" value={fmtPnL(totalPnL)} tone={totalPnL >= 0 ? "up" : "down"} />
          <button
            type="button"
            onClick={onRefresh}
            disabled={disabled}
            className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-40"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", refreshing && "animate-spin")} />
            刷新
          </button>
        </div>
      </div>
    </div>
  );
}

function MetricPill({ label, value, tone }: { label: string; value: string; tone?: "up" | "down" }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-md border bg-card px-2.5 py-1.5">
      <span className="text-muted-foreground">{label}</span>
      <span className={cn("font-semibold tabular-nums", tone === "up" && "text-success", tone === "down" && "text-danger")}>{value}</span>
    </span>
  );
}

function PositionListPanel({
  positions,
  signals,
  selected,
  totalValue,
  totalPnL,
  showAdd,
  addSymbol,
  addCost,
  addShares,
  addDate,
  search,
  onSearchChange,
  onShowAdd,
  onCancelAdd,
  onAddField,
  onAdd,
  onRemove,
  onSelect,
  onKeyDown,
}: {
  positions: Position[];
  signals: PositionSignal[];
  selected: string | null;
  totalValue: number;
  totalPnL: number;
  showAdd: boolean;
  addSymbol: string;
  addCost: string;
  addShares: string;
  addDate: string;
  search: string;
  onSearchChange: (value: string) => void;
  onShowAdd: () => void;
  onCancelAdd: () => void;
  onAddField: (field: "symbol" | "cost" | "shares" | "date", value: string) => void;
  onAdd: () => void;
  onRemove: (symbol: string) => void;
  onSelect: (signal: PositionSignal) => void;
  onKeyDown: (event: React.KeyboardEvent) => void;
}) {
  const signalMap = new Map(signals.map((item) => [item.symbol, item]));
  const query = search.trim().toLowerCase();
  const visible = positions.filter((pos) => {
    const signal = signalMap.get(pos.symbol);
    if (!query) return true;
    return `${pos.symbol} ${signal?.name ?? ""}`.toLowerCase().includes(query);
  });

  return (
    <aside className="flex min-h-0 w-full shrink-0 flex-col border-r bg-card/45 md:w-[280px]">
      <div className="space-y-3 border-b p-4">
        <div className="rounded-lg border bg-card p-3">
          <div className="mb-2 flex items-center gap-2 text-muted-foreground">
            <Wallet className="h-4 w-4" />
            <span className="text-xs font-medium">组合总览</span>
          </div>
          <p className="text-xl font-semibold tabular-nums">{fmtMoney(totalValue)}</p>
          <p className={cn("mt-1 flex items-center gap-1 text-sm font-medium", totalPnL >= 0 ? "text-success" : "text-danger")}>
            {totalPnL >= 0 ? <TrendingUp className="h-3.5 w-3.5" /> : <TrendingDown className="h-3.5 w-3.5" />}
            {fmtPnL(totalPnL)}
          </p>
        </div>

        {showAdd ? (
          <div className="space-y-2 rounded-lg border bg-card p-3">
            <input
              value={addSymbol}
              onChange={(event) => onAddField("symbol", event.target.value)}
              onKeyDown={onKeyDown}
              placeholder="代码，如 600519"
              className="w-full rounded-md border bg-background px-2 py-1.5 text-xs outline-none focus:ring-2 focus:ring-primary/15"
              autoFocus
            />
            <div className="grid grid-cols-2 gap-2">
              <input
                type="number"
                value={addCost}
                onChange={(event) => onAddField("cost", event.target.value)}
                onKeyDown={onKeyDown}
                placeholder="成本价"
                step="0.01"
                className="rounded-md border bg-background px-2 py-1.5 text-xs outline-none focus:ring-2 focus:ring-primary/15"
              />
              <input
                type="number"
                value={addShares}
                onChange={(event) => onAddField("shares", event.target.value)}
                onKeyDown={onKeyDown}
                placeholder="股数"
                step="100"
                className="rounded-md border bg-background px-2 py-1.5 text-xs outline-none focus:ring-2 focus:ring-primary/15"
              />
            </div>
            <input
              type="date"
              value={addDate}
              onChange={(event) => onAddField("date", event.target.value)}
              className="w-full rounded-md border bg-background px-2 py-1.5 text-xs outline-none focus:ring-2 focus:ring-primary/15"
            />
            <div className="flex gap-2">
              <button type="button" onClick={onAdd} className="flex-1 rounded-md bg-primary px-2 py-1.5 text-xs font-medium text-primary-foreground">
                确认添加
              </button>
              <button type="button" onClick={onCancelAdd} className="rounded-md border px-2 py-1.5 text-xs text-muted-foreground hover:bg-muted">
                取消
              </button>
            </div>
          </div>
        ) : (
          <button
            type="button"
            onClick={onShowAdd}
            className="flex w-full items-center justify-center gap-1.5 rounded-lg border border-dashed border-muted-foreground/30 px-3 py-2 text-xs text-muted-foreground transition-colors hover:border-primary/50 hover:text-primary"
          >
            <Plus className="h-3.5 w-3.5" />
            添加标的
          </button>
        )}

        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <input
            value={search}
            onChange={(event) => onSearchChange(event.target.value)}
            placeholder="搜索代码/名称"
            className="w-full rounded-md border bg-background py-1.5 pl-8 pr-2 text-xs outline-none focus:ring-2 focus:ring-primary/15"
          />
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        <div className="mb-2 flex items-center justify-between px-1">
          <p className="text-xs font-medium text-muted-foreground">持仓列表</p>
          <span className="text-[10px] text-muted-foreground">{visible.length} / {positions.length}</span>
        </div>
        {positions.length === 0 ? (
          <p className="rounded-lg border border-dashed px-3 py-8 text-center text-xs text-muted-foreground">
            暂无标的，点击上方按钮添加。
          </p>
        ) : visible.length === 0 ? (
          <p className="rounded-lg border border-dashed px-3 py-8 text-center text-xs text-muted-foreground">没有匹配的标的。</p>
        ) : (
          <div className="space-y-1">
            {visible.map((pos) => {
              const signal = signalMap.get(pos.symbol);
              const stats = positionStats(signal, pos);
              const isSelected = selected === pos.symbol;
              return (
                <div
                  key={pos.symbol}
                  role="button"
                  tabIndex={0}
                  onClick={() => signal && onSelect(signal)}
                  className={cn(
                    "group rounded-md border px-3 py-2 text-xs transition-colors",
                    signal ? "cursor-pointer hover:border-primary/35 hover:bg-primary/[0.03]" : "opacity-70",
                    isSelected ? "border-primary/40 bg-primary/10" : "bg-card",
                  )}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <p className="truncate font-medium">{signal?.name || pos.symbol}</p>
                      <p className="mt-0.5 font-mono text-[10px] text-muted-foreground">{pos.symbol}</p>
                    </div>
                    <button
                      type="button"
                      onClick={(event) => {
                        event.stopPropagation();
                        onRemove(pos.symbol);
                      }}
                      className="rounded p-1 text-muted-foreground opacity-0 transition-opacity hover:bg-danger/10 hover:text-danger group-hover:opacity-100"
                      title="删除"
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  </div>
                  {signal ? (
                    <>
                      <div className="mt-2 flex items-center gap-2">
                        <span className="font-semibold tabular-nums">¥{fmtPrice(signal.price)}</span>
                        <span className={cn("tabular-nums", signal.change_pct >= 0 ? "text-success" : "text-danger")}>
                          {fmtPct(signal.change_pct)}
                        </span>
                        {pos.shares > 0 && (
                          <span className={cn("ml-auto tabular-nums", stats.pnl >= 0 ? "text-success" : "text-danger")}>
                            {fmtPnL(stats.pnl)}
                          </span>
                        )}
                      </div>
                      <div className="mt-2 flex items-center justify-between">
                        <span className={cn("rounded-full border px-2 py-0.5 text-[10px] font-medium", DECISION_COLORS[signal.decision] || DECISION_COLORS.hold)}>
                          {signal.decision_label}
                        </span>
                        {pos.shares > 0 && <span className="text-[10px] text-muted-foreground">{pos.shares} 股</span>}
                      </div>
                    </>
                  ) : (
                    <p className="mt-2 text-[10px] text-muted-foreground">等待分析数据</p>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </aside>
  );
}

function SignalDetailPanel({
  signal,
  position,
  dark,
}: {
  signal: PositionSignal | null;
  position?: Position;
  dark: boolean;
}) {
  if (!signal) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <div className="max-w-sm rounded-lg border border-dashed p-8 text-center text-muted-foreground">
          <Target className="mx-auto h-10 w-10 opacity-30" />
          <p className="mt-3 text-sm font-medium text-foreground">选择一个标的查看信号详情</p>
          <p className="mt-1 text-xs">左侧添加或选择持仓后，中间会展示结构化信号、雷达图和止盈止损区间。</p>
        </div>
      </div>
    );
  }

  const stats = positionStats(signal, position);
  const dims = signal.dimensions ?? [];
  const cards = dims.length
    ? dims.slice(0, 6).map((dim) => ({ label: DIMENSION_NAMES[dim.id] || dim.label, score: dim.score, signal: dim.signal }))
    : [
        { label: "趋势", score: signal.trend.score, signal: "" },
        { label: "技术", score: signal.technical.score, signal: "" },
        { label: "动量", score: signal.momentum.score, signal: "" },
        { label: "事件", score: signal.events.score, signal: "" },
      ];

  return (
    <main className="min-h-0 flex-1 overflow-y-auto p-4 md:p-5">
      <div className="mx-auto max-w-4xl space-y-4">
        <section className="rounded-lg border bg-card p-4">
          <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
            <div>
              <h2 className="text-xl font-semibold">
                {signal.name}
                <span className="ml-2 align-middle font-mono text-sm font-normal text-muted-foreground">{signal.symbol}</span>
              </h2>
              <p className="mt-1 text-xs text-muted-foreground">结构化信号详情，用于判断当前标的是否仍值得持有。</p>
            </div>
            <div className="text-left md:text-right">
              <p className="text-2xl font-bold tabular-nums">¥{fmtPrice(signal.price)}</p>
              <p className={cn("text-sm font-medium", signal.change_pct >= 0 ? "text-success" : "text-danger")}>
                {signal.change_pct >= 0 ? <TrendingUp className="mr-0.5 inline h-3.5 w-3.5" /> : <TrendingDown className="mr-0.5 inline h-3.5 w-3.5" />}
                {fmtPct(signal.change_pct)}
              </p>
            </div>
          </div>

          <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            <InfoCard label="持仓成本" value={position?.cost ? `¥${fmtPrice(position.cost)}` : "未填写"} />
            <InfoCard label="持仓数量" value={position?.shares ? `${position.shares} 股` : "观察标的"} />
            <InfoCard label="持仓市值" value={position?.shares ? fmtMoney(stats.value) : "-"} />
            <InfoCard label="持仓盈亏" value={position?.shares ? `${fmtPnL(stats.pnl)} (${fmtPct(stats.pnlPct)})` : "-"} tone={stats.pnl >= 0 ? "up" : "down"} />
          </div>

          <div className="mt-2 grid gap-2 sm:grid-cols-3 lg:grid-cols-5">
            <InfoCard label="今开" value={fmtMarketPrice(signal.market_basics?.open)} />
            <InfoCard label="昨收" value={fmtMarketPrice(signal.market_basics?.prev_close)} />
            <InfoCard label="日高" value={fmtMarketPrice(signal.market_basics?.high)} tone="up" />
            <InfoCard label="日低" value={fmtMarketPrice(signal.market_basics?.low)} tone="down" />
            <InfoCard label="成交量" value={fmtVolume(signal.market_basics?.volume)} />
          </div>
        </section>

        <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          <ScoreCard label="综合评分" score={signal.overall_score} value={signal.decision_label} />
          {cards.map((card) => (
            <ScoreCard key={card.label} label={card.label} score={card.score} value={card.signal || `${Math.round(card.score * 100)}分`} />
          ))}
        </section>

        <section className="grid gap-4 lg:grid-cols-[300px_1fr]">
          <div className="rounded-lg border bg-card p-4">
            <div className="mb-2 flex items-center gap-2">
              <BarChart3 className="h-4 w-4 text-primary" />
              <p className="text-sm font-medium">信号雷达</p>
            </div>
            <RadarChart signal={signal} dark={dark} />
          </div>

          <div className="space-y-2">
            {dims.length ? (
              dims.map((dim) => <DimensionAccordion key={dim.id} dim={dim} defaultOpen={dim.id === "technical"} />)
            ) : (
              <div className="rounded-lg border bg-card p-4 text-xs text-muted-foreground">
                <p>趋势：{signal.trend.ma_pattern}</p>
                <p className="mt-2">技术形态：{signal.technical.patterns.join("、") || "-"}</p>
                <p className="mt-2">动量：RSI {signal.momentum.rsi}，MACD {signal.momentum.macd_signal}，量比 {signal.momentum.vol_ratio}x</p>
                <p className="mt-2">事件：{signal.events.relevant_count} 条，情绪 {signal.events.sentiment}</p>
              </div>
            )}
          </div>
        </section>

        <SLTPIndicator price={signal.price} sl={signal.stop_loss} tp={signal.take_profit} rr={signal.risk_reward} />
      </div>
    </main>
  );
}

function InfoCard({ label, value, tone }: { label: string; value: string; tone?: "up" | "down" }) {
  return (
    <div className="rounded-md bg-muted/35 px-2.5 py-1.5">
      <p className="text-[10px] text-muted-foreground">{label}</p>
      <p className={cn("mt-0.5 text-xs font-semibold tabular-nums", tone === "up" && "text-success", tone === "down" && "text-danger")}>{value}</p>
    </div>
  );
}

function ScoreCard({ label, score, value }: { label: string; score: number; value: string }) {
  return (
    <div className="rounded-lg border bg-card p-3">
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">{label}</p>
        <span className={cn("text-xs font-semibold", scoreTone(score))}>{Math.round(score * 100)}分</span>
      </div>
      <p className="mt-1 truncate text-sm font-medium">{value}</p>
      <ScoreBar score={score} />
    </div>
  );
}

function AiDecisionPanel({
  signal,
  position,
  aiLoading,
  aiContent,
  aiStreaming,
  onAiAnalyze,
  onClose,
}: {
  signal: PositionSignal | null;
  position?: Position;
  aiLoading: boolean;
  aiContent: string | null;
  aiStreaming: string;
  onAiAnalyze: () => void;
  onClose?: () => void;
}) {
  if (!signal) {
    return (
      <aside className="hidden w-[360px] shrink-0 border-l bg-muted/10 p-4 xl:block">
        <div className="rounded-lg border border-dashed p-6 text-center text-xs text-muted-foreground">
          选择标的后展示 AI 决策与风控建议。
        </div>
      </aside>
    );
  }

  const stats = positionStats(signal, position);
  const riskItems = buildRiskItems(signal, position, stats.pnlPct);
  const blocked = isAiError(aiContent);

  return (
    <aside className="flex h-full min-h-0 w-full shrink-0 flex-col overflow-hidden border-l bg-card/45 xl:w-[380px]">
      <div className="flex items-center justify-between border-b p-4">
        <div>
          <p className="text-sm font-semibold">AI 决策 / 风控建议</p>
          <p className="mt-0.5 text-xs text-muted-foreground">{signal.name} · {signal.symbol}</p>
        </div>
        {onClose && (
          <button type="button" onClick={onClose} className="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground xl:hidden">
            <X className="h-4 w-4" />
          </button>
        )}
      </div>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
        <section className={cn("rounded-lg border p-4", DECISION_COLORS[signal.decision] || DECISION_COLORS.hold)}>
          <div className="flex items-start gap-3">
            <Target className="mt-0.5 h-5 w-5 shrink-0" />
            <div className="min-w-0 flex-1">
              <p className="text-xs opacity-75">当前建议</p>
              <h3 className="mt-1 text-xl font-bold">{signal.decision_label}</h3>
              <p className="mt-2 text-xs opacity-80">
                综合评分 {signal.overall_score.toFixed(2)} · {confidenceLabel(signal.confidence)}
              </p>
            </div>
          </div>
        </section>

        <section className="rounded-lg border bg-card p-4">
          <div className="mb-3 flex items-center gap-2">
            <ShieldCheck className="h-4 w-4 text-primary" />
            <p className="text-sm font-medium">风控清单</p>
          </div>
          <div className="space-y-2">
            {riskItems.map((item) => (
              <div key={item.label} className="flex items-start justify-between gap-3 rounded-md bg-muted/35 px-3 py-2">
                <div>
                  <p className="text-xs font-medium">{item.label}</p>
                  <p className="mt-0.5 text-[10px] text-muted-foreground">{item.desc}</p>
                </div>
                <span className={cn("shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium", item.tone)}>{item.value}</span>
              </div>
            ))}
          </div>
        </section>

        <section className="rounded-lg border bg-card p-4">
          <div className="mb-3 flex items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <Sparkles className="h-4 w-4 text-primary" />
              <p className="text-sm font-medium">AI 深度分析</p>
            </div>
            <button
              type="button"
              onClick={onAiAnalyze}
              disabled={aiLoading}
              className="rounded-md border px-2.5 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-50"
            >
              {aiLoading ? "分析中" : aiContent ? "重新分析" : "开始分析"}
            </button>
          </div>

          {aiLoading ? (
            <div className="space-y-3">
              <div className="flex items-center gap-2 text-xs text-primary">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                AI 正在生成建议
              </div>
              {aiStreaming ? (
                <p className="whitespace-pre-wrap text-xs leading-relaxed text-muted-foreground">{aiStreaming}</p>
              ) : (
                <div className="space-y-2">
                  <div className="h-2 w-[92%] animate-pulse rounded bg-primary/10" />
                  <div className="h-2 w-[74%] animate-pulse rounded bg-primary/10" />
                  <div className="h-2 w-[84%] animate-pulse rounded bg-primary/10" />
                </div>
              )}
            </div>
          ) : aiContent ? (
            <div className={cn("rounded-md border p-3", blocked ? "border-danger/20 bg-danger/5" : "border-primary/15 bg-primary/5")}>
              <p className="whitespace-pre-wrap text-xs leading-relaxed">{aiContent}</p>
            </div>
          ) : (
            <p className="rounded-lg border border-dashed p-4 text-xs leading-relaxed text-muted-foreground">
              点击开始分析后，AI 会结合当前信号和持仓成本，给出更完整的持有、减仓或风险观察建议。
            </p>
          )}
        </section>

        <section className="grid grid-cols-2 gap-2">
          <button type="button" className="rounded-md border px-3 py-2 text-xs hover:bg-muted">继续持有</button>
          <button type="button" className="rounded-md border px-3 py-2 text-xs hover:bg-muted">减仓观察</button>
          <Link to="/alpha-forge" className="col-span-2 inline-flex items-center justify-center gap-1.5 rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground hover:opacity-90">
            <FileText className="h-3.5 w-3.5" />
            生成 AlphaForge 报告
          </Link>
        </section>
      </div>
    </aside>
  );
}

function buildRiskItems(signal: PositionSignal, position: Position | undefined, pnlPct: number) {
  const items = [
    {
      label: "仓位状态",
      desc: position?.shares ? `当前记录 ${position.shares} 股` : "未填写持仓数量，仅作为观察标的",
      value: position?.shares ? "持仓" : "观察",
      tone: position?.shares ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground",
    },
    {
      label: "浮动盈亏",
      desc: position?.shares ? "基于成本价和当前价估算" : "缺少成本或股数时不计算",
      value: position?.shares ? fmtPct(pnlPct) : "-",
      tone: !position?.shares ? "bg-muted text-muted-foreground" : pnlPct >= 0 ? "bg-success/10 text-success" : "bg-danger/10 text-danger",
    },
    {
      label: "止损约束",
      desc: signal.stop_loss ? `跌破 ¥${fmtPrice(signal.stop_loss)} 需要复核` : "后端暂未返回止损位",
      value: signal.stop_loss ? "已给出" : "缺失",
      tone: signal.stop_loss ? "bg-warning/10 text-warning" : "bg-muted text-muted-foreground",
    },
    {
      label: "风险收益",
      desc: signal.risk_reward ? `当前盈亏比约 1:${signal.risk_reward}` : "后端暂未返回盈亏比",
      value: signal.risk_reward && signal.risk_reward >= 2 ? "较好" : "观察",
      tone: signal.risk_reward && signal.risk_reward >= 2 ? "bg-success/10 text-success" : "bg-warning/10 text-warning",
    },
  ];
  return items;
}

function PageState({
  loading,
  error,
  onRetry,
}: {
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}) {
  if (loading) {
    return (
      <div className="flex h-full items-center justify-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin" />
        正在加载持仓信号
      </div>
    );
  }
  if (error) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <div className="max-w-sm rounded-lg border border-danger/20 bg-danger/5 p-6 text-center">
          <AlertTriangle className="mx-auto h-8 w-8 text-danger/70" />
          <p className="mt-3 text-sm font-medium">分析失败</p>
          <p className="mt-1 text-xs text-muted-foreground">{error}</p>
          <button type="button" onClick={onRetry} className="mt-4 rounded-md border px-3 py-1.5 text-xs hover:bg-muted">
            重试
          </button>
        </div>
      </div>
    );
  }
  return null;
}

export function TrackingDashboard() {
  const { dark } = useDarkMode();
  const [positions, setPositions] = useState<Position[]>([]);
  const [watchlistReady, setWatchlistReady] = useState(false);
  const [signals, setSignals] = useState<PositionSignal[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<PositionSignal | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [showAdd, setShowAdd] = useState(false);
  const [addSymbol, setAddSymbol] = useState("");
  const [addCost, setAddCost] = useState("");
  const [addShares, setAddShares] = useState("");
  const [addDate, setAddDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [search, setSearch] = useState("");
  const [mobileDecisionOpen, setMobileDecisionOpen] = useState(false);
  const [aiCache, setAiCache] = useState<Record<string, string>>(() => {
    try {
      const raw = localStorage.getItem(AI_CACHE_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch {
      return {};
    }
  });
  const [aiLoading, setAiLoading] = useState(false);
  const [aiStreaming, setAiStreaming] = useState("");
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const positionsRef = useRef(positions);
  const signalsRef = useRef(signals);
  positionsRef.current = positions;
  signalsRef.current = signals;

  const selectedPos = positions.find((pos) => pos.symbol === selected?.symbol);
  const aiContent = selected ? aiCache[selected.symbol] ?? null : null;
  const posMap = useMemo(() => new Map(positions.map((pos) => [pos.symbol, pos])), [positions]);
  const totalPnL = useMemo(() => signals.reduce((sum, signal) => {
    const pos = posMap.get(signal.symbol);
    return pos && pos.shares > 0 ? sum + (signal.price - pos.cost) * pos.shares : sum;
  }, 0), [posMap, signals]);
  const totalValue = useMemo(() => signals.reduce((sum, signal) => {
    const pos = posMap.get(signal.symbol);
    return pos && pos.shares > 0 ? sum + signal.price * pos.shares : sum;
  }, 0), [posMap, signals]);

  useEffect(() => {
    let cancelled = false;
    api.getWatchlist()
      .then((res) => {
        if (cancelled) return;
        if (res.items?.length) {
          setPositions(res.items);
          localStorage.setItem(LS_KEY, JSON.stringify(res.items));
        } else {
          setPositions(loadPositions());
        }
      })
      .catch(() => {
        if (!cancelled) setPositions(loadPositions());
      })
      .finally(() => {
        if (!cancelled) setWatchlistReady(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const analyze = useCallback((silent = false) => {
    if (!silent) setRefreshing(true);
    const list = positionsRef.current;
    if (!list.length) {
      setSignals([]);
      setSelected(null);
      setLoading(false);
      setRefreshing(false);
      return;
    }

    const body: { symbols: string[]; _cache_bust?: number } = { symbols: list.map((pos) => pos.symbol) };
    if (!silent) body._cache_bust = Date.now();
    api.analyzePositions(body)
      .then((res) => {
        setSignals(res.signals);
        setError(null);
        setSelected((prev) => {
          if (prev && res.signals.some((item) => item.symbol === prev.symbol)) {
            return res.signals.find((item) => item.symbol === prev.symbol) ?? prev;
          }
          return res.signals[0] ?? null;
        });
      })
      .catch((err: unknown) => {
        const msg = err instanceof Error ? err.message : "分析失败";
        if (!signalsRef.current.length) setError(msg);
        else toast.error(msg);
      })
      .finally(() => {
        setLoading(false);
        setRefreshing(false);
      });
  }, []);

  useEffect(() => {
    if (!watchlistReady) return;
    if (positions.length) {
      setLoading(true);
      analyze(true);
    } else {
      setLoading(false);
    }
  }, [analyze, positions.length, watchlistReady]);

  useEffect(() => {
    intervalRef.current = setInterval(() => {
      if (positionsRef.current.length) analyze(true);
    }, REFRESH_MS);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [analyze]);

  const resetAddForm = () => {
    setAddSymbol("");
    setAddCost("");
    setAddShares("");
    setAddDate(new Date().toISOString().slice(0, 10));
    setShowAdd(false);
  };

  const addPosition = () => {
    const code = normalizeCode(addSymbol);
    if (!code) {
      toast.error("请输入 6 位股票代码，如 600519");
      return;
    }
    if (positions.some((pos) => pos.symbol === code)) {
      toast.error("该标的已在列表中");
      return;
    }
    const next = [...positions, {
      symbol: code,
      cost: parseFloat(addCost) || 0,
      shares: parseInt(addShares, 10) || 0,
      date: addDate,
    }];
    setPositions(next);
    savePositions(next);
    resetAddForm();
    // 同步：持仓股自动加入定时分析任务（默认 15:05 / 短线 / 开启）
    api
      .createTrackingTask({ symbol: code, horizon: "短线", time: "15:05", enabled: true })
      .catch(() => {});
    setLoading(true);
    api.analyzePositions({ symbols: next.map((pos) => pos.symbol) })
      .then((res) => {
        setSignals(res.signals);
        setError(null);
        setSelected(res.signals.find((item) => item.symbol === code) ?? res.signals[0] ?? null);
      })
      .catch((err) => setError(err instanceof Error ? err.message : "分析失败"))
      .finally(() => setLoading(false));
  };

  const removePosition = (code: string) => {
    const next = positions.filter((pos) => pos.symbol !== code);
    setPositions(next);
    savePositions(next);
    api.removeWatchlistItem(code).catch(() => {});
    // 同步：删除持仓时一并删除定时分析任务（及其历史）
    api.deleteTrackingTaskBySymbol(code).catch(() => {});
    if (selected?.symbol === code) setSelected(null);
    if (!next.length) {
      setSignals([]);
      return;
    }
    api.analyzePositions({ symbols: next.map((pos) => pos.symbol) })
      .then((res) => {
        setSignals(res.signals);
        setSelected((prev) => prev && res.signals.some((item) => item.symbol === prev.symbol) ? prev : res.signals[0] ?? null);
      })
      .catch(() => {});
  };

  const runAiAnalysis = async () => {
    if (!selected) return;
    const symbol = selected.symbol;
    setAiLoading(true);
    setAiStreaming("");

    try {
      const response = await api.aiAnalyzePosition(
        selected.symbol,
        selected,
        selectedPos ? { cost: selectedPos.cost, shares: selectedPos.shares } : undefined,
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const reader = response.body?.getReader();
      if (!reader) throw new Error("响应体为空");

      const decoder = new TextDecoder();
      let buffer = "";
      let finalText = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop() || "";

        for (const block of parts) {
          const lines = block.split("\n");
          let eventType = "";
          let dataStr = "";
          for (const line of lines) {
            if (line.startsWith("event: ")) eventType = line.slice(7).trim();
            if (line.startsWith("data: ")) dataStr = line.slice(6);
          }
          if (!eventType || !dataStr) continue;
          try {
            const data = JSON.parse(dataStr);
            if (eventType === "text_delta") {
              setAiStreaming((prev) => prev + (data.delta || ""));
            } else if (eventType === "analysis_complete") {
              finalText = data.content || "";
              setAiCache((prev) => {
                const next = { ...prev, [symbol]: finalText };
                localStorage.setItem(AI_CACHE_KEY, JSON.stringify(next));
                return next;
              });
              setAiStreaming("");
            }
          } catch {
            // 忽略单个 SSE 块解析失败，继续读取后续内容。
          }
        }
      }

      if (!finalText) {
        const fallback = "AI 分析已完成，但没有返回正文内容。";
        setAiCache((prev) => {
          const next = { ...prev, [symbol]: fallback };
          localStorage.setItem(AI_CACHE_KEY, JSON.stringify(next));
          return next;
        });
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "未知错误";
      const errMsg = `AI 分析失败：${msg}`;
      setAiCache((prev) => {
        const next = { ...prev, [symbol]: errMsg };
        localStorage.setItem(AI_CACHE_KEY, JSON.stringify(next));
        return next;
      });
      toast.error("AI 分析失败");
    } finally {
      setAiLoading(false);
    }
  };

  const handleAddKey = (event: React.KeyboardEvent) => {
    if (event.key === "Enter") addPosition();
  };

  const pageState = <PageState loading={loading} error={error} onRetry={() => analyze()} />;

  return (
    <div className="flex h-[calc(100vh-3.5rem)] flex-col">
      <HeaderBar
        totalValue={totalValue}
        totalPnL={totalPnL}
        count={positions.length}
        refreshing={refreshing}
        disabled={refreshing || !positions.length}
        onRefresh={() => analyze()}
      />

      <div className="min-h-0 flex-1 overflow-hidden">
        {loading || error ? (
          pageState
        ) : (
          <div className="flex h-full flex-col md:flex-row">
            <PositionListPanel
              positions={positions}
              signals={signals}
              selected={selected?.symbol ?? null}
              totalValue={totalValue}
              totalPnL={totalPnL}
              showAdd={showAdd}
              addSymbol={addSymbol}
              addCost={addCost}
              addShares={addShares}
              addDate={addDate}
              search={search}
              onSearchChange={setSearch}
              onShowAdd={() => setShowAdd(true)}
              onCancelAdd={resetAddForm}
              onAddField={(field, value) => {
                if (field === "symbol") setAddSymbol(value);
                if (field === "cost") setAddCost(value);
                if (field === "shares") setAddShares(value);
                if (field === "date") setAddDate(value);
              }}
              onAdd={addPosition}
              onRemove={removePosition}
              onSelect={(signal) => {
                setSelected(signal);
                setMobileDecisionOpen(false);
              }}
              onKeyDown={handleAddKey}
            />

            <div className="min-h-0 flex flex-1">
              <SignalDetailPanel signal={selected} position={selectedPos} dark={dark} />
              <div className="hidden xl:block">
                <AiDecisionPanel
                  signal={selected}
                  position={selectedPos}
                  aiLoading={aiLoading}
                  aiContent={aiContent}
                  aiStreaming={aiStreaming}
                  onAiAnalyze={runAiAnalysis}
                />
              </div>
            </div>

            {selected && (
              <button
                type="button"
                onClick={() => setMobileDecisionOpen(true)}
                className="fixed bottom-4 right-4 z-30 rounded-full bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow-lg xl:hidden"
              >
                AI 决策
              </button>
            )}

            {mobileDecisionOpen && (
              <div className="fixed inset-0 z-40 bg-background/80 backdrop-blur-sm xl:hidden">
                <div className="absolute inset-x-0 bottom-0 top-12 overflow-hidden rounded-t-xl bg-background shadow-xl">
                  <AiDecisionPanel
                    signal={selected}
                    position={selectedPos}
                    aiLoading={aiLoading}
                    aiContent={aiContent}
                    aiStreaming={aiStreaming}
                    onAiAnalyze={runAiAnalysis}
                    onClose={() => setMobileDecisionOpen(false)}
                  />
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
