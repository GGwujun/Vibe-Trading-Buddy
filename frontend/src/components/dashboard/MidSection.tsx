import { useEffect, useMemo, useState } from "react";
import { Panel, EmptyHint, Pct } from "./primitives";
import { Chart } from "./Chart";
import { api, type MarketBarsResponse, type MarketPools, type TrackingWatchlistItem } from "@/lib/api";
import type { EChartsOption } from "echarts";

/** 4. 我的持仓 (uses tracking watchlist; latest price comes from elsewhere if available). */
export function MyHoldings({
  watchlist,
  spotMap,
}: {
  watchlist: TrackingWatchlistItem[];
  spotMap: Map<string, { price?: number; change_pct?: number; name?: string }>;
}) {
  if (!watchlist.length) return <EmptyHint>暂无自选/持仓</EmptyHint>;
  return (
    <ul className="space-y-1">
      {watchlist.slice(0, 8).map((w) => {
        const sym = (w.symbol || "").replace(/\.(SH|SZ|BJ)$/i, "");
        const spot = spotMap.get(sym) || spotMap.get(w.symbol);
        return (
          <li key={w.symbol} className="flex items-center justify-between gap-2 text-xs">
            <span className="truncate">
              <span className="font-medium text-foreground">{spot?.name || w.name || w.symbol}</span>
              <span className="ml-1 text-muted-foreground">{w.symbol}</span>
            </span>
            <span className="flex items-center gap-2">
              <span className="tabular-nums text-muted-foreground">{spot?.price?.toFixed(2) ?? "-"}</span>
              <Pct value={spot?.change_pct} className="w-16 text-right" />
            </span>
          </li>
        );
      })}
    </ul>
  );
}

/** 5. 走势图 K线 (lightweight candlestick; default index, switchable). */
export function KLinePanel({ codes }: { codes: { symbol: string; name: string }[] }) {
  const [sel, setSel] = useState(codes[0]?.symbol ?? "000001");
  const [resp, setResp] = useState<MarketBarsResponse | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.getMarketBars(sel, 60).then((r) => { if (!cancelled) setResp(r); }).catch(() => {});
    return () => { cancelled = true; };
  }, [sel]);

  const option = useMemo((): EChartsOption => {
    const bars = resp?.bars ?? [];
    const up = "#ef4444", down = "#22c55e";
    return {
      animation: false,
      grid: { left: 36, right: 12, top: 12, bottom: 48 },
      tooltip: { trigger: "axis" },
      xAxis: { type: "category", data: bars.map((b) => b.date), axisLabel: { fontSize: 10 } },
      yAxis: { scale: true, splitLine: { lineStyle: { type: "dashed", opacity: 0.4 } } },
      dataZoom: [{ type: "inside" }, { type: "slider", height: 16, bottom: 6 }],
      series: [
        {
          type: "candlestick",
          data: bars.map((b) => [b.open, b.close, b.low, b.high]),
          itemStyle: { color: up, color0: down, borderColor: up, borderColor0: down },
        },
      ],
    };
  }, [resp]);

  return (
    <Panel
      title="走势图（日K）"
      right={
        <select
          value={sel}
          onChange={(e) => setSel(e.target.value)}
          className="h-7 rounded border bg-background px-1 text-[11px]"
        >
          {(codes.length ? codes : [{ symbol: "000001", name: "上证指数" }]).map((c) => (
            <option key={c.symbol} value={c.symbol}>{c.name}</option>
          ))}
        </select>
      }
      bodyClassName="p-1"
    >
      {resp && resp.bars.length ? (
        <Chart option={option} height={260} />
      ) : (
        <EmptyHint>{resp ? "该标的暂无K线数据" : "加载中…"}</EmptyHint>
      )}
    </Panel>
  );
}

/** 6. 涨跌停情况 (real pool). */
export function LimitBoard({ pools }: { pools: MarketPools | null | undefined }) {
  if (!pools) return <EmptyHint>盘后/今日涨停池未同步</EmptyHint>;
  const limitUpList = pools.limit_up_list ?? [];
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-2">
        <div className="rounded-md bg-red-500/10 px-2 py-2 text-center">
          <div className="text-2xl font-bold tabular-nums text-red-500">{pools.limit_up_count}</div>
          <div className="text-[10px] text-muted-foreground">涨停 · 最高 {pools.max_limit_up_height} 板</div>
        </div>
        <div className="rounded-md bg-green-500/10 px-2 py-2 text-center">
          <div className="text-2xl font-bold tabular-nums text-green-500">{pools.limit_down_count}</div>
          <div className="text-[10px] text-muted-foreground">跌停</div>
        </div>
      </div>
      {limitUpList.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {limitUpList.slice(0, 16).map((s) => (
            <span key={s.symbol} className="rounded bg-muted/60 px-1.5 py-0.5 text-[10px]">
              {s.name}
              {s.days > 1 && <b className="ml-0.5 text-red-500">{s.days}板</b>}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/** 7. 连板梯队 (ladder: 5板 / 4板 / 3板 ...). */
export function LimitLadder({ pools }: { pools: MarketPools | null | undefined }) {
  const ladder = pools?.limitup_ladder ?? [];
  if (!ladder.length) return <EmptyHint>连板梯队盘后同步</EmptyHint>;
  const maxCount = Math.max(...ladder.map((r) => r.count), 1);
  return (
    <div className="space-y-1">
      {ladder.map((rung) => (
        <div key={rung.days} className="flex items-center gap-2 text-[11px]">
          <span className="w-10 shrink-0 font-bold text-red-500">{rung.days}板</span>
          <div className="h-5 flex-1 rounded bg-muted/40">
            <div
              className="flex h-full items-center rounded bg-red-500/40 px-1 text-[10px] text-white"
              style={{ width: `${(rung.count / maxCount) * 100}%` }}
            >
              {rung.count}
            </div>
          </div>
          <span className="hidden max-w-[40%] truncate text-muted-foreground sm:inline">
            {rung.stocks.slice(0, 4).map((s) => s.name).join("、")}
          </span>
        </div>
      ))}
    </div>
  );
}
