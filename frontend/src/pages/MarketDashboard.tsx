import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import type { EChartsOption } from "echarts";
import {
  Activity,
  ArrowRight,
  BarChart3,
  Flame,
  Gauge,
  Layers3,
  Loader2,
  RefreshCw,
  ShieldAlert,
  TrendingUp,
  Wallet,
} from "lucide-react";
import {
  api,
  type MarketBarsResponse,
  type MarketCapital,
  type MarketDashboardResponse,
  type MarketDashboardStageResponse,
  type MarketIndexRow,
  type MarketPools,
  type MarketSentiment,
  type MarketThemes,
  type MultiPeriodRow,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { Chart } from "@/components/dashboard/Chart";
import { EmptyHint, Pct, fmtYi, pctBg } from "@/components/dashboard/primitives";

const INDEX_OPTIONS = [
  { symbol: "000001.SH", name: "上证指数" },
  { symbol: "399001.SZ", name: "深证指数" },
  { symbol: "399006.SZ", name: "创业板指" },
  { symbol: "000688.SH", name: "科创50" },
  { symbol: "899050.BJ", name: "北证50" },
  { symbol: "000300.SH", name: "沪深300" },
];

function num(value: unknown): number | null {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function pct(value: unknown, digits = 1): string {
  const n = num(value);
  if (n === null) return "-";
  return `${n > 0 ? "+" : ""}${n.toFixed(digits)}%`;
}

function integer(value: unknown): string {
  const n = num(value);
  return n === null ? "-" : Math.round(n).toLocaleString("zh-CN");
}

function yi(value: unknown): string {
  const n = num(value);
  if (n === null) return "-";
  return `${n.toFixed(1)}亿`;
}

function valueTone(value: unknown): string {
  const n = num(value) ?? 0;
  if (n > 0) return "text-red-500";
  if (n < 0) return "text-green-500";
  return "text-muted-foreground";
}

function asRows<T = Record<string, unknown>>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function field<T = unknown>(obj: unknown, key: string): T | undefined {
  return obj && typeof obj === "object" ? ((obj as Record<string, T>)[key]) : undefined;
}

function fmtTime(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

function DashboardSection({
  title,
  sub,
  icon,
  right,
  className,
  children,
}: {
  title: string;
  sub?: string;
  icon?: React.ReactNode;
  right?: React.ReactNode;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <section className={cn("rounded-md border bg-background/95 shadow-sm", className)}>
      <header className="flex items-start justify-between gap-3 px-4 py-3">
        <div className="flex min-w-0 items-center gap-2">
          {icon}
          <div className="min-w-0">
            <div className="flex items-baseline gap-2">
              <h2 className="truncate text-base font-semibold tracking-tight">{title}</h2>
              {sub && <span className="truncate text-xs text-muted-foreground">{sub}</span>}
            </div>
          </div>
        </div>
        {right}
      </header>
      <div className="px-4 pb-4">{children}</div>
    </section>
  );
}

function MetricTile({
  label,
  value,
  tone = "neutral",
  note,
}: {
  label: string;
  value: React.ReactNode;
  tone?: "red" | "green" | "amber" | "neutral";
  note?: string;
}) {
  const toneClass = {
    red: "text-red-500",
    green: "text-green-500",
    amber: "text-amber-500",
    neutral: "text-foreground",
  }[tone];
  return (
    <div className="rounded-md border bg-muted/20 px-3 py-3 text-center shadow-sm">
      <div className={cn("text-2xl font-bold tabular-nums", toneClass)}>{value}</div>
      <div className="mt-1 text-[11px] text-muted-foreground">{label}</div>
      {note && <div className="mt-1 truncate text-[10px] text-muted-foreground/70">{note}</div>}
    </div>
  );
}

function SummaryHero({ data }: { data: MarketDashboardResponse }) {
  const sentiment = data.sentiment;
  const pools = data.pools;
  const breadth = data.market_overview?.breadth;
  const direction = sentiment?.breadth_strength !== undefined ? `${((sentiment.breadth_strength + 1) * 50).toFixed(1)}%` : "-";
  const summary = buildSummary(data);

  return (
    <DashboardSection
      title="一句话总收口"
      sub={sentiment?.stage ? sentiment.stage : undefined}
      icon={<BarChart3 className="h-5 w-5 text-amber-500" />}
      right={<span className="rounded bg-amber-500/15 px-2 py-1 text-xs font-medium text-amber-600 dark:text-amber-300">{sentiment?.stage || "盘面快照"}</span>}
    >
      <div className="grid gap-4 xl:grid-cols-[1fr_360px]">
        <p className="text-lg font-medium leading-relaxed text-foreground">{summary}</p>
        <div className="grid grid-cols-3 gap-3">
          <MetricTile label="情绪温度" value={sentiment ? Math.round(sentiment.temperature) : "-"} tone="amber" />
          <MetricTile label="方向广度" value={direction} tone={(sentiment?.breadth_strength ?? 0) >= 0 ? "red" : "green"} />
          <MetricTile label="连板高度" value={pools?.max_limit_up_height ?? breadth?.limit_up ?? "-"} tone="red" />
        </div>
      </div>
    </DashboardSection>
  );
}

function buildSummary(data: MarketDashboardResponse): string {
  const themes = data.themes?.main_lines ?? [];
  const mainNames = themes.slice(0, 3).map((t) => t.name).filter(Boolean).join(" / ");
  const breadth = data.market_overview?.breadth;
  const pools = data.pools;
  const sentiment = data.sentiment;
  const bits = [
    mainNames ? `${mainNames}领涨` : "",
    pools ? `涨停${pools.limit_up_count}家、跌停${pools.limit_down_count}家` : "",
    breadth ? `两市成交${yi(breadth.turnover_billion)}` : "",
    sentiment ? `情绪${Math.round(sentiment.temperature)}，${sentiment.stage_reason}` : "",
  ].filter(Boolean);
  return bits.length ? bits.join("，") : "盘面快照未同步，等待数据任务完成后生成总收口。";
}

function HoldingsStrip({
  dashboard,
  closeStage,
}: {
  dashboard: MarketDashboardResponse;
  closeStage: MarketDashboardStageResponse | null;
}) {
  const stageRows = asRows<Record<string, unknown>>(field(closeStage?.data, "holding_sector_strength"));
  const rows = stageRows.length
    ? stageRows.map((r) => ({
        symbol: String(r.symbol ?? ""),
        name: String(r.name ?? r.symbol ?? ""),
        change_pct: num(r.change_pct ?? r.return_pct),
        note: String(r.sector ?? r.theme ?? ""),
      }))
    : dashboard.watchlist.slice(0, 8).map((w) => ({ symbol: w.symbol, name: w.name || w.symbol, change_pct: null, note: "持仓/自选" }));

  return (
    <DashboardSection title="我的持仓" icon={<Wallet className="h-5 w-5 text-amber-500" />}>
      {rows.length ? (
        <div className="flex flex-wrap items-center gap-2">
          {rows.map((row) => (
            <span key={row.symbol} className="inline-flex items-center gap-1 rounded-md border bg-muted/30 px-3 py-1.5 text-xs shadow-sm">
              <span className="font-medium">{row.name}</span>
              {row.change_pct !== null && <span className={valueTone(row.change_pct)}>{pct(row.change_pct)}</span>}
              {row.note && <span className="hidden text-muted-foreground sm:inline">{row.note}</span>}
            </span>
          ))}
        </div>
      ) : (
        <EmptyHint>持仓快照未同步</EmptyHint>
      )}
    </DashboardSection>
  );
}

function MarketEnvironmentBlock({ data }: { data: MarketDashboardResponse }) {
  const overviewRows = data.market_overview?.indices ?? [];
  const breadth = data.market_overview?.breadth;
  const indexMap = new Map(overviewRows.map((row) => [row.symbol, row]));
  const adv = breadth?.advancers ?? 0;
  const dec = breadth?.decliners ?? 0;
  const total = Math.max(adv + dec + (breadth?.flat ?? 0), 1);
  // 最新交易日基准：板块快照覆盖到的最新一天。指数日期落后于此即"数据滞后"。
  const latestDate = data.themes?.trade_date;

  return (
    <DashboardSection title="盘型 / 环境" sub="A股 + 海外" icon={<Layers3 className="h-5 w-5 text-amber-500" />}>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {INDEX_OPTIONS.map((item) => (
          <IndexMiniCard key={item.symbol} meta={item} row={indexMap.get(item.symbol)} latestDate={latestDate} />
        ))}
      </div>
      <div className="mt-3 rounded-md border bg-muted/20 px-3 py-3">
        <div className="mb-2 text-xs text-muted-foreground">市场涨跌分布</div>
        {breadth ? (
          <div className="flex items-center gap-3">
            <span className="tabular-nums text-red-500">↑ {integer(adv)}</span>
            <div className="h-3 flex-1 overflow-hidden rounded-full bg-muted">
              <div className="h-full bg-red-500" style={{ width: `${(adv / total) * 100}%` }} />
            </div>
            <span className="tabular-nums text-green-500">↓ {integer(dec)}</span>
          </div>
        ) : (
          <div className="text-xs text-muted-foreground">市场宽度未同步</div>
        )}
      </div>
    </DashboardSection>
  );
}

function IndexMiniCard({ meta, row, latestDate }: { meta: { symbol: string; name: string }; row?: MarketIndexRow; latestDate?: string }) {
  // 指数日期落后于最新交易日 → 数据源抽风导致缺最新日，静默回退到了旧值，必须标出来。
  const stale = !!latestDate && !!row?.trade_date && row.trade_date < latestDate;
  const [bars, setBars] = useState<MarketBarsResponse | null>(null);
  useEffect(() => {
    let cancelled = false;
    api.getMarketBars(meta.symbol, 30)
      .then((resp) => {
        if (!cancelled) setBars(resp);
      })
      .catch(() => {
        if (!cancelled) setBars({ status: "error", bars: [] });
      });
    return () => {
      cancelled = true;
    };
  }, [meta.symbol]);

  const option = useMemo<EChartsOption>(() => {
    const chartBars = bars?.bars ?? [];
    const values = chartBars.map((b) => b.close);
    const color = (row?.change_pct ?? 0) >= 0 ? "#ef4444" : "#22c55e";
    return {
      animation: false,
      grid: { left: 2, right: 2, top: 6, bottom: 2 },
      xAxis: { type: "category", show: false, data: chartBars.map((b) => b.date) },
      yAxis: { type: "value", show: false, scale: true },
      series: [
        {
          type: "line",
          data: values,
          smooth: true,
          symbol: "none",
          lineStyle: { color, width: 2 },
          areaStyle: { color: color.replace(")", ", 0.18)") },
        },
      ],
    };
  }, [bars, row?.change_pct]);

  return (
    <div className="rounded-md border bg-muted/20 p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="text-sm font-medium text-muted-foreground">{meta.name}</div>
        <div className="flex items-center gap-1.5">
          {stale && (
            <span className="rounded bg-amber-500/15 px-1 py-0.5 text-[10px] font-medium text-amber-600 dark:text-amber-300" title={`指数数据滞后：最新交易日 ${latestDate}，指数仅到 ${row?.trade_date}`}>
              滞后
            </span>
          )}
          <span className={cn("text-xs font-semibold tabular-nums", valueTone(row?.change_pct))}>{row ? pct(row.change_pct) : "数据不可用"}</span>
        </div>
      </div>
      <div className={cn("mt-1 text-2xl font-bold tabular-nums", valueTone(row?.change_pct))}>
        {row ? row.price.toLocaleString("zh-CN", { maximumFractionDigits: 3 }) : "-"}
      </div>
      {bars?.bars?.length ? <Chart option={option} height={64} /> : <div className="h-16 text-xs text-muted-foreground">K线未同步</div>}
    </div>
  );
}

function LimitStats({ data }: { data: MarketDashboardResponse }) {
  const pools = data.pools;
  const breadth = data.market_overview?.breadth;
  const limitUp = pools?.limit_up_count ?? breadth?.limit_up;
  const limitDown = pools?.limit_down_count ?? breadth?.limit_down;
  const touched = field<number>(pools, "touched_limit_up_count");
  const failed = field<number>(pools, "failed_limit_up_count");
  const nonSt = field<number>(pools, "non_st_limit_up_count");
  const st = field<number>(pools, "st_limit_up_count");
  const sealedAmt = field<number>(pools, "sealed_amount_billion");
  const failRate = field<number>(pools, "fail_rate");
  const promoteRate = field<number>(pools, "promotion_rate");

  return (
    <DashboardSection
      title="涨跌停情况"
      sub={breadth?.turnover_billion ? `两市成交 ${yi(breadth.turnover_billion)}` : "两市成交未同步"}
      icon={<Flame className="h-5 w-5 text-amber-500" />}
    >
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
        <MetricTile label="正式封板涨停" value={integer(limitUp)} tone="red" />
        <MetricTile label="触及涨停 + 炸板" value={integer(touched)} note={touched === undefined ? "未同步" : undefined} />
        <MetricTile label="跌停数量" value={integer(limitDown)} tone="green" />
        <MetricTile label="非 ST 涨停" value={integer(nonSt)} tone="red" note={nonSt === undefined ? "未同步" : undefined} />
        <MetricTile label="炸板数量" value={integer(failed)} tone="amber" note={failed === undefined ? "未同步" : undefined} />
        <MetricTile label="封板成交额" value={sealedAmt === undefined ? "-" : `${sealedAmt.toFixed(1)}亿`} note={sealedAmt === undefined ? "未同步" : undefined} />
        <MetricTile label="炸板率" value={failRate === undefined ? "-" : pct(failRate)} tone="amber" note={failRate === undefined ? "未同步" : undefined} />
        <MetricTile label="晋级率" value={promoteRate === undefined ? "-" : pct(promoteRate)} tone="red" note={promoteRate === undefined ? "未同步" : undefined} />
        <MetricTile label="ST 涨停" value={integer(st)} note={st === undefined ? "未同步" : undefined} />
      </div>
    </DashboardSection>
  );
}

function SentimentGauge({ sentiment }: { sentiment?: MarketSentiment | null }) {
  const temperature = sentiment?.temperature ?? 0;
  const option = useMemo<EChartsOption>(() => ({
    series: [
      {
        type: "gauge",
        min: 0,
        max: 100,
        radius: "94%",
        splitNumber: 5,
        axisLine: {
          lineStyle: {
            width: 14,
            color: [
              [0.35, "#22c55e"],
              [0.6, "#facc15"],
              [0.8, "#fb923c"],
              [1, "#ef4444"],
            ],
          },
        },
        pointer: { width: 4 },
        axisTick: { show: false },
        splitLine: { length: 10 },
        axisLabel: { fontSize: 10, color: "#64748b" },
        detail: { formatter: "{value}", fontSize: 28, fontWeight: 700, offsetCenter: [0, "48%"] },
        data: [{ value: temperature }],
      },
    ],
  }), [temperature]);

  return (
    <DashboardSection title="情绪运行阶段" icon={<Gauge className="h-5 w-5 text-amber-500" />}>
      {sentiment ? (
        <div className="grid gap-3 md:grid-cols-[360px_1fr]">
          <Chart option={option} height={250} />
          <div className="flex flex-col justify-center gap-3">
            <div>
              <div className="text-3xl font-bold">{sentiment.temperature}</div>
              <div className="mt-1 text-sm text-muted-foreground">{sentiment.stage} · {sentiment.label}</div>
            </div>
            <p className="max-w-3xl text-sm leading-relaxed text-muted-foreground">{sentiment.stage_reason}</p>
          </div>
        </div>
      ) : (
        <EmptyHint>情绪快照未同步</EmptyHint>
      )}
    </DashboardSection>
  );
}

function CapitalEvidenceBlock({ capital, pools }: { capital?: MarketCapital | null; pools?: MarketPools | null }) {
  return (
    <div className="grid gap-3 xl:grid-cols-3">
      <DashboardSection title="资金流证据" sub="行业净流入 TOP5" icon={<TrendingUp className="h-5 w-5 text-amber-500" />}>
        <SectorCapitalBars capital={capital} />
      </DashboardSection>
      <DashboardSection title="个股资金" sub="净流入 / 净流出">
        <StockCapitalColumns capital={capital} />
      </DashboardSection>
      <DashboardSection title="连板梯队" sub={`最高 ${pools?.max_limit_up_height ?? "-"} 板`}>
        <LimitLadderList pools={pools} />
      </DashboardSection>
    </div>
  );
}

function SectorCapitalBars({ capital }: { capital?: MarketCapital | null }) {
  const rows = capital?.sector_top5 ?? [];
  if (!rows.length) return <EmptyHint>行业资金流未同步</EmptyHint>;
  const max = Math.max(...rows.map((r) => Math.abs(r.main_net)), 1);
  return (
    <div className="space-y-3">
      {rows.map((row) => (
        <div key={row.sector} className="grid grid-cols-[72px_1fr_72px] items-center gap-2 text-xs">
          <span className="truncate text-muted-foreground">{row.sector}</span>
          <div className="h-3 rounded-full bg-muted">
            <div className="h-full rounded-full bg-red-500" style={{ width: `${Math.max(4, Math.abs(row.main_net) / max * 100)}%` }} />
          </div>
          <span className="text-right tabular-nums text-red-500">{fmtYi(row.main_net)}</span>
        </div>
      ))}
    </div>
  );
}

function StockCapitalColumns({ capital }: { capital?: MarketCapital | null }) {
  const inflow = capital?.stock_inflow_top ?? [];
  const outflow = capital?.stock_outflow_top ?? [];
  if (!inflow.length && !outflow.length) return <EmptyHint>个股资金未同步</EmptyHint>;
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <StockList title="净流入 TOP" rows={inflow} tone="red" />
      <StockList title="净流出 TOP" rows={outflow} tone="green" />
    </div>
  );
}

function StockList({ title, rows, tone }: { title: string; rows: { symbol: string; name: string; main_net: number; change_pct: number }[]; tone: "red" | "green" }) {
  return (
    <div>
      <div className={cn("mb-2 text-xs font-medium", tone === "red" ? "text-red-500" : "text-green-500")}>{title}</div>
      <ul className="space-y-1">
        {rows.slice(0, 5).map((row) => (
          <li key={row.symbol} className="flex items-center justify-between gap-2 text-xs">
            <span className="truncate">{row.name}</span>
            <span className={cn("tabular-nums", tone === "red" ? "text-red-500" : "text-green-500")}>{fmtYi(row.main_net)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function LimitLadderList({ pools }: { pools?: MarketPools | null }) {
  const ladder = pools?.limitup_ladder ?? [];
  if (!ladder.length) return <EmptyHint>连板梯队未同步</EmptyHint>;
  return (
    <div className="space-y-2">
      {ladder.slice(0, 6).map((rung) => (
        <div key={rung.days} className="grid grid-cols-[54px_1fr] gap-2 text-xs">
          <span className={cn("rounded-md px-2 py-1 text-center font-bold", rung.days >= 5 ? "bg-red-500/20 text-red-500" : "bg-amber-500/15 text-amber-600 dark:text-amber-300")}>
            {rung.days}板
          </span>
          <div className="flex flex-wrap gap-1">
            {rung.stocks.slice(0, 8).map((s) => (
              <span key={s.symbol} className="rounded bg-muted/60 px-2 py-1">{s.name || s.symbol}</span>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function ThemeLayers({ themes }: { themes?: MarketThemes | null }) {
  const main = themes?.main_lines ?? [];
  const observe = themes?.observe ?? [];
  const concept = themes?.concept_sectors ?? [];
  const layers = [
    { label: "主线", hint: "主线最强", rows: main.slice(0, 1), tone: "red" },
    { label: "次主线", hint: "次级留存", rows: [...main.slice(1, 3), ...concept.slice(3, 4)], tone: "amber" },
    { label: "活口", hint: "活口观察", rows: observe.slice(0, 3), tone: "neutral" },
    { label: "情绪链", hint: "情绪温度", rows: concept.slice(0, 2), tone: "green" },
  ];

  return (
    <DashboardSection title="主线 & 观察板块" sub="四分层：主线 / 次主线 / 活口 / 情绪链" icon={<Layers3 className="h-5 w-5 text-amber-500" />}>
      {themes ? (
        <div className="grid gap-3 md:grid-cols-2">
          {layers.map((layer) => (
            <div key={layer.label} className="rounded-md border bg-muted/20 p-3">
              <div className="mb-3 flex items-center justify-between">
                <span className="rounded bg-amber-500/15 px-2 py-1 text-xs font-medium text-amber-600 dark:text-amber-300">{layer.label}</span>
                <span className="text-xs text-muted-foreground">{layer.hint}</span>
              </div>
              <div className="space-y-2">
                {layer.rows.length ? layer.rows.map((row) => (
                  <div key={`${layer.label}-${row.name}`} className="text-sm">
                    <div className="font-semibold">{row.name}</div>
                    <div className="mt-1 flex flex-wrap gap-1">
                      <span className="rounded bg-muted px-2 py-0.5 text-xs text-muted-foreground">{row.leader || "领涨未同步"}</span>
                      <Pct value={row.change_pct} className="rounded bg-muted px-2 py-0.5 text-xs" />
                    </div>
                  </div>
                )) : <div className="text-xs text-muted-foreground">该层级未同步</div>}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <EmptyHint>板块快照未同步</EmptyHint>
      )}
    </DashboardSection>
  );
}

function RiskBoundary({ data }: { data: MarketDashboardResponse }) {
  const pools = data.pools;
  const sentiment = data.sentiment;
  const risks = [
    pools ? `跌停${pools.limit_down_count}家，最高连板${pools.max_limit_up_height}板。` : "涨跌停池未同步。",
    sentiment ? `当前情绪为${sentiment.stage}，${sentiment.stage_reason}` : "情绪快照未同步。",
    field(data.pools, "fail_rate") === undefined ? "炸板率未同步，不能判断封板质量边界。" : `炸板率${pct(field(data.pools, "fail_rate"))}。`,
  ];
  return (
    <DashboardSection title="风险 / 风险边界" icon={<ShieldAlert className="h-5 w-5 text-red-500" />} className="border-red-500/30 bg-red-500/5">
      <p className="text-sm leading-relaxed text-muted-foreground">{risks.join(" ")}</p>
    </DashboardSection>
  );
}

function MultiPeriodTable({ rows }: { rows?: MultiPeriodRow[] }) {
  const safeRows = rows ?? [];
  const Cell = ({ v }: { v: number | null }) => (
    <td className="px-3 py-2 text-right tabular-nums">{v === null ? <span className="text-muted-foreground/60">-</span> : <Pct value={v} />}</td>
  );
  return (
    <DashboardSection
      title="多周期涨幅榜 TOP"
      sub="标 · 为前日新题材"
      icon={<TrendingUp className="h-5 w-5 text-amber-500" />}
      right={<span className="rounded bg-amber-500/15 px-2 py-1 text-xs text-amber-600 dark:text-amber-300">新题材溢价</span>}
    >
      {safeRows.length ? (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] text-sm">
            <thead>
              <tr className="border-b text-xs text-muted-foreground">
                <th className="px-3 py-2 text-left font-normal">名称</th>
                <th className="px-3 py-2 text-left font-normal">题材</th>
                <th className="px-3 py-2 text-right font-normal">当日</th>
                <th className="px-3 py-2 text-right font-normal">5日</th>
                <th className="px-3 py-2 text-right font-normal">20日</th>
                <th className="px-3 py-2 text-right font-normal">60日</th>
              </tr>
            </thead>
            <tbody>
              {safeRows.slice(0, 12).map((row) => (
                <tr key={row.symbol} className="border-b border-border/50">
                  <td className="px-3 py-2">
                    <span className="font-semibold">{row.name}</span>
                    {row.approx && <span className="ml-1 text-amber-500">· 新</span>}
                  </td>
                  <td className="px-3 py-2 text-muted-foreground">{row.symbol}</td>
                  <Cell v={row.d1} />
                  <Cell v={row.d5} />
                  <Cell v={row.d20} />
                  <Cell v={row.d60} />
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyHint>多周期涨幅榜未同步</EmptyHint>
      )}
    </DashboardSection>
  );
}

function ThemeHeatmapBlock({ themes }: { themes?: MarketThemes | null }) {
  // 优先行业板块（语义干净），概念板块里混入"昨日涨停/打板"等机械分类，仅作回退。
  const sectors = themes?.industry_sectors ?? themes?.concept_sectors ?? [];
  const option = useMemo<EChartsOption>(() => ({
    tooltip: { formatter: (p: any) => `${p?.name ?? ""}<br/>${pct(p?.data?.change_pct, 2)}` },
    series: [
      {
        type: "treemap",
        roam: false,
        nodeClick: false,
        breadcrumb: { show: false },
        label: { color: "#fff", fontSize: 12, fontWeight: 600 },
        data: sectors.slice(0, 24).map((s) => ({
          name: s.name,
          // 面积=成交额（热度），与涨跌解耦；旧数据无 turnover 时回退到成分股数，绝不用涨跌幅——否则红格被放大、绿格被挤没。
          value: Math.max(1, s.turnover ?? (s.advancers ?? 0) + (s.decliners ?? 0)),
          change_pct: s.change_pct,
          itemStyle: { color: pctBg(s.change_pct) },
        })),
      },
    ],
  }), [sectors]);

  return (
    <DashboardSection title="行业热力图" sub="面积=热度 · 颜色=涨跌" icon={<Activity className="h-5 w-5 text-amber-500" />}>
      {sectors.length ? <Chart option={option} height={360} /> : <EmptyHint>行业热力图未同步</EmptyHint>}
    </DashboardSection>
  );
}

export function MarketDashboard() {
  const [data, setData] = useState<MarketDashboardResponse | null>(null);
  const [closeStage, setCloseStage] = useState<MarketDashboardStageResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (silent = false) => {
    if (!silent) setRefreshing(true);
    try {
      const [dashboard, close] = await Promise.all([
        api.getMarketDashboard(),
        api.getMarketDashboardStage("close-review").catch(() => null),
      ]);
      setData(dashboard);
      setCloseStage(close);
      setError(null);
    } catch {
      setError("总览大屏数据加载失败。");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    load(true);
  }, [load]);

  if (loading) {
    return (
      <div className="flex h-[calc(100vh-3.5rem)] items-center justify-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        加载总览大屏...
      </div>
    );
  }

  if (!data) {
    return (
      <div className="flex h-[calc(100vh-3.5rem)] items-center justify-center text-sm text-muted-foreground">
        总览大屏暂不可用
      </div>
    );
  }

  return (
    <div className="flex h-[calc(100vh-3.5rem)] flex-col bg-muted/20">
      <header className="shrink-0 border-b bg-background">
        <div className="flex flex-col gap-4 px-4 py-4 md:px-6 xl:flex-row xl:items-center xl:justify-between">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <Gauge className="h-5 w-5 text-primary" />
              <h1 className="text-xl font-semibold tracking-tight">总览大屏</h1>
              <span className="rounded bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary">A 股全局</span>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs text-muted-foreground">{fmtTime(data.updated_at) ? `更新 ${fmtTime(data.updated_at)}` : ""}</span>
            <button
              type="button"
              onClick={() => load()}
              disabled={refreshing}
              className="inline-flex h-9 items-center gap-1.5 rounded-md border bg-background px-3 text-xs text-muted-foreground transition hover:bg-muted hover:text-foreground disabled:opacity-50"
            >
              <RefreshCw className={cn("h-3.5 w-3.5", refreshing && "animate-spin")} />
              刷新
            </button>
            <Link
              to="/agent"
              className="inline-flex h-9 items-center gap-1.5 rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground transition hover:opacity-90"
            >
              问智能体
              <ArrowRight className="h-3.5 w-3.5" />
            </Link>
          </div>
        </div>
      </header>

      <main className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4 md:p-6">
        {error && (
          <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-300">
            {error}
          </div>
        )}

        <SummaryHero data={data} />
        <HoldingsStrip dashboard={data} closeStage={closeStage} />
        <MarketEnvironmentBlock data={data} />
        <LimitStats data={data} />
        <SentimentGauge sentiment={data.sentiment} />
        <CapitalEvidenceBlock capital={data.capital} pools={data.pools} />
        <ThemeLayers themes={data.themes} />
        <RiskBoundary data={data} />
        <MultiPeriodTable rows={data.multi_period} />
        <ThemeHeatmapBlock themes={data.themes} />

      </main>
    </div>
  );
}

export default MarketDashboard;
