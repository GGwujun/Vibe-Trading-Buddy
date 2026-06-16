import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  Banknote,
  Building2,
  ChevronDown,
  FileText,
  GitBranch,
  Globe,
  Newspaper,
  Shield,
  Target,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import { api, type LogicChainResponse } from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  MarketEmptyState,
  MarketErrorState,
  MarketIntelHeader,
  MarketLoadingState,
  normalizeChinaSymbol,
  plainSymbol,
  SymbolActionBar,
} from "@/components/market/MarketIntelShell";
import { toast } from "sonner";

const ICONS: Record<string, typeof Globe> = {
  globe: Globe,
  building: Building2,
  "file-text": FileText,
  "trending-up": TrendingUp,
  banknote: Banknote,
  newspaper: Newspaper,
  shield: Shield,
};

function fmtPrice(value: number): string {
  return value >= 1000 ? value.toFixed(0) : value.toFixed(2);
}

function fmtPct(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

export function LogicChain() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [query, setQuery] = useState(searchParams.get("q") ?? "");
  const [code, setCode] = useState(plainSymbol(searchParams.get("symbol") ?? ""));
  const [result, setResult] = useState<LogicChainResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set(["technical"]));

  const analyze = useCallback(async (input?: string) => {
    const raw = (input || code).trim();
    if (!raw) return;
    const normalized = normalizeChinaSymbol(raw);
    if (!/^\d{6}\.(SZ|SH)$/.test(normalized)) {
      toast.error("请输入有效的 A 股代码");
      return;
    }

    setLoading(true);
    setError(null);
    try {
      const res = await api.getLogicChain(normalized);
      setResult(res);
      setCode(plainSymbol(res.code));
      setExpanded(new Set(["technical"]));
      const next = new URLSearchParams(searchParams);
      next.set("symbol", normalized);
      if (query.trim()) next.set("q", query.trim());
      setSearchParams(next, { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "逻辑链生成失败");
    } finally {
      setLoading(false);
    }
  }, [code, query, searchParams, setSearchParams]);

  const applySearch = useCallback(() => {
    const next = new URLSearchParams();
    if (query.trim()) next.set("q", query.trim());
    if (code.trim()) next.set("symbol", normalizeChinaSymbol(code));
    setSearchParams(next);
    analyze(code);
  }, [analyze, code, query, setSearchParams]);

  useEffect(() => {
    setQuery(searchParams.get("q") ?? "");
    setCode(plainSymbol(searchParams.get("symbol") ?? ""));
  }, [searchParams]);

  useEffect(() => {
    const sym = searchParams.get("symbol");
    if (sym) analyze(sym);
    // 只在 URL 标的变化时自动分析。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams.get("symbol")]);

  const toggleLayer = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const flowParams = new URLSearchParams(searchParams);
  if (result?.code) flowParams.set("symbol", result.code);

  return (
    <div className="flex h-[calc(100vh-3.5rem)] flex-col">
      <MarketIntelHeader
        active="logic-chain"
        query={query}
        symbol={code}
        onQueryChange={setQuery}
        onSymbolChange={setCode}
        onSearch={applySearch}
      />

      <div className="flex-1 overflow-y-auto p-4 md:p-6">
        {loading ? (
          <MarketLoadingState label="正在生成标的逻辑链" />
        ) : error ? (
          <MarketErrorState message={error} onRetry={() => analyze()} />
        ) : !result ? (
          <MarketEmptyState
            icon={GitBranch}
            title="输入标的代码开始逻辑链分析"
            description="也可以从新闻线索、事件雷达或机会清单点击标的进入，系统会自动带入上下文。"
            action={
              <div className="flex flex-wrap justify-center gap-2">
                {["000001", "600519", "300750"].map((item) => (
                  <button
                    key={item}
                    type="button"
                    onClick={() => analyze(item)}
                    className="rounded-md border px-3 py-1.5 text-xs hover:bg-muted"
                  >
                    {item}
                  </button>
                ))}
              </div>
            }
          />
        ) : (
          <div className="mx-auto grid max-w-6xl gap-4 lg:grid-cols-[1fr_280px]">
            <main className="space-y-4">
              <section className="rounded-lg border bg-card p-4">
                <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                  <div>
                    <h2 className="text-xl font-semibold">
                      {result.name}
                      <span className="ml-2 align-middle font-mono text-sm font-normal text-muted-foreground">{result.code}</span>
                    </h2>
                    <p className="mt-1 text-xs text-muted-foreground">从技术、新闻、基本面、资金与风险维度解释当前标的。</p>
                  </div>
                  <div className="text-left sm:text-right">
                    <p className="text-2xl font-bold tabular-nums">¥{fmtPrice(result.price)}</p>
                    <p className={cn("text-sm font-medium", result.change_pct >= 0 ? "text-success" : "text-danger")}>
                      {result.change_pct >= 0 ? <TrendingUp className="mr-0.5 inline h-3.5 w-3.5" /> : <TrendingDown className="mr-0.5 inline h-3.5 w-3.5" />}
                      {fmtPct(result.change_pct)}
                    </p>
                  </div>
                </div>
              </section>

              <section className="space-y-3">
                {result.layers.map((layer, index) => {
                  const Icon = ICONS[layer.icon] || Globe;
                  const isExpanded = expanded.has(layer.id);
                  const tone = layer.score >= 0.6 ? "green" : layer.score >= 0.4 ? "amber" : "red";
                  return (
                    <div key={layer.id} className="relative">
                      {index < result.layers.length - 1 && <div className="absolute left-5 top-full h-3 w-px bg-border" />}
                      <div className="overflow-hidden rounded-lg border bg-card">
                        <button
                          type="button"
                          onClick={() => toggleLayer(layer.id)}
                          className="flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-muted/30"
                        >
                          <div className={cn(
                            "flex h-8 w-8 shrink-0 items-center justify-center rounded-lg",
                            tone === "green" && "bg-success/10",
                            tone === "amber" && "bg-warning/10",
                            tone === "red" && "bg-danger/10",
                          )}>
                            <Icon className={cn(
                              "h-4 w-4",
                              tone === "green" && "text-success",
                              tone === "amber" && "text-warning",
                              tone === "red" && "text-danger",
                            )} />
                          </div>
                          <div className="min-w-0 flex-1">
                            <span className="text-sm font-medium">{layer.label}</span>
                            <span className="ml-2 text-base">{layer.signal}</span>
                          </div>
                          <span className="text-xs text-muted-foreground">{(layer.score * 100).toFixed(0)} 分</span>
                          <ChevronDown className={cn("h-4 w-4 text-muted-foreground transition-transform", isExpanded && "rotate-180")} />
                        </button>

                        {isExpanded && (
                          <div className="border-t px-4 pb-4 pt-3">
                            <div className="grid gap-2 sm:grid-cols-2">
                              {layer.items.map((item) => (
                                <div key={item.label} className="flex items-center justify-between gap-2 rounded-lg bg-muted/30 px-3 py-2">
                                  <span className="text-xs text-muted-foreground">{item.label}</span>
                                  <span className="flex items-center gap-1.5 text-xs font-medium">
                                    {item.value}
                                    <span>{item.signal}</span>
                                  </span>
                                </div>
                              ))}
                            </div>
                            <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-muted/50">
                              <div
                                className={cn(
                                  "h-full rounded-full",
                                  tone === "green" && "bg-success",
                                  tone === "amber" && "bg-warning",
                                  tone === "red" && "bg-danger",
                                )}
                                style={{ width: `${layer.score * 100}%` }}
                              />
                            </div>
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}
              </section>

              <section className={cn(
                "rounded-lg border-2 p-5",
                result.decision.score >= 0.6
                  ? "border-success/35 bg-success/5"
                  : result.decision.score >= 0.4
                    ? "border-warning/35 bg-warning/5"
                    : "border-danger/35 bg-danger/5",
              )}>
                <div className="mb-4 flex items-center gap-3">
                  <Target className="h-5 w-5 text-primary" />
                  <h3 className="text-base font-semibold">综合判断</h3>
                  <span className="ml-auto text-xl">{result.decision.signal}</span>
                </div>
                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                  <DecisionMetric label="操作建议" value={`${result.decision.signal} ${result.decision.action}`} />
                  <DecisionMetric label="建议仓位" value={`${result.decision.position_pct}%`} />
                  <DecisionMetric label="止损位" value={`¥${fmtPrice(result.decision.stop_loss)}`} danger />
                  <DecisionMetric label="止盈位" value={`¥${fmtPrice(result.decision.take_profit)}`} success />
                </div>
                <div className="mt-4 flex flex-wrap gap-4 text-xs text-muted-foreground">
                  <span>综合评分 <strong className="text-foreground">{result.decision.score.toFixed(2)}</strong></span>
                  <span>盈亏比 <strong className="text-foreground">1:{result.decision.risk_reward}</strong></span>
                </div>
              </section>
            </main>

            <aside className="space-y-4">
              <section className="rounded-lg border bg-card p-4">
                <p className="text-sm font-medium">下一步动作</p>
                <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                  逻辑链用于快速解释标的，正式投研结论建议继续生成 AlphaForge 报告。
                </p>
                <div className="mt-3 space-y-2">
                  <Link to="/alpha-forge" className="block rounded-md bg-primary px-3 py-2 text-center text-xs font-medium text-primary-foreground hover:opacity-90">
                    生成 AlphaForge 报告
                  </Link>
                  <Link to="/tracking-dashboard" className="block rounded-md border px-3 py-2 text-center text-xs hover:bg-muted">
                    加入跟踪看板
                  </Link>
                </div>
              </section>

              <section className="rounded-lg border bg-card p-4">
                <p className="text-sm font-medium">回看上下文</p>
                <SymbolActionBar symbol={result.code} params={flowParams} className="mt-3" />
              </section>
            </aside>
          </div>
        )}
      </div>
    </div>
  );
}

function DecisionMetric({
  label,
  value,
  success,
  danger,
}: {
  label: string;
  value: string;
  success?: boolean;
  danger?: boolean;
}) {
  return (
    <div className="rounded-lg bg-background p-3 text-center">
      <p className={cn("text-lg font-bold tabular-nums", success && "text-success", danger && "text-danger")}>{value}</p>
      <p className="mt-1 text-[10px] text-muted-foreground">{label}</p>
    </div>
  );
}
