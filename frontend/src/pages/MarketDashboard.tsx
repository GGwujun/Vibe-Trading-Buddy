import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  ArrowRight,
  CandlestickChart as CandleIcon,
  Flame,
  Globe,
  Loader2,
  RefreshCw,
  Wallet,
} from "lucide-react";
import { api, type MarketDashboardResponse } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Panel, EmptyHint } from "@/components/dashboard/primitives";
import { IndexTicker, MarketSummary, BreadthPanel } from "@/components/dashboard/TopSection";
import { MyHoldings, KLinePanel, LimitBoard, LimitLadder } from "@/components/dashboard/MidSection";
import { CapitalEvidence, MainThemes, MultiPeriodMovers, ThemeHeatmap } from "@/components/dashboard/BottomSection";

const INDEX_KLINE_OPTIONS = [
  { symbol: "000001.SH", name: "上证指数" },
  { symbol: "399001.SZ", name: "深证成指" },
  { symbol: "399006.SZ", name: "创业板指" },
  { symbol: "000300.SH", name: "沪深300" },
  { symbol: "000905.SH", name: "中证500" },
  { symbol: "000852.SH", name: "中证1000" },
  { symbol: "000688.SH", name: "科创50" },
];

export function MarketDashboard() {
  const [data, setData] = useState<MarketDashboardResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (silent = false) => {
    if (!silent) setRefreshing(true);
    try {
      const dashboard = await api.getMarketDashboard();
      setData(dashboard);
      setError(null);
    } catch {
      setError("盘面数据加载失败，部分模块可能不可用。");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    load(true);
  }, [load]);

  const spotMap = useMemo(() => {
    const m = new Map<string, { price?: number; change_pct?: number; name?: string }>();
    const push = (sym: string, name: string, price?: number, change_pct?: number) => {
      const bare = sym.replace(/\.(SH|SZ|BJ)$/i, "");
      const entry = { price, change_pct, name };
      m.set(bare, entry);
      m.set(sym, entry);
    };
    const ov = data?.market_overview;
    (ov?.top_gainers ?? []).forEach((r) => push(r.symbol, r.name, r.price, r.change_pct));
    (ov?.top_losers ?? []).forEach((r) => push(r.symbol, r.name, r.price, r.change_pct));
    return m;
  }, [data]);

  const ov = data?.market_overview;
  const sentiment = data?.sentiment;
  const environment = data?.environment;
  const breadth = ov?.breadth;
  const limitUpReal = breadth?.limit_up_real === true;

  if (loading) {
    return (
      <div className="flex h-[calc(100vh-3.5rem)] items-center justify-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        加载盘面...
      </div>
    );
  }

  return (
    <div className="flex h-[calc(100vh-3.5rem)] flex-col bg-muted/20">
      <header className="shrink-0 border-b bg-background">
        <div className="flex flex-col gap-4 px-4 py-4 md:px-6 xl:flex-row xl:items-start xl:justify-between">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <Globe className="h-5 w-5 text-primary" />
              <h1 className="text-xl font-semibold tracking-tight">AI 盘面</h1>
              <span className="rounded bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary">A 股驾驶舱</span>
            </div>
            <p className="mt-1 max-w-3xl text-xs leading-relaxed text-muted-foreground">
              指数轮动、盘面情绪、资金证据、连板梯队、题材热力与四个交易时段工作台。
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs text-muted-foreground">
              {data?.updated_at ? `更新 ${new Date(data.updated_at).toLocaleTimeString("zh-CN", { hour12: false })}` : ""}
            </span>
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

        <IndexTicker indices={ov?.indices ?? []} />
        <MarketSummary breadth={breadth} sentiment={sentiment} environment={environment} />

        <Panel title="盘面速览">
          <BreadthPanel breadth={breadth} limitUpReal={limitUpReal} />
        </Panel>

        <div className="grid gap-3 lg:grid-cols-3">
          <Panel title="我的持仓" icon={<Wallet className="h-3.5 w-3.5" />}>
            <MyHoldings watchlist={data?.watchlist ?? []} spotMap={spotMap} />
          </Panel>
          <div className="lg:col-span-2">
            <KLinePanel codes={INDEX_KLINE_OPTIONS} />
          </div>
        </div>

        <div className="grid gap-3 lg:grid-cols-3">
          <Panel title="涨跌停情况" icon={<Flame className="h-3.5 w-3.5" />}>
            <LimitBoard pools={data?.pools} />
          </Panel>
          <Panel title="连板梯队" icon={<CandleIcon className="h-3.5 w-3.5" />}>
            <LimitLadder pools={data?.pools} />
          </Panel>
          <Panel title="情绪运行阶段" icon={<Activity className="h-3.5 w-3.5" />}>
            {sentiment ? (
              <div className="flex h-full flex-col justify-center gap-2 py-2">
                <div className="text-3xl font-bold text-foreground">{sentiment.stage}</div>
                <div className="text-[11px] leading-relaxed text-muted-foreground">{sentiment.stage_reason}</div>
                <div className="text-[10px] text-muted-foreground/70">温度 {sentiment.temperature} · {sentiment.label}</div>
              </div>
            ) : <EmptyHint>暂不可用</EmptyHint>}
          </Panel>
        </div>

        <CapitalEvidence capital={data?.capital ?? null} />

        <div className="grid gap-3 lg:grid-cols-3">
          <MainThemes themes={data?.themes ?? null} />
          <div className="lg:col-span-2">
            <ThemeHeatmap themes={data?.themes ?? null} />
          </div>
        </div>

        <MultiPeriodMovers rows={data?.multi_period} />

        {data?.errors && data.errors.length > 0 && (
          <div className="pt-2 text-[10px] text-muted-foreground/60">
            部分模块降级：{data.errors.map((e) => e.source).join("、")}
          </div>
        )}
      </main>
    </div>
  );
}

export default MarketDashboard;
