import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import type { EChartsOption } from "echarts";
import {
  ArrowLeft,
  BarChart3,
  Bell,
  CalendarClock,
  ClipboardList,
  Database,
  Gauge,
  Loader2,
  RefreshCw,
  ShieldAlert,
  Target,
  TrendingDown,
  TrendingUp,
  Zap,
} from "lucide-react";
import { api, type MarketDashboardStage, type MarketDashboardStageResponse } from "@/lib/api";
import { Chart } from "@/components/dashboard/Chart";
import { getChartTheme } from "@/lib/chart-theme";
import { cn } from "@/lib/utils";

type Row = Record<string, any>;

const STAGE_META: Record<MarketDashboardStage, { title: string; desc: string; nav: string; icon: ReactNode }> = {
  "morning-brief": {
    title: "早盘内参",
    desc: "",
    nav: "08:50",
    icon: <Bell className="h-4 w-4 text-amber-500" />,
  },
  "intraday-monitor": {
    title: "盘中监控",
    desc: "",
    nav: "盘中",
    icon: <Gauge className="h-4 w-4 text-cyan-500" />,
  },
  "tail-strategy": {
    title: "尾盘策略",
    desc: "",
    nav: "14:30",
    icon: <ShieldAlert className="h-4 w-4 text-amber-500" />,
  },
  "close-review": {
    title: "收盘复盘",
    desc: "",
    nav: "盘后",
    icon: <ClipboardList className="h-4 w-4 text-primary" />,
  },
};

