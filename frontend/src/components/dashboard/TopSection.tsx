import { useEffect, useState } from "react";
import { Panel, EmptyHint, Pct } from "./primitives";
import type {
  MarketIndexRow,
  MarketBreadth,
  MarketSentiment,
  MarketEnvironment,
} from "@/lib/api";
import { cn } from "@/lib/utils";

export function IndexTicker({ indices }: { indices: MarketIndexRow[] }) {
  const [active, setActive] = useState(0);
  useEffect(() => {
    if (indices.length <= 1) return;
    const t = setInterval(() => setActive((a) => (a + 1) % indices.length), 3000);
    return () => clearInterval(t);
  }, [indices.length]);
  if (!indices.length) return <EmptyHint>指数暂不可用</EmptyHint>;
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
      {indices.map((idx, i) => {
        const up = idx.change_pct >= 0;
        return (
          <div
            key={idx.symbol + idx.name}
            className={cn(
              "rounded-md border px-2.5 py-2 transition-all",
              i === active ? "border-primary/60 bg-primary/5 shadow-sm" : "bg-background"
            )}
          >
            <div className="truncate text-[11px] text-muted-foreground">{idx.name}</div>
            <div className={cn("text-base font-semibold tabular-nums", up ? "text-red-500" : "text-green-500")}>
              {idx.price.toLocaleString()}
            </div>
            <Pct value={idx.change_pct} className="text-[11px]" />
          </div>
        );
      })}
    </div>
  );
}

export function MarketSummary({
  breadth,
  sentiment,
  environment,
}: {
  breadth: MarketBreadth | undefined;
  sentiment: MarketSentiment | null | undefined;
  environment: MarketEnvironment | null | undefined;
}) {
  const temp = sentiment?.temperature ?? 0;
  const tempTone = temp >= 60 ? "text-red-500" : temp >= 40 ? "text-amber-500" : "text-blue-500";
  return (
    <div className="grid gap-3 lg:grid-cols-3">
      <Panel title="盘面一句话" className="lg:col-span-2">
        {sentiment ? (
          <div className="space-y-2">
            <div className="text-sm font-medium leading-relaxed text-foreground">{sentiment.summary}</div>
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
              <span>环境：<b className="text-foreground">{environment?.regime ?? "-"}</b></span>
              <span>成交额：<b className="text-foreground">{breadth?.turnover_billion ?? 0} 亿</b></span>
              <span>阶段：<b className="text-foreground">{sentiment.stage}</b> · {sentiment.stage_reason}</span>
            </div>
          </div>
        ) : (
          <EmptyHint>情绪指标暂不可用</EmptyHint>
        )}
      </Panel>

      <Panel title="情绪温度">
        {sentiment ? (
          <div className="flex items-center gap-3">
            <div className={cn("text-3xl font-bold tabular-nums", tempTone)}>{temp.toFixed(0)}</div>
            <div className="text-xs">
              <div className={cn("font-medium", tempTone)}>{sentiment.label}</div>
              <div className="text-muted-foreground">广度 {sentiment.breadth_strength > 0 ? "+" : ""}{sentiment.breadth_strength}</div>
            </div>
            <div className="ml-auto h-2 w-16 overflow-hidden rounded-full bg-muted">
              <div
                className={cn("h-full", temp >= 60 ? "bg-red-500" : temp >= 40 ? "bg-amber-500" : "bg-blue-500")}
                style={{ width: `${Math.max(0, Math.min(100, temp))}%` }}
              />
            </div>
          </div>
        ) : (
          <EmptyHint>暂不可用</EmptyHint>
        )}
      </Panel>
    </div>
  );
}

export function BreadthPanel({ breadth, limitUpReal }: { breadth: MarketBreadth | undefined; limitUpReal?: boolean }) {
  if (!breadth) return <EmptyHint>盘面数据暂不可用</EmptyHint>;
  const cells = [
    { label: "上涨", value: breadth.advancers, tone: "text-red-500" },
    { label: "下跌", value: breadth.decliners, tone: "text-green-500" },
    { label: "平盘", value: breadth.flat, tone: "text-muted-foreground" },
    { label: `涨停${limitUpReal ? "" : "≈"}`, value: breadth.limit_up, tone: "text-red-500" },
    { label: `跌停${limitUpReal ? "" : "≈"}`, value: breadth.limit_down, tone: "text-green-500" },
    { label: "成交额 亿", value: breadth.turnover_billion, tone: "text-foreground" },
  ];
  return (
    <div className="grid grid-cols-3 gap-2 sm:grid-cols-6">
      {cells.map((c) => (
        <div key={c.label} className="rounded-md bg-muted/40 px-2 py-2 text-center">
          <div className={cn("text-lg font-semibold tabular-nums", c.tone)}>{c.value}</div>
          <div className="text-[10px] text-muted-foreground">{c.label}</div>
        </div>
      ))}
    </div>
  );
}
