import { useMemo } from "react";
import { Panel, EmptyHint, Pct, fmtYi, pctBg } from "./primitives";
import { Chart } from "./Chart";
import type {
  MarketCapital,
  MarketThemes,
  MultiPeriodRow,
  CapitalStockItem,
} from "@/lib/api";
import type { EChartsOption } from "echarts";
import { cn } from "@/lib/utils";

export function CapitalEvidence({ capital }: { capital: MarketCapital | null | undefined }) {
  if (!capital) return <EmptyHint>资金流数据暂不可用</EmptyHint>;
  return (
    <div className="grid gap-3 lg:grid-cols-3">
      <Panel title="行业净流入 TOP5" bodyClassName="p-1">
        <SectorBar capital={capital} />
      </Panel>
      <StockFlow title="个股净流入 TOP" items={capital.stock_inflow_top} tone="in" />
      <StockFlow title="个股净流出 TOP" items={capital.stock_outflow_top} tone="out" />
    </div>
  );
}

function SectorBar({ capital }: { capital: MarketCapital }) {
  const sectors = capital.sector_top5 ?? [];
  const option = useMemo((): EChartsOption => {
    const names = sectors.map((s) => s.sector);
    const nets = sectors.map((s) => s.main_net / 1e8);
    return {
      animation: false,
      grid: { left: 70, right: 28, top: 8, bottom: 18 },
      tooltip: { trigger: "axis", formatter: (p: any) => `${p?.[0]?.name ?? ""}<br/>${((p?.[0]?.value as number) ?? 0).toFixed(2)} 亿` },
      xAxis: { type: "value", axisLabel: { fontSize: 10 } },
      yAxis: { type: "category", data: names, axisLabel: { fontSize: 10 } },
      series: [
        {
          type: "bar",
          data: nets.map((v) => ({ value: v, itemStyle: { color: v >= 0 ? "#ef4444" : "#22c55e" } })),
          barWidth: 12,
        },
      ],
    };
  }, [sectors]);
  if (!sectors.length) return <EmptyHint>行业资金流暂不可用</EmptyHint>;
  return <Chart option={option} height={180} />;
}

function StockFlow({ title, items, tone }: { title: string; items: CapitalStockItem[]; tone: "in" | "out" }) {
  if (!items.length) return <Panel title={title}><EmptyHint>暂不可用</EmptyHint></Panel>;
  return (
    <Panel title={title}>
      <ul className="space-y-1">
        {items.slice(0, 8).map((s) => (
          <li key={s.symbol} className="flex items-center justify-between gap-2 text-[11px]">
            <span className="truncate">
              <span className="font-medium text-foreground">{s.name}</span>
              <span className={cn("ml-1", tone === "in" ? "text-red-500" : "text-green-500")}>{fmtYi(s.main_net)}</span>
            </span>
            <Pct value={s.change_pct} className="w-14 text-right" />
          </li>
        ))}
      </ul>
    </Panel>
  );
}

export function MainThemes({ themes }: { themes: MarketThemes | null | undefined }) {
  if (!themes) return <EmptyHint>板块数据暂不可用</EmptyHint>;
  const mainLines = themes.main_lines ?? [];
  const observe = themes.observe ?? [];
  return (
    <Panel title="主线 & 观察板块">
      <div className="space-y-2">
        <div>
          <div className="mb-1 text-[10px] text-muted-foreground">主线（领涨）</div>
          <div className="flex flex-wrap gap-1">
            {mainLines.length ? mainLines.map((m) => (
              <span key={m.name} className="rounded bg-red-500/15 px-2 py-0.5 text-[11px]">
                {m.name} <Pct value={m.change_pct} />
              </span>
            )) : <span className="text-[11px] text-muted-foreground">无</span>}
          </div>
        </div>
        <div>
          <div className="mb-1 text-[10px] text-muted-foreground">观察</div>
          <div className="flex flex-wrap gap-1">
            {observe.length ? observe.map((m) => (
              <span key={m.name} className="rounded bg-muted/60 px-2 py-0.5 text-[11px]">
                {m.name} <Pct value={m.change_pct} />
              </span>
            )) : <span className="text-[11px] text-muted-foreground">无</span>}
          </div>
        </div>
      </div>
    </Panel>
  );
}

export function MultiPeriodMovers({ rows }: { rows: MultiPeriodRow[] | undefined }) {
  if (!rows || !rows.length) return <EmptyHint>多周期涨幅榜暂不可用</EmptyHint>;
  const Cell = ({ v }: { v: number | null }) => (
    <td className="px-2 py-1 text-right tabular-nums">
      {v === null ? <span className="text-muted-foreground/50">-</span> : <Pct value={v} />}
    </td>
  );
  return (
    <Panel title={`多周期涨幅榜${rows.some((r) => r.approx) ? "（部分估算）" : ""}`} bodyClassName="p-1">
      <table className="w-full text-[11px]">
        <thead>
          <tr className="text-muted-foreground">
            <th className="px-2 py-1 text-left font-normal">名称</th>
            <th className="px-2 py-1 text-right font-normal">1日</th>
            <th className="px-2 py-1 text-right font-normal">5日</th>
            <th className="px-2 py-1 text-right font-normal">20日</th>
            <th className="px-2 py-1 text-right font-normal">60日</th>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 10).map((r) => (
            <tr key={r.symbol} className="border-t border-border/40">
              <td className="px-2 py-1">
                <span className="font-medium text-foreground">{r.name}</span>
                <span className="ml-1 text-[10px] text-muted-foreground">{r.symbol}</span>
              </td>
              <Cell v={r.d1} /><Cell v={r.d5} /><Cell v={r.d20} /><Cell v={r.d60} />
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  );
}

export function ThemeHeatmap({ themes }: { themes: MarketThemes | null | undefined }) {
  const sectors = themes?.concept_sectors ?? [];
  const option = useMemo((): EChartsOption => {
    const data = sectors.map((s) => ({
      name: s.name,
      value: Math.max(1, Math.abs(s.change_pct) + 1),
      change_pct: s.change_pct,
      itemStyle: { color: pctBg(s.change_pct) },
    }));
    return {
      tooltip: { formatter: (p: any) => `${p?.name ?? ""} ${(p?.data?.change_pct ?? 0).toFixed(2)}%` },
      series: [{ type: "treemap", data, roam: false, nodeClick: false, breadcrumb: { show: false }, label: { fontSize: 10, color: "#fff" } }],
    };
  }, [sectors]);
  if (!sectors.length) return <EmptyHint>题材热力图暂不可用</EmptyHint>;
  return (
    <Panel title="题材热力图（概念板块）" bodyClassName="p-1">
      <Chart option={option} height={280} />
    </Panel>
  );
}