function arr<T = Row>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function n(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function pct(value: unknown): string {
  const valueNum = n(value, NaN);
  if (!Number.isFinite(valueNum)) return "-";
  return `${valueNum > 0 ? "+" : ""}${valueNum.toFixed(2)}%`;
}

function pctTone(value: unknown): string {
  const valueNum = n(value);
  if (valueNum > 0) return "text-red-500";
  if (valueNum < 0) return "text-green-500";
  return "text-muted-foreground";
}

function money(value: unknown): string {
  const valueNum = n(value, NaN);
  if (!Number.isFinite(valueNum)) return "-";
  if (Math.abs(valueNum) >= 1e8) return `${(valueNum / 1e8).toFixed(2)}亿`;
  if (Math.abs(valueNum) >= 1e4) return `${(valueNum / 1e4).toFixed(1)}万`;
  return valueNum.toFixed(0);
}

function StageShell({
  stage,
  payload,
  loading,
  refreshing,
  error,
  onRefresh,
}: {
  stage: MarketDashboardStage;
  payload: MarketDashboardStageResponse | null;
  loading: boolean;
  refreshing: boolean;
  error: string | null;
  onRefresh: () => void;
}) {
  const meta = STAGE_META[stage];
  const data = (payload?.data ?? {}) as Row;
  return (
    <div className="flex h-[calc(100vh-3.5rem)] flex-col bg-muted/20 text-foreground">
      <header className="shrink-0 border-b bg-background/95 px-4 py-3 md:px-6">
        <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
          <div className="min-w-0">
            <Link to="/market-dashboard" className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
              <ArrowLeft className="h-3.5 w-3.5" />
              总览大屏
            </Link>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              {meta.icon}
              <h1 className="text-xl font-semibold tracking-tight">{meta.title}</h1>
              <span className="rounded border bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">{meta.nav}</span>
              {payload?.date && <span className="text-[11px] text-muted-foreground">数据日 {payload.date}</span>}
              {payload?.updated_at && (
                <span className="text-[11px] text-muted-foreground">
                  更新 {new Date(payload.updated_at).toLocaleTimeString("zh-CN", { hour12: false })}
                </span>
              )}
            </div>
            {meta.desc && <p className="mt-1 max-w-4xl text-xs leading-relaxed text-muted-foreground">{meta.desc}</p>}
          </div>
          <button
            type="button"
            onClick={onRefresh}
            disabled={refreshing}
            className="inline-flex h-9 items-center gap-1.5 rounded-md border bg-background px-3 text-xs transition hover:bg-muted disabled:opacity-50"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", refreshing && "animate-spin")} />
            刷新
          </button>
        </div>
      </header>

      <main className="min-h-0 flex-1 overflow-y-auto p-4 md:p-6">
        {loading ? (
          <div className="flex min-h-[360px] items-center justify-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-5 w-5 animate-spin" />
            正在加载
          </div>
        ) : error ? (
          <div className="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">{error}</div>
        ) : (
          <div className="mx-auto max-w-7xl space-y-4">
            <DataStatus data={data} errors={payload?.errors ?? []} />
            {stage === "morning-brief" && <MorningBriefView data={data} />}
            {stage === "intraday-monitor" && <IntradayView data={data} />}
            {stage === "tail-strategy" && <TailStrategyView data={data} />}
            {stage === "close-review" && <CloseReviewView data={data} />}
          </div>
        )}
      </main>
    </div>
  );
}

function DataStatus({ data, errors }: { data: Row; errors: { source: string; message: string }[] }) {
  const missing = arr<string>(data.missing_tables);
  const partial = data.data_status && data.data_status !== "ok";
  if (!partial && !missing.length && !errors.length) return null;
  return (
    <section className="rounded-md border bg-card px-3 py-3 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <Database className="h-3.5 w-3.5 text-amber-500" />
        <span className="font-medium">{data.data_status === "missing" ? "数据准备中" : "部分指标等待同步"}</span>
      </div>
      {(missing.length > 0 || errors.length > 0) && <div className="mt-2 text-muted-foreground">已隐藏内部同步细节，页面只展示已确认快照。</div>}
    </section>
  );
}

function Section({ title, desc, icon, right, children }: { title: string; desc?: string; icon?: ReactNode; right?: ReactNode; children: ReactNode }) {
  return (
    <section className="rounded-md border bg-card/95 shadow-sm">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b px-4 py-3">
        <div className="flex items-center gap-2">
          {icon}
          <div>
            <h2 className="text-sm font-semibold">{title}</h2>
            {desc && <p className="mt-0.5 text-[11px] text-muted-foreground">{desc}</p>}
          </div>
        </div>
        {right}
      </header>
      <div className="p-4">{children}</div>
    </section>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <div className="flex min-h-[76px] items-center justify-center rounded-md border border-dashed bg-muted/20 text-xs text-muted-foreground">{children}</div>;
}

function Metric({ label, value, tone }: { label: string; value: ReactNode; tone?: string }) {
  return (
    <div className="rounded-md border bg-muted/20 px-3 py-3">
      <div className={cn("text-2xl font-semibold tabular-nums", tone)}>{value}</div>
      <div className="mt-1 text-[11px] text-muted-foreground">{label}</div>
    </div>
  );
}

function LegacyMorningBriefView({ data }: { data: Row }) {
  const mappedThemes = arr(data.mapped_themes);
  const overnight = arr(data.overnight_markets);
  const events = arr(data.key_events);
  return (
    <>
      <Section title="早盘内参 · 08:50 推送" desc="只展示盘前快照；数据不完整时不生成结论。" icon={<Bell className="h-4 w-4 text-amber-500" />}>
        <div className="rounded-md border-l-2 border-amber-500 bg-muted/40 px-4 py-3 text-sm leading-7">
          {String(data.brief || data.risk_note || "盘前快照未同步。")}
        </div>
      </Section>
      <div className="grid gap-4 xl:grid-cols-2">
        <Section title="隔夜市场" icon={<TrendingUp className="h-4 w-4 text-red-500" />}>
          {overnight.length ? <SimpleRows rows={overnight} primary="name" secondary="change_pct" /> : <Empty>隔夜市场快照未同步</Empty>}
        </Section>
        <Section title="今日事件" icon={<CalendarClock className="h-4 w-4 text-primary" />}>
          {events.length ? <TextRows rows={events} /> : <Empty>事件快照未同步</Empty>}
        </Section>
      </div>
      <Section title="美股 / 事件 → A 股映射" desc="映射基于盘前题材与板块快照。" icon={<Target className="h-4 w-4 text-cyan-500" />}>
        {mappedThemes.length ? <ThemeTable rows={mappedThemes} /> : <Empty>映射数据未同步</Empty>}
      </Section>
    </>
  );
}

void LegacyMorningBriefView;

function LegacyMorningBriefView2({ data }: { data: Row }) {
  const indices = arr(data.overnight_indices);
  const themes = arr(data.us_theme_mapping);
  const transmissions = arr(data.transmission_analysis);
  const news = (data.premarket_news ?? {}) as Record<string, Row[]>;
  const newsGroups = [
    { key: "policy", title: "政策" },
    { key: "industry", title: "产业" },
    { key: "catalyst", title: "催化" },
    { key: "risk", title: "风险" },
  ];
  return (
    <>
      <Section title="早盘内参：预测总结当天股市情况" icon={<Bell className="h-4 w-4 text-amber-500" />}>
        <div className="rounded-md border-l-2 border-amber-500 bg-muted/40 px-4 py-3 text-sm leading-7">
          {String(data.prediction_summary || "盘前预测快照未同步。")}
        </div>
      </Section>
      <Section title="隔夜 & 美股（收盘情况）：关键指数的走势图" icon={<TrendingUp className="h-4 w-4 text-red-500" />}>
        {indices.length ? <OvernightIndexCharts rows={indices} /> : <Empty>隔夜指数未同步</Empty>}
      </Section>
      <div className="grid gap-4 xl:grid-cols-2">
        <Section title="美股题材表现（映射 A 股）" icon={<Target className="h-4 w-4 text-cyan-500" />}>
          {themes.length ? <UsThemeMappingTable rows={themes} /> : <Empty>美股题材表现未同步</Empty>}
        </Section>
        <Section title="美股 → A 股传导分析" icon={<Zap className="h-4 w-4 text-amber-500" />}>
          {transmissions.length ? <TransmissionRows rows={transmissions} /> : <Empty>传导分析未同步</Empty>}
        </Section>
      </div>
      <Section title="A 股盘前要闻（政策，产业，催化，风险）" icon={<CalendarClock className="h-4 w-4 text-primary" />}>
        {newsGroups.some((group) => arr(news[group.key]).length) ? (
          <div className="grid gap-3 xl:grid-cols-4">
            {newsGroups.map((group) => (
              <div key={group.key} className="rounded-md border bg-background">
                <div className="border-b px-3 py-2 text-xs font-semibold">{group.title}</div>
                <div className="space-y-2 p-3">
                  {arr(news[group.key]).length ? <NewsRows rows={arr(news[group.key]).slice(0, 5)} /> : <Empty>暂无要闻</Empty>}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <Empty>盘前要闻未同步</Empty>
        )}
      </Section>
    </>
  );
}

function OvernightIndexCharts({ rows }: { rows: Row[] }) {
  return (
    <div className="grid gap-3 xl:grid-cols-2">
      {rows.slice(0, 4).map((row) => (
        <div key={row.symbol} className="rounded-md border bg-background p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold">{row.name || row.symbol}</div>
              <div className="text-[11px] text-muted-foreground">{row.symbol} · 收盘 {Number.isFinite(n(row.close, NaN)) ? n(row.close).toFixed(2) : "-"}</div>
            </div>
            <span className={cn("text-sm font-semibold tabular-nums", pctTone(row.change_pct))}>{pct(row.change_pct)}</span>
          </div>
          <IndexLineChart row={row} />
        </div>
      ))}
    </div>
  );
}

function IndexLineChart({ row }: { row: Row }) {
  const option = useMemo((): EChartsOption => {
    const t = getChartTheme();
    const history = arr(row.history);
    const labels = history.map((item) => String(item.trade_date || item.date || ""));
    const values = history.map((item) => n(item.close, NaN));
    const lineColor = n(row.change_pct) >= 0 ? t.upColor : t.downColor;
    return {
      animation: false,
      grid: { left: 8, right: 8, top: 8, bottom: 22, containLabel: true },
      tooltip: {
        trigger: "axis",
        backgroundColor: t.tooltipBg,
        borderColor: t.tooltipBorder,
        textStyle: { color: t.tooltipText },
      },
      xAxis: {
        type: "category",
        data: labels,
        axisLabel: { color: t.textColor, fontSize: 10 },
        axisLine: { lineStyle: { color: t.axisColor } },
        axisTick: { show: false },
      },
      yAxis: {
        type: "value",
        scale: true,
        axisLabel: { color: t.textColor, fontSize: 10 },
        splitLine: { lineStyle: { color: t.gridColor } },
      },
      series: [{
        type: "line",
        data: values,
        smooth: true,
        symbolSize: 4,
        lineStyle: { width: 2, color: lineColor },
        itemStyle: { color: lineColor },
      }],
    };
  }, [row]);
  return <Chart option={option} height={150} />;
}

function UsThemeMappingTable({ rows }: { rows: Row[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[620px] text-sm">
        <thead className="text-xs text-muted-foreground">
          <tr>
            <th className="px-2 py-2 text-left font-medium">美股题材</th>
            <th className="px-2 py-2 text-left font-medium">代理标的</th>
            <th className="px-2 py-2 text-left font-medium">映射 A 股方向</th>
            <th className="px-2 py-2 text-right font-medium">涨跌幅</th>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 12).map((row) => (
            <tr key={row.theme_id || row.proxy_symbol} className="border-t">
              <td className="px-2 py-3 font-medium">{row.theme_name || "-"}</td>
              <td className="px-2 py-3 text-muted-foreground">{row.proxy_symbol} · {row.proxy_name || "-"}</td>
              <td className="px-2 py-3 text-muted-foreground">{arr<string>(row.a_share_mapping).join("、") || "-"}</td>
              <td className={cn("px-2 py-3 text-right tabular-nums", pctTone(row.change_pct))}>{pct(row.change_pct)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TransmissionRows({ rows }: { rows: Row[] }) {
  return (
    <div className="space-y-2">
      {rows.slice(0, 8).map((row) => (
        <div key={row.theme_id} className="rounded-md border bg-background px-3 py-2 text-xs">
          <div className="flex items-center justify-between gap-2">
            <span className="font-semibold">{row.us_theme || "-"}</span>
            <span className={cn("tabular-nums", pctTone(row.signal_strength))}>{pct(row.signal_strength)}</span>
          </div>
          <div className="mt-1 text-muted-foreground">{arr<string>(row.a_share_themes).join("、") || "-"}</div>
          <div className="mt-1 leading-5">{row.reason || "-"}</div>
        </div>
      ))}
    </div>
  );
}

function NewsRows({ rows }: { rows: Row[] }) {
  return (
    <div className="space-y-2">
      {rows.map((row, index) => (
        <div key={`${row.title}-${index}`} className="text-xs leading-5">
          <div className="font-medium">{row.title || "-"}</div>
          {(row.summary || row.source) && <div className="mt-0.5 text-muted-foreground">{row.summary || row.source}</div>}
        </div>
      ))}
    </div>
  );
}

void LegacyMorningBriefView2;

function MorningBriefView({ data }: { data: Row }) {
  const indices = arr(data.overnight_indices);
  const themes = arr(data.us_theme_mapping);
  const transmissions = arr(data.transmission_analysis);
  const news = (data.premarket_news ?? {}) as Record<string, Row[]>;
  const newsGroups = [
    { key: "policy", title: "政策" },
    { key: "industry", title: "产业" },
    { key: "catalyst", title: "催化" },
    { key: "risk", title: "风险" },
  ];
  const bestIndex = indices.reduce<Row | null>((best, item) => (best === null || n(item.change_pct) > n(best.change_pct) ? item : best), null);
  const tone = n(bestIndex?.change_pct) >= 0 ? "隔夜美股偏强" : "隔夜美股偏弱";

  return (
    <div className="space-y-4">
      <section className="rounded-md border bg-card/95 px-5 py-4 shadow-sm">
        <div className="flex flex-wrap items-center gap-2">
          <Bell className="h-4 w-4 text-amber-500" />
          <h2 className="text-lg font-semibold text-amber-600 dark:text-amber-300">早盘内参 · 08:50 推送</h2>
          <span className="rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-xs font-medium text-amber-600 dark:text-amber-300">{tone}</span>
          <span className="rounded-md border bg-muted px-2 py-0.5 text-xs text-muted-foreground">盘前快照</span>
        </div>
        <p className="mt-3 text-base font-medium leading-7 text-foreground">{String(data.prediction_summary || "盘前预测快照未同步。")}</p>
      </section>

      <Section title="隔夜美股 & 海外" desc="收盘表现" icon={<TrendingUp className="h-4 w-4 text-red-500" />}>
        {indices.length ? <MorningIndexGrid rows={indices} /> : <Empty>隔夜指数未同步</Empty>}
      </Section>

      <Section title="美股题材表现" desc="映射 A 股" icon={<Target className="h-4 w-4 text-cyan-500" />}>
        {themes.length ? <MorningThemeRows rows={themes} /> : <Empty>美股题材表现未同步</Empty>}
      </Section>

      <Section title="美股 → A股 传导分析" desc="今日可能被催化的方向" icon={<Zap className="h-4 w-4 text-amber-500" />}>
        {transmissions.length ? <MorningTransmissionTable rows={transmissions} /> : <Empty>传导分析未同步</Empty>}
      </Section>

      <Section title="A股盘前要闻" desc="政策 / 产业 / 催化 / 风险" icon={<CalendarClock className="h-4 w-4 text-primary" />}>
        {newsGroups.some((group) => arr(news[group.key]).length) ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            {newsGroups.map((group) => (
              <MorningNewsGroup key={group.key} title={group.title} rows={arr(news[group.key]).slice(0, 5)} />
            ))}
          </div>
        ) : (
          <Empty>盘前要闻未同步</Empty>
        )}
      </Section>
    </div>
  );
}

function MorningIndexGrid({ rows }: { rows: Row[] }) {
  const order = ["^IXIC", "^GSPC", "^SOX", "^DJI", "^HSI", "HSTECH", "^HSTECH"];
  const sorted = [...rows].sort((a, b) => {
    const ai = order.indexOf(String(a.symbol));
    const bi = order.indexOf(String(b.symbol));
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });
  return (
    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
      {sorted.slice(0, 6).map((row) => (
        <div key={row.symbol} className="min-h-[154px] rounded-md border bg-background px-4 py-3 shadow-sm">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold text-muted-foreground">{row.name || row.symbol}</div>
              <div className={cn("mt-2 text-3xl font-bold tabular-nums", pctTone(row.change_pct))}>
                {Number.isFinite(n(row.close, NaN)) ? n(row.close).toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 }) : "-"}
              </div>
            </div>
            <span className={cn("text-sm font-semibold tabular-nums", pctTone(row.change_pct))}>{pct(row.change_pct)}</span>
          </div>
          <MorningMiniLine row={row} />
        </div>
      ))}
    </div>
  );
}

function MorningMiniLine({ row }: { row: Row }) {
  const option = useMemo((): EChartsOption => {
    const t = getChartTheme();
    const history = arr(row.history);
    const values = history.map((item) => n(item.close, NaN));
    const lineColor = n(row.change_pct) >= 0 ? t.upColor : t.downColor;
    return {
      animation: false,
      grid: { left: 4, right: 4, top: 10, bottom: 4 },
      tooltip: { show: false },
      xAxis: { type: "category", data: history.map((item) => String(item.trade_date || item.date || "")), show: false },
      yAxis: { type: "value", scale: true, show: false },
      series: [{
        type: "line",
        data: values,
        smooth: true,
        symbol: "none",
        lineStyle: { width: 2, color: lineColor },
        areaStyle: { color: `${lineColor}22` },
      }],
    };
  }, [row]);
  return <Chart option={option} height={64} />;
}

function MorningThemeRows({ rows }: { rows: Row[] }) {
  return (
    <div className="space-y-2">
      {rows.slice(0, 8).map((row) => (
        <div key={row.theme_id || row.proxy_symbol} className="grid gap-3 rounded-md border bg-background px-4 py-3 text-sm shadow-sm md:grid-cols-[1fr_120px_1fr] md:items-center">
          <div className="font-semibold">{row.theme_name || row.proxy_name || "-"}</div>
          <div className={cn("flex items-center gap-2 tabular-nums md:justify-center", pctTone(row.change_pct))}>
            <span className="font-semibold">{pct(row.change_pct)}</span>
            <span className="text-muted-foreground">→</span>
          </div>
          <div className="font-medium text-amber-600 dark:text-amber-300 md:text-right">{arr<string>(row.a_share_mapping).join("、") || "-"}</div>
        </div>
      ))}
    </div>
  );
}

function MorningTransmissionTable({ rows }: { rows: Row[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[820px] text-sm">
        <thead className="bg-muted/40 text-xs text-muted-foreground">
          <tr>
            <th className="px-4 py-3 text-left font-medium">A股方向</th>
            <th className="px-4 py-3 text-left font-medium">传导强度</th>
            <th className="px-4 py-3 text-left font-medium">逻辑</th>
            <th className="px-4 py-3 text-left font-medium">关注个股</th>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 8).map((row) => {
            const source = (row.source_data ?? {}) as Row;
            const focus = arr<string>(source.focus_stocks);
            const direction = String(row.direction || "");
            const label = direction === "strong" ? "强" : direction === "weak" ? "弱" : direction === "medium" || n(row.signal_strength) > 0.2 ? "中" : "中";
            return (
              <tr key={row.theme_id} className="border-t">
                <td className="px-4 py-4 text-base font-semibold">{arr<string>(row.a_share_themes).join("、") || row.us_theme || "-"}</td>
                <td className="px-4 py-4">
                  <span className={cn("rounded-md border px-2 py-1 text-xs font-semibold", label === "强" ? "border-red-500/30 bg-red-500/10 text-red-500" : label === "弱" ? "border-muted bg-muted text-muted-foreground" : "border-amber-500/30 bg-amber-500/10 text-amber-600 dark:text-amber-300")}>{label}</span>
                </td>
                <td className="px-4 py-4 text-muted-foreground">{row.reason || "-"}</td>
                <td className="px-4 py-4 font-medium text-amber-600 dark:text-amber-300">{focus.length ? focus.join("、") : "-"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function MorningNewsGroup({ title, rows }: { title: string; rows: Row[] }) {
  return (
    <div className="rounded-md border bg-background shadow-sm">
      <div className="border-b px-4 py-3">
        <span className="rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-xs font-semibold text-amber-600 dark:text-amber-300">{title}</span>
      </div>
      <div className="space-y-2 p-4">
        {rows.length ? rows.map((row, index) => (
          <div key={`${row.title}-${index}`} className="border-l-2 border-amber-500/35 py-0.5 pl-3 text-xs leading-5">
            <div className="line-clamp-2 font-semibold">{row.title || "-"}</div>
            {(row.summary || row.source) && <div className="mt-1 line-clamp-2 text-muted-foreground">{row.summary || row.source}</div>}
          </div>
        )) : <Empty>暂无要闻</Empty>}
      </div>
    </div>
  );
}

function LegacyIntradayView({ data }: { data: Row }) {
  const breadth = (data.breadth ?? {}) as Row;
  const hotSectors = arr(data.hot_sectors);
  const sectorCapital = arr(data.sector_capital_top);
  const inflow = arr(data.stock_inflow_top);
  const outflow = arr(data.stock_outflow_top);
  const alerts = arr(data.alerts);
  return (
    <>
      <Section title="A 股盘中监控" desc="跟踪涨跌停、资金和板块快照。" icon={<Gauge className="h-4 w-4 text-cyan-500" />}>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Metric label="涨停" value={breadth.limit_up ?? "-"} tone="text-red-500" />
          <Metric label="跌停" value={breadth.limit_down ?? "-"} tone="text-green-500" />
          <Metric label="连板高度" value={breadth.max_limit_up_height ?? "-"} />
          <Metric label="数据策略" value="已准备" />
        </div>
      </Section>
      <div className="grid gap-4 xl:grid-cols-2">
        <Section title="行业板块涨幅榜" icon={<BarChart3 className="h-4 w-4 text-primary" />}>
          {hotSectors.length ? <BarList rows={hotSectors} /> : <Empty>板块热度未同步</Empty>}
        </Section>
        <Section title="行业净流入 TOP5" icon={<TrendingUp className="h-4 w-4 text-red-500" />}>
          {sectorCapital.length ? <FlowRows rows={sectorCapital} nameKey="sector" /> : <Empty>行业资金未同步</Empty>}
        </Section>
      </div>
      <div className="grid gap-4 xl:grid-cols-3">
        <Section title="个股净流入 TOP" icon={<TrendingUp className="h-4 w-4 text-red-500" />}>
          {inflow.length ? <FlowRows rows={inflow} nameKey="name" /> : <Empty>个股净流入未同步</Empty>}
        </Section>
        <Section title="个股净流出 TOP" icon={<TrendingDown className="h-4 w-4 text-green-500" />}>
          {outflow.length ? <FlowRows rows={outflow} nameKey="name" /> : <Empty>个股净流出未同步</Empty>}
        </Section>
        <Section title="异动提醒" icon={<Zap className="h-4 w-4 text-amber-500" />}>
          {alerts.length ? <TextRows rows={alerts} /> : <Empty>异动快照未同步</Empty>}
        </Section>
      </div>
    </>
  );
}

function LegacyTailStrategyView({ data }: { data: Row }) {
  const pools = (data.pools ?? {}) as Row;
  const decisions = arr(data.decisions);
  const rules = arr<string>(data.rules);
  return (
    <>
      <Section title="尾盘观察清单" desc="基于涨跌停和连板结构。" icon={<ShieldAlert className="h-4 w-4 text-amber-500" />}>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Metric label="涨停" value={pools.limit_up_count ?? "-"} tone="text-red-500" />
          <Metric label="跌停" value={pools.limit_down_count ?? "-"} tone="text-green-500" />
          <Metric label="连板高度" value={pools.max_limit_up_height ?? "-"} />
          <Metric label="数据日" value={pools.trade_date ?? data.trade_date ?? "-"} />
        </div>
      </Section>
      <Section title="连板梯队" icon={<BarChart3 className="h-4 w-4 text-red-500" />}>
        {arr(pools.limitup_ladder).length ? <Ladder rows={arr(pools.limitup_ladder)} /> : <Empty>连板梯队未同步</Empty>}
      </Section>
      <Section title="明日动作卡" icon={<Target className="h-4 w-4 text-primary" />}>
        {decisions.length ? <DecisionGrid rows={decisions} /> : <Empty>策略候选表未同步，暂不生成尾盘动作卡</Empty>}
      </Section>
      <Section title="本地规则" icon={<ClipboardList className="h-4 w-4 text-cyan-500" />}>
        {rules.length ? <ul className="grid gap-2 md:grid-cols-2">{rules.map((rule) => <li key={rule} className="rounded-md border bg-background px-3 py-2 text-xs">{rule}</li>)}</ul> : <Empty>规则未同步</Empty>}
      </Section>
    </>
  );
}

function LegacyCloseReviewView({ data }: { data: Row }) {
  const pools = (data.pools ?? {}) as Row;
  const themes = (data.themes ?? {}) as Row;
  const sectorCapital = arr(data.sector_capital_top);
  const questions = arr<string>(data.review_questions);
  return (
    <>
      <Section title="收盘复盘问答" desc="只基于已确认的收盘数据回答。" icon={<ClipboardList className="h-4 w-4 text-primary" />}>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <Metric label="涨停" value={pools.limit_up_count ?? "-"} tone="text-red-500" />
          <Metric label="跌停" value={pools.limit_down_count ?? "-"} tone="text-green-500" />
          <Metric label="连板高度" value={pools.max_limit_up_height ?? "-"} />
          <Metric label="主线数量" value={arr(themes.main_lines).length || "-"} />
        </div>
      </Section>
      <div className="grid gap-4 xl:grid-cols-2">
        <Section title="主线 & 观察板块" icon={<Target className="h-4 w-4 text-red-500" />}>
          {arr(themes.main_lines).length ? <ThemePills rows={arr(themes.main_lines)} /> : <Empty>主线题材未同步</Empty>}
        </Section>
        <Section title="行业净流入 TOP5" icon={<TrendingUp className="h-4 w-4 text-red-500" />}>
          {sectorCapital.length ? <FlowRows rows={sectorCapital} nameKey="sector" /> : <Empty>行业资金未同步</Empty>}
        </Section>
      </div>
      <Section title="复盘问题库" icon={<BarChart3 className="h-4 w-4 text-cyan-500" />}>
        {questions.length ? <ul className="grid gap-2 md:grid-cols-3">{questions.map((q) => <li key={q} className="rounded-md border bg-background px-3 py-3 text-xs">{q}</li>)}</ul> : <Empty>复盘问题未同步</Empty>}
      </Section>
    </>
  );
}

void LegacyIntradayView;
void LegacyTailStrategyView;
void LegacyCloseReviewView;

function LegacyIntradayView3({ data }: { data: Row }) {
  const breadth = (data.breadth ?? {}) as Row;
  const emotion = (data.emotion ?? {}) as Row;
  const hotThemes = arr(data.hot_themes);
  const ladder = arr(data.limitup_ladder);
  const sectorCapital = arr(data.sector_capital_top);
  const inflow = arr(data.stock_inflow_top);
  const outflow = arr(data.stock_outflow_top);
  const alerts = arr(data.alerts);
  return (
    <>
      <Section title="情绪运行阶段" desc="基于涨跌停与连板结构。" icon={<Gauge className="h-4 w-4 text-amber-500" />}>
        <div className="grid gap-3 md:grid-cols-[220px_1fr]">
          <div className="rounded-md border bg-background px-4 py-4 text-center">
            <div className={cn("text-4xl font-semibold tabular-nums", n(emotion.score) >= 70 ? "text-red-500" : n(emotion.score) <= 35 ? "text-green-500" : "text-amber-500")}>
              {n(emotion.score, NaN).toFixed(1)}
            </div>
            <div className="mt-1 text-xs text-muted-foreground">温度 · {emotion.temperature || "-"}</div>
            <div className="mt-2 text-sm font-semibold">{emotion.phase || "-"}</div>
          </div>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <Metric label="涨停" value={breadth.limit_up ?? "-"} tone="text-red-500" />
            <Metric label="跌停" value={breadth.limit_down ?? "-"} tone="text-green-500" />
            <Metric label="连板高度" value={breadth.max_limit_up_height ?? "-"} />
            <Metric label="信号" value={emotion.phase || "-"} />
          </div>
        </div>
        <div className="mt-3 rounded-md border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">{emotion.note || "情绪快照未同步。"}</div>
      </Section>
      <div className="grid gap-4 xl:grid-cols-2">
        <Section title="行业 / 题材热度" icon={<BarChart3 className="h-4 w-4 text-primary" />}>
          {hotThemes.length ? <BarList rows={hotThemes} /> : <Empty>题材热度未同步</Empty>}
        </Section>
        <Section title="资金流向 TOP" icon={<TrendingUp className="h-4 w-4 text-red-500" />}>
          <div className="grid gap-3 md:grid-cols-3">
            <div>{sectorCapital.length ? <FlowRows rows={sectorCapital} nameKey="sector" /> : <Empty>行业资金未同步</Empty>}</div>
            <div>{inflow.length ? <FlowRows rows={inflow} nameKey="name" /> : <Empty>个股净流入未同步</Empty>}</div>
            <div>{outflow.length ? <FlowRows rows={outflow} nameKey="name" /> : <Empty>个股净流出未同步</Empty>}</div>
          </div>
        </Section>
      </div>
      <div className="grid gap-4 xl:grid-cols-2">
        <Section title="涨跌停情况 / 连板梯队" icon={<Target className="h-4 w-4 text-red-500" />}>
          {ladder.length ? <Ladder rows={ladder} /> : <Empty>连板梯队未同步</Empty>}
        </Section>
        <Section title="盘中异动提醒" icon={<Zap className="h-4 w-4 text-amber-500" />}>
          {alerts.length ? <TextRows rows={alerts} /> : <Empty>异动提醒表未同步</Empty>}
        </Section>
      </div>
    </>
  );
}

void LegacyIntradayView3;

function IntradayView({ data }: { data: Row }) {
  const breadth = (data.breadth ?? {}) as Row;
  const emotion = (data.emotion ?? {}) as Row;
  const hotThemes = arr(data.hot_themes);
  const hotSectors = arr(data.hot_sectors);
  const ladder = arr(data.limitup_ladder);
  const sectorCapital = arr(data.sector_capital_top);
  const chartRows = hotSectors.length ? hotSectors : hotThemes.slice(0, 12);
  const rotationRows = hotThemes.length ? hotThemes : [...hotSectors, ...sectorCapital];
  return (
    <div className="space-y-4">
      <IntradayHero breadth={breadth} emotion={emotion} />

      <Section title="行业板块涨幅榜" desc="实时 · 主力资金流向" icon={<BarChart3 className="h-4 w-4 text-red-500" />}>
        {chartRows.length ? <SectorGainChart rows={chartRows} /> : <Empty>行业板块快照未同步</Empty>}
      </Section>

      <Section title="连板梯队 · 异动" desc="涨停封板高度" icon={<Target className="h-4 w-4 text-red-500" />}>
        {ladder.length ? <LimitupMomentumList rows={ladder} /> : <Empty>连板梯队未同步</Empty>}
      </Section>

      <Section title="板块轮动 · 实时热度" desc="行业 + 概念 · 资金流向哪" icon={<Zap className="h-4 w-4 text-amber-500" />}>
        {rotationRows.length ? <RotationHeatCards rows={rotationRows} capitalRows={sectorCapital} /> : <Empty>板块轮动快照未同步</Empty>}
      </Section>
    </div>
  );
}

function IntradayHero({ breadth, emotion }: { breadth: Row; emotion: Row }) {
  const score = n(emotion.score, NaN);
  const metrics = [
    { label: "情绪温度", value: Number.isFinite(score) ? score.toFixed(1) : "-", tone: score >= 70 ? "text-red-500" : score <= 35 ? "text-green-500" : "text-amber-500" },
    { label: "涨停", value: breadth.limit_up ?? "-", tone: "text-red-500" },
    { label: "最高连板", value: breadth.max_limit_up_height ?? "-", tone: "text-red-500" },
    { label: "成交额(亿)", value: breadth.turnover_billion ?? "-", tone: "text-foreground" },
  ];
  return (
    <section className="rounded-md border bg-card/95 px-5 py-5 shadow-sm">
      <div className="flex flex-col gap-4 xl:flex-row xl:items-center xl:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Gauge className="h-5 w-5 text-muted-foreground" />
            <h2 className="text-lg font-semibold">A股盘中监控</h2>
            <span className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-xs font-medium text-emerald-600 dark:text-emerald-300">实时 · 东财(约延迟)</span>
          </div>
        </div>
        <div className="grid min-w-[320px] grid-cols-4 gap-4">
          {metrics.map((item) => (
            <div key={item.label} className="text-center">
              <div className={cn("text-2xl font-bold tabular-nums", item.tone)}>{item.value}</div>
              <div className="mt-1 text-[11px] text-muted-foreground">{item.label}</div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function SectorGainChart({ rows }: { rows: Row[] }) {
  const option = useMemo((): EChartsOption => {
    const t = getChartTheme();
    const data = rows
      .slice(0, 10)
      .map((row) => ({ name: String(row.name || row.sector || "-"), value: n(row.change_pct) }))
      .reverse();
    const max = Math.max(5, ...data.map((item) => Math.abs(item.value)));
    return {
      animation: false,
      grid: { left: 104, right: 52, top: 8, bottom: 28 },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        backgroundColor: t.tooltipBg,
        borderColor: t.tooltipBorder,
        textStyle: { color: t.tooltipText },
        valueFormatter: (value) => pct(value),
      },
      xAxis: {
        type: "value",
        min: 0,
        max,
        axisLabel: { color: t.textColor, formatter: (value: number) => `${value}%` },
        splitLine: { lineStyle: { color: t.gridColor } },
      },
      yAxis: {
        type: "category",
        data: data.map((item) => item.name),
        axisLabel: { color: t.textColor, fontSize: 12 },
        axisTick: { show: false },
        axisLine: { show: false },
      },
      series: [{
        type: "bar",
        data: data.map((item) => Math.max(0, item.value)),
        barWidth: 22,
        itemStyle: { color: t.upColor, borderRadius: [3, 3, 3, 3] },
        label: {
          show: true,
          position: "right",
          color: t.textColor,
          formatter: ({ value }) => pct(value),
        },
      }],
    };
  }, [rows]);
  return <Chart option={option} height={380} />;
}

function LimitupMomentumList({ rows }: { rows: Row[] }) {
  const stocks: Row[] = rows.flatMap((row) => arr<Row>(row.stocks).map((stock) => ({ ...stock, days: stock.days ?? row.days })));
  const sorted = stocks.sort((a, b) => n(b.days) - n(a.days)).slice(0, 12);
  return (
    <div className="max-h-[420px] space-y-3 overflow-y-auto pr-1">
      {sorted.map((stock, index) => (
        <div key={`${stock.symbol}-${index}`} className="grid grid-cols-[54px_1fr_86px] items-center gap-3 rounded-md border bg-background px-4 py-3">
          <span className="rounded-md border border-red-500/30 bg-red-500/10 px-2 py-1 text-center text-xs font-bold text-red-500">{stock.days || 1}板</span>
          <div className="min-w-0">
            <div className="truncate text-base font-semibold">{stock.name || stock.symbol || "-"}</div>
            <div className="mt-1 text-xs text-muted-foreground">{stock.symbol || "-"} · 封板</div>
          </div>
          <div className="text-right text-sm font-bold text-red-500">+10.0%</div>
        </div>
      ))}
    </div>
  );
}

function RotationHeatCards({ rows, capitalRows }: { rows: Row[]; capitalRows: Row[] }) {
  const capitalMap = new Map(capitalRows.map((row) => [String(row.sector || row.name), row]));
  return (
    <div className="space-y-4">
      <div>
        <div className="mb-3 text-xs font-medium text-muted-foreground">行业板块</div>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {rows.slice(0, 8).map((row) => {
            const name = String(row.name || row.sector || "-");
            const flow = capitalMap.get(name);
            const value = Math.max(0, Math.min(100, Math.abs(n(row.change_pct)) * 24));
            return (
              <div key={`${name}-${row.board_type || ""}`} className="rounded-md border bg-background px-4 py-4 shadow-sm">
                <div className="text-base font-semibold">{name}</div>
                <div className={cn("mt-2 text-2xl font-bold tabular-nums", pctTone(row.change_pct))}>{pct(row.change_pct)}</div>
                <div className="mt-4 h-2 overflow-hidden rounded bg-muted">
                  <div className={cn("h-full", n(row.change_pct) >= 0 ? "bg-red-500" : "bg-green-500")} style={{ width: `${Math.max(4, value)}%` }} />
                </div>
                {flow ? <div className="mt-2 text-[11px] text-muted-foreground">主力净流：{money(flow.main_net)}</div> : null}
              </div>
            );
          })}
        </div>
      </div>
      {capitalRows.length ? (
        <div>
          <div className="mb-3 text-xs font-medium text-muted-foreground">资金流向</div>
          <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-5">
            {capitalRows.slice(0, 5).map((row) => (
              <div key={row.sector || row.name} className="flex items-center justify-between gap-3 rounded-md border bg-background px-3 py-2 text-xs">
                <span className="truncate font-medium">{row.sector || row.name}</span>
                <span className={cn("tabular-nums", n(row.main_net) >= 0 ? "text-red-500" : "text-green-500")}>{money(row.main_net)}</span>
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function LegacyTailStrategyView2({ data }: { data: Row }) {
  const pools = (data.pools ?? {}) as Row;
  const emotion = (data.emotion ?? {}) as Row;
  const watchlist = arr(data.watchlist);
  const nextDayPlan = arr(data.next_day_plan);
  const riskBlocks = arr(data.risk_blocks);
  const rules = arr<string>(data.rules);
  return (
    <>
      <Section title="尾盘情绪 & 市场状态" desc="基于收盘前市场快照。" icon={<ShieldAlert className="h-4 w-4 text-amber-500" />}>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <Metric label="情绪温度" value={n(emotion.score, NaN).toFixed(1)} tone={n(emotion.score) >= 70 ? "text-red-500" : "text-amber-500"} />
          <Metric label="涨停" value={pools.limit_up_count ?? "-"} tone="text-red-500" />
          <Metric label="跌停" value={pools.limit_down_count ?? "-"} tone="text-green-500" />
          <Metric label="连板高度" value={pools.max_limit_up_height ?? "-"} />
          <Metric label="数据日" value={pools.trade_date ?? data.trade_date ?? "-"} />
        </div>
        <div className="mt-3 rounded-md border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">{emotion.note || "尾盘情绪快照未同步。"}</div>
      </Section>
      <div className="grid gap-4 xl:grid-cols-2">
        <Section title="尾盘筛选清单" desc="基于涨停池观察；不生成买卖指令。" icon={<Target className="h-4 w-4 text-primary" />}>
          {watchlist.length ? <WatchRows rows={watchlist} /> : <Empty>尾盘观察池未同步</Empty>}
        </Section>
        <Section title="次日计划" desc="根据涨跌停结构归档。" icon={<ClipboardList className="h-4 w-4 text-cyan-500" />}>
          {nextDayPlan.length ? <PlanCards rows={nextDayPlan} /> : <Empty>次日计划表未同步</Empty>}
        </Section>
      </div>
      <div className="grid gap-4 xl:grid-cols-2">
        <Section title="风险规避 / 可用数据" icon={<ShieldAlert className="h-4 w-4 text-green-500" />}>
          {riskBlocks.length ? <RiskBlocks rows={riskBlocks} /> : <Empty>风险块未同步</Empty>}
        </Section>
        <Section title="本地规则" icon={<Database className="h-4 w-4 text-primary" />}>
          {rules.length ? <ul className="grid gap-2">{rules.map((rule) => <li key={rule} className="rounded-md border bg-background px-3 py-2 text-xs">{rule}</li>)}</ul> : <Empty>规则未同步</Empty>}
        </Section>
      </div>
    </>
  );
}

void LegacyTailStrategyView2;

function TailStrategyView({ data }: { data: Row }) {
  const pools = (data.pools ?? {}) as Row;
  const emotion = (data.emotion ?? {}) as Row;
  const candidates = arr(data.tail_candidates);
  const watchGroups = arr(data.watch_groups);
  const fallbackWatch = arr(data.watchlist);
  const score = n(emotion.score, NaN);
  const canAct = score >= 60 && n(pools.limit_up_count) >= Math.max(20, n(pools.limit_down_count));
  const envLabel = canAct ? "可做(进攻偏谨慎)" : "先观察(防守优先)";
  const position = canAct ? "5-6成" : "3-4成";
  return (
    <div className="space-y-4">
      <TailStrategyHero emotion={emotion} pools={pools} envLabel={envLabel} position={position} />

      <Section
        title="尾盘买入机会 · 6选"
        desc=""
        icon={<Target className="h-4 w-4 text-amber-500" />}
        right={<span className="rounded-md border border-red-500/30 bg-red-500/10 px-2 py-1 text-xs font-semibold text-red-500">仓位建议</span>}
      >
        {candidates.length ? <TailCandidateGrid rows={candidates} /> : <Empty>尾盘候选快照未同步</Empty>}
      </Section>

      <Section title="尾盘观察清单" desc="分类：资金回流 / 次日预期 / 强延续 / 趋势跟踪 / 高盈亏比" icon={<ClipboardList className="h-4 w-4 text-primary" />}>
        {watchGroups.length ? <TailWatchGroups rows={watchGroups} /> : fallbackWatch.length ? <TailFallbackWatch rows={fallbackWatch} /> : <Empty>尾盘观察清单未同步</Empty>}
      </Section>
    </div>
  );
}

function TailStrategyHero({ emotion, pools, envLabel, position }: { emotion: Row; pools: Row; envLabel: string; position: string }) {
  const note = String(emotion.note || "尾盘快照未同步，暂不生成方向性判断。");
  return (
    <section className="rounded-md border border-amber-500/30 bg-amber-500/5 px-5 py-5 shadow-sm">
      <div className="grid gap-4 xl:grid-cols-[1.35fr_0.55fr_0.5fr_1.35fr] xl:items-center">
        <div className="flex flex-wrap items-center gap-3">
          <ShieldAlert className="h-6 w-6 text-amber-500" />
          <div className="text-xl font-bold text-amber-600 dark:text-amber-300">先判环境，再决定要不要做</div>
          <span className="rounded-md border bg-muted px-2 py-1 text-xs text-muted-foreground">尾盘候选</span>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">市场环境</div>
          <div className="mt-1 text-2xl font-bold text-amber-600 dark:text-amber-300">{envLabel}</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">建议总仓位</div>
          <div className="mt-1 text-2xl font-bold">{position}</div>
        </div>
        <div className="text-sm leading-6 text-muted-foreground">
          情绪温度 {n(emotion.score, NaN).toFixed(1)}，涨停 {pools.limit_up_count ?? "-"}，跌停 {pools.limit_down_count ?? "-"}，最高连板 {pools.max_limit_up_height ?? "-"}。{note}
        </div>
      </div>
    </section>
  );
}

function TailCandidateGrid({ rows }: { rows: Row[] }) {
  return (
    <div className="grid gap-4 xl:grid-cols-2">
      {rows.slice(0, 6).map((row, index) => (
        <div key={`${row.symbol}-${index}`} className="rounded-md border bg-background px-5 py-4 shadow-sm">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className="rounded-md bg-amber-500/15 px-2 py-1 text-sm font-bold text-amber-600 dark:text-amber-300">{row.rank || index + 1}</span>
                <span className="truncate text-xl font-bold">{row.name || row.symbol || "-"}</span>
                <span className="text-sm text-muted-foreground">{row.symbol || "-"}</span>
              </div>
              <div className="mt-3 font-semibold text-amber-600 dark:text-amber-300">{row.theme || "-"} / {row.line_type || "-"}</div>
            </div>
            <span className="rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-xs font-semibold text-amber-600 dark:text-amber-300">{row.stage || "观察"}</span>
          </div>
          <p className="mt-3 line-clamp-2 text-sm leading-6 text-muted-foreground">{row.reason || "-"}</p>
          <div className="mt-4 grid gap-3 md:grid-cols-2">
            <TailInfoBox label="建议仓位" value={String(row.position_note || "观察仓").replace(/[；;]不生成买入指令/g, "")} tone="text-red-500" />
            <TailInfoBox label="止损参考" value={row.stop_note || "-"} tone="text-green-500" />
            <TailInfoBox label="风险点" value={row.risk || "-"} />
            <TailInfoBox label="盈亏比" value={row.rr ? `盈亏比 ${row.rr}` : "-"} tone="text-red-500" />
          </div>
        </div>
      ))}
    </div>
  );
}

function TailInfoBox({ label, value, tone }: { label: string; value: ReactNode; tone?: string }) {
  return (
    <div className="rounded-md bg-muted/35 px-3 py-3 text-xs">
      <div className="text-muted-foreground">{label}</div>
      <div className={cn("mt-1 font-semibold leading-5", tone)}>{value}</div>
    </div>
  );
}

function TailWatchGroups({ rows }: { rows: Row[] }) {
  return (
    <div className="grid gap-4 xl:grid-cols-2">
      {rows.map((group) => (
        <div key={group.title} className="rounded-md border bg-background px-5 py-4 shadow-sm">
          <div className="text-lg font-bold text-amber-600 dark:text-amber-300">{group.title}</div>
          <div className="mt-4 space-y-3">
            {arr<Row>(group.items).length ? arr<Row>(group.items).map((item, index) => (
              <div key={`${item.name}-${index}`} className="border-l-2 border-amber-500/40 pl-3">
                <div className="font-semibold">{item.name || "-"}</div>
                <div className="mt-1 text-xs leading-5 text-muted-foreground">{item.note || "-"}</div>
              </div>
            )) : <Empty>暂无观察项</Empty>}
          </div>
        </div>
      ))}
    </div>
  );
}

function TailFallbackWatch({ rows }: { rows: Row[] }) {
  const groups = [
    { title: "尾盘资金回流", items: rows.slice(0, 2) },
    { title: "次日有预期", items: rows.slice(2, 4) },
    { title: "强势板块 · 可能延续", items: rows.slice(4, 6) },
    { title: "趋势票 · 继续跟踪", items: rows.slice(6, 8) },
    { title: "高盈亏比", items: rows.slice(0, 2) },
  ];
  return <TailWatchGroups rows={groups.map((group) => ({ title: group.title, items: group.items.map((item) => ({ name: item.name || item.symbol, note: item.basis || item.label })) }))} />;
}

function CloseReviewView({ data }: { data: Row }) {
  const pools = (data.pools ?? {}) as Row;
  const summary = (data.summary ?? {}) as Row;
  const reviewCards = arr(data.close_review_cards);
  const stylePanels = arr(data.close_style_panels);
  const newThemes = arr(data.new_theme_tracking);
  const holdingStrength = arr(data.holding_sector_strength);
  const topicCards = arr(data.topic_cards);
  const sectorCapital = arr(data.sector_capital_top);
  return (
    <div className="space-y-4">
      <CloseReviewHero summary={summary} pools={pools} />

      <Section title="收盘八问" desc="今天的钱、亏钱效应、情绪、次日方向、主线、高标、龙头战法、市场风格" icon={<ClipboardList className="h-4 w-4 text-amber-500" />}>
        {reviewCards.length ? <CloseQuestionGrid rows={reviewCards} /> : <Empty>收盘复盘问题快照未同步</Empty>}
      </Section>

      <div className="grid gap-4 xl:grid-cols-3">
        <CloseStylePanel panel={stylePanels[0]} fallbackTopics={topicCards} />
        <CloseActivePanel panel={stylePanels[1]} fallbackFlows={sectorCapital} />
        <CloseRegulationPanel panel={stylePanels[2]} />
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Section title="新题材 / 新板块溢价跟踪" desc="判断能否继续扩散" icon={<Target className="h-4 w-4 text-red-500" />}>
          {newThemes.length ? <CloseThemeTracking rows={newThemes} /> : <Empty>新题材溢价快照未同步</Empty>}
        </Section>
        <Section title="我的持仓 · 板块强弱" icon={<BarChart3 className="h-4 w-4 text-amber-500" />}>
          {holdingStrength.length ? <HoldingStrengthRows rows={holdingStrength} /> : <Empty>持仓板块强弱未同步</Empty>}
        </Section>
      </div>
    </div>
  );
}

function CloseReviewHero({ summary, pools }: { summary: Row; pools: Row }) {
  return (
    <section className="rounded-md border bg-card/95 px-5 py-5 shadow-sm">
      <div className="flex flex-wrap items-center gap-3">
        <ClipboardList className="h-6 w-6 text-amber-500" />
        <div className="text-xl font-bold">收盘复盘</div>
        <span className="rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-xs font-semibold text-amber-600 dark:text-amber-300">发酵 → 分歧</span>
        <span className="rounded-md border bg-muted px-2 py-1 text-xs text-muted-foreground">收盘快照</span>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-5">
        <Metric label="情绪温度" value={summary.emotion_score ?? "-"} tone="text-amber-500" />
        <Metric label="情绪阶段" value={summary.emotion_phase ?? "-"} />
        <Metric label="涨停" value={pools.limit_up_count ?? summary.limit_up ?? "-"} tone="text-red-500" />
        <Metric label="跌停" value={pools.limit_down_count ?? summary.limit_down ?? "-"} tone="text-green-500" />
        <Metric label="连板高度" value={pools.max_limit_up_height ?? summary.max_limit_up_height ?? "-"} />
      </div>
    </section>
  );
}

function CloseQuestionGrid({ rows }: { rows: Row[] }) {
  return (
    <div className="grid gap-4 xl:grid-cols-2">
      {rows.slice(0, 8).map((row, index) => (
        <div key={`${row.title}-${index}`} className={cn("rounded-md border bg-background px-5 py-4 shadow-sm", closeToneBorder(row.tone))}>
          <div className="text-lg font-bold">{row.title}</div>
          <p className="mt-3 line-clamp-3 text-sm leading-7 text-muted-foreground">{row.summary || "-"}</p>
        </div>
      ))}
    </div>
  );
}

function CloseStylePanel({ panel, fallbackTopics }: { panel?: Row; fallbackTopics: Row[] }) {
  const metrics = arr(panel?.metrics).length ? arr(panel?.metrics) : [
    { name: "连板", value: 35 },
    { name: "打板", value: 35 },
    { name: "低吸", value: 45 },
    { name: "单波", value: 40 },
    { name: "趋势", value: Math.min(90, 30 + fallbackTopics.length * 8) },
  ];
  const option = useMemo((): EChartsOption => {
    const t = getChartTheme();
    return {
      backgroundColor: "transparent",
      radar: {
        radius: "62%",
        indicator: metrics.map((item) => ({ name: String(item.name), max: 100 })),
        splitLine: { lineStyle: { color: t.gridColor } },
        splitArea: { areaStyle: { color: ["transparent", "rgba(234,179,8,0.04)"] } },
        axisName: { color: t.textColor, fontSize: 12 },
      },
      series: [{
        type: "radar",
        data: [{ value: metrics.map((item) => n(item.value)), name: "风格" }],
        areaStyle: { color: "rgba(245, 158, 11, 0.18)" },
        lineStyle: { color: "#f59e0b", width: 2 },
        itemStyle: { color: "#f59e0b" },
        symbolSize: 5,
      }],
    };
  }, [metrics]);
  return (
    <Section title={String(panel?.title || "市场风格偏好")} desc={String(panel?.subtitle || "连板 / 趋势 / 反包 / 单波 / 低吸")} icon={<BarChart3 className="h-4 w-4 text-amber-500" />}>
      <Chart option={option} height={260} />
    </Section>
  );
}

function CloseActivePanel({ panel, fallbackFlows }: { panel?: Row; fallbackFlows: Row[] }) {
  const rows = arr(panel?.items).length ? arr(panel?.items) : fallbackFlows.slice(0, 3).map((row) => ({ name: row.sector, amount: row.main_net, tag: row.leader }));
  return (
    <Section title={String(panel?.title || "主力 / 游资活跃")} desc={String(panel?.subtitle || "龙虎榜席位")} icon={<TrendingUp className="h-4 w-4 text-amber-500" />}>
      <div className="space-y-3">
        {rows.length ? rows.map((row, index) => (
          <div key={`${row.name}-${index}`} className="rounded-md border bg-background px-4 py-3 shadow-sm">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="font-bold">{row.name || "-"}</div>
                <div className="mt-1 text-xs text-muted-foreground">{row.tag || "-"}</div>
              </div>
              <div className="text-right font-bold text-red-500">买 {n(row.amount).toFixed(1)}亿</div>
            </div>
          </div>
        )) : <Empty>主力活跃快照未同步</Empty>}
      </div>
    </Section>
  );
}

function CloseRegulationPanel({ panel }: { panel?: Row }) {
  return (
    <Section title={String(panel?.title || "监管票专项")} desc={String(panel?.subtitle || "影响短线 / 高标 / 接力")} icon={<ShieldAlert className="h-4 w-4 text-green-500" />}>
      {panel ? (
        <div className="rounded-md border border-green-500/25 bg-green-500/5 px-4 py-4 shadow-sm">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-lg font-bold">{panel.name || "-"}</span>
            <span className="rounded-md bg-green-500/10 px-2 py-1 text-xs font-semibold text-green-600 dark:text-green-300">{panel.tag || "关注"}</span>
          </div>
          <p className="mt-3 text-sm leading-7 text-muted-foreground">{panel.summary || "-"}</p>
        </div>
      ) : <Empty>监管专项快照未同步</Empty>}
    </Section>
  );
}

function CloseThemeTracking({ rows }: { rows: Row[] }) {
  return (
    <div className="space-y-3">
      {rows.slice(0, 6).map((row, index) => (
        <div key={`${row.name}-${index}`} className="rounded-md border bg-background px-4 py-3 shadow-sm">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="font-bold">{row.name || "-"}</div>
            <span className="rounded-md border border-red-500/25 bg-red-500/10 px-2 py-1 text-xs font-semibold text-red-500">{row.judgement || "待确认"}</span>
          </div>
          <div className="mt-2 text-sm leading-6 text-muted-foreground">{row.summary || "-"}</div>
        </div>
      ))}
    </div>
  );
}

function HoldingStrengthRows({ rows }: { rows: Row[] }) {
  return (
    <div className="space-y-3">
      {rows.map((row, index) => (
        <div key={`${row.symbol || row.name}-${index}`} className="rounded-md border bg-background px-4 py-3 shadow-sm">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="truncate font-bold">{row.name || row.symbol || "-"}</div>
              <div className="mt-1 text-xs text-muted-foreground">{row.symbol || "-"} · {row.sector || "板块待匹配"}</div>
            </div>
            <div className="text-right">
              <div className={cn("font-semibold", pctTone(row.change_pct))}>{pct(row.change_pct)}</div>
              <div className={cn("mt-1 text-xs", pctTone(row.pnl_pct))}>{row.pnl_pct == null ? "盈亏 -" : `盈亏 ${pct(row.pnl_pct)}`}</div>
            </div>
          </div>
          <div className="mt-2 text-xs leading-5 text-muted-foreground">{row.note || "-"}</div>
        </div>
      ))}
    </div>
  );
}

function closeToneBorder(tone: unknown): string {
  if (tone === "green") return "border-l-2 border-l-green-500";
  if (tone === "amber") return "border-l-2 border-l-amber-500";
  return "border-l-2 border-l-red-500";
}

function WatchRows({ rows }: { rows: Row[] }) {
  return (
    <div className="space-y-2">
      {rows.slice(0, 10).map((row) => (
        <div key={`${row.symbol}-${row.label}`} className="rounded-md border bg-background px-3 py-2 text-xs">
          <div className="flex items-center justify-between gap-2">
            <span className="font-semibold">{row.name || row.symbol}</span>
            <span className="rounded bg-muted px-2 py-0.5 text-muted-foreground">{row.label}</span>
          </div>
          <div className="mt-1 text-muted-foreground">{row.basis}</div>
        </div>
      ))}
    </div>
  );
}

function PlanCards({ rows }: { rows: Row[] }) {
  return (
    <div className="grid gap-3 md:grid-cols-2">
      {rows.map((row) => (
        <div key={row.title} className="rounded-md border bg-background px-3 py-3 text-xs">
          <div className="font-semibold">{row.title}</div>
          <div className="mt-2 text-muted-foreground">{arr<string>(row.items).join("、") || "暂无"}</div>
          <div className="mt-2 leading-5">{row.basis}</div>
        </div>
      ))}
    </div>
  );
}

function RiskBlocks({ rows }: { rows: Row[] }) {
  return (
    <div className="grid gap-3 md:grid-cols-3">
      {rows.map((row) => (
        <div key={row.title} className="rounded-md border bg-background px-3 py-3 text-xs">
          <div className="text-muted-foreground">{row.title}</div>
          <div className="mt-1 text-xl font-semibold tabular-nums">{row.value}</div>
          <div className="mt-1 text-muted-foreground">{row.basis}</div>
        </div>
      ))}
    </div>
  );
}

function TopicCards({ rows }: { rows: Row[] }) {
  return (
    <div className="grid gap-3 md:grid-cols-2">
      {rows.slice(0, 8).map((row) => (
        <div key={row.name} className="rounded-md border bg-background px-3 py-3 text-xs">
          <div className="flex items-center justify-between gap-2">
            <span className="font-semibold">{row.name || "-"}</span>
            <span className={cn("tabular-nums", pctTone(row.change_pct))}>{pct(row.change_pct)}</span>
          </div>
          <div className="mt-1 text-muted-foreground">领涨：{row.leader || "-"}</div>
          <div className="mt-2 leading-5">{row.basis || "基于快照数据。"}</div>
        </div>
      ))}
    </div>
  );
}
void TopicCards;

function SimpleRows({ rows, primary, secondary }: { rows: Row[]; primary: string; secondary: string }) {
  return (
    <div className="space-y-2">
      {rows.slice(0, 8).map((row, index) => (
        <div key={`${row[primary]}-${index}`} className="flex items-center justify-between gap-3 rounded-md border bg-background px-3 py-2 text-xs">
          <span className="font-medium">{row[primary] ?? "-"}</span>
          <span className={pctTone(row[secondary])}>{pct(row[secondary])}</span>
        </div>
      ))}
    </div>
  );
}

function TextRows({ rows }: { rows: Row[] }) {
  return (
    <div className="space-y-2">
      {rows.slice(0, 8).map((row, index) => (
        <div key={`${row.title || row.name || index}`} className="rounded-md border bg-background px-3 py-2 text-xs leading-5">
          <div className="font-medium">{row.title || row.name || row.symbol || "-"}</div>
          {(row.summary || row.reason || row.message) && <div className="mt-1 text-muted-foreground">{row.summary || row.reason || row.message}</div>}
        </div>
      ))}
    </div>
  );
}

function ThemeTable({ rows }: { rows: Row[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[720px] text-sm">
        <thead className="text-xs text-muted-foreground">
          <tr>
            <th className="px-2 py-2 text-left font-medium">方向</th>
            <th className="px-2 py-2 text-left font-medium">领涨</th>
            <th className="px-2 py-2 text-right font-medium">涨幅</th>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 12).map((row) => (
            <tr key={row.name} className="border-t">
              <td className="px-2 py-3 font-medium">{row.name}</td>
              <td className="px-2 py-3 text-muted-foreground">{row.leader || "-"}</td>
              <td className={cn("px-2 py-3 text-right tabular-nums", pctTone(row.change_pct))}>{pct(row.change_pct)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function BarList({ rows }: { rows: Row[] }) {
  const max = Math.max(...rows.map((row) => Math.abs(n(row.change_pct))), 1);
  return (
    <div className="space-y-2">
      {rows.slice(0, 10).map((row) => {
        const value = n(row.change_pct);
        return (
          <div key={row.name} className="grid grid-cols-[120px_1fr_64px] items-center gap-3 text-xs">
            <div className="truncate font-medium">{row.name}</div>
            <div className="h-3 overflow-hidden rounded bg-muted">
              <div className={cn("h-full", value >= 0 ? "bg-red-500" : "bg-green-500")} style={{ width: `${Math.max(4, Math.abs(value) / max * 100)}%` }} />
            </div>
            <div className={cn("text-right tabular-nums", pctTone(value))}>{pct(value)}</div>
          </div>
        );
      })}
    </div>
  );
}

function FlowRows({ rows, nameKey }: { rows: Row[]; nameKey: string }) {
  return (
    <div className="space-y-2">
      {rows.slice(0, 8).map((row, index) => (
        <div key={`${row.symbol || row[nameKey]}-${index}`} className="flex items-center justify-between gap-3 rounded-md border bg-background px-3 py-2 text-xs">
          <span className="truncate font-medium">{row[nameKey] || row.symbol || "-"}</span>
          <span className={cn("tabular-nums", n(row.main_net) >= 0 ? "text-red-500" : "text-green-500")}>{money(row.main_net)}</span>
        </div>
      ))}
    </div>
  );
}

function Ladder({ rows }: { rows: Row[] }) {
  const max = Math.max(...rows.map((row) => n(row.count)), 1);
  return (
    <div className="space-y-2">
      {rows.map((row) => (
        <div key={row.days} className="grid grid-cols-[52px_1fr] items-center gap-3 text-xs">
          <span className="font-semibold text-red-500">{row.days}板</span>
          <div className="rounded-md border bg-background px-2 py-2">
            <div className="mb-1 h-2 overflow-hidden rounded bg-muted">
              <div className="h-full bg-red-500" style={{ width: `${Math.max(4, n(row.count) / max * 100)}%` }} />
            </div>
            <div className="truncate text-muted-foreground">{arr(row.stocks).slice(0, 8).map((item) => item.name || item.symbol).join("、")}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

function DecisionGrid({ rows }: { rows: Row[] }) {
  return (
    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
      {rows.map((row) => (
        <div key={`${row.symbol}-${row.action}`} className="rounded-md border bg-background px-3 py-3 text-xs">
          <div className="flex items-center justify-between gap-2">
            <span className="font-semibold">{row.name || row.symbol}</span>
            <span className="rounded bg-muted px-2 py-0.5 text-muted-foreground">{row.action}</span>
          </div>
          <div className="mt-2 text-muted-foreground">{row.reason || row.risk_note || "-"}</div>
        </div>
      ))}
    </div>
  );
}

function ThemePills({ rows }: { rows: Row[] }) {
  return (
    <div className="flex flex-wrap gap-2">
      {rows.slice(0, 12).map((row) => (
        <span key={row.name} className="rounded-md border bg-background px-2 py-1 text-xs">
          {row.name} <span className={pctTone(row.change_pct)}>{pct(row.change_pct)}</span>
        </span>
      ))}
    </div>
  );
}

function MarketStagePage({ stage }: { stage: MarketDashboardStage }) {
  const [payload, setPayload] = useState<MarketDashboardStageResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (initial = false) => {
    if (initial) setLoading(true);
    else setRefreshing(true);
    try {
      const data = await api.getMarketDashboardStage(stage);
      setPayload(data);
      setError(data.status === "error" ? data.error || "页面数据加载失败" : null);
    } catch {
      setError("页面数据加载失败。");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [stage]);

  useEffect(() => {
    load(true);
  }, [load]);

  useEffect(() => {
    if (stage !== "intraday-monitor") return undefined;
    const id = window.setInterval(() => {
      void load(false);
    }, 60_000);
    return () => window.clearInterval(id);
  }, [load, stage]);

  return <StageShell stage={stage} payload={payload} loading={loading} refreshing={refreshing} error={error} onRefresh={() => load()} />;
}

export function MorningBrief() {
  return <MarketStagePage stage="morning-brief" />;
}

export function IntradayMonitor() {
  return <MarketStagePage stage="intraday-monitor" />;
}

export function TailStrategy() {
  return <MarketStagePage stage="tail-strategy" />;
}

export function CloseReview() {
  return <MarketStagePage stage="close-review" />;
}
